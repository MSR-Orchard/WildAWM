#!/usr/bin/env python3
"""Lightweight LLM judge for task synthesis and verification."""
import json
import os
import re
import time
import urllib.error
import urllib.request

from wildawm import const_config as cfg

_BACKOFF = [5, 15, 45]


def _env_spec() -> dict:
    spec = {"ref": os.environ.get("AWM_LIGHTWEIGHT_JUDGE_REF", "env/lightweight"),
            "base_url": os.environ["AWM_LIGHTWEIGHT_JUDGE_BASE_URL"],
            "api_key": os.environ.get("AWM_LIGHTWEIGHT_JUDGE_API_KEY", ""),
            "api_model": os.environ["AWM_LIGHTWEIGHT_JUDGE_MODEL"],
            "provider_type": os.environ.get("AWM_LIGHTWEIGHT_JUDGE_PROVIDER_TYPE",
                                            "openai_generic")}
    reg = None
    try:
        if "/" in spec["ref"]:
            reg = cfg.model_spec(spec["ref"])
        else:
            reg = cfg.model_spec_for_api(spec["base_url"], spec["api_model"])
    except Exception:
        reg = None
    if reg:
        spec["ref"] = reg.get("ref", spec["ref"])
        spec["provider"] = reg.get("provider", spec.get("provider"))
        spec["quirks"] = reg.get("quirks") or {}
    else:
        try:
            quirks = json.loads(os.environ.get(
                "AWM_LIGHTWEIGHT_JUDGE_QUIRKS_JSON", "{}"))
        except (TypeError, ValueError):
            quirks = {}
        spec["quirks"] = quirks if isinstance(quirks, dict) else {}
    return spec


PROMPT = """# Decide whether this task was COMPLETED — judge ONLY the evidence below

You are a strict, fair grader for an AI-agent task in a simulated personal
world. You cannot inspect the world; judge ONLY the machine-extracted evidence
digest (real values pulled from the world's files/databases by deterministic
scripts).

## Task (the persona's request)
{task_md}

## Rubric — what COMPLETED requires
{rubric_md}

## Evidence digest
```json
{evidence}
```

Rules:
- Judge against the rubric's INTENT: a faithful solution in the persona's own
  words or a service's real storage format counts — do NOT demand exact
  strings, exact timestamps, or fields the agent's tools stamp automatically.
- If a REQUIRED artifact is absent from the evidence ("ABSENT…"), the task is
  not completed.
- `completed` — every required artifact is present and faithful.
- `failed` — one or more required artifacts are missing or wrong (this
  includes partially-done work: partial counts as failed here).
- `world_imperfect` — the evidence shows WORLD-ERROR entries or clear signs
  the environment itself was broken (missing databases, unreachable services)
  rather than the agent failing to act.

Also output a calibrated CONFIDENCE for EACH of the three classes independently
on a 0-100 scale (how likely each class is the correct verdict; they need NOT
sum to 100). Semantics: `completed` = every requirement met; `failed` = the
agent did not complete the task (agent fail); `world_imperfect` = the
environment or the verification itself is flawed (env bug, missing service,
verifier bug), not the agent.

Think briefly, then end with EXACTLY these two lines:
`LLM_VERDICT: completed` | `LLM_VERDICT: failed` | `LLM_VERDICT: world_imperfect`
`CONFIDENCE: completed=<0-100> failed=<0-100> world_imperfect=<0-100>`
"""


def _http_chat(spec: dict, prompt: str, *, timeout: int, retries: int = 3,
               max_output_tokens: int | None = None,
               reasoning_effort: str | None = None) -> dict:
    base = spec["base_url"].rstrip("/")
    if base.endswith("/chat/completions"):
        url = base
    elif spec.get("provider_type") == "azure_openai" and not (
            base.endswith("/openai/v1") or base.endswith("/v1") or base.endswith("/openai")):
        url = base + "/openai/v1/chat/completions"
    else:
        url = base + "/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {spec.get('api_key', '')}"}
    if spec.get("provider_type") == "azure_openai":
        headers["api-key"] = spec.get("api_key", "")
    body_obj = {
        "model": spec["api_model"],
        "messages": [{"role": "user", "content": prompt}],
        cfg.chat_token_limit_param(spec): (
            cfg.LIGHTWEIGHT_JUDGE_MAX_OUTPUT_TOKENS
            if max_output_tokens is None else int(max_output_tokens)),
    }
    # Use a lightweight-specific effort, never the process-wide AWM_REASONING_EFFORT.
    lw_effort = (
        os.environ.get(
            "AWM_LIGHTWEIGHT_JUDGE_REASONING_EFFORT",
            cfg.LIGHTWEIGHT_JUDGE_REASONING_EFFORT,
        )
        if reasoning_effort is None else reasoning_effort
    ).strip()
    reasoning_policy = None
    if lw_effort:
        reasoning_policy = cfg.reasoning_effort_policy(spec, lw_effort)
    elif (spec.get("quirks") or {}).get("reasoning_effort") is not None:
        values = ((spec.get("quirks") or {}).get("reasoning_effort") or {}).get(
            "values", [])
        default_effort = "none" if "none" in values \
            else cfg.reasoning_effort_default(spec)
        if default_effort:
            reasoning_policy = cfg.reasoning_effort_policy(spec, default_effort)
    cfg.apply_reasoning_to_body(body_obj, reasoning_policy, api="chat")
    body = json.dumps(body_obj).encode()
    last_err = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            msg = (data.get("choices") or [{}])[0].get("message", {})
            # Thinking models may leave content null and put the answer in reasoning fields.
            text = (msg.get("content") or msg.get("reasoning")
                    or msg.get("reasoning_content") or "")
            if text.strip():
                usage = data.get("usage") or {}
                return {"text": text, "status": "ok",
                        "input_tokens": usage.get("prompt_tokens"),
                        "output_tokens": usage.get("completion_tokens"),
                        "reasoning_effort": (reasoning_policy or {}).get("effort"),
                        "reasoning_policy": reasoning_policy}
            last_err = "empty completion"
        except urllib.error.HTTPError as e:
            try:
                last_err = f"HTTP {e.code}: {e.read().decode(errors='replace')}"
            except Exception:
                last_err = f"HTTP {e.code}"
            if e.code in (401, 403):
                return {"text": "", "status": "auth", "error": last_err}
            if e.code not in (429,) and e.code < 500:
                return {"text": "", "status": "error", "error": last_err}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)
        if attempt < retries:
            time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
    return {"text": "", "status": "infra", "error": last_err}


def judge(task_md: str, rubric_md: str, evidence: list[dict], *,
          spec: dict | None = None,
          fallback_verdict: str | None = None,
          timeout: int = cfg.LLM_MEDIUM_TIMEOUT_S,
          provider_retries: int = 3,
          max_output_tokens: int | None = None,
          reasoning_effort: str | None = None) -> dict:
    """Return {verdict, confidence, justification, status, tokens}; infra = the call failed."""
    spec = spec or _env_spec()
    prompt = PROMPT.format(
        task_md=(task_md or "").strip()[:4500],
        rubric_md=(rubric_md or "").strip()[:4500],
        evidence=json.dumps(evidence, indent=1, ensure_ascii=False)[:9000])
    r = _http_chat(spec, prompt, timeout=timeout,
                   retries=max(0, int(provider_retries)),
                   max_output_tokens=max_output_tokens,
                   reasoning_effort=reasoning_effort)
    trajectory = [{"type": "input", "role": "user", "content": prompt},
                  {"type": "output", "role": "assistant", "content": r.get("text", ""),
                   "status": r["status"]}]
    if r["status"] != "ok":
        return {"verdict": "infra", "confidence": None,
                "justification": r.get("error", ""),
                "status": r["status"], "trajectory": trajectory}
    verdict = parse_verdict(r["text"])
    parse_fallback = False
    if (verdict == "infra"
            and fallback_verdict in {"completed", "failed", "world_imperfect"}):
        verdict = fallback_verdict
        parse_fallback = True
    return {"verdict": verdict,
            "confidence": parse_confidence(r["text"], verdict),
            "justification": r["text"], "status": "ok",
            "parse_fallback": parse_fallback,
            "input_tokens": r.get("input_tokens"),
            "output_tokens": r.get("output_tokens"),
            "reasoning_effort": r.get("reasoning_effort"),
            "reasoning_policy": r.get("reasoning_policy"),
            "trajectory": trajectory}


def parse_verdict(text: str) -> str:
    m = re.search(r"llm_verdict[^a-z]{0,3}(completed|failed|world_imperfect"
                  r"|partial(?:ly_completed)?|agent_fail)", (text or "").lower())
    if m:
        v = m.group(1)
        if v.startswith("partial") or v == "agent_fail":
            return "failed"
        return v

    # Accept a decisive confidence line when the sentinel line is missing.
    conf = parse_confidence(text)
    if conf:
        ordered = sorted(conf.items(), key=lambda kv: kv[1], reverse=True)
        top, second = ordered[0], ordered[1]
        if top[1] >= 60 and top[1] - second[1] >= 20:
            return top[0]
    return "infra"


def parse_confidence(text: str, verdict: str | None = None) -> dict | None:
    """Per-class 0-100 confidence from `CONFIDENCE:`; falls back to one-hot on `verdict`."""
    classes = ("completed", "failed", "world_imperfect")
    m = re.search(r"confidence\b[^\n]*", text or "", re.I)
    line = m.group(0) if m else ""
    conf = {}
    for c in classes:
        mm = re.search(rf"{c}\s*[=:]\s*(\d+(?:\.\d+)?)", line, re.I)
        if mm:
            conf[c] = max(0, min(100, int(round(float(mm.group(1))))))
    if len(conf) == 3:
        return conf
    if verdict in classes:
        return {c: (100 if c == verdict else 0) for c in classes}
    return None
