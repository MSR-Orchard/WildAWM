#!/usr/bin/env python3
"""Heavy judge: an agentic harness over read-only BEFORE/AFTER snapshot copies."""
import json
import re
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

from wildawm.synthesis import harness
from wildawm.synthesis.tasks import claude_runner as cr
from wildawm.synthesis.tasks import vision_shim as vs
from wildawm import const_config as cfg
from wildawm.json_utils import load_jsonc

CONSTITUTION = Path(__file__).resolve().parent / "prompts" / "judge_constitution.md"


def _heavy_llm(model_ref: str | None = None) -> cr.LLM:
    """Claude Code heavy-judge spec; routing comes from the throwaway container's proxy env."""
    ref = model_ref or cfg.HEAVY_JUDGE_MODEL
    return cr.LLM(model=cfg.model_spec(ref)["api_model"],
                  route="subscription", model_ref=ref)


def _failed_checks(rule_result: dict) -> list[tuple[str, str]]:
    out = []
    for s in rule_result.get("scripts", []):
        for cid, ok in (s.get("checks") or {}).items():
            if not ok:
                out.append((f"{s['name']}::{cid}",
                            (s.get("details", {}).get(cid) or "")[:300]))
        for cid, msg in (s.get("errors") or {}).items():
            out.append((f"{s['name']}::{cid}", f"WORLD-ERROR {msg[:250]}"))
    return out


def _extract_webharbor_for_judge(snapshot_dir: Path) -> None:
    """Extract `webharbor.tar.gz` into `<snapshot_dir>/web/` so the judge can query site DBs."""
    wh = Path(snapshot_dir) / "webharbor.tar.gz"
    web = Path(snapshot_dir) / "web"
    if web.is_dir() or not wh.is_file():
        return
    web.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(wh, "r:gz") as tf:
            # filter="data" blocks path traversal and unsafe links.
            tf.extractall(web, filter="data")
    except (tarfile.TarError, OSError, TypeError):
        pass


_WEBSYN_LOCK = threading.Lock()


def _expected_websyn_sites() -> list[str]:
    sites_path = cfg.PROJECT_ROOT / "docker" / "webharbor" / "sites.json"
    try:
        data = load_jsonc(sites_path)
        sites = data.get("sites") if isinstance(data, dict) else None
        out = [str(s.get("id", "")).strip() for s in (sites or [])
               if isinstance(s, dict) and str(s.get("id", "")).strip()]
        if out:
            return out
    except Exception:
        pass
    return []


def _websyn_cache_complete(cache: Path) -> bool:
    sites = _expected_websyn_sites()
    return bool(sites) and all((cache / site / "static").is_dir()
                               for site in sites)


def _ensure_websyn_cache() -> str | None:
    """Populate once and return a shared read-only cache of the WebHarbor site tree, or None."""
    cache = Path(cfg.WEBSYN_CACHE_DIR)
    marker = cache / ".image_id"
    image = cfg.WEBHARBOR_IMAGE
    try:
        ins = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S)
        img_id = ins.stdout.strip() if ins.returncode == 0 else ""
    except Exception:  # noqa: BLE001 (docker unavailable)
        img_id = ""
    with _WEBSYN_LOCK:
        if (img_id and marker.is_file() and marker.read_text().strip() == img_id
                and _websyn_cache_complete(cache)):
            return str(cache)
        if not img_id:
            return None
        cache.mkdir(parents=True, exist_ok=True)
        # Stream the tree to disk rather than buffering it in memory.
        tmp_tar = cache / ".websyn.tar.gz.part"
        try:
            with open(tmp_tar, "wb") as fh:
                proc = subprocess.run(
                    ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c",
                     "cd /opt/WebSyn && tar czf - --exclude=instance ."],
                    stdout=fh, stderr=subprocess.PIPE,
                    timeout=cfg.WEBSYN_CACHE_TIMEOUT_S)
            if (proc.returncode != 0 or not tmp_tar.exists()
                    or tmp_tar.stat().st_size == 0):
                return None
            with tarfile.open(tmp_tar, "r:gz") as tf:
                tf.extractall(cache, filter="data")
            if not _websyn_cache_complete(cache):
                return None
            marker.write_text(img_id)
            return str(cache)
        except Exception:  # noqa: BLE001
            return None
        finally:
            tmp_tar.unlink(missing_ok=True)


def run_judge(world_before: Path, world_after: Path, task_md: str,
              rubrics_md: str, skill_md: str, rule_result: dict, *,
              timeout: int | None = None, model_ref: str | None = None,
              proxy_url: str | None = None) -> dict:
    """Returns {verdict, confidence, too_strict, genuine_miss, justification, status, call}."""
    # Let the judge query site DBs before the snapshots are mounted read-only.
    _extract_webharbor_for_judge(Path(world_before))
    _extract_webharbor_for_judge(Path(world_after))
    # Shared read-only site tree for visual checks; None if unavailable.
    websyn_dir = _ensure_websyn_cache()
    timeout = timeout or cfg.LLM_HEAVY_TIMEOUT_S
    failed = _failed_checks(rule_result)
    fail_block = "\n".join(f"- `{n}` was judged false by the deterministic "
                           f"layer. Extracted: {d}" for n, d in failed) or "(none)"
    prompt = f"""# Judge whether this task was COMPLETED

## The task (the persona's request)
{task_md.strip()}

## Rubric
{(rubrics_md or '').strip()[:4000]}

## Judging notes (skill)
{(skill_md or '').strip()[:3000]}

## Deterministic checks that came back FALSE (may be too strict — verify against reality)
{fail_block}

## World snapshots — read-only, your ONLY ground-truth evidence
- BEFORE the agent acted:  {world_before}
- AFTER the agent acted:   {world_after}

Each snapshot dir holds `workspace/` (the persona's files, INCLUDING any
images the agent downloaded), `data/*.db` (the MCP services' SQLite — query
with `sqlite3 <file> ".tables"` / SELECT), and `web/` (the WebHarbor site
databases, extracted from webharbor.tar.gz, one database per mirrored site at
`web/<site>/instance/`). DIFF before vs after to see exactly
what the agent changed. Use read-only `ls` / `sqlite3` / `grep` / `diff` ONLY;
do NOT modify the snapshots. Judge ONLY from these snapshots. For any image
file under `workspace/`, you MAY call `describe_image` / `vision_describe_image`
to verify a visual fact yourself — see the evidence-provenance rules below.

## Shared served-site tree (read-only /websyn mount)
A `/websyn/` mount holds the full served WebHarbor site tree (source + all
static images) for every mirrored site under `/websyn/<site>/`. These served images are the
INDEPENDENT ground-truth for any visual fact the task hinges on: when a
requirement turns on an image's appearance, `describe_image` /
`vision_describe_image` the actual served image under
`/websyn/<site>/static/images/…` and confirm the attribute yourself. Do NOT
query `/websyn/<site>/instance/` for grading — that holds the image-baked SEED
database, not the per-world state; the per-world after-state lives only in
`<snapshot>/web/<site>/instance/<site>.db`.

## OUTPUT — write your verdict as JSON (REQUIRED)
`failed` = the agent did not complete the task (agent fail); `world_imperfect`
= the environment or the verification itself is flawed (env bug, missing
service, verifier bug), not the agent. Also give a calibrated CONFIDENCE for
each of the three classes independently on a 0-100 scale (they need NOT sum to
100).

When you reach a verdict, use the Write tool to write this exact JSON file to
`{{verdict_path}}` (and nothing else there):

{{"verdict": "completed" | "failed" | "world_imperfect",
  "confidence": {{"completed": <0-100>, "failed": <0-100>, "world_imperfect": <0-100>}},
  "too_strict": ["<rulecheck name>", ...],     // false checks that are actually too strict
  "genuine_miss": ["<rulecheck name>", ...],   // false checks the agent genuinely missed
  "reason": "<3-6 sentence justification citing the rows/files you relied on>"}}

Writing this file is how your verdict is recorded — do not skip it. After
writing it, end your turn.
"""
    hkind = harness.resolve_harness("heavy_judge")
    if hkind == "claude-code":
        model_ref = model_ref or cfg.HEAVY_JUDGE_MODEL
    mcp_config = disallowed_tools = None
    env_extra = None
    vision_note_override = None
    oc_model = harness.opencode_model_from_ref(model_ref) if (
        hkind == "opencode" and model_ref) else None
    # describe_image MCP is always attached; text-only claude-code models also lose Read.
    if hkind == "claude-code":
        mcp_config = vs.mcp_config()
        if cfg.judge_needs_vision_shim(model_ref):
            prompt += vs.JUDGE_NOTE
            disallowed_tools = "Read"
        else:
            prompt += vs.JUDGE_IMAGE_NOTE
    elif hkind == "opencode" and not cfg.judge_needs_vision_shim(model_ref):
        vision_note_override = vs.JUDGE_IMAGE_NOTE_OC
    if hkind == "claude-code":
        spec = cfg.model_spec(model_ref)
        env_extra = {
            "AWM_OPENAI_BASE_URL": spec["base_url"],
            "AWM_OPENAI_API_KEY": spec.get("api_key", ""),
            "AWM_OPENAI_MODEL": spec["api_model"],
            "OPENAI_BASE_URL": spec["base_url"],
            "OPENAI_API_KEY": spec.get("api_key", ""),
            "OPENAI_MODEL": spec["api_model"],
            "ANTHROPIC_BASE_URL": f"http://localhost:{cfg.LLM_PROXY_PORT}",
            "ANTHROPIC_API_KEY": "not-used",
        }
    with tempfile.TemporaryDirectory(prefix="xai-judge-") as work:
        cons = Path(work) / "judge_constitution.md"
        cons.write_text(CONSTITUTION.read_text() if CONSTITUTION.exists() else "")
        vpath = Path(work) / "verdict.json"
        rec = harness.run_agent(
            prompt.replace("{verdict_path}", str(vpath)),
            role="heavy_judge",
            claude_llm=_heavy_llm(model_ref) if hkind == "claude-code" else None,
            opencode_model=oc_model, timeout=timeout, cwd=work,
            system_prompt_file=str(cons), retries=2, proxy_url=proxy_url,
            env_extra=env_extra,
            mcp_config=mcp_config, disallowed_tools=disallowed_tools,
            vision_note=vision_note_override,
            websyn_dir=websyn_dir,
            extra_paths=[str(world_before), str(world_after)])
        parsed = _read_verdict_file(vpath)
        # Archive the judge's full transcript (best effort).
        if rec.extra.get("harness") == "opencode":
            trajectory = rec.extra.pop("trajectory", None) or []
        else:
            trajectory = _load_session_trajectory(rec.session_id)
    # An infra-failed run may leave a stale verdict.json; trust it only on ok or empty-text runs.
    if not rec.ok and (rec.status != "empty" or parsed is None):
        return {"verdict": "infra", "confidence": None,
                "too_strict": [], "genuine_miss": [],
                "justification": rec.stderr_tail or rec.stdout_tail,
                "status": rec.status, "call": rec.to_dict(),
                "trajectory": trajectory}
    text = rec.result_text or ""
    if parsed is not None:
        verdict = _norm_verdict(parsed.get("verdict"))
        return {"verdict": verdict,
                "confidence": _clean_confidence(parsed.get("confidence"), verdict),
                "too_strict": parsed.get("too_strict", []),
                "genuine_miss": parsed.get("genuine_miss", []),
                "justification": (parsed.get("reason") or text),
                "status": "ok", "call": rec.to_dict(),
                "trajectory": trajectory}
    # Fallback: parse the final message.
    verdict = parse_judge(text)
    return {"verdict": verdict,
            "confidence": _clean_confidence(None, verdict),
            "too_strict": re.findall(r"RULECHECK\s+(\S+):\s*too-strict", text),
            "genuine_miss": re.findall(r"RULECHECK\s+(\S+):\s*genuine-miss", text),
            "justification": text, "status": "ok", "call": rec.to_dict(),
            "trajectory": trajectory}


def _claude_project_roots() -> list[Path]:
    """The `projects/` dirs claude may write session transcripts to (incl. AWM_CLAUDE_CONFIG_DIR)."""
    roots, seen = [], set()
    for base in (str(cfg.CLAUDE_CONFIG_DIR), str(Path.home() / ".claude")):
        if not base:
            continue
        p = Path(base) / "projects"
        if str(p) not in seen:
            seen.add(str(p))
            roots.append(p)
    return roots


def _load_session_trajectory(session_id: str) -> list:
    """Read the judge's session transcript JSONL by session id; returns [] when not found."""
    if not session_id:
        return []
    try:
        match = None
        for root in _claude_project_roots():
            hits = sorted(root.glob(f"*/{session_id}.jsonl"))
            if hits:
                match = hits[0]
                break
        if match is None:
            return []
        steps = []
        for line in match.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                steps.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return steps
    except OSError:
        return []


def _read_verdict_file(p: Path) -> dict | None:
    try:
        return load_jsonc(p)
    except (OSError, ValueError):
        return None


def _clean_confidence(raw, verdict: str | None = None) -> dict | None:
    """Normalize per-class confidence to {completed, failed, world_imperfect} ints in 0-100."""
    classes = ("completed", "failed", "world_imperfect")
    alias = {"agent_fail": "failed", "imperfect_world": "world_imperfect",
             "partially_completed": "failed", "partial": "failed"}
    conf: dict = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = alias.get(str(k).strip().lower(), str(k).strip().lower())
            if key in classes and isinstance(v, (int, float)):
                conf[key] = max(0, min(100, int(round(float(v)))))
    if len(conf) == 3:
        return conf
    if verdict in classes:
        return {c: (100 if c == verdict else 0) for c in classes}
    return None


def _norm_verdict(v: str) -> str:
    v = (v or "").strip().lower()
    if "world" in v or "imperfect" in v:
        return "world_imperfect"
    if "partial" in v or v in ("agent_fail", "fail", "failed"):
        return "failed"
    if "complet" in v:
        return "completed"
    return "infra"


def parse_judge(text: str) -> str:
    """Fallback free-text parse: the XAI_JUDGE line, else a clear concluding statement."""
    m = re.search(r"xai_judge[^a-z]{0,3}(completed|failed|world_imperfect"
                  r"|partial(?:ly_completed)?|agent_fail)", (text or "").lower())
    if m:
        return _norm_verdict(m.group(1))
    low = (text or "").lower()
    # Affirmative world-broken phrasing only, so "world_imperfect does not apply" won't match.
    if re.search(r"(world|environment)\s+(is|was)\s+(broken|imperfect|down|"
                 r"unreachable)|verdict[:\s]+world_imperfect", low):
        return "world_imperfect"
    if re.search(r"\b(every|all)\b[^.]{0,40}\b(check|criteri|require)[^.]{0,20}"
                 r"\b(pass|met|satisfi|present)", low) or "task is completed" in low \
            or "is genuinely complete" in low or "genuinely completed" in low:
        return "completed"
    if re.search(r"\bnot (completed|done|fulfilled)\b|missing|did not|"
                 r"failed to|is absent|never (created|sent|wrote|added)", low):
        return "failed"
    return "infra"
