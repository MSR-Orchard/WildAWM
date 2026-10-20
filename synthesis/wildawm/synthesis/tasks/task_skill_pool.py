#!/usr/bin/env python3
"""Agent-authored task-synthesis skills (Markdown notes) shared within one workspace."""
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fcntl

from wildawm import const_config as cfg
from wildawm.core.paths import PROJECT_ROOT
from wildawm.json_utils import loads_jsonc
from wildawm.synthesis import harness
from wildawm.synthesis.tasks import claude_runner as cr

CONTAINER_PATH = "/tmp/xai_synthesis_skills"


def root() -> Path:
    return Path(cfg.TASKS_XAI_DIR) / cfg.XAI_SYNTHESIS_SKILL_DIRNAME


def _ensure_root() -> Path:
    p = root()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _lock_path() -> Path:
    return _ensure_root() / ".skills.lock"


@contextmanager
def _locked():
    p = _lock_path()
    with p.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _safe_name(path: str) -> str:
    base = str(path or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-")
    if not base:
        base = f"note-{int(time.time())}"
    if not base.endswith(".md"):
        base += ".md"
    return base[:80]


def _doc_paths() -> list[Path]:
    p = root()
    if not p.is_dir():
        return []
    return sorted(x for x in p.glob("*.md") if x.is_file())


def list_docs(*, max_docs: int | None = None) -> list[dict[str, str]]:
    docs = []
    for p in _doc_paths():
        try:
            content = _trim_content(p.read_text(errors="replace"))
        except OSError:
            continue
        if not content:
            continue
        docs.append({"path": p.name, "content": content})
    if max_docs is not None:
        docs = docs[:max(0, int(max_docs))]
    return docs


def prompt_section(*, max_docs: int | None = None) -> str:
    if not cfg.XAI_SYNTHESIS_SKILLS_ENABLED:
        return ""
    all_docs = list_docs(max_docs=cfg.XAI_SYNTHESIS_SKILL_MAX_DOCS)
    docs = all_docs[:max_docs or cfg.XAI_SYNTHESIS_SKILL_PROMPT_DOCS]
    if not all_docs:
        return (
            f"\n\n## Evolving task-synthesis skills\n"
            f"A read-only skill directory is available at `{CONTAINER_PATH}`. "
            "It is currently empty. Future skills are advisory operational lessons, not task requirements.\n"
        )
    rendered = "\n".join(
        f"- `{d['path']}`: {d['content'].replace(chr(10), ' ')}"
        for d in docs
    )
    names = ", ".join(f"`{d['path']}`" for d in all_docs)
    return (
        f"\n\n## Evolving task-synthesis skills\n"
        f"A read-only copy is available at `{CONTAINER_PATH}`. Use these agent-authored lessons to avoid repeated synthesis failures. Do not copy them into public task wording, do not treat them as hidden requirements, and never use them to bypass a gate. The pool contains {len(all_docs)} skill(s): {names}. Read a relevant file from the directory when its preview is not shown below.\n"
        f"Previewed skills:\n{rendered}\n"
    )


def stage_readonly(container: str, *, dest: str = CONTAINER_PATH) -> None:
    """Copy the current notes into a container as root-owned read-only files."""
    if not cfg.XAI_SYNTHESIS_SKILLS_ENABLED:
        return
    src_root = _ensure_root()
    with tempfile.TemporaryDirectory(prefix="wildawm-xai-skills-") as td:
        tmp = Path(td) / "skills"
        tmp.mkdir()
        for p in _doc_paths():
            shutil.copy2(p, tmp / p.name)
        cr.run_cmd(["docker", "exec", "--user", "root", container, "rm", "-rf", dest],
                   role="task_skill_stage", check=False,
                   timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
        cr.run_cmd(["docker", "cp", f"{tmp}/.", f"{container}:{dest}"],
                   role="task_skill_stage", check=False,
                   timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
        cr.run_cmd(
            ["docker", "exec", "--user", "root", container, "sh", "-c",
             f"mkdir -p {dest} && chown -R root:root {dest} && "
             f"find {dest} -type d -exec chmod 0555 {{}} + && "
             f"find {dest} -type f -exec chmod 0444 {{}} +"],
            role="task_skill_stage", check=False,
            timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    _ = src_root


def _trim_content(content: Any) -> str:
    text = str(content or "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    limit = max(1, int(cfg.XAI_SYNTHESIS_SKILL_MAX_CHARS))
    sentence_floor = max(0, int(limit * 0.25))
    word_floor = max(0, int(limit * 0.6))
    if len(text) > limit:
        clipped = text[:limit].rstrip()
        sentence = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
        if sentence >= sentence_floor:
            clipped = clipped[:sentence + 1]
        else:
            space = clipped.rfind(" ")
            if space >= word_floor:
                clipped = clipped[:space]
        text = clipped.rstrip()
    if text and text[-1] not in ".!?)`]}'\"":
        sentence = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
        if sentence >= sentence_floor:
            text = text[:sentence + 1].rstrip()
    return text


def apply_updates(updates: list[dict[str, Any]]) -> dict:
    if not cfg.XAI_SYNTHESIS_SKILLS_ENABLED:
        return {"ok": False, "reason": "disabled", "applied": []}
    applied: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    with _locked():
        base = _ensure_root()
        pool_was_full = len(_doc_paths()) >= cfg.XAI_SYNTHESIS_SKILL_MAX_DOCS
        for upd in updates:
            action = str(upd.get("action") or "update").strip().lower()
            name = _safe_name(str(upd.get("path") or upd.get("name") or ""))
            path = base / name
            if action == "delete":
                try:
                    path.unlink()
                    applied.append({"action": "delete", "path": name})
                except FileNotFoundError:
                    rejected.append({"action": "delete", "path": name,
                                     "reason": "skill does not exist"})
                continue
            if action not in {"upsert", "create", "update", "rewrite", "replace"}:
                rejected.append({"action": action, "path": name,
                                 "reason": "unsupported action"})
                continue
            content = _trim_content(upd.get("content"))
            if not content or content.upper() == "NO_UPDATE":
                continue
            exists = path.exists()
            if pool_was_full and action in {"create", "upsert"}:
                rejected.append({"action": action, "path": name,
                                 "reason": "skill pool is full; create/upsert is disabled"})
                continue
            if ((pool_was_full
                 or len(_doc_paths()) >= cfg.XAI_SYNTHESIS_SKILL_MAX_DOCS)
                    and not exists):
                rejected.append({"action": action, "path": name,
                                 "reason": "skill pool is full; only rewrite, replace, or delete an existing skill"})
                continue
            if action in {"update", "rewrite", "replace"} and not exists:
                rejected.append({"action": action, "path": name,
                                 "reason": "skill does not exist"})
                continue
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(content)
            os.replace(tmp, path)
            applied.append({"action": "replace" if exists else "create",
                            "path": name})
    return {"ok": True, "applied": applied, "rejected": rejected,
            "count": len(applied), "skill_count": len(_doc_paths())}


def _writer_prompt(event: dict[str, Any], docs: list[dict[str, str]]) -> str:
    full = len(docs) >= cfg.XAI_SYNTHESIS_SKILL_MAX_DOCS
    capacity_rule = (
        "The skill pool is FULL. You MUST NOT create or rename a skill. You may only rewrite/replace/delete an existing path, or return no updates."
        if full else
        f"The skill pool has {len(docs)}/{cfg.XAI_SYNTHESIS_SKILL_MAX_DOCS} documents. You may create a skill only when no existing skill should be rewritten or replaced."
    )
    return f"""# XAI task-synthesis skill writer

You are an isolated post-stage call using the task-generator model. Decide whether this real stage outcome teaches a reusable task-synthesis skill. The orchestrator does not supply a domain lesson and does not require an update. If the event has no durable lesson, return no updates.

Storage state:
- {capacity_rule}
- Each complete skill document must be <= {cfg.XAI_SYNTHESIS_SKILL_MAX_CHARS} characters.

Hard constraints:
- Output ONLY JSON, no markdown fences.
- Use `{{"updates":[]}}` for no update.
- At most 2 updates.
- Derive the lesson from the supplied evidence. Do not invent a failure cause.
- Keep skills general and operational. Do not preserve a one-off task slug, answer, entity, site, app, schema, or domain rule as a universal requirement.
- Do not write hidden task requirements or instructions for bypassing gates.
- Prefer rewriting, replacing, merging, or deleting an existing skill over creating a duplicate.

Existing skills:
{json.dumps(docs, indent=1, ensure_ascii=False)}

Stage outcome:
{json.dumps(event, indent=1, ensure_ascii=False)}

Return schema:
{{"updates":[{{"action":"create|rewrite|replace|delete","path":"short-name.md","content":"complete skill document"}}],"reason":"brief evidence-based reason"}}
"""


def update_from_event(event: dict[str, Any]) -> dict:
    """Ask an isolated skill writer for a bounded patch and apply it; never affects verdicts."""
    if not cfg.XAI_SYNTHESIS_SKILLS_ENABLED:
        return {"ok": False, "reason": "disabled", "applied": []}
    docs = list_docs(max_docs=cfg.XAI_SYNTHESIS_SKILL_MAX_DOCS)
    prompt = _writer_prompt(event, docs)
    try:
        rec = harness.run_agent(
            prompt, role="task_skill_writer",
            claude_llm=cr.LLM(model=cfg.TASK_GENERATOR_LLM_MODEL,
                              effort=cfg.TASK_GENERATOR_LLM_EFFORT,
                              route=cfg.TASK_GENERATOR_LLM_ROUTE),
            timeout=cfg.XAI_SYNTHESIS_SKILL_WRITER_TIMEOUT_S,
            cwd=str(PROJECT_ROOT), workdir=str(PROJECT_ROOT),
            allowed_tools="", retries=0, output_format="text",
            proxy_url=cfg.TASK_GENERATOR_LLM_PROXY,
            nudge_max=0)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"writer_error:{type(e).__name__}: {e}", "applied": []}
    raw = rec.result_text or ""
    try:
        data = loads_jsonc(raw)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"parse_error:{e}", "raw": raw[:1000],
                "call": rec.to_dict(), "applied": []}
    updates = data.get("updates") if isinstance(data, dict) else []
    if not isinstance(updates, list):
        updates = []
    applied = apply_updates(updates[:2])
    return {**applied, "reason": data.get("reason") if isinstance(data, dict) else "",
            "call": rec.to_dict()}
