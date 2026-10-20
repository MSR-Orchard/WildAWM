#!/usr/bin/env python3
"""Instrumented runner for every LLM/subprocess call, with a status taxonomy and transient retry."""
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path

from wildawm.json_utils import loads_jsonc
from typing import Callable

from wildawm import const_config as cfg

# Process-global cap on concurrent host-side model calls across parallel slots.
_HOST_LLM_SEM: threading.Semaphore | None = None
_HOST_LLM_LOCK = threading.Lock()


@contextmanager
def host_llm_slot(enabled: bool = True):
    global _HOST_LLM_SEM
    if not enabled:
        yield
        return
    if _HOST_LLM_SEM is None:
        with _HOST_LLM_LOCK:
            if _HOST_LLM_SEM is None:
                _HOST_LLM_SEM = threading.Semaphore(cfg.HOST_LLM_MAX_CONCURRENCY)
    with _HOST_LLM_SEM:
        yield

_AUTH_PAT = re.compile(
    r"401|403|invalid.{0,8}(api.?key|credential)|unauthoriz|authentication"
    r"|credit balance|billing|expired.{0,12}token|oauth.{0,12}(error|expired)",
    re.I)
_QUOTA_PAT = re.compile(r"quota.{0,20}exhaust|out of quota|insufficient_quota", re.I)
_RATE_PAT = re.compile(r"429|rate.?limit|overloaded|resource_exhausted|too many requests", re.I)
_SERVER_PAT = re.compile(r"50[0234]|internal server|bad gateway|service unavailable"
                         r"|connection (reset|refused|aborted)|temporarily unavailable", re.I)
_INTERRUPT_MARKER = "WILDAWM_RUNNER_INTERRUPTED:"


class BatchAbortError(RuntimeError):
    """Unrecoverable infra failure (auth/quota); must propagate and stop the batch."""

    def __init__(self, record: "CallRecord"):
        super().__init__(f"batch abort: {record.status} in {record.role}: "
                         f"{record.stderr_tail}")
        self.record = record


@dataclass
class CallRecord:
    role: str
    status: str = "ok"  # ok|timeout|auth|quota|rate_limit|server|empty|parse_miss|...
    rc: int | None = None
    attempts: int = 1
    seconds: float = 0.0
    result_text: str = ""
    num_turns: int = 0
    cost_usd: float = 0.0
    session_id: str = ""
    stderr_tail: str = ""
    stdout_tail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return asdict(self)


def _classify_error_text(blob: str) -> str:
    """Map failure text (stderr / non-zero stdout) to a status; never run on successful output."""
    if _QUOTA_PAT.search(blob):
        return "quota"
    if _AUTH_PAT.search(blob):
        return "auth"
    if _RATE_PAT.search(blob):
        return "rate_limit"
    if _SERVER_PAT.search(blob):
        return "server"
    return "error" if blob.strip() else "empty"


def _is_max_turns_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("subtype") == "error_max_turns":
        return True
    if payload.get("terminal_reason") == "max_turns":
        return True
    errors = payload.get("errors") or []
    return any("maximum number of turns" in str(e).lower() for e in errors)


def _classify(rc: int | None, stdout: str, stderr: str, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if _INTERRUPT_MARKER in stderr:
        return "interrupted"
    s = stdout.strip()
    payload = None
    if s:
        try:
            payload = loads_jsonc(s)
        except (ValueError, TypeError):
            payload = None
    if _is_max_turns_payload(payload):
        return "max_turns"
    if rc in (0, None):
        # Success payloads hold cost numbers that match 50x/429 patterns; trust the envelope.
        if not s:
            return "empty"
        if payload is None:
            return "parse_miss"
        if isinstance(payload, dict) and (
                payload.get("is_error") or payload.get("api_error_status")):
            return _classify_error_text(json.dumps(
                                        payload.get("api_error_status") or payload)
                                        + "\n" + stderr)
        return "ok"
    return _classify_error_text(f"{stderr}\n{stdout}")


def run_cmd(cmd: list[str], *, role: str, timeout: int | None = None,
            check: bool = True, env: dict | None = None,
            cwd: str | None = None) -> CallRecord:
    """Checked subprocess for plumbing (docker cp / compose / tar); raises on failure."""
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env, cwd=cwd)
        rec = CallRecord(role=role, rc=r.returncode, seconds=time.time() - t0,
                         stdout_tail=r.stdout, stderr_tail=r.stderr,
                         status="ok" if r.returncode == 0 else "error")
        rec.result_text = r.stdout
    except subprocess.TimeoutExpired:
        rec = CallRecord(role=role, rc=124, seconds=time.time() - t0,
                         status="timeout")
    if check and not rec.ok:
        raise RuntimeError(f"[{role}] command failed ({rec.status}, rc={rec.rc}): "
                           f"{' '.join(cmd[:6])}... :: {rec.stderr_tail}")
    return rec


def _parse_jsonl(path: Path) -> list:
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def capture_claude_session_transcript(session_id: str,
                                      container: str | None = None) -> list:
    """Best-effort load of a Claude Code session JSONL transcript; safe after a timeout."""
    if not session_id:
        return []
    try:
        if container:
            find = subprocess.run(
                [
                    "docker", "exec", container, "sh", "-c",
                    f"find /home/agent/.claude/projects /root/.claude/projects "
                    f"/home/*/.claude/projects -name '{session_id}.jsonl' "
                    "2>/dev/null | head -1",
                ],
                capture_output=True, text=True,
                timeout=cfg.HTTP_SHORT_TIMEOUT_S)
            remote = (find.stdout or "").strip().splitlines()
            remote = remote[0].strip() if remote else ""
            if not remote:
                return []
            fd, dest = tempfile.mkstemp(suffix=".jsonl")
            os.close(fd)
            try:
                cp = subprocess.run(
                    ["docker", "cp", f"{container}:{remote}", dest],
                    capture_output=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
                if cp.returncode != 0:
                    return []
                return _parse_jsonl(Path(dest))
            finally:
                try:
                    os.unlink(dest)
                except OSError:
                    pass

        roots, seen = [], set()
        for base in (str(cfg.CLAUDE_CONFIG_DIR), str(Path.home() / ".claude")):
            p = Path(base) / "projects"
            if str(p) not in seen:
                seen.add(str(p))
                roots.append(p)
        for root in roots:
            hits = sorted(root.glob(f"*/{session_id}.jsonl"))
            if hits:
                return _parse_jsonl(hits[0])
    except Exception:
        return []
    return []


def is_claude_llm_assistant_event(event: object) -> bool:
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return False
    msg = event.get("message") or {}
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    if msg.get("model") == "<synthetic>":
        return False
    return bool(msg.get("content"))


def count_claude_llm_assistant_turns(events: list) -> int:
    return sum(1 for event in events if is_claude_llm_assistant_event(event))


_BACKOFF = [5, 15, 45]
def _claude_code_model_env(model: str, base_url: str | None = None) -> dict:
    spec = cfg.model_spec_for_api(base_url, model)
    context_window = spec.get("context_window") if spec else None
    return cfg.claude_code_model_env(model, context_window)


def _decode_output(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode(errors="replace")


def _drain_temp_output(f) -> str:
    try:
        f.seek(0)
        return _decode_output(f.read())
    except Exception:
        return ""
    finally:
        try:
            f.close()
        except Exception:
            pass


def _stop_process_group(proc: subprocess.Popen, *, grace_s: float = 5.0) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=cfg.HTTP_PROBE_TIMEOUT_S)
    except Exception:
        pass


def _docker_run_name(cmd: list[str]) -> str | None:
    """Return the explicit `docker run --name` value, if the command has one."""
    if len(cmd) < 2 or cmd[:2] != ["docker", "run"]:
        return None
    for i, token in enumerate(cmd):
        if token == "--name" and i + 1 < len(cmd):
            return cmd[i + 1]
        if token.startswith("--name="):
            return token.split("=", 1)[1]
    return None


def _cleanup_named_docker_run_container(cmd: list[str]) -> list[str]:
    """Force-remove a named `docker run` container left alive after a timeout."""
    name = _docker_run_name(cmd)
    if not name:
        return []
    try:
        r = subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True,
            timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    except Exception:
        return []
    if r.returncode == 0:
        return [name]
    detail = (r.stderr or r.stdout or "").lower()
    if "no such container" in detail:
        return []
    return []


def _signal_docker_exec_children(cmd: list[str], sig: signal.Signals) -> list[int]:
    target = _docker_exec_target(cmd)
    if not target:
        return []
    container, exec_argv = target
    marker = _claude_prompt_marker(exec_argv)
    if not marker:
        return []
    try:
        top = subprocess.run(
            ["docker", "top", container, "-eo", "pid,comm,args"],
            capture_output=True, text=True,
            timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S)
    except Exception:
        return []
    if top.returncode != 0:
        return []
    signaled: list[int] = []
    for line in top.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, comm, args = parts
        if comm != "claude":
            continue
        if marker not in _norm_marker(args):
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        try:
            os.kill(pid, sig)
            signaled.append(pid)
        except (ProcessLookupError, OSError):
            pass
    return signaled


def _interrupt_process_group(proc: subprocess.Popen, cmd: list[str], *,
                             cleanup_docker_exec: bool,
                             grace_s: float = 20.0) -> list:
    signaled: list = []
    if cleanup_docker_exec:
        signaled = _signal_docker_exec_children(cmd, signal.SIGINT)
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return signaled
    try:
        os.killpg(pgid, signal.SIGINT)
    except (ProcessLookupError, OSError):
        return signaled
    try:
        proc.wait(timeout=grace_s)
        return signaled
    except subprocess.TimeoutExpired:
        pass
    _stop_process_group(proc)
    if cleanup_docker_exec:
        signaled += _cleanup_docker_exec_children(cmd)
    signaled += _cleanup_named_docker_run_container(cmd)
    _stop_process_group(proc)
    return signaled


def _docker_exec_target(cmd: list[str]) -> tuple[str, list[str]] | None:
    if len(cmd) < 3 or cmd[:2] != ["docker", "exec"]:
        return None
    value_opts = {
        "-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir",
        "--detach-keys", "--index",
    }
    i = 2
    while i < len(cmd):
        token = cmd[i]
        if token == "--":
            i += 1
            break
        if not token.startswith("-"):
            return token, cmd[i + 1:]
        if token in value_opts:
            i += 2
        else:
            i += 1
    if i < len(cmd):
        return cmd[i], cmd[i + 1:]
    return None


def _norm_marker(text: str) -> str:
    return " ".join(text.split())


def _claude_prompt_marker(exec_argv: list[str]) -> str:
    try:
        claude_i = exec_argv.index("claude")
    except ValueError:
        return ""
    args = exec_argv[claude_i + 1:]
    try:
        prompt_i = args.index("-p")
        prompt = args[prompt_i + 1]
    except (ValueError, IndexError):
        return ""
    return _norm_marker(prompt)[:120]


def _cleanup_docker_exec_children(cmd: list[str]) -> list[int]:
    victims = _signal_docker_exec_children(cmd, signal.SIGTERM)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        remaining: list[int] = []
        for pid in victims:
            try:
                if sig != signal.SIGTERM:
                    os.kill(pid, sig)
                remaining.append(pid)
            except ProcessLookupError:
                pass
            except OSError:
                pass
        victims = remaining
        if not victims:
            break
        time.sleep(2 if sig == signal.SIGTERM else 0)
    return victims


def _run_process_capture(cmd: list[str], *, timeout: int | None = None,
                         env: dict | None = None, cwd: str | None = None,
                         cleanup_docker_exec: bool = False,
                         stall_check: Callable[[], tuple[bool, str]] | None = None,
                         interrupt_check: Callable[[], tuple[bool, str]] | None = None,
                         stall_poll_s: float = 10.0
                         ) -> tuple[int, str, str, bool, list]:
    out_f = tempfile.TemporaryFile()
    err_f = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        cmd, stdout=out_f, stderr=err_f, stdin=subprocess.DEVNULL,
        env=env, cwd=cwd, start_new_session=True)
    timed_out = False
    stall_reason = ""
    interrupt_reason = ""
    cleaned: list = []
    interrupted_pids: list[int] = []
    try:
        if stall_check is None and interrupt_check is None:
            rc = proc.wait(timeout=timeout)
        else:
            deadline = time.monotonic() + timeout if timeout else None
            while True:
                wait_s = stall_poll_s
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    wait_s = min(wait_s, remaining)
                try:
                    rc = proc.wait(timeout=wait_s)
                    break
                except subprocess.TimeoutExpired:
                    if interrupt_check is not None:
                        should_interrupt, reason = interrupt_check()
                        if should_interrupt:
                            interrupt_reason = (
                                reason or "runner interrupt check stopped the process")
                            interrupted_pids = _interrupt_process_group(
                                proc, cmd,
                                cleanup_docker_exec=cleanup_docker_exec)
                            rc = proc.returncode if proc.returncode is not None else 130
                            break
                    if stall_check is not None:
                        should_stop, reason = stall_check()
                        if should_stop:
                            timed_out = True
                            stall_reason = (
                                reason or "runner stall check stopped the process")
                            _stop_process_group(proc)
                            if cleanup_docker_exec:
                                cleaned = _cleanup_docker_exec_children(cmd)
                            cleaned += _cleanup_named_docker_run_container(cmd)
                            _stop_process_group(proc)
                            rc = 124
                            break
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process_group(proc)
        if cleanup_docker_exec:
            cleaned = _cleanup_docker_exec_children(cmd)
        cleaned += _cleanup_named_docker_run_container(cmd)
        _stop_process_group(proc)
        rc = 124
    stdout = _drain_temp_output(out_f)
    stderr = _drain_temp_output(err_f)
    if stall_reason:
        stderr = (stderr + "\n" + stall_reason).strip()
    if interrupt_reason:
        detail = f"{_INTERRUPT_MARKER} {interrupt_reason}"
        if interrupted_pids:
            detail += f" (pids={interrupted_pids})"
        stderr = (stderr + "\n" + detail).strip()
    return rc, stdout, stderr, timed_out, cleaned


def run_process_with_timeout(cmd: list[str], *, timeout: int | None = None,
                             env: dict | None = None, cwd: str | None = None,
                             cleanup_docker_exec: bool = False,
                             stall_check: Callable[[], tuple[bool, str]] | None = None,
                             interrupt_check: Callable[[], tuple[bool, str]] | None = None,
                             stall_poll_s: float = 10.0
                             ) -> tuple[int, bool, list]:
    """Run a command with process-group timeout cleanup."""
    rc, _stdout, _stderr, timed_out, cleaned = _run_process_capture(
        cmd, timeout=timeout, env=env, cwd=cwd,
        cleanup_docker_exec=cleanup_docker_exec, stall_check=stall_check,
        interrupt_check=interrupt_check,
        stall_poll_s=stall_poll_s)
    return rc, timed_out, cleaned


def run_claude(cmd: list[str], *, role: str, timeout: int,
               env: dict | None = None, cwd: str | None = None,
               retries: int = 2, abort_on_auth: bool = True,
               stall_check: Callable[[], tuple[bool, str]] | None = None,
               interrupt_check: Callable[[], tuple[bool, str]] | None = None,
               stall_poll_s: float = 10.0) -> CallRecord:
    """Run a `claude -p --output-format json` command with classification and transient retry."""
    attempts = 0
    last: CallRecord | None = None
    while attempts <= retries:
        attempts += 1
        t0 = time.time()
        rc, stdout, stderr, timed_out, cleaned = _run_process_capture(
            cmd, timeout=timeout, env=env, cwd=cwd, cleanup_docker_exec=True,
            stall_check=stall_check if attempts == 1 else None,
            interrupt_check=interrupt_check if attempts == 1 else None,
            stall_poll_s=stall_poll_s)
        status = _classify(rc, stdout, stderr, timed_out)
        rec = CallRecord(role=role, status=status, rc=rc, attempts=attempts,
                         seconds=time.time() - t0,
                         stdout_tail=stdout, stderr_tail=stderr)
        if cleaned:
            rec.extra["docker_exec_cleaned_pids"] = cleaned
        if rec.status == "interrupted":
            rec.extra["terminal_reason"] = "llm_turn_budget_interrupt"
            marker_i = stderr.find(_INTERRUPT_MARKER)
            if marker_i >= 0:
                rec.extra["interrupt_reason"] = stderr[marker_i + len(_INTERRUPT_MARKER):].strip()
        payload = None
        if stdout.strip():
            try:
                payload = loads_jsonc(stdout)
            except (ValueError, TypeError):
                payload = None
        if isinstance(payload, dict):
            rec.result_text = payload.get("result") or payload.get("content") or ""
            rec.num_turns = payload.get("num_turns") or 0
            rec.cost_usd = payload.get("total_cost_usd") or 0.0
            rec.session_id = payload.get("session_id") or ""
            for key in ("subtype", "terminal_reason", "errors"):
                if key in payload:
                    rec.extra[key] = payload[key]
            if status == "ok" and not rec.result_text.strip():
                rec.status = "empty"
        elif status == "ok":
            rec.status = "parse_miss"
            rec.result_text = stdout
        if rec.status == "ok":
            return rec
        last = rec
        if rec.status in ("auth", "quota"):
            if abort_on_auth and attempts > 1:  # confirmed twice
                raise BatchAbortError(rec)
            time.sleep(3)
            continue
        if rec.status in ("rate_limit", "server") and attempts <= retries:
            time.sleep(_BACKOFF[min(attempts - 1, len(_BACKOFF) - 1)])
            continue
        if rec.status == "timeout":
            return rec  # timeouts are not retried
        if rec.status == "interrupted":
            return rec
        if attempts <= retries:
            time.sleep(2)
    return last


def http_chat(spec: dict, messages: list[dict], *, role: str,
              max_tokens: int = 4000, timeout: int = cfg.LLM_MEDIUM_TIMEOUT_S,
              retries: int = 3,
              reasoning_effort: str | None = None) -> CallRecord:
    """OpenAI-compatible chat call with the same taxonomy and retry policy as run_claude."""
    with host_llm_slot():
        return _http_chat(spec, messages, role=role, max_tokens=max_tokens,
                          timeout=timeout, retries=retries,
                          reasoning_effort=reasoning_effort)


def _http_chat(spec: dict, messages: list[dict], *, role: str,
               max_tokens: int, timeout: int, retries: int,
               reasoning_effort: str | None) -> CallRecord:
    base = spec["base_url"].rstrip("/")
    if base.endswith("/chat/completions"):
        url = base
    elif spec.get("provider_type") == "azure_openai" and not (
            base.endswith("/openai/v1") or base.endswith("/v1") or base.endswith("/openai")):
        url = base + "/openai/v1/chat/completions"
    else:
        url = base + "/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {spec['api_key']}"}
    if spec.get("provider_type") == "azure_openai":
        headers["api-key"] = spec["api_key"]
    body_obj = {"model": spec["api_model"], "messages": messages,
                cfg.chat_token_limit_param(spec): max_tokens}
    # Explicit "none" omits the parameter; blank/None uses the model default.
    if cfg.normalize_reasoning_effort(reasoning_effort) == "none":
        reasoning_policy = None
    else:
        reasoning_policy = cfg.reasoning_effort_policy(spec, reasoning_effort)
    cfg.apply_reasoning_to_body(body_obj, reasoning_policy, api="chat")
    body = json.dumps(body_obj).encode()

    attempts, last = 0, None
    while attempts <= retries:
        attempts += 1
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            msg = (data.get("choices") or [{}])[0].get("message", {})
            # Thinking models may leave content null and put the answer in reasoning fields.
            text = (msg.get("content") or msg.get("reasoning")
                    or msg.get("reasoning_content") or "")
            usage = data.get("usage") or {}
            rec = CallRecord(role=role, status="ok" if text.strip() else "empty",
                             rc=0, attempts=attempts, seconds=time.time() - t0,
                             result_text=text,
                             extra={"input_tokens": usage.get("prompt_tokens"),
                                    "output_tokens": usage.get("completion_tokens"),
                                    "model": spec["ref"],
                                    "reasoning_effort": (
                                        reasoning_policy or {}).get("effort"),
                                    "reasoning_policy": reasoning_policy})
            if rec.ok:
                return rec
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode(errors="replace")
            except Exception:
                pass
            status = ("auth" if e.code in (401, 403)
                      else "quota" if "insufficient_quota" in detail
                      else "rate_limit" if e.code == 429
                      else "server" if e.code >= 500 else "error")
            rec = CallRecord(role=role, status=status, rc=e.code, attempts=attempts,
                             seconds=time.time() - t0, stderr_tail=detail)
        except urllib.error.URLError as e:
            rec = CallRecord(role=role, status="server", attempts=attempts,
                             seconds=time.time() - t0, stderr_tail=str(e))
        except (TimeoutError, OSError) as e:
            rec = CallRecord(role=role, status="timeout", attempts=attempts,
                             seconds=time.time() - t0, stderr_tail=str(e))
        last = rec
        if rec.status in ("auth", "quota"):
            if attempts > 1:
                raise BatchAbortError(rec)
            time.sleep(3)
        elif rec.status in ("rate_limit", "server", "timeout", "empty") and attempts <= retries:
            time.sleep(_BACKOFF[min(attempts - 1, len(_BACKOFF) - 1)])
        else:
            break
    return last


def claude_host_env(extra: dict | None = None) -> dict:
    """Env for host-side `claude` CLI runs; strips Anthropic overrides to use the subscription."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_BASE_URL", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    if extra:
        env.update(extra)
    env.update(_claude_code_model_env(extra.get("ANTHROPIC_MODEL", "") if extra else ""))
    return env


def claude_proxy_env(proxy_url: str, extra: dict | None = None) -> dict:
    """Env for `claude` CLI runs routed through the Anthropic-to-OpenAI proxy at ``proxy_url``."""
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = proxy_url
    env["ANTHROPIC_API_KEY"] = "not-used"
    if extra:
        env.update(extra)
    env.update(_claude_code_model_env(extra.get("ANTHROPIC_MODEL", "") if extra else ""))
    return env


@dataclass
class LLM:
    """Reusable claude-code invocation spec."""
    model: str = ""  # blank: the subscription's default model
    effort: str = ""
    route: str = "subscription"  # subscription | proxy | custom
    base_url: str = ""  # ANTHROPIC_BASE_URL when route=custom
    api_key: str = ""  # ANTHROPIC_API_KEY for route=proxy/custom
    model_ref: str = ""


def _llm_registry_spec(llm: "LLM", extra: dict | None = None) -> dict | None:
    if llm.model_ref:
        try:
            return cfg.model_spec(llm.model_ref)
        except Exception:
            return None
    extra = extra or {}
    base_url = (extra.get("AWM_OPENAI_BASE_URL") or extra.get("OPENAI_BASE_URL")
                or llm.base_url or "")
    model = (extra.get("AWM_OPENAI_MODEL") or extra.get("OPENAI_MODEL")
             or llm.model or "")
    try:
        return cfg.model_spec_for_api(base_url, model)
    except Exception:
        return None


def _llm_reasoning_policy(llm: "LLM", extra: dict | None = None) -> dict | None:
    spec = _llm_registry_spec(llm, extra)
    if not spec:
        return None
    return cfg.reasoning_effort_policy(spec, llm.effort or None,
                                       harness="claude-code")


def _llm_cli_effort(llm: "LLM", policy: dict | None) -> str:
    effort = (policy or {}).get("effort") or cfg.normalize_reasoning_effort(
        llm.effort or "")
    if effort:
        return effort
    # Subscription aliases are not in models.json but still accept --effort.
    return "high" if llm.route == "subscription" else ""


def _route_env(llm: "LLM", proxy_url: str | None, extra: dict | None) -> dict:
    """Resolve the env for a route: subscription, proxy, or custom endpoint."""
    if llm.route == "subscription":
        env = claude_host_env(extra)
        env.update(_claude_code_model_env(llm.model))
        return env
    if llm.route == "proxy":
        env = claude_proxy_env(proxy_url or cfg.HEAVY_JUDGE_ANTHROPIC_PROXY, extra)
        env["ANTHROPIC_AUTH_TOKEN"] = env.get("ANTHROPIC_API_KEY", "not-used")
        env.update(_claude_code_model_env(llm.model))
        return env
    if llm.route == "custom":
        env = dict(os.environ)
        env["ANTHROPIC_BASE_URL"] = llm.base_url
        env["ANTHROPIC_API_KEY"] = llm.api_key or "not-used"
        env["ANTHROPIC_AUTH_TOKEN"] = env["ANTHROPIC_API_KEY"]
        if extra:
            env.update(extra)
        env.update(_claude_code_model_env(llm.model))
        return env
    raise ValueError(f"unknown route {llm.route!r}")


def _container_url(url: str) -> str:
    return cfg.container_host_url(url)


def run_claude_code(prompt: str, llm: "LLM" = None, *, role: str, timeout: int,
                    container: str | None = None, cwd: str | None = None,
                    workdir: str = "/workspace", as_user: bool = True,
                    system_prompt_file: str | None = None,
                    allowed_tools: str | None = None, output_format: str = "json",
                    proxy_url: str | None = None, env_extra: dict | None = None,
                    mcp_config: str | None = None,
                    mcp_strict: bool = True,
                    disallowed_tools: str | None = None,
                    session_id: str | None = None,
                    resume_session_id: str | None = None,
                    max_turns: int | None = None,
                    retries: int = 1, abort_on_auth: bool = True,
                    stall_check: Callable[[], tuple[bool, str]] | None = None,
                    interrupt_check: Callable[[], tuple[bool, str]] | None = None,
                    stall_poll_s: float = 10.0) -> CallRecord:
    """Run a claude-code turn on the host or in ``container``; ``allowed_tools=""`` means no tools."""
    llm = llm or LLM()
    reasoning_policy = _llm_reasoning_policy(llm, env_extra)
    effective_effort = _llm_cli_effort(llm, reasoning_policy)
    effort_env = {**cfg.reasoning_effort_env(reasoning_policy),
                  **_claude_code_model_env(llm.model)}
    if effective_effort:
        effort_env["CLAUDE_CODE_EFFORT_LEVEL"] = effective_effort
    claude = ["claude", "-p", prompt, "--model", llm.model,
              "--output-format", output_format,
              "--dangerously-skip-permissions"]
    if effective_effort and effective_effort != "none":
        claude += ["--effort", effective_effort]
    if resume_session_id:
        claude += ["--resume", resume_session_id]
    elif session_id:
        claude += ["--session-id", session_id]
    if system_prompt_file:
        claude += ["--append-system-prompt-file", system_prompt_file]
    if allowed_tools is not None:
        claude += ["--allowedTools", allowed_tools]
    if disallowed_tools:
        claude += ["--disallowedTools", disallowed_tools]
    if mcp_config:
        claude += ["--mcp-config", mcp_config]
        if mcp_strict:
            claude += ["--strict-mcp-config"]
    max_turns = cfg.TASK_SYNTHESIS_MAX_LLM_TURNS if max_turns is None else max_turns
    if max_turns:
        claude += ["--max-turns", str(max_turns)]

    if container:
        # Container already holds credentials; only route deltas and effort go via -e.
        base_env = _route_env(llm, proxy_url, None)
        deltas = {k: base_env[k] for k in (
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
        ) if k in base_env}
        if "ANTHROPIC_BASE_URL" in deltas:
            deltas["ANTHROPIC_BASE_URL"] = _container_url(deltas["ANTHROPIC_BASE_URL"])
        deltas.update(env_extra or {})
        deltas.update(effort_env)
        deltas.setdefault("HOME", "/home/agent")
        ex = ["docker", "exec"]
        if as_user:
            ex += ["--user", f"{os.getuid()}:{os.getgid()}"]
        for k, v in deltas.items():
            ex += ["-e", f"{k}={v}"]
        ex += ["-w", workdir, container]
        rec = run_claude(ex + claude, role=role, timeout=timeout, retries=retries,
                         abort_on_auth=abort_on_auth, stall_check=stall_check,
                         interrupt_check=interrupt_check,
                         stall_poll_s=stall_poll_s)
        if not rec.session_id and (resume_session_id or session_id):
            rec.session_id = resume_session_id or session_id or ""
        if resume_session_id:
            rec.extra["resumed_from_session_id"] = resume_session_id
        return rec

    env = _route_env(llm, proxy_url, {**effort_env, **(env_extra or {})})
    with host_llm_slot(llm.route == "proxy"):
        rec = run_claude(claude, role=role, timeout=timeout, env=env, cwd=cwd,
                         retries=retries, abort_on_auth=abort_on_auth,
                         stall_check=stall_check, interrupt_check=interrupt_check,
                         stall_poll_s=stall_poll_s)
        if not rec.session_id and (resume_session_id or session_id):
            rec.session_id = resume_session_id or session_id or ""
        if resume_session_id:
            rec.extra["resumed_from_session_id"] = resume_session_id
        return rec
