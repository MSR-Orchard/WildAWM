#!/usr/bin/env python3
"""Build `reason_helper.md`: a leak-linted solver hint that never reveals answers."""
import json
import re
from dataclasses import dataclass
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.json_utils import load_jsonc
from wildawm.synthesis.tasks import claude_runner as cr
from simpleArgParser import parse_args

TEMPLATE = """## Note from the task pipeline (read once, then just do the task)

- This task has been **verified solvable** in the current world state: every
  person, file, record, thread, and page it refers to really exists and is
  reachable through your interfaces (shell/files, the MCP apps, the browser).
- There are **no trick steps**. If you cannot find something, search
  differently (other folders, other search terms, scroll further) — do not
  conclude it does not exist.
- This is a **multi-interface task**: use the browser, MCP apps, and
  filesystem/CLI where the task requires them. Do not stop early; finish every
  requested outcome.
- Where the task says a choice depends on how something LOOKS, you must open
  the page and look at the actual image — titles and descriptions are not
  enough.
"""


def tier2_block(meta: dict) -> str:
    interfaces = set((meta or {}).get("interfaces") or [])
    if not {"cli", "mcp", "web"}.issubset({str(i).lower() for i in interfaces}):
        return ""
    return (
        "\n- Expected shape of the work (interfaces only, no answers): use the "
        "CLI/filesystem to write and run the required program, MCP apps for "
        "declared app work, and the browser/web UI for the declared Web work. "
        "Finish every public prerequisite before the final action. When the "
        "task includes a visual criterion, inspect the actual image.\n"
    )


_PATH_PAT = re.compile(r"(?:~|/workspace|/home)[\w./-]+|\b[\w-]+/[\w./-]+\.\w{1,5}\b")
_SLUG_PAT = re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+){2,}\b")
_EMAIL_PAT = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_URL_PAT = re.compile(r"https?://\S+|\b[\w-]+\.(?:com|org|io|co)/\S+")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower())


def lint(helper: str, task_md: str, meta: dict) -> list[str]:
    """Deterministic leak check: identity content not already public in task.md."""
    violations: list[str] = []
    task_norm = _norm(task_md)
    helper_norm = _norm(helper)

    # Structured ground truths from meta must never appear.
    ig = (meta or {}).get("image_grounding") or {}
    secrets = []
    for key in ("truth", "distractor", "attribute"):
        v = ig.get(key)
        if isinstance(v, str) and len(v) >= 6:
            secrets.append(v)
    for a in (meta or {}).get("grounded_anchors") or []:
        if isinstance(a, str) and len(a) >= 10:
            secrets.append(a)
    for s in secrets:
        if _norm(s) in helper_norm and _norm(s) not in task_norm:
            violations.append(f"identity leak (meta secret): {s[:80]!r}")

    # Concrete entities in the helper must already be public in task.md.
    for pat, kind in ((_PATH_PAT, "path"), (_SLUG_PAT, "slug"),
                      (_EMAIL_PAT, "email"), (_URL_PAT, "url")):
        for m in pat.findall(helper):
            tok = m.strip(".,;:)('\"`")
            if len(tok) < 6:
                continue
            if _norm(tok) not in task_norm:
                violations.append(f"identity leak ({kind} not in task.md): {tok[:80]!r}")
    return violations


_DERIVE_PROMPT = """You are preparing a SOLVER BRIEF for an AI agent that will attempt a task.

You will see the task text and the task author's private rationale. Write a
short brief (<= 10 bullet lines) that:
- assures the solver the task is feasible in this world (existence claims),
- warns about effort (long task, multiple interfaces, must look at images
  where the task says so, don't give up early),
- contains ABSOLUTELY NO identity information that is not already verbatim in
  the task text: no target names/slugs, no "the correct one is …", no file
  paths, no people, no step-by-step plan, no interface order. Existence only.

## Task text (public)
{task_md}

## Author's private rationale (DO NOT leak its contents)
{reason_md}

Output ONLY the brief markdown, starting with the heading
'## Note from the task pipeline'.
"""


def build(task_md: str, reason_md: str, meta: dict, *,
          use_llm: bool = True) -> dict:
    """Return {helper, source, lint_violations, llm_violations}; leaks fall back to TEMPLATE."""
    if use_llm:
        try:
            spec = cfg.model_spec(cfg.LIGHTWEIGHT_JUDGE_MODEL)
            rec = cr.http_chat(spec, [{"role": "user", "content":
                                       _DERIVE_PROMPT.format(
                                           task_md=task_md[:6000],
                                           reason_md=reason_md[:6000])}],
                               role="reason_helper",
                               timeout=cfg.LLM_SHORT_TIMEOUT_S, retries=2)
            if rec.ok:
                draft = rec.result_text.strip()
                bad = lint(draft, task_md, meta)
                if not bad:
                    return {"helper": draft, "source": "llm",
                            "lint_violations": [], "llm_violations": []}
                return {"helper": TEMPLATE, "source": "template_fallback",
                        "lint_violations": [], "llm_violations": bad}
        except cr.BatchAbortError:
            raise
        except Exception as e:                      # noqa: BLE001
            return {"helper": TEMPLATE, "source": f"template_error:{e}",
                    "lint_violations": [], "llm_violations": []}
    return {"helper": TEMPLATE, "source": "template",
            "lint_violations": [], "llm_violations": []}


def final_lint_ok(result: dict) -> bool:
    """Whether the helper text shown to solvers is lint-clean (ignores discarded LLM drafts)."""
    return not bool((result or {}).get("lint_violations"))


def compose_solver_prompt(task_md: str, helper: str, *, tier: int = 1,
                          meta: dict | None = None) -> str:
    """The exact message an ensemble solver receives."""
    extra = tier2_block(meta or {}) if tier >= 2 else ""
    return task_md.rstrip() + "\n\n---\n" + helper.rstrip() + extra + "\n"


if __name__ == "__main__":
    @dataclass
    class ReasonHelperArgs:
        task_dir: str
        no_llm: bool = False

    a = parse_args(ReasonHelperArgs)
    d = Path(a.task_dir)
    meta = load_jsonc(d / "meta.json") if (d / "meta.json").exists() else {}
    out = build((d / "task.md").read_text(),
                (d / "reason.md").read_text() if (d / "reason.md").exists() else "",
                meta, use_llm=not a.no_llm)
    (d / "reason_helper.md").write_text(out["helper"])
    print(json.dumps({k: v for k, v in out.items() if k != "helper"}, indent=1))
