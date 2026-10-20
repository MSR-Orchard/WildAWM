"""Host-side client for a world's worldctl port; hot-path RPCs are async, the rest run in threads."""

from urllib.parse import quote

import httpx

from wildawm.const_config import (
    DASHBOARD_AGENT_CHAT_TIMEOUT_S,
    HTTP_MEDIUM_TIMEOUT_S,
    HTTP_PROBE_TIMEOUT_S,
    HTTP_SHORT_TIMEOUT_S,
    agent_chat_timeout,
)


class WorldctlError(RuntimeError):
    """worldctl unreachable, returned an error, or returned bad JSON."""


CHAT_TIMEOUT_S = DASHBOARD_AGENT_CHAT_TIMEOUT_S


def _base(port: int) -> str:
    return f"http://localhost:{port}"


def _headers(token: str | None) -> dict:
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _wrap_error(verb: str, path: str, e: Exception) -> WorldctlError:
    if isinstance(e, httpx.HTTPStatusError):
        body = e.response.text[:400]
        return WorldctlError(f"{verb} {path} HTTP {e.response.status_code}: {body}")
    return WorldctlError(f"{verb} {path}: {e}")


# Sync transport: used only from worker threads.

def _get(
        port: int, token: str | None, path: str,
        timeout: float = HTTP_SHORT_TIMEOUT_S) -> dict:
    try:
        r = httpx.get(_base(port) + path, headers=_headers(token), timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        raise _wrap_error("GET", path, e)


# Async transport: used on the request event loop.

async def _aget(
        port: int, token: str | None, path: str,
        timeout: float = HTTP_SHORT_TIMEOUT_S) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(_base(port) + path, headers=_headers(token))
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001
        raise _wrap_error("GET", path, e)


async def _apost(port: int, token: str | None, path: str, body: dict,
                 timeout: float = CHAT_TIMEOUT_S) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(_base(port) + path, headers=_headers(token), json=body)
            r.raise_for_status()
            return r.json()
    except Exception as e:  # noqa: BLE001
        raise _wrap_error("POST", path, e)


def health(port: int) -> dict:
    return _get(port, None, "/health", timeout=HTTP_PROBE_TIMEOUT_S)


def agents(port: int, token: str | None) -> dict:
    return _get(port, token, "/agents", timeout=HTTP_SHORT_TIMEOUT_S)


def agent_ready(port: int, token: str | None, kind: str) -> bool:
    try:
        h = _get(
            port, token, f"/agents/{kind}/health",
            timeout=HTTP_PROBE_TIMEOUT_S)
        return bool(h.get("ready"))
    except WorldctlError:
        return False


def reply(port: int, token: str | None, kind: str, session_id: str) -> str:
    sid = f"?session_id={quote(session_id)}" if session_id else ""
    return _get(
        port, token, f"/agents/{kind}/reply{sid}",
        timeout=HTTP_SHORT_TIMEOUT_S).get("reply", "")


def raw(port: int, token: str | None, kind: str, session_id: str) -> dict:
    """Harness-native session log for archiving: {"raw": str, "ext": str}."""
    sid = f"?session_id={quote(session_id)}" if session_id else ""
    return _get(port, token, f"/agents/{kind}/raw{sid}",
                timeout=HTTP_MEDIUM_TIMEOUT_S)


def budget(port: int, token: str | None, kind: str, session_id: str) -> dict:
    """Read the adapter's current trajectory budget without mutating it."""
    sid = f"?session_id={quote(session_id)}" if session_id else ""
    return _get(port, token, f"/agents/{kind}/budget{sid}",
                timeout=HTTP_SHORT_TIMEOUT_S)


def turn_count(port: int, token: str | None, kind: str, session_id: str) -> dict:
    """Return the adapter-native turn-count envelope unchanged."""
    sid = f"?session_id={quote(session_id)}" if session_id else ""
    return _get(port, token, f"/agents/{kind}/turn_count{sid}",
                timeout=HTTP_SHORT_TIMEOUT_S)


def proxy_trajectory(
    port: int, token: str | None, kind: str, session_id: str,
) -> dict:
    """Return the policy proxy's authoritative token-in/token-out trajectory."""
    sid = f"?session_id={quote(session_id)}" if session_id else ""
    return _get(port, token, f"/agents/{kind}/proxy_trajectory{sid}",
                timeout=HTTP_MEDIUM_TIMEOUT_S)


async def chat(port: int, token: str | None, kind: str,
               session_id: str, message: str,
               timeout_s: float | None = None,
               max_turns: int | None = None,
               reasoning_effort: str | None = None) -> dict:
    body: dict = {"session_id": session_id, "message": message}
    if timeout_s is not None:
        body["timeout_s"] = timeout_s
    if max_turns is not None:
        body["max_turns"] = max_turns
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    # Wait beyond the in-world budget so the adapter's force-end surfaces first.
    wait = agent_chat_timeout(float(timeout_s), hops=3) if timeout_s else CHAT_TIMEOUT_S
    return await _apost(port, token, f"/agents/{kind}/chat", body, timeout=wait)


async def history(port: int, token: str | None, kind: str, session_id: str) -> list:
    sid = f"?session_id={quote(session_id)}" if session_id else ""
    data = await _aget(
        port, token, f"/agents/{kind}/history{sid}",
        timeout=HTTP_SHORT_TIMEOUT_S)
    return data.get("history", [])
