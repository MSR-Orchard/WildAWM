#!/usr/bin/env python3
"""Run one OpenCode episode: MCP readiness, turn cap, deadline, completion nudges, sampling proxy."""
from __future__ import annotations

import array
import fcntl
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SAMPLING = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty", "max_tokens", "max_completion_tokens")


class PolicyProxy:
    """Forward chat completions upstream with the configured sampling and reasoning effort."""

    def __init__(self, upstream: str, sampling: dict, reasoning_effort: str | None, max_calls: int, log_path: Path):
        self.upstream = upstream.rstrip("/")
        self.sampling = {key: value for key, value in sampling.items() if value is not None}
        self.reasoning_effort = None if reasoning_effort in (None, "", "none") else reasoning_effort
        self.max_calls = max_calls
        self.calls = 0
        self.lock = threading.Lock()
        self.log_path = log_path
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                return

            def do_GET(self):
                proxy.forward(self, None)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                proxy.forward(self, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def prepare(self, body: dict) -> dict:
        record = {"client_sampling": {key: body[key] for key in SAMPLING if key in body}}
        for key in (*SAMPLING, "reasoning", "reasoningEffort", "reasoning_effort"):
            body.pop(key, None)
        body.update(self.sampling)
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        record.update(model=body.get("model"), sampling={key: body[key] for key in SAMPLING if key in body}, reasoning_effort=body.get("reasoning_effort"))
        return record

    def forward(self, handler: BaseHTTPRequestHandler, body: dict | None) -> None:
        try:
            data = None
            if body is not None:
                record = self.prepare(body)
                with self.lock:
                    if self.calls >= self.max_calls:
                        return self.reply(handler, 429, {"error": {"message": f"maximum of {self.max_calls} model calls reached", "type": "max_model_calls"}})
                    self.calls += 1
                    record.update(at=time.time(), call=self.calls, path=handler.path)
                    with self.log_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, sort_keys=True) + "\n")
                data = json.dumps(body).encode()
            headers = {"Content-Type": "application/json"}
            for name in ("Authorization", "x-api-key", "anthropic-version"):
                if handler.headers.get(name):
                    headers[name] = handler.headers[name]
            path = handler.path[3:] if handler.path.startswith("/v1/") and self.upstream.endswith("/v1") else handler.path
            request = urllib.request.Request(self.upstream + path, data=data, headers=headers, method="POST" if data is not None else "GET")
            try:
                response = urllib.request.urlopen(request, timeout=3600)
            except urllib.error.HTTPError as error:
                handler.send_response(error.code)
                handler.send_header("Content-Type", error.headers.get("Content-Type", "application/json"))
                handler.end_headers()
                handler.wfile.write(error.read())
                return
            with response:
                handler.send_response(response.status)
                handler.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                handler.end_headers()
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
        except Exception as error:
            self.reply(handler, 502, {"error": {"message": f"policy proxy error: {type(error).__name__}: {error}"}})

    @staticmethod
    def reply(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def close(self) -> None:
        self.server.shutdown()


def part(event: dict) -> dict:
    value = event.get("part")
    return value if isinstance(value, dict) else {}


def is_step(event: dict) -> bool:
    return part(event).get("type") == "step-finish" or event.get("type") == "step_finish"


def as_int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def finish_reason(events: list[dict]) -> str | None:
    """Finish reason of the last model step (stop, tool-calls, length, ...)."""
    for event in reversed(events):
        if is_step(event):
            return part(event).get("reason")
    return None


def summarize(events: list[dict]) -> dict:
    session, final, turns, tokens_in, tokens_out, current = None, "", 0, 0, 0, []
    for event in events:
        session = session or event.get("sessionID") or part(event).get("sessionID")
        if part(event).get("type") == "text" and isinstance(part(event).get("text"), str):
            current.append(part(event)["text"])
        if is_step(event):
            turns += 1
            final = "".join(current).strip() or final
            current = []
            tokens = part(event).get("tokens") if isinstance(part(event).get("tokens"), dict) else {}
            tokens_in += as_int(tokens.get("input"))
            tokens_out += as_int(tokens.get("output")) + as_int(tokens.get("reasoning"))
    final = "".join(current).strip() or final
    return {"session_id": session, "final_text": final, "turns": turns, "input_tokens": tokens_in, "output_tokens": tokens_out}


def terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def tree_cpu_ticks(root: int) -> int | None:
    """CPU clock ticks used by a process and its live descendants (Linux /proc)."""
    rows = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = stat.read_text()
            fields = text[text.rfind(")") + 2:].split()
            rows[int(text.split(" ", 1)[0])] = (int(fields[1]), int(fields[11]) + int(fields[12]))
        except (OSError, ValueError, IndexError):
            continue
    if root not in rows:
        return None
    tree, grew = {root}, True
    while grew:
        grew = False
        for pid, (parent, _) in rows.items():
            if pid not in tree and parent in tree:
                tree.add(pid)
                grew = True
    return sum(rows[pid][1] for pid in tree)


class StallWatch:
    """Flags a run with no OpenCode output for `limit` seconds while its process tree is idle."""

    def __init__(self, limit: float, pid: int):
        self.limit, self.pid = limit, pid
        self.probe = min(5.0, max(0.2, limit / 4))
        self.reset()

    def reset(self) -> None:
        self.last = self.checked = time.monotonic()
        self.ticks, self.busy_until = tree_cpu_ticks(self.pid), 0.0

    def stalled(self, now: float) -> bool:
        if now - self.checked >= self.probe:
            ticks = tree_cpu_ticks(self.pid)
            if self.ticks is not None and ticks is not None and (ticks - self.ticks) / os.sysconf("SC_CLK_TCK") / (now - self.checked) >= 0.2:
                self.busy_until = now + max(1.0, self.probe * 3)
            self.ticks, self.checked = ticks, now
        return now - self.last >= self.limit and now > self.busy_until


def stream_interrupted(events: list[dict]) -> bool:
    """True when the provider stream broke mid-response (transport fault, not model behavior)."""
    for event in reversed(events):
        error = event.get("error") if event.get("type") == "error" and isinstance(event.get("error"), dict) else None
        message = (error or {}).get("data", {}).get("message") if isinstance((error or {}).get("data"), dict) else None
        if isinstance(message, str) and ("502 Bad Gateway" in message or "Provider stream ended before emitting a message_stop event" in message):
            return True
    return False


class Runner:
    def __init__(self, spec: dict):
        self.spec = spec
        self.out = Path(spec["out"])
        self.out.mkdir(parents=True, exist_ok=True)
        state = Path(spec.get("state") or self.out / "state")
        self.env = {
            "HOME": str(state / "home"),
            "XDG_DATA_HOME": str(state / "data"),
            "XDG_CACHE_HOME": str(state / "cache"),
            "XDG_CONFIG_HOME": spec.get("config_home") or str(state / "config"),
            "OPENCODE_CONFIG": spec["config"],
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "true",
            "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "true",
            **{key: str(value) for key, value in (spec.get("env") or {}).items()},
        }
        for name in ("home", "data", "cache", "config"):
            (state / name).mkdir(parents=True, exist_ok=True)
        if spec.get("user"):
            subprocess.run(["chown", "-R", spec["user"], str(state), str(self.out)], check=False)

    def command(self, *args: str) -> tuple[list[str], dict]:
        if self.spec.get("user"):
            return ["/usr/sbin/runuser", "-u", self.spec["user"], "--", "env", *[f"{k}={v}" for k, v in self.env.items()], self.spec["opencode"], *args], dict(os.environ)
        return [self.spec["opencode"], *args], {**os.environ, **self.env}

    def wait_mcp(self) -> None:
        """Start `opencode serve` until every configured MCP server reports connected."""
        config = json.loads(Path(self.spec["config"]).read_text(encoding="utf-8"))
        expected = sorted(name for name, value in (config.get("mcp") or {}).items() if isinstance(value, dict) and value.get("enabled") is not False)
        if not expected:
            return
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        argv, env = self.command("serve", "--hostname", "127.0.0.1", "--port", str(port))
        timeout = float(self.spec.get("mcp_ready_timeout", 120))
        with (self.out / "mcp-readiness.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(argv, cwd=self.spec["workdir"], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline, last = time.monotonic() + timeout, "not ready"
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"opencode serve exited {process.returncode}")
                    try:
                        statuses = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=30).read())
                        failed = {name: statuses.get(name) for name in expected if (statuses.get(name) or {}).get("status") != "connected"}
                        if not failed:
                            return
                        last = json.dumps(failed)
                    except (OSError, ValueError) as error:
                        last = f"{type(error).__name__}: {error}"
                    time.sleep(0.5)
                raise RuntimeError(f"MCP servers not connected within {timeout:.0f}s: {last}")
            finally:
                terminate(process)

    def run_once(self, args: list[str], deadline: float, turns_left: int, raw, stderr, on_event) -> tuple[int, list[dict], str | None]:
        argv, env = self.command(*args)
        process = subprocess.Popen(argv, cwd=self.spec["workdir"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr, bufsize=0, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        events, stop, turns, pending, eof, drain = [], None, 0, b"", False, None
        watch = StallWatch(float(self.spec["stall_timeout"]), process.pid) if self.spec.get("stall_timeout") else None
        try:
            while True:
                now = time.monotonic()
                if stop is None and drain is None and now >= deadline:
                    stop = "timeout"
                    terminate(process)
                if stop is None and drain is None and watch and watch.stalled(now):
                    stop = "stall_timeout"
                    terminate(process)
                if process.poll() is not None and drain is None:
                    queued = array.array("i", [0])
                    fcntl.ioctl(process.stdout.fileno(), termios.FIONREAD, queued, True)
                    drain = queued[0]
                if drain is None:
                    if not selector.select(timeout=min(0.2, max(0.0, deadline - now))):
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                else:
                    chunk = os.read(process.stdout.fileno(), min(65536, drain)) if drain else b""
                    drain -= len(chunk)
                finished = drain == 0
                if not chunk and not eof:
                    eof = True
                    selector.unregister(process.stdout)
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                if (finished or eof) and pending:
                    lines.append(pending)
                    pending = b""
                if lines and watch:
                    watch.reset()
                for line in lines:
                    text = line.decode("utf-8", errors="replace")
                    raw.write(text + "\n")
                    raw.flush()
                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    events.append(event)
                    if on_event:
                        on_event(event)
                    turns += is_step(event)
                if stop is None and turns_left > 0 and turns >= turns_left and process.poll() is None:
                    stop = "max_turns"
                    terminate(process)
                if finished:
                    break
            return process.wait(), events, stop
        finally:
            selector.close()
            process.stdout.close()

    def run(self, on_event=None, followup=None, max_followups: int = 0) -> dict:
        spec = self.spec
        started = time.monotonic()
        deadline = started + float(spec["timeout"])
        marker = Path(spec["marker"])
        marker.unlink(missing_ok=True)
        proxy = None
        if spec.get("proxy"):
            settings = spec["proxy"]
            proxy = PolicyProxy(settings["upstream"], settings.get("sampling") or {}, settings.get("reasoning_effort"), int(settings.get("max_calls") or spec["max_turns"]), self.out / "proxy.jsonl")
            config = json.loads(Path(spec["config"]).read_text(encoding="utf-8"))
            for provider in config["provider"].values():
                provider["options"]["baseURL"] = f"http://127.0.0.1:{proxy.port}/v1"
            Path(spec["config"]).write_text(json.dumps(config, indent=2), encoding="utf-8")
        result = {"stop_reason": None, "returncode": 0, "nudges": 0, "followups": 0, "stream_retries": 0, "error": None}
        events: list[dict] = []
        try:
            self.wait_mcp()
            base = ["run", "--format", "json", "--model", spec["model"], "--agent", spec.get("agent") or "wildawm", "--variant", spec.get("variant") or "none", "--dir", spec["workdir"]]
            args, session = [*base, spec["prompt"]], None
            with (self.out / "events.jsonl").open("w", encoding="utf-8") as raw, (self.out / "stderr.log").open("w", encoding="utf-8") as stderr:
                while True:
                    turns_left = int(spec["max_turns"]) - summarize(events)["turns"]
                    if turns_left <= 0:
                        result["stop_reason"] = "max_turns"
                        break
                    code, new, stop = self.run_once(args, deadline, turns_left, raw, stderr, on_event)
                    events.extend(new)
                    result["returncode"] = code
                    session = session or summarize(new)["session_id"]
                    errored = any(event.get("type") == "error" for event in new)
                    if stop:
                        result["stop_reason"] = stop
                        break
                    if code != 0 or errored:
                        if session and stream_interrupted(new) and result["stream_retries"] < int(spec.get("stream_retries", 2)) and time.monotonic() < deadline:
                            result["stream_retries"] += 1
                            args = [*base, "--session", session, spec["nudge"]]
                            continue
                        result["stop_reason"] = "harness_error"
                        break
                    if session is None or time.monotonic() >= deadline:
                        break
                    reply = followup(events) if followup and result["followups"] < max_followups else None
                    if reply:
                        result["followups"] += 1
                        marker.unlink(missing_ok=True)
                        events.append({"type": "user_message", "sessionID": session, "part": {"type": "user", "text": reply}})
                        args = [*base, "--session", session, reply]
                        continue
                    if marker.is_file() or result["nudges"] >= int(spec.get("completion_nudges", 0)):
                        break
                    if finish_reason(new) in (spec.get("stop_on_finish") or ()):
                        result["stop_reason"] = finish_reason(new)
                        break
                    result["nudges"] += 1
                    events.append({"type": "user_message", "sessionID": session, "part": {"type": "user", "text": spec["nudge"]}})
                    args = [*base, "--session", session, spec["nudge"]]
        except Exception as error:
            result.update(stop_reason="harness_error", error=f"{type(error).__name__}: {error}")
        finally:
            if proxy:
                proxy.close()
        result.update(summarize(events))
        result.update(completed=marker.is_file(), completion_summary=marker.read_text(errors="replace").strip() if marker.is_file() else None,
                      wall_time_s=round(time.monotonic() - started, 2), model_calls=proxy.calls if proxy else None)
        (self.out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["events"] = events
        return result


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = Runner(spec).run()
    print(json.dumps({key: value for key, value in result.items() if key != "events"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
