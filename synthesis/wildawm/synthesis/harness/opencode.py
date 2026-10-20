#!/usr/bin/env python3
"""opencode synthesis runner: one headless ``opencode run`` turn returning a ``CallRecord``."""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from wildawm_common import DESCRIBE_IMAGE_MCP

from wildawm import const_config as cfg
from wildawm.synthesis.tasks import claude_runner as cr
from wildawm.synthesis.harness import config as occ

CallRecord = cr.CallRecord
BatchAbortError = cr.BatchAbortError

# Canonical describe_image MCP, shared with the evaluation component.
DESCRIBE_IMAGE_MCP_SRC = DESCRIBE_IMAGE_MCP

# Re-injects the agentic keep-going directive opencode's default prompt lacks for local models.
AGENTIC_PERSIST_NOTE = """
## Keep going until the task is fully done
You are an autonomous agent. Keep working until the ENTIRE task is complete and
verified before ending your turn — do not hand back early. CRITICAL: never end
your turn right after merely announcing an action. When you say you are going to
read, run, write, or check something, you MUST actually emit that tool call in
the SAME response — do NOT stop after a sentence like "Now let me…" or "Let me
check…". Text with no tool call ends your turn, so only reply with text-only when
everything required is finished. If unsure whether you are done, do more — keep
calling tools — rather than stopping.
"""

# Steers the model to read figures via the vision MCP instead of the image-blind `read` tool.
VISION_NOTE = """
## Reading images (text-only model)
You cannot see images directly. The `read` tool will NOT return image pixels for
this model. For any image file (.png/.jpg/.jpeg/.gif/.webp/.bmp), call the
`vision_describe_image` tool with the image's ABSOLUTE path and a focused
question; treat its text reply as the image contents. Use `bash` (cat, sqlite3,
grep, diff, curl) for all text/DB evidence. If `vision_describe_image` returns a
`[describe_image error]`, cite it and fall back to text/DB evidence instead of
trying to read the image bytes.
"""

# opencode exposes no reasoning `--variant` for these model families, so it is skipped.
_NO_VARIANT_SUBSTRINGS = ("glm", "qwen", "deepseek", "kimi", "minimax", "k2p",
                          "big-pickle")


def _supports_variant(model: str) -> bool:
    m = model.lower()
    return not any(s in m for s in _NO_VARIANT_SUBSTRINGS)


# Prints MCP servers registered in claude's config; opencode containers fall back to fixed URLs.
_READ_CLAUDE_MCP = (
    "import json,glob,os\n"
    "out={}\n"
    "paths=glob.glob('/home/*/.claude.json')+['/root/.claude.json',"
    "os.path.expanduser('~/.claude.json')]\n"
    "for p in paths:\n"
    "  try: d=json.load(open(p))\n"
    "  except Exception: continue\n"
    "  groups=list((d.get('projects') or {}).values())+[d]\n"
    "  for g in groups:\n"
    "    for n,s in (g.get('mcpServers') or {}).items():\n"
    "      if isinstance(s,dict) and s.get('url'): out[n]={'type':s.get('type','http'),'url':s['url']}\n"
    "print(json.dumps(out))\n"
)


def _container_synth_mcp(container: str, *, timeout_ms: int = 8000) -> dict:
    """Return opencode `mcp` entries for the app + browser MCP servers."""
    try:
        r = subprocess.run(
            ["docker", "exec", container, "python3", "-c", _READ_CLAUDE_MCP],
            capture_output=True, text=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
        registered = json.loads((r.stdout or "").strip() or "{}")
    except Exception:
        registered = {}
    if not registered:
        base = f"http://mcp:{cfg.MCP_INTERNAL_PORT}"
        try:
            probe = subprocess.run(
                ["docker", "exec", container, "python3", "-c",
                 "import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://mcp:%s/apps', timeout=5)).get('loaded', [])))" % cfg.MCP_INTERNAL_PORT],
                capture_output=True, text=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
            active = json.loads((probe.stdout or "").strip() or "[]")
        except Exception:
            active = []
        registered = {str(svc): {"type": "http", "url": f"{base}/{svc}/mcp"}
                      for svc in active if str(svc).strip()}
        registered["wildawm"] = {"type": "http", "url": f"{base}/mcp"}
        registered["browser"] = {
            "type": "http",
            "url": f"http://localhost:{cfg.BROWSER_MCP_PORT}/mcp",
        }
    out: dict = {}
    for name, srv in registered.items():
        if isinstance(srv, dict) and srv.get("url"):
            out[name] = {"type": "remote", "url": srv["url"],
                         "enabled": True, "timeout": timeout_ms}
    return out


@dataclass
class OpencodeModel:
    """A resolved opencode model target (an OpenAI-compatible upstream)."""
    model: str
    base_url: str
    upstream_base_url: str = ""  # host-side endpoint for local proxies
    api_key: str = "not-used"
    effort: str = "max"  # reasoning-effort variant
    provider_id: str = "glm"
    ref: str = ""
    reasoning_policy: dict | None = None
    context_window: int | None = None  # models.json model_info.context_window


def _model_context_tokens(model: OpencodeModel) -> int:
    """Resolve the synthesis context size; explicit env takes precedence."""
    override = os.environ.get("AWM_OPENCODE_MODEL_CONTEXT_TOKENS")
    if override and override.strip():
        return max(1, int(override))
    if model.context_window is not None:
        return max(1, int(model.context_window))
    return cfg.OPENCODE_MODEL_CONTEXT_TOKENS


_OPENCODE_RESPONSE_EVENT_TYPES = {"text", "tool_use", "step_finish", "error"}


def _reduce_events(events: list[dict]) -> dict:
    """Reduce the opencode --format json event stream to CallRecord fields."""
    text_parts: "OrderedDict[str, tuple[str, str]]" = OrderedDict()  # partID -> (msgID, text)
    last_msg = ""
    steps = 0
    cost = 0.0
    out_tokens = 0
    in_tokens = 0
    reasoning_tokens = 0
    tool_calls = 0
    vision_calls = 0
    session_id = ""
    errors: list[dict] = []
    response_events = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        session_id = session_id or ev.get("sessionID", "") or ""
        etype = ev.get("type")
        if etype in _OPENCODE_RESPONSE_EVENT_TYPES:
            response_events += 1
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if etype == "text":
            pid = part.get("id") or f"_{len(text_parts)}"
            mid = part.get("messageID", "") or ""
            text_parts[pid] = (mid, part.get("text", "") or "")
            last_msg = mid or last_msg
        elif etype == "step_finish":
            steps += 1
            cost += float(part.get("cost") or 0.0)
            tk = part.get("tokens") or {}
            out_tokens += int(tk.get("output") or 0)
            in_tokens = max(in_tokens, int(tk.get("input") or 0))
            reasoning_tokens += int(tk.get("reasoning") or 0)
        elif etype == "tool_use":
            tool_calls += 1
            if "describe_image" in str(part.get("tool") or ""):
                vision_calls += 1
        elif etype == "error":
            err = ev.get("error")
            if isinstance(err, dict):
                errors.append(err)
    # Final text = text parts of the last assistant message.
    if last_msg:
        final = [t for (mid, t) in text_parts.values() if mid == last_msg]
    else:
        final = [t for (_mid, t) in text_parts.values()]
    result_text = "\n".join(s for s in final if s).strip()
    return {
        "result_text": result_text,
        "num_turns": steps,
        "cost_usd": cost,
        "session_id": session_id,
        "tool_calls": tool_calls,
        "vision_calls": vision_calls,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "reasoning_tokens": reasoning_tokens,
        "errors": errors,
        "events": events,
        "response_events": response_events,
    }


def _classify_opencode_error(errors: list[dict]) -> str | None:
    """Map opencode `error` events to the most severe status, or None if none is fatal."""
    severity = {"auth": 5, "quota": 4, "server": 3, "rate_limit": 2,
                "max_output": 1, "interrupted": 1, "error": 0}
    worst: str | None = None
    for err in errors:
        name = str(err.get("name") or "")
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        code = data.get("statusCode")
        if name == "ProviderAuthError":
            st = "auth"
        elif name == "APIError":
            has_code = isinstance(code, int) or (
                isinstance(code, str) and code.strip().isdigit())
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = 0
            blob = f"{code} {data.get('message') or ''} {data.get('responseBody') or ''}"
            if code in (401, 403):
                st = "auth"
            elif "insufficient_quota" in blob.lower():
                st = "quota"
            elif code == 429:
                st = "rate_limit"
            elif code >= 500:
                st = "server"
            elif not has_code:
                # A status-less APIError is a transient network/stream drop: retry as server.
                st = "server"
            else:
                st = cr._classify_error_text(blob) if blob.strip() else "error"
        elif name == "MessageAbortedError":
            st = "interrupted"
        elif name == "MessageOutputLengthError":
            st = "max_output"
        else:
            st = "error"
        if worst is None or severity.get(st, 0) > severity.get(worst, 0):
            worst = st
    return worst


def _parse(stdout: str, stderr: str, rc: int | None,
           timed_out: bool, interrupted: bool,
           output_reader: "Callable[[str], str | None] | None" = None,
           require_response_event: bool = False) -> CallRecord:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if output_reader is not None:
        _hydrate_truncated_tool_outputs(events, output_reader)
    red = _reduce_events(events)
    rec = CallRecord(role="", rc=rc, num_turns=red["num_turns"],
                     cost_usd=red["cost_usd"], session_id=red["session_id"],
                     result_text=red["result_text"],
                     stdout_tail=stdout, stderr_tail=stderr)
    rec.extra.update({"tool_calls": red["tool_calls"],
                      "n_tool_uses": red["tool_calls"],
                      "n_vision_calls": red["vision_calls"],
                      "input_tokens": red["input_tokens"],
                      "output_tokens": red["output_tokens"],
                      "reasoning_tokens": red["reasoning_tokens"],
                      "opencode_event_count": len(red["events"]),
                      "opencode_response_event_count": red["response_events"],
                      "harness": "opencode"})
    # Full transcript from the JSONL stream; archivers pop it before serializing.
    rec.extra["trajectory"] = red["events"]
    err_status = _classify_opencode_error(red["errors"])
    protocol_no_response = (
        require_response_event
        and rc in (0, None)
        and not timed_out
        and not interrupted
        and red["response_events"] == 0
    )
    if timed_out:
        rec.status = "timeout"
    elif interrupted:
        rec.status = "interrupted"
        rec.extra["terminal_reason"] = "llm_turn_budget_interrupt"
    elif protocol_no_response:
        # Resume exiting rc=0 with no events is an opencode protocol failure: treat as infra.
        rec.status = "server"
        rec.extra["opencode_protocol_error"] = "resume_no_response_event"
    elif err_status in ("auth", "quota", "rate_limit", "server"):
        # Surface upstream errors even if some text leaked.
        rec.status = err_status
        if red["errors"]:
            rec.extra["opencode_error"] = red["errors"][-1]
    elif rc not in (0, None):
        # A non-zero exit wins over leaked text unless a provider error classified it.
        rec.status = cr._classify_error_text(f"{stderr}\n{stdout}")
    elif rec.result_text.strip():
        rec.status = "ok"
    elif err_status:
        rec.status = "empty" if err_status in ("max_output",) else err_status
        if red["errors"]:
            rec.extra["opencode_error"] = red["errors"][-1]
    elif not events:
        rec.status = "parse_miss" if stdout.strip() else "empty"
    else:
        rec.status = "empty"
    return rec


def _hydrate_truncated_tool_outputs(events: list[dict], reader: Callable[[str], str | None]) -> None:
    """Attach full opencode tool output when the JSONL event carries only a truncated preview."""
    for ev in events:
        try:
            part = ev.get("part") or {}
            if ev.get("type") != "tool_use" or not isinstance(part, dict):
                continue
            state = part.get("state") or {}
            if not isinstance(state, dict):
                continue
            meta = state.get("metadata") or {}
            if not isinstance(meta, dict) or meta.get("truncated") is not True:
                continue
            output_path = str(meta.get("outputPath") or "").strip()
            if not output_path:
                continue
            full = reader(output_path)
            if full is None:
                continue
            state["output_full"] = full
            meta["output_full"] = full
            meta["output_full_chars"] = len(full)
            meta["output_hydrated"] = True
        except Exception:  # noqa: BLE001 - transcript hydration is best-effort
            continue


_DB_PART_EVENT_TYPES = {
    "step-start": "step_start",
    "step-finish": "step_finish",
    "tool": "tool_use",
}


def _rows_to_opencode_events(rows: list[dict]) -> list[dict]:
    """Convert opencode SQLite `part` rows into JSONL-shaped assistant events."""
    out: list[dict] = []
    for row in rows:
        try:
            data = row.get("data")
            if isinstance(data, str):
                data = json.loads(data)
            msg_data = row.get("message_data")
            if isinstance(msg_data, str):
                msg_data = json.loads(msg_data)
            role = (msg_data or {}).get("role")
            if role == "user":
                continue
            if not isinstance(data, dict):
                continue
            ptype = str(data.get("type") or "")
            part = dict(data)
            part.setdefault("id", row.get("id") or "")
            part.setdefault("messageID", row.get("message_id") or "")
            part.setdefault("sessionID", row.get("session_id") or "")
            out.append({
                "type": _DB_PART_EVENT_TYPES.get(ptype, ptype),
                "timestamp": row.get("time_created") or row.get("time_updated") or 0,
                "sessionID": row.get("session_id") or "",
                "part": part,
            })
        except Exception:  # noqa: BLE001 - DB replay is best-effort
            continue
    return out


_DB_HIGHWATER_PY = r"""
import os, sqlite3, sys
db, sid = sys.argv[1], sys.argv[2]
if not os.path.exists(db):
    print(0)
    raise SystemExit
con = sqlite3.connect(db)
part_max = con.execute(
    "select coalesce(max(time_created), 0) from part where session_id=?",
    (sid,),
).fetchone()[0] or 0
msg_max = con.execute(
    "select coalesce(max(time_created), 0) from message where session_id=?",
    (sid,),
).fetchone()[0] or 0
print(max(int(part_max), int(msg_max)))
"""


_DB_EVENTS_SINCE_PY = r"""
import json, os, sqlite3, sys
db, sid, since = sys.argv[1], sys.argv[2], int(sys.argv[3])
if not os.path.exists(db):
    print("[]")
    raise SystemExit
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
rows = con.execute(
    "select "
    "p.id as id, "
    "p.message_id as message_id, "
    "p.session_id as session_id, "
    "p.time_created as time_created, "
    "p.time_updated as time_updated, "
    "p.data as data, "
    "m.data as message_data "
    "from part p "
    "left join message m on m.id = p.message_id "
    "where p.session_id = ? and p.time_created > ? "
    "order by p.time_created, p.id",
    (sid, since),
).fetchall()
print(json.dumps([dict(r) for r in rows]))
"""


def _opencode_db_path(container_scratch: str) -> str:
    return f"{container_scratch}/xdg-data/opencode/opencode.db"


def _opencode_db_highwater(container: str | None, container_scratch: str | None,
                           session_id: str | None) -> int | None:
    if not (container and container_scratch and session_id):
        return None
    try:
        p = subprocess.run(
            ["docker", "exec", container, "python3", "-c", _DB_HIGHWATER_PY,
             _opencode_db_path(container_scratch), session_id],
            capture_output=True, text=True, timeout=cfg.HTTP_SHORT_TIMEOUT_S)
        if p.returncode != 0:
            return None
        return int((p.stdout or "0").strip() or "0")
    except Exception:
        return None


def _opencode_db_events_since(container: str | None, container_scratch: str | None,
                              session_id: str | None, since_ms: int | None
                              ) -> list[dict]:
    if not (container and container_scratch and session_id):
        return []
    try:
        p = subprocess.run(
            ["docker", "exec", container, "python3", "-c", _DB_EVENTS_SINCE_PY,
             _opencode_db_path(container_scratch), session_id, str(int(since_ms or 0))],
            capture_output=True, text=True, timeout=cfg.HTTP_SHORT_TIMEOUT_S)
        if p.returncode != 0:
            return []
        rows = json.loads((p.stdout or "[]").strip() or "[]")
        if not isinstance(rows, list):
            return []
        return _rows_to_opencode_events(rows)
    except Exception:
        return []


def _free_host_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_proxy_health(url: str, *, timeout_s: float = 10.0) -> bool:
    import urllib.request
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _start_reasoning_proxy(model: OpencodeModel, host_scratch: str
                           ) -> tuple[subprocess.Popen | None, str | None]:
    """Start a short-lived host proxy for model-specific reasoning injection."""
    policy = model.reasoning_policy
    if not policy:
        return None, None
    port = _free_host_port()
    log_path = Path(host_scratch) / "reasoning_proxy.log"
    traj_dir = Path(host_scratch) / "proxy_traj"
    img_dir = Path(host_scratch) / "proxy_img"
    traj_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "AWM_LLM_PROXY_PORT": str(port),
        "AWM_OPENAI_BASE_URL": model.upstream_base_url or model.base_url,
        "AWM_OPENAI_API_KEY": model.api_key,
        "AWM_OPENAI_MODEL": model.model,
        "AWM_PROXY_PIN_MODEL": "1",
        "AWM_PROXY_MODEL_REF": model.ref or model.model,
        "AWM_PROXY_TRAJ_DIR": str(traj_dir),
        "AWM_PROXY_IMAGE_DIR": str(img_dir),
    })
    env.update(cfg.reasoning_effort_env(policy))
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            [os.sys.executable,
             str(cfg.PROJECT_ROOT / "docker" / "anthropic-to-openai-proxy.py")],
            cwd=str(cfg.PROJECT_ROOT), env=env,
            stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    host_url = f"http://127.0.0.1:{port}"
    if not _wait_proxy_health(host_url):
        try:
            proc.terminate()
        except Exception:
            pass
        raise RuntimeError(f"opencode reasoning proxy failed to start; see {log_path}")
    return proc, f"http://{cfg.DOCKER_HOST_GATEWAY}:{port}/v1"


def _stop_reasoning_proxy(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _scratch_env(container_scratch: str) -> dict:
    """Per-run XDG/HOME dirs so opencode's session DB and logs never collide."""
    home = f"{container_scratch}/home"
    return {
        "HOME": home,
        "XDG_CONFIG_HOME": f"{container_scratch}/xdg-config",
        "XDG_DATA_HOME": f"{container_scratch}/xdg-data",
        "XDG_STATE_HOME": f"{container_scratch}/xdg-state",
        "XDG_CACHE_HOME": f"{container_scratch}/xdg-cache",
        "OPENCODE_CONFIG": f"{container_scratch}/opencode.json",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        # Stay offline: never fetch the models.dev catalog (would hang behind a firewall).
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
    }


_FAKETIME_BASE_RE = re.compile(
    r"^\s*@(?P<date>\d{4}-\d{2}-\d{2})(?:\s+(?P<time>\d{2}:\d{2}(?::\d{2})?))?"
)


def _parse_faketime_base(raw: str) -> datetime | None:
    """Parse the dashboard's `/etc/faketimerc` form: `@YYYY-MM-DD HH:MM:SS`."""
    m = _FAKETIME_BASE_RE.search(raw or "")
    if not m:
        return None
    ts = f"{m.group('date')} {m.group('time') or '00:00:00'}"
    if len(ts.rsplit(" ", 1)[-1].split(":")) == 2:
        ts += ":00"
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _container_faketime_base(container: str | None) -> datetime | None:
    if not container:
        return None
    try:
        p = subprocess.run(
            ["docker", "exec", container, "cat", "/etc/faketimerc"],
            capture_output=True, text=True, timeout=cfg.HTTP_SHORT_TIMEOUT_S)
        if p.returncode == 0:
            return _parse_faketime_base(p.stdout)
    except Exception:
        return None
    return None


def _faketime_at(base: datetime, offset_s: float) -> str:
    # Keep opencode message time monotonic across separate resume processes.
    dt = base + timedelta(seconds=max(0, int(offset_s)))
    return "@" + dt.strftime("%Y-%m-%d %H:%M:%S")


def _faketime_from_epoch_ms(epoch_ms: int, offset_s: float) -> str | None:
    """Build a FAKETIME override offset from an epoch-ms timestamp (the session high-water)."""
    if not epoch_ms or epoch_ms <= 0:
        return None
    dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc) + timedelta(
        seconds=max(0, int(offset_s)))
    return "@" + dt.strftime("%Y-%m-%d %H:%M:%S")


def _max_faketime_override(a: str | None, b: str | None) -> str | None:
    """Return the later of two FAKETIME overrides; unparseable values sort lowest."""
    if not a and not b:
        return None
    if not a:
        return b
    if not b:
        return a
    ta = _parse_faketime_base(a)
    tb = _parse_faketime_base(b)
    if ta is None:
        return b
    if tb is None:
        return a
    return a if ta >= tb else b


def _faketime_after(raw: str | None, offset_s: float) -> str | None:
    """Return a FAKETIME override strictly after another override."""
    dt = _parse_faketime_base(raw or "")
    if dt is None:
        return None
    dt = dt + timedelta(seconds=max(1, int(offset_s)))
    return "@" + dt.strftime("%Y-%m-%d %H:%M:%S")


def _cmd_opencode(model: OpencodeModel, prompt: str, *, workdir: str,
                  scratch: str, resume_session_id: str | None) -> list[str]:
    # argv without the leading `opencode` binary; callers supply it.
    argv = ["run", "--format", "json",
            "-m", f"{model.provider_id}/{model.model}",
            "--dangerously-skip-permissions", "--dir", workdir]
    if _supports_variant(model.model) and model.effort:
        argv += ["--variant", model.effort]
    if resume_session_id:
        argv += ["--session", resume_session_id]
    # No `--` separator: yargs would divert the positional message into argv['--'].
    argv += [prompt]
    return argv


def run_opencode_code(prompt: str, model: OpencodeModel, *, role: str,
                      timeout: int,
                      container: str | None = None,
                      image: str | None = None,
                      cwd: str | None = None,
                      workdir: str = "/workspace",
                      extra_paths: list[str] | None = None,
                      as_user: bool = True,
                      system_prompt_file: str | None = None,
                      allowed_tools: str | None = None,
                      disallowed_tools: str | None = None,
                      mcp_config: str | None = None,
                      vision: bool = True,
                      vision_note: str | None = None,
                      websyn_dir: str | None = None,
                      container_mcp: bool = False,
                      vision_base_url: str | None = None,
                      vision_model: str | None = None,
                      vision_api_key: str | None = None,
                      python_cmd: str = "python3",
                      session_id: str | None = None,
                      resume_session_id: str | None = None,
                      opencode_container_scratch: str | None = None,
                      max_turns: int | None = None,
                      nudge_max: int = 0, nudge_prompt: str | None = None,
                      done_check: Callable[[], tuple[bool, str]] | None = None,
                      retries: int = 1, abort_on_auth: bool = True,
                      env_extra: dict | None = None,
                      stall_check: Callable[[], tuple[bool, str]] | None = None,
                      interrupt_check: Callable[[], tuple[bool, str]] | None = None,
                      stall_poll_s: float = 10.0,
                      # Accepted to match run_claude_code's signature; unused.
                      output_format: str = "json", proxy_url: str | None = None,
                      mcp_strict: bool = True) -> CallRecord:
    """Run one opencode turn (exec into ``container`` or a throwaway ``image`` run)."""
    if not container and not (image and cwd):
        raise ValueError("run_opencode_code needs either container= (exec) or "
                         "image= + cwd= (throwaway run)")
    uid, gid = os.getuid(), os.getgid()
    run_workdir = workdir if container else str(cwd)
    container_scratch = (
        opencode_container_scratch
        if opencode_container_scratch
        else f"/tmp/awm-oc-{uuid.uuid4().hex[:12]}"
    )

    mcp = {}
    if vision:
        _vs = cfg.opencode_vision_spec()
        mcp.update(occ.vision_mcp_block(
            python_cmd=python_cmd,
            script_path=f"{container_scratch}/describe_image_mcp.py",
            base_url=vision_base_url or _vs["base_url"],
            model=vision_model or _vs["api_model"],
            api_key=vision_api_key or _vs["api_key"],
            extra_env=cfg.vision_reasoning_env()))
    if container_mcp and container:
        mcp.update(_container_synth_mcp(container))
    mcp.update(occ.translate_claude_mcp(mcp_config))

    # Stage per-run inputs in a host dir that is copied/mounted into the container.
    host_scratch = tempfile.mkdtemp(prefix="awm-oc-")
    # opencode needs writable HOME + XDG dirs in both run and exec mode.
    for sub in ("home", "xdg-config", "xdg-data", "xdg-state", "xdg-cache"):
        (Path(host_scratch) / sub).mkdir(parents=True, exist_ok=True)
    env_extra = dict(env_extra or {})
    models_path = env_extra.get("AWM_MODELS_PATH")
    if models_path and os.path.exists(models_path):
        shutil.copy2(models_path, Path(host_scratch) / "models.json")
        env_extra["AWM_MODELS_PATH"] = f"{container_scratch}/models.json"
    shutil.copy2(DESCRIBE_IMAGE_MCP_SRC,
                 Path(host_scratch) / "describe_image_mcp.py")
    instructions: list[str] = []
    # Always first: counter opencode's premature stops (see AGENTIC_PERSIST_NOTE).
    (Path(host_scratch) / "agentic_note.md").write_text(AGENTIC_PERSIST_NOTE)
    instructions.append(f"{container_scratch}/agentic_note.md")
    if system_prompt_file:
        if os.path.exists(system_prompt_file):
            # Host-side file: stage it into the scratch dir.
            shutil.copy2(system_prompt_file, Path(host_scratch) / "constitution.md")
            instructions.append(f"{container_scratch}/constitution.md")
        elif container:
            # Path already inside the container: use it directly.
            instructions.append(system_prompt_file)
        else:
            print(f"[opencode] WARNING: system_prompt_file not found on host and "
                  f"no container to read it from: {system_prompt_file}", flush=True)
    if vision:
        (Path(host_scratch) / "vision_note.md").write_text(
            vision_note or VISION_NOTE)
        instructions.append(f"{container_scratch}/vision_note.md")

    tools_gate = occ.translate_tool_gate(allowed_tools, disallowed_tools)
    if vision and container_mcp:
        # The text-only model can't read screenshots; force vision_describe_image instead.
        tools_gate.setdefault("browser_browser_take_screenshot", False)
    proxy_proc = None
    effective_base_url = model.base_url
    try:
        proxy_proc, proxy_base_url = _start_reasoning_proxy(model, host_scratch)
        if proxy_base_url:
            effective_base_url = proxy_base_url
    except RuntimeError as e:
        shutil.rmtree(host_scratch, ignore_errors=True)
        return CallRecord(role=role, status="server", rc=2,
                          stderr_tail=str(e))
    cfg_dict = occ.build_opencode_config(
        provider_id=model.provider_id, model=model.model,
        base_url=effective_base_url, api_key=model.api_key,
        instructions=instructions or None, tools=tools_gate or None,
        mcp=mcp or None,
        # Same bound as claude: the real cap is the wall-clock timeout.
        max_steps=max_turns if max_turns is not None
        else cfg.SYNTHESIS_MAX_LLM_TURNS,
        # Large output budget so final-text roles don't hit finish_reason=length.
        max_output_tokens=cfg.OPENCODE_MODEL_OUTPUT_TOKENS,
        context_tokens=_model_context_tokens(model))
    (Path(host_scratch) / "opencode.json").write_text(json.dumps(cfg_dict, indent=2))

    env = _scratch_env(container_scratch)
    env.update(env_extra)
    faketime_base = _container_faketime_base(container)
    fake_elapsed_s = 0.0
    last_resume_faketime_override: str | None = None

    try:
        # Resume turns get a later FAKETIME so opencode's session DB order stays monotonic.
        if container:
            _stage_into_container(container, host_scratch, container_scratch, uid, gid)
        else:
            # Validate/memoize read-only mounts once; _prefix() reuses them.
            mounted = {str(cwd), container_scratch}
            extra_mounts: list[str] = []
            for p0 in (extra_paths or []):
                p = str(p0)
                if not p or p in mounted:
                    continue
                if not os.path.exists(p):
                    print(f"[opencode] WARNING: extra_path does not exist, not "
                          f"mounting (model will not see it): {p}", flush=True)
                    continue
                extra_mounts += ["-v", f"{p}:{p}:ro"]
                mounted.add(p)

        def _prefix(turn_env: dict) -> list[str]:
            if container:
                out = ["docker", "exec"]
                if as_user:
                    out += ["--user", f"{uid}:{gid}"]
                for k, v in turn_env.items():
                    out += ["-e", f"{k}={v}"]
                out += ["-w", run_workdir, container, "opencode"]
                return out
            safe_role = re.sub(r"[^A-Za-z0-9_.-]+", "-", role).replace("_", "-")
            safe_role = (safe_role.strip("-") or "role")[:32]
            run_name = f"wildawm-opencode-run-{safe_role}-{uuid.uuid4().hex[:12]}"
            out = [
                "docker", "run", "--rm", "--name", run_name, "-i",
                "--entrypoint", "opencode",
            ]
            if as_user:
                out += ["--user", f"{uid}:{gid}"]
            out += ["-v", f"{cwd}:{cwd}",
                    "-v", f"{host_scratch}:{container_scratch}"]
            out += extra_mounts
            # Read-only site tree for judge web-fact checks (run mode only).
            if websyn_dir:
                out += ["-v", f"{websyn_dir}:/websyn:ro"]
            for k, v in turn_env.items():
                out += ["-e", f"{k}={v}"]
            out += ["-w", run_workdir, image]
            return out

        def _turn(turn_prompt: str, resume_sid: str | None,
                  turn_timeout: int | None = None) -> CallRecord:
            nonlocal fake_elapsed_s, last_resume_faketime_override
            turn_env = dict(env)
            faketime_override = None
            db_replay_since_ms = None
            if resume_sid:
                # Session high-water timestamp: feeds DB replay and the FAKETIME override.
                db_replay_since_ms = _opencode_db_highwater(
                    container, container_scratch, resume_sid)
                # Frozen libfaketime clock: push the resume clock past the session's last message.
                margin = max(3600.0, fake_elapsed_s + 60.0)
                base_override = (
                    _faketime_at(faketime_base, margin)
                    if faketime_base is not None else None)
                hw_override = _faketime_from_epoch_ms(
                    db_replay_since_ms or 0, max(margin, 60.0))
                # Take the later override so the clock never regresses below the high-water.
                faketime_override = _max_faketime_override(
                    base_override, hw_override) or hw_override or base_override
                # Never reuse the previous resume's fake time; it can reorder opencode's session DB.
                min_after_previous = _faketime_after(
                    last_resume_faketime_override, 3600.0)
                faketime_override = _max_faketime_override(
                    faketime_override, min_after_previous)
                if faketime_override:
                    turn_env["FAKETIME"] = faketime_override
            argv = _cmd_opencode(model, turn_prompt, workdir=run_workdir,
                                 scratch=container_scratch,
                                 resume_session_id=resume_sid)
            rec = _run_with_retry(_prefix(turn_env) + argv, role=role,
                                  timeout=turn_timeout or timeout,
                                  env=None, retries=retries,
                                  abort_on_auth=abort_on_auth,
                                  container=container if container else None,
                                  session_id=session_id,
                                  resume_session_id=resume_sid,
                                  container_scratch=container_scratch,
                                  host_scratch=host_scratch,
                                  db_replay_since_ms=db_replay_since_ms,
                                  stall_check=stall_check,
                                  interrupt_check=interrupt_check,
                                  stall_poll_s=stall_poll_s)
            fake_elapsed_s += max(0.0, float(rec.seconds or 0.0)) + 5.0
            rec.extra["opencode_container_scratch"] = container_scratch
            if faketime_override:
                rec.extra["opencode_faketime_override"] = faketime_override
                last_resume_faketime_override = faketime_override
            if db_replay_since_ms is not None:
                rec.extra["opencode_db_highwater_ms"] = db_replay_since_ms
                if faketime_override == hw_override and hw_override is not None:
                    rec.extra["opencode_faketime_source"] = "db_highwater"
                elif faketime_override == base_override and base_override is not None:
                    rec.extra["opencode_faketime_source"] = "faketimerc_base"
                else:
                    rec.extra["opencode_faketime_source"] = "max"
            return rec

        deadline = time.monotonic() + timeout
        rec = _turn(prompt, resume_session_id)
        # Resume the session with reminders until the role's deliverables are done.
        if (nudge_max and nudge_prompt and rec.session_id
                and rec.status in ("ok", "empty", "parse_miss")):
            rec = _nudge_loop(rec, _turn, nudge_prompt, nudge_max, deadline,
                              done_check)
        return rec
    finally:
        _stop_reasoning_proxy(proxy_proc)
        shutil.rmtree(host_scratch, ignore_errors=True)


def _nudge_loop(rec: CallRecord, turn_fn, nudge_prompt: str,
                nudge_max: int, deadline: float,
                done_check: "Callable[[], tuple[bool, str]] | None" = None
                ) -> CallRecord:
    """Resume the session with a reminder until a real completion signal or the budget runs out."""

    def _done_status() -> tuple[bool, str]:
        if done_check is not None:
            try:
                ok, reason = done_check()
                return bool(ok), str(reason or "")
            except Exception as e:                  # a flaky probe must not end the loop
                print(f"[opencode] done_check raised, treating as not-done: {e}",
                      flush=True)
                return False, f"done_check raised: {type(e).__name__}: {e}"
        # Fallback: only the explicit DONE sentinel, with no pending tool call.
        return (
            int(rec.extra.get("tool_calls") or 0) == 0
            and bool(re.search(r"\bDONE\b", rec.result_text or "")),
            "",
        )

    def _nudge_text(reason: str) -> str:
        reason = (reason or "").strip()
        if not reason:
            return nudge_prompt
        return (
            f"{nudge_prompt}\n\n"
            "HARNESS DONE CHECK IS STILL FAILING:\n"
            f"{reason[:2000]}\n\n"
            "Fix that specific failing condition before replying DONE."
        )

    sid = rec.session_id
    total_turns = rec.num_turns
    total_tools = int(rec.extra.get("tool_calls") or 0)
    traj = list(rec.extra.get("trajectory") or [])
    nudges = 0
    stop_reason = "nudge_max"
    # Already complete after the main turn (probe only)? then skip nudging.
    done, reason = _done_status()
    if done_check is not None and done:
        rec.extra["nudges_used"] = 0
        rec.extra["nudge_stop_reason"] = "done"
        return rec
    no_progress_streak = 0
    for _ in range(nudge_max):
        remaining = deadline - time.monotonic()
        if remaining <= cfg.HTTP_SHORT_TIMEOUT_S:
            stop_reason = "time_budget"
            break
        nrec = turn_fn(_nudge_text(reason), sid, int(remaining))
        nudges += 1
        total_turns += nrec.num_turns
        total_tools += int(nrec.extra.get("tool_calls") or 0)
        traj += list(nrec.extra.get("trajectory") or [])
        sid = nrec.session_id or sid
        rec = nrec
        if nrec.status not in ("ok", "empty", "parse_miss"):
            stop_reason = f"infra:{nrec.status}"
            break
        done, reason = _done_status()
        if done:
            stop_reason = "done"
            break
        made_progress = (
            nrec.num_turns > 0
            or int(nrec.extra.get("tool_calls") or 0) > 0
            or bool((nrec.result_text or "").strip())
        )
        if not made_progress:
            no_progress_streak += 1
            nrec.status = "server"
            nrec.extra["opencode_protocol_error"] = (
                nrec.extra.get("opencode_protocol_error")
                or "resume_no_progress"
            )
            stop_reason = "infra:server"
            break
        no_progress_streak = 0
    rec.num_turns = total_turns
    rec.session_id = sid
    rec.extra["tool_calls"] = total_tools
    rec.extra["nudges_used"] = nudges
    rec.extra["nudge_stop_reason"] = stop_reason
    rec.extra["nudge_no_progress_streak"] = no_progress_streak
    rec.extra["trajectory"] = traj
    return rec


def _stage_into_container(container: str, host_scratch: str,
                          container_scratch: str, uid: int, gid: int) -> None:
    cr.run_cmd(["docker", "exec", container, "sh", "-c",
                f"mkdir -p {container_scratch} && chmod 777 {container_scratch}"],
               role="opencode_stage", timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    cr.run_cmd(["docker", "cp", f"{host_scratch}/.",
                f"{container}:{container_scratch}/"],
               role="opencode_stage", timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    cr.run_cmd(["docker", "exec", container, "sh", "-c",
                f"chmod -R a+rwX {container_scratch}"],
               role="opencode_stage", timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)


def _run_with_retry(cmd: list[str], *, role: str, timeout: int,
                    env: dict | None, retries: int, abort_on_auth: bool,
                    container: str | None, session_id: str | None,
                    resume_session_id: str | None,
                    container_scratch: str | None = None,
                    host_scratch: str | None = None,
                    db_replay_since_ms: int | None = None,
                    stall_check=None, interrupt_check=None,
                    stall_poll_s: float = 10.0) -> CallRecord:
    attempts = 0
    last: CallRecord | None = None
    while attempts <= retries:
        attempts += 1
        t0 = time.time()
        rc, stdout, stderr, timed_out, _cleaned = cr._run_process_capture(
            cmd, timeout=timeout, env=env,
            cleanup_docker_exec=bool(container),
            stall_check=stall_check if attempts == 1 else None,
            interrupt_check=interrupt_check if attempts == 1 else None,
            stall_poll_s=stall_poll_s)
        interrupted = cr._INTERRUPT_MARKER in stderr
        if os.environ.get("AWM_OC_DEBUG"):
            print(f"[oc-debug] rc={rc} timed_out={timed_out} "
                  f"cmd={' '.join(cmd)}\n[oc-debug] STDOUT:\n"
                  f"{stdout}\n[oc-debug] STDERR:\n{stderr}",
                  flush=True)
        if (timed_out or interrupted) and container:
            _kill_opencode_in_container(container)
        rec = _parse(
            stdout, stderr, rc, timed_out, interrupted,
            output_reader=_tool_output_reader(
                container=container,
                container_scratch=container_scratch,
                host_scratch=host_scratch,
            ),
            require_response_event=bool(resume_session_id),
        )
        if (rec.extra.get("opencode_protocol_error")
                == "resume_no_response_event"
                and container and container_scratch and resume_session_id
                and db_replay_since_ms is not None):
            db_events = _opencode_db_events_since(
                container, container_scratch, resume_session_id,
                db_replay_since_ms)
            if db_events:
                replay_stdout = "\n".join(
                    json.dumps(ev, separators=(",", ":")) for ev in db_events)
                replay = _parse(
                    replay_stdout, stderr, rc, timed_out, interrupted,
                    output_reader=_tool_output_reader(
                        container=container,
                        container_scratch=container_scratch,
                        host_scratch=host_scratch,
                    ),
                    require_response_event=True,
                )
                if int(replay.extra.get("opencode_response_event_count") or 0) > 0:
                    # Resume produced DB parts despite empty stdout: still a usable turn.
                    if replay.status in ("empty", "parse_miss"):
                        replay.status = "ok"
                    replay.extra["opencode_db_replay_events"] = len(db_events)
                    replay.extra["opencode_db_replay_since_ms"] = db_replay_since_ms
                    replay.extra["opencode_db_replay_from_protocol_error"] = (
                        "resume_no_response_event")
                    rec = replay
        rec.role = role
        rec.attempts = attempts
        rec.seconds = time.time() - t0
        if rec.session_id and resume_session_id:
            rec.extra["resumed_from_session_id"] = resume_session_id
        if rec.status == "ok":
            return rec
        last = rec
        if rec.status in ("auth", "quota"):
            if abort_on_auth and attempts > 1:
                raise BatchAbortError(rec)
            time.sleep(3)
            continue
        if rec.status in ("rate_limit", "server") and attempts <= retries:
            time.sleep(cr._BACKOFF[min(attempts - 1, len(cr._BACKOFF) - 1)])
            continue
        if rec.status in ("timeout", "interrupted"):
            return rec
        if attempts <= retries:
            time.sleep(2)
    return last


def _tool_output_reader(*, container: str | None,
                        container_scratch: str | None,
                        host_scratch: str | None) -> Callable[[str], str | None] | None:
    if container:
        def _read_container(path: str) -> str | None:
            try:
                p = subprocess.run(
                    ["docker", "exec", container, "cat", path],
                    capture_output=True, text=True,
                    timeout=cfg.HTTP_SHORT_TIMEOUT_S,
                )
                if p.returncode == 0:
                    return p.stdout
            except Exception:  # noqa: BLE001
                return None
            return None
        return _read_container
    if container_scratch and host_scratch:
        croot = str(container_scratch).rstrip("/")
        hroot = Path(host_scratch)

        def _read_host(path: str) -> str | None:
            try:
                if not path.startswith(croot + "/"):
                    return None
                rel = path[len(croot) + 1:]
                target = (hroot / rel).resolve()
                if hroot.resolve() not in target.parents and target != hroot.resolve():
                    return None
                if target.exists() and target.is_file():
                    return target.read_text(errors="replace")
            except Exception:  # noqa: BLE001
                return None
            return None
        return _read_host
    return None


def _kill_opencode_in_container(container: str) -> None:
    """Best-effort kill of the in-container opencode after a host-side timeout/interrupt."""
    try:
        subprocess.run(["docker", "exec", container, "pkill", "-9", "-f",
                        "opencode run"], capture_output=True,
                       timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S)
    except Exception:
        pass
