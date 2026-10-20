#!/usr/bin/env python3
"""Self-contained stdio MCP server exposing `describe_image`, backed by an external vision model."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import mimetypes
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:
    # MCP SDK 2.x names the class MCPServer (same decorator/run interface).
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ModuleNotFoundError:
        FastMCP = None  # type: ignore[assignment]  # raw transport available

TRANSPORT = os.environ.get("AWM_VISION_MCP_TRANSPORT", "").strip().lower()
if TRANSPORT not in ("sdk", "raw"):
    TRANSPORT = "sdk" if FastMCP is not None else "raw"


# No baked endpoint/model: callers inject WILDAWM_VISION_* (synthesis) or AWM_VISION_* (evaluation); WILDAWM_VISION_* wins.
def _vision_env(name: str, fallback: str) -> str:
    return os.environ.get(name, os.environ.get(fallback, "")).strip()


def _env_int(primary: str, fallback: str, default: int) -> int:
    for name in (primary, fallback):
        raw = os.environ.get(name, "").strip()
        if raw:
            return int(raw)
    return int(default)


BASE_URL = _vision_env("WILDAWM_VISION_BASE_URL", "AWM_VISION_BASE_URL").rstrip("/")
API_KEY = _vision_env("WILDAWM_VISION_API_KEY", "AWM_VISION_API_KEY")
MODEL = _vision_env("WILDAWM_VISION_MODEL", "AWM_VISION_MODEL")
FALLBACK_BASE_URL = _vision_env(
    "WILDAWM_VISION_FALLBACK_BASE_URL", "AWM_VISION_FALLBACK_BASE_URL").rstrip("/")
FALLBACK_API_KEY = _vision_env(
    "WILDAWM_VISION_FALLBACK_API_KEY", "AWM_VISION_FALLBACK_API_KEY")
FALLBACK_MODEL = _vision_env(
    "WILDAWM_VISION_FALLBACK_MODEL", "AWM_VISION_FALLBACK_MODEL")


def _missing_vision_config() -> list[str]:
    """Names of the required vision env variables that are unset."""
    return [name for name, value in (
        ("WILDAWM_VISION_BASE_URL", BASE_URL),
        ("WILDAWM_VISION_MODEL", MODEL)) if not value]


if not BASE_URL or not MODEL:
    # Stay importable without config; every sdk use path fails loud instead.
    _VISION_CONFIG_ERROR = (
        "describe_image requires a vision LLM endpoint: set "
        + " and ".join(_missing_vision_config())
        + " (or AWM_VISION_BASE_URL / AWM_VISION_MODEL) to an "
          "OpenAI-compatible multimodal chat-completions endpoint")
else:
    _VISION_CONFIG_ERROR = ""
# AWM_VISION_* wins over AWM_DESCRIBE_IMAGE_* when both are set.
TIMEOUT = _env_int("AWM_VISION_TIMEOUT", "AWM_DESCRIBE_IMAGE_TIMEOUT", 60)
RETRIES = max(1, _env_int("AWM_VISION_RETRIES", "AWM_DESCRIBE_IMAGE_RETRIES", 3))
BACKEND_COUNT = 1 + int(bool(
    FALLBACK_BASE_URL
    and FALLBACK_MODEL
    and (FALLBACK_BASE_URL, FALLBACK_MODEL) != (BASE_URL, MODEL)
))
HARD_TIMEOUT = max(1, int(os.environ.get(
    "AWM_DESCRIBE_IMAGE_HARD_TIMEOUT",
    str(
        TIMEOUT * RETRIES * BACKEND_COUNT
        + sum(min(2 * attempt, 8) for attempt in range(1, RETRIES))
        + 15
    ),
)))
LOCK_TIMEOUT = max(1, int(os.environ.get(
    "AWM_DESCRIBE_IMAGE_LOCK_TIMEOUT", str(HARD_TIMEOUT))))
MAX_TOKENS = _env_int("AWM_VISION_MAX_TOKENS", "AWM_DESCRIBE_IMAGE_MAX_TOKENS", 400)
MAX_BYTES = 20 * 1024 * 1024
CACHE_DIR = Path(os.environ.get(
    "AWM_DESCRIBE_IMAGE_CACHE_DIR", "/tmp/wildawm-image-descriptions"))
# raw always sends a reasoning effort (default "none"); sdk sends it only when set.
REASONING_EFFORT = os.environ.get(
    "AWM_VISION_REASONING_EFFORT", "none" if TRANSPORT == "raw" else ""
).strip().lower()
REASONING_CHAT_PARAM = os.environ.get(
    "AWM_VISION_REASONING_CHAT_PARAM", "reasoning_effort").strip()

_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

_DEFAULT_PROMPT = (
    "Describe this image objectively and specifically: the main objects, "
    "their colors, and any visible text, labels, or prices. Be concise and "
    "factual; do not speculate."
)


def _sniff_image_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"BM"):
        return "image/bmp"
    return None


def _image_data_uri(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _vision_body(data_uri: str, prompt: str, *, model: str, max_tokens: int) -> dict:
    """Chat-completions request body, mapping the reasoning effort per REASONING_CHAT_PARAM."""
    body_obj = {
        "model": model,
        "max_completion_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
    }
    if REASONING_EFFORT:
        if REASONING_CHAT_PARAM in ("reasoning", "reasoning.effort"):
            body_obj["reasoning"] = {"effort": REASONING_EFFORT}
        elif REASONING_CHAT_PARAM == "reasoning_effort":
            body_obj["reasoning_effort"] = REASONING_EFFORT
        elif REASONING_CHAT_PARAM == "reasoningEffort":
            body_obj["reasoningEffort"] = REASONING_EFFORT
        elif REASONING_CHAT_PARAM in (
            "chat_template_kwargs.reasoning_effort",
            "chat_template_kwargs.deepseek_reasoning_effort",
        ):
            ctk = dict(body_obj.get("chat_template_kwargs") or {})
            if REASONING_EFFORT == "none":
                ctk["thinking"] = False
                ctk.pop("reasoning_effort", None)
            else:
                ctk["thinking"] = True
                ctk["reasoning_effort"] = REASONING_EFFORT
            body_obj["chat_template_kwargs"] = ctk
        elif REASONING_CHAT_PARAM == "chat_template_kwargs.enable_thinking":
            ctk = dict(body_obj.get("chat_template_kwargs") or {})
            ctk["enable_thinking"] = REASONING_EFFORT != "none"
            body_obj["chat_template_kwargs"] = ctk
    return body_obj


def _vision_text(payload: dict) -> str:
    """The visible description; some backends put it in reasoning fields."""
    msg = ((payload.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content") or msg.get("reasoning") or msg.get("reasoning_content") or ""
    return text.strip() if isinstance(text, str) else ""


def _vision_post(*, base_url: str, api_key: str, body: dict) -> tuple[dict | None, int | None, str]:
    """POST one request: (payload, None, "") or (None, http_status or None, detail)."""
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read()), None, ""
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        return None, exc.code, detail
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, None, f"connection/timeout error: {exc}"


class _HardTimeoutError(Exception):
    pass


def _hard_timeout_handler(_signum, _frame):
    raise _HardTimeoutError


def _acquire_lock(lock_file) -> bool:
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


def _mime_for(path: Path, raw: bytes) -> tuple[str | None, str]:
    sniffed = _sniff_image_mime(raw)
    suffix_mime = _MIME.get(path.suffix.lower())
    if sniffed:
        return sniffed, ""
    if suffix_mime:
        return None, (
            f"file extension {path.suffix!r} suggests {suffix_mime}, but the "
            "file header is not a supported image."
        )
    guessed = mimetypes.guess_type(str(path))[0]
    return None, (
        f"unsupported image type for {str(path)!r}"
        + (f" (guessed MIME: {guessed})" if guessed else "")
        + ". Pass a PNG, JPEG, GIF, WebP, or BMP image file."
    )


def _vision_call_backend(data_uri: str, prompt: str, *, base_url: str,
                         api_key: str, model: str) -> tuple[str | None, str]:
    body = _vision_body(data_uri, prompt, model=model, max_tokens=MAX_TOKENS)
    payload, code, detail = _vision_post(
        base_url=base_url, api_key=api_key, body=body)
    if payload is None:
        if code is None:
            return None, detail
        if code == 400 and "multimodal" in detail.lower():
            return None, (
                f"!the configured vision model '{model}' at {base_url} is not "
                f"multimodal. Point AWM_VISION_MODEL/AWM_VISION_BASE_URL at a "
                f"vision-capable model. {detail}"
            )
        if code in (401, 403):
            return None, f"!auth failed (HTTP {code}) at {base_url}: {detail}"
        if code != 429 and 400 <= code < 500:
            return None, (
                f"!vision request rejected (HTTP {code}) at {base_url}: {detail}"
            )
        return None, f"HTTP {code}: {detail}"
    text = _vision_text(payload)
    if text:
        return text, ""
    return None, "empty response from vision model"


def _vision_backends() -> list[tuple[str, str, str]]:
    backends = [(BASE_URL, API_KEY, MODEL)]
    fallback = (FALLBACK_BASE_URL, FALLBACK_API_KEY, FALLBACK_MODEL)
    if (fallback[0] and fallback[2]
            and (fallback[0], fallback[2]) != (BASE_URL, MODEL)):
        backends.append(fallback)
    return backends


def _vision_call(data_uri: str, prompt: str) -> tuple[str | None, str]:
    failures = []
    all_terminal = True
    for base_url, api_key, model in _vision_backends():
        text, reason = _vision_call_backend(
            data_uri, prompt, base_url=base_url, api_key=api_key, model=model)
        if text is not None:
            return text, ""
        terminal = reason.startswith("!")
        all_terminal = all_terminal and terminal
        failures.append(f"{model}@{base_url}: {reason.lstrip('!')}")
    prefix = "!" if all_terminal else ""
    return None, prefix + "; ".join(failures)


def _error_result(message: str) -> dict[str, object]:
    return {"result": message, "error": message}


def _success_result(*, text: str, path: Path, raw: bytes, mime: str) -> dict[str, object]:
    return {
        "result": text,
        "image_sha256": hashlib.sha256(raw).hexdigest(),
        "image_path": str(path),
        "image_bytes": len(raw),
        "image_mime": mime,
    }


def _describe_image_result(path: str, question: str = "") -> dict[str, object]:
    if _VISION_CONFIG_ERROR:
        return _error_result(f"[describe_image error] {_VISION_CONFIG_ERROR}")
    p = Path(path)
    if not p.exists():
        return _error_result(
            f"[describe_image error] file not found: {path!r}. Pass an absolute "
            "path to an existing image file."
        )
    if not p.is_file():
        return _error_result(
            f"[describe_image error] not a regular file: {path!r}.")
    try:
        raw = p.read_bytes()
    except OSError as exc:
        return _error_result(
            f"[describe_image error] cannot read {path!r}: {exc}")
    if not raw:
        return _error_result(
            f"[describe_image error] empty file (0 bytes): {path!r}.")
    if len(raw) > MAX_BYTES:
        return _error_result(
            f"[describe_image error] image too large "
            f"({len(raw) // 1024 // 1024}MB > 20MB): {path!r}."
        )
    mime, mime_error = _mime_for(p, raw)
    if mime is None:
        return _error_result(f"[describe_image error] {mime_error}")

    data_uri = _image_data_uri(raw, mime)
    prompt = question.strip() or _DEFAULT_PROMPT
    cache_key = hashlib.sha256(
        b"\0".join((
            json.dumps([(base, model) for base, _, model in _vision_backends()]).encode(),
            prompt.encode(),
            raw,
        ))
    ).hexdigest()
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = CACHE_DIR / f"{cache_key}.txt"
        if cache_path.exists():
            cached = cache_path.read_text(errors="replace").strip()
            if cached:
                return _success_result(
                    text=cached, path=p, raw=raw, mime=mime)
        lock_path = CACHE_DIR / ".request.lock"
        with lock_path.open("a+") as lock_file:
            if not _acquire_lock(lock_file):
                return _error_result(
                    "[describe_image error] timed out waiting for another "
                    f"vision request after {LOCK_TIMEOUT}s. Proceed using "
                    "other evidence if possible."
                )
            if cache_path.exists():
                cached = cache_path.read_text(errors="replace").strip()
                if cached:
                    return _success_result(
                        text=cached, path=p, raw=raw, mime=mime)
            last = ""
            for attempt in range(1, RETRIES + 1):
                text, reason = _vision_call(data_uri, prompt)
                if text is not None:
                    tmp_path = cache_path.with_suffix(".tmp")
                    tmp_path.write_text(text + "\n")
                    os.replace(tmp_path, cache_path)
                    return _success_result(
                        text=text, path=p, raw=raw, mime=mime)
                if reason.startswith("!"):
                    return _error_result(
                        f"[describe_image error] {reason[1:]}")
                last = reason
                if attempt < RETRIES:
                    time.sleep(min(2 * attempt, 8))
    except OSError as exc:
        return _error_result(
            f"[describe_image error] local cache/lock failure: {exc}")
    return _error_result(
        f"[describe_image error] configured vision backends failed "
        f"after {RETRIES} attempts: {last}. Could not describe {path!r}; "
        "proceed using other evidence if possible."
    )


def describe_image(path: str, question: str = "") -> str:
    """Describe an image and return plain text (Python/CLI API)."""

    return str(_describe_image_result(path, question)["result"])


_RAW_TOOL = {
    "name": "describe_image",
    "description": "Describe an image file with the configured external vision model. Use an absolute path and a focused question.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to a PNG, JPEG, GIF, WebP, or BMP image."},
            "question": {"type": "string", "description": "Focused visual question."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


def _raw_call(data_uri: str, prompt: str) -> tuple[str | None, str, bool]:
    """One request as (text, reason, terminal); HTTP errors except 429/500/502/503/504 are terminal."""
    body = _vision_body(data_uri, prompt, model=MODEL, max_tokens=MAX_TOKENS)
    payload, code, detail = _vision_post(
        base_url=BASE_URL, api_key=API_KEY, body=body)
    if payload is None:
        if code is None:
            return None, detail, False
        return None, f"HTTP {code}: {detail}", code not in (429, 500, 502, 503, 504)
    text = _vision_text(payload)
    return (text, "", False) if text else (None, "empty response from vision model", False)


def _raw_describe(path: str, question: str = "") -> dict:
    """Describe an image, raising on failure; the JSON-RPC handler reports it as tool error text."""
    image = Path(path)
    if not image.is_absolute() or not image.is_file():
        raise ValueError(f"file not found: {path!r}. Pass an absolute path to an existing image file.")
    raw = image.read_bytes()
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError(f"image must be non-empty and at most 20 MiB: {path!r}")
    mime = _sniff_image_mime(raw)
    if mime is None:
        raise ValueError(f"unsupported image type for {path!r}. Pass a PNG, JPEG, GIF, WebP, or BMP image file.")
    data_uri = _image_data_uri(raw, mime)
    reason = ""
    for attempt in range(1, RETRIES + 1):
        text, reason, terminal = _raw_call(data_uri, question.strip() or _DEFAULT_PROMPT)
        if text is not None:
            return {"result": text, "image_path": str(image), "image_bytes": len(raw), "image_mime": mime}
        if terminal:
            break
        if attempt < RETRIES:
            time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"vision backend failed: {reason}. Proceed using other evidence if possible.")


def _raw_handle(request: dict) -> dict | None:
    method, rid = request.get("method", ""), request.get("id")
    if rid is None or method.startswith("notifications/"):
        return None
    if method == "initialize":
        result = {"protocolVersion": (request.get("params") or {}).get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}}, "serverInfo": {"name": "vision", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [_RAW_TOOL]}
    elif method == "tools/call":
        params = request.get("params") or {}
        try:
            if params.get("name") != "describe_image":
                raise ValueError("unknown tool")
            value = _raw_describe(**(params.get("arguments") or {}))
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value, "isError": False}
        except Exception as error:
            result = {"content": [{"type": "text", "text": f"[describe_image error] {error}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


if FastMCP is not None:
    mcp = FastMCP("vision")

    @mcp.tool(name="describe_image", structured_output=True)
    def describe_image_tool(path: str, question: str = "") -> dict[str, object]:
        """Describe an image and return a receipt bound to the exact bytes submitted to the vision backend."""

        return _describe_image_result(path, question)
else:  # pragma: no cover - exercised only in sdk-less environments
    mcp = None


def _raw_main() -> int:
    if len(sys.argv) > 1:
        print(_raw_describe(sys.argv[1], " ".join(sys.argv[2:]))["result"])
        return 0
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = _raw_handle(request) if isinstance(request, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def main() -> int:
    if TRANSPORT == "raw":
        return _raw_main()
    if _VISION_CONFIG_ERROR:
        print(f"[describe_image error] {_VISION_CONFIG_ERROR}", file=sys.stderr)
        return 2
    if len(sys.argv) > 1:
        path = sys.argv[1]
        question = " ".join(sys.argv[2:]).strip()
        previous_handler = signal.signal(signal.SIGALRM, _hard_timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, HARD_TIMEOUT)
        try:
            print(describe_image(path, question))
        except _HardTimeoutError:
            print(
                "[describe_image error] hard timeout after "
                f"{HARD_TIMEOUT}s. Proceed using other evidence if possible."
            )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
        return 0
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
