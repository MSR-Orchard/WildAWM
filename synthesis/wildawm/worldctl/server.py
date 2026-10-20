#!/usr/bin/env python3
"""WildAWM in-world control server: token-gated HTTP proxy to the agent adapters and WebHarbor."""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from wildawm.const_config import AGENT_ADAPTER_PORT as DEFAULT_AGENT_ADAPTER_PORT
from wildawm.const_config import HTTP_MEDIUM_TIMEOUT_S
from wildawm.const_config import HTTP_PROBE_TIMEOUT_S
from wildawm.const_config import HTTP_SHORT_TIMEOUT_S
from wildawm.const_config import LLM_PROXY_PORT as DEFAULT_LLM_PROXY_PORT
from wildawm.const_config import WEBHARBOR_CTRL_PORT as DEFAULT_WEBHARBOR_CTRL_PORT
from wildawm.const_config import WORLDCTL_CHAT_TIMEOUT_S
from wildawm.const_config import WORLDCTL_PORT as DEFAULT_WORLDCTL_PORT
from wildawm.const_config import agent_chat_timeout

WORLD_ID = os.environ.get("AWM_WORLD_ID", "default")
PORT = int(os.environ.get("AWM_WORLDCTL_PORT", str(DEFAULT_WORLDCTL_PORT)))
TOKEN = os.environ.get("AWM_WORLDCTL_TOKEN", "")
ADAPTER_PORT = int(os.environ.get("AWM_AGENT_ADAPTER_PORT", str(DEFAULT_AGENT_ADAPTER_PORT)))
LLM_PROXY_PORT = int(os.environ.get("AWM_LLM_PROXY_PORT", str(DEFAULT_LLM_PROXY_PORT)))
WEBHARBOR_HOST = os.environ.get("AWM_WEBHARBOR_HOST", "webharbor")
WEBHARBOR_PORT = int(os.environ.get("AWM_WEBHARBOR_CTRL_INTERNAL_PORT", str(DEFAULT_WEBHARBOR_CTRL_PORT)))
LOG_DIR = os.environ.get("AWM_WORLDCTL_LOG_DIR", "/var/log/worldctl")

CHAT_TIMEOUT_S = WORLDCTL_CHAT_TIMEOUT_S

KINDS = ("nanobot", "opencode", "claude-code", "codex", "hermes", "openclaw")


def _make_logger() -> logging.Logger:
    log = logging.getLogger("worldctl")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        f"%(asctime)s worldctl[{WORLD_ID}] %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(os.path.join(LOG_DIR, "wildawm.worldctl.log"))
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:  # pragma: no cover - best effort
        log.warning("could not open log file in %s: %s", LOG_DIR, e)
    return log


LOG = _make_logger()


def _adapter_base(kind: str) -> str:
    # Agents are compose services named `agent-<kind>`, resolved via service-name DNS.
    return f"http://agent-{kind}:{ADAPTER_PORT}"


def _proxy_base(kind: str) -> str:
    return f"http://agent-{kind}:{LLM_PROXY_PORT}"


def _adapter_get(kind: str, path: str, timeout: float = HTTP_SHORT_TIMEOUT_S):
    with urllib.request.urlopen(_adapter_base(kind) + path, timeout=timeout) as r:
        return json.loads(r.read())


def _adapter_post(kind: str, path: str, body: dict, timeout: float = CHAT_TIMEOUT_S):
    req = urllib.request.Request(
        _adapter_base(kind) + path, data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _proxy_get(kind: str, path: str, timeout: float = HTTP_SHORT_TIMEOUT_S):
    with urllib.request.urlopen(_proxy_base(kind) + path, timeout=timeout) as r:
        return json.loads(r.read())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        LOG.info("http %s", fmt % args)

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authed(self) -> bool:
        if not TOKEN:
            return True  # no token configured: open (dev only)
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Bearer ") and hdr[7:].strip() == TOKEN:
            return True
        if self.headers.get("X-Worldctl-Token", "").strip() == TOKEN:
            return True
        return False

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)

        if path == "/health":
            return self._send(200, {"ok": True, "world": WORLD_ID,
                                    "service": "worldctl"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})

        if path == "/agents":
            agents = {}
            for kind in KINDS:
                try:
                    h = _adapter_get(kind, "/health", timeout=HTTP_PROBE_TIMEOUT_S)
                    agents[kind] = {"reachable": True, "ready": bool(h.get("ready")),
                                    "detail": h.get("detail")}
                except Exception as e:  # noqa: BLE001
                    agents[kind] = {"reachable": False, "ready": False,
                                    "detail": str(e)}
            return self._send(200, {"world": WORLD_ID, "agents": agents})

        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "agents" and parts[2] in (
                "health", "budget", "turn_count", "history", "reply", "raw",
                "proxy_trajectory", "quiesce"):
            kind = parts[1]
            if kind not in KINDS:
                return self._send(404, {"error": f"unknown kind {kind!r}"})
            sub = parts[2]
            sid = (q.get("session_id") or [""])[0]
            try:
                if sub == "health":
                    return self._send(
                        200, _adapter_get(kind, "/health",
                                          timeout=HTTP_PROBE_TIMEOUT_S))
                if sub == "proxy_trajectory":
                    # The proxy's ``session`` selector is not the session_id; return everything.
                    return self._send(
                        200, _proxy_get(kind, "/trajectory",
                                        timeout=HTTP_MEDIUM_TIMEOUT_S))
                qs = f"?session_id={quote(sid)}" if sid else ""
                return self._send(
                    200, _adapter_get(kind, f"/{sub}{qs}",
                                      timeout=HTTP_SHORT_TIMEOUT_S))
            except urllib.error.HTTPError as e:
                return self._send(e.code, {"error": e.read().decode("utf-8", "ignore")[:500]})
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": f"adapter {kind}: {e}"})

        if path == "/webharbor/health":
            try:
                with urllib.request.urlopen(
                    f"http://{WEBHARBOR_HOST}:{WEBHARBOR_PORT}/health",
                    timeout=HTTP_PROBE_TIMEOUT_S) as r:
                    return self._send(200, json.loads(r.read()))
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": f"webharbor: {e}"})

        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        body = self._body()
        parts = path.strip("/").split("/")

        if len(parts) == 3 and parts[0] == "agents" and parts[2] == "quiesce":
            kind = parts[1]
            if kind not in KINDS:
                return self._send(404, {"error": f"unknown kind {kind!r}"})
            try:
                response = _adapter_post(
                    kind, "/quiesce", body,
                    timeout=HTTP_MEDIUM_TIMEOUT_S)
            except urllib.error.HTTPError as error:
                return self._send(
                    error.code,
                    {"error": error.read().decode("utf-8", "ignore")[:500]},
                )
            except Exception as error:  # noqa: BLE001
                return self._send(
                    502, {"error": f"adapter {kind}: {error}"})
            return self._send(200, response)

        if len(parts) == 3 and parts[0] == "agents" and parts[2] == "chat":
            kind = parts[1]
            if kind not in KINDS:
                return self._send(404, {"error": f"unknown kind {kind!r}"})
            sid = body.get("session_id") or "default"
            message = body.get("message", "")
            adapter_body = {"session_id": sid, "message": message}
            # The adapter enforces the turn budget; wait a bit longer so its force-end surfaces first.
            timeout_s = body.get("timeout_s")
            if timeout_s is not None:
                adapter_body["timeout_s"] = timeout_s
            if body.get("max_turns") is not None:
                adapter_body["max_turns"] = body.get("max_turns")
            if body.get("reasoning_effort"):
                adapter_body["reasoning_effort"] = body.get("reasoning_effort")
            wait = agent_chat_timeout(float(timeout_s), hops=2) if timeout_s else CHAT_TIMEOUT_S
            LOG.info("chat -> %s session=%s msg_len=%d timeout_s=%s max_turns=%s reasoning_effort=%s",
                     kind, sid, len(message), timeout_s, body.get("max_turns"),
                     body.get("reasoning_effort") or "")
            t0 = time.time()
            try:
                res = _adapter_post(kind, "/chat", adapter_body, timeout=wait)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:500]
                LOG.warning("chat %s HTTP %s: %s", kind, e.code, detail)
                return self._send(e.code, {"error": detail})
            except Exception as e:  # noqa: BLE001
                LOG.warning("chat %s failed: %s", kind, e)
                return self._send(502, {"error": f"adapter {kind}: {e}"})
            LOG.info("chat <- %s session=%s in %.1fs reply_len=%d",
                     kind, sid, time.time() - t0, len(res.get("content") or ""))
            return self._send(200, res)

        if path == "/webharbor/reset":
            site = body.get("site")
            url = (f"http://{WEBHARBOR_HOST}:{WEBHARBOR_PORT}/reset/{site}" if site
                   else f"http://{WEBHARBOR_HOST}:{WEBHARBOR_PORT}/reset-all")
            try:
                req = urllib.request.Request(url, method="POST")
                with urllib.request.urlopen(
                        req, timeout=HTTP_SHORT_TIMEOUT_S) as r:
                    return self._send(200, json.loads(r.read()))
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": f"webharbor reset: {e}"})

        return self._send(404, {"error": "not found"})


def main():
    LOG.info("starting worldctl on :%d (adapter_port=%d, token=%s)",
             PORT, ADAPTER_PORT, "set" if TOKEN else "MISSING(open)")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
