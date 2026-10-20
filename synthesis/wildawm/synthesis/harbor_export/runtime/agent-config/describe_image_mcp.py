#!/usr/bin/env python3
"""Stdio MCP server exposing describe_image for text-only judge models."""
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
    from mcp.server.mcpserver import MCPServer as FastMCP

BASE_URL = os.environ.get("WILDAWM_VISION_LLM_ENDPOINT", "").strip().rstrip("/")
API_KEY = os.environ.get("WILDAWM_VISION_LLM_KEY", "").strip()
MODEL = os.environ.get("WILDAWM_VISION_LLM_MODEL_NAME", "").strip()
FALLBACK_BASE_URL = os.environ.get(
    "WILDAWM_VISION_LLM_FALLBACK_ENDPOINT", "").strip().rstrip("/")
FALLBACK_API_KEY = os.environ.get(
    "WILDAWM_VISION_LLM_FALLBACK_KEY", "").strip()
FALLBACK_MODEL = os.environ.get(
    "WILDAWM_VISION_LLM_FALLBACK_MODEL_NAME", "").strip()
TIMEOUT = int(os.environ.get("WILDAWM_DESCRIBE_IMAGE_TIMEOUT", "60"))
RETRIES = max(1, int(os.environ.get("WILDAWM_DESCRIBE_IMAGE_RETRIES", "3")))
BACKEND_COUNT = 1 + int(bool(
    FALLBACK_BASE_URL
    and FALLBACK_MODEL
    and (FALLBACK_BASE_URL, FALLBACK_MODEL) != (BASE_URL, MODEL)
))
HARD_TIMEOUT = max(1, int(os.environ.get(
    "WILDAWM_DESCRIBE_IMAGE_HARD_TIMEOUT",
    str(
        TIMEOUT * RETRIES * BACKEND_COUNT
        + sum(min(2 * attempt, 8) for attempt in range(1, RETRIES))
        + 15
    ),
)))
LOCK_TIMEOUT = max(1, int(os.environ.get(
    "WILDAWM_DESCRIBE_IMAGE_LOCK_TIMEOUT", str(HARD_TIMEOUT))))
MAX_TOKENS = int(os.environ.get("WILDAWM_DESCRIBE_IMAGE_MAX_TOKENS", "4096"))
MAX_BYTES = 20 * 1024 * 1024
CACHE_DIR = Path(os.environ.get(
    "WILDAWM_DESCRIBE_IMAGE_CACHE_DIR", "/tmp/wildawm-image-descriptions"))
TIMING_LOG = Path(os.environ.get(
    "WILDAWM_DESCRIBE_IMAGE_TIMING_LOG",
    "/tmp/wildawm-logs/vision-timing.jsonl",
))
REASONING_EFFORT = os.environ.get(
    "WILDAWM_VISION_LLM_REASONING_EFFORT", "").strip().lower()
REASONING_CHAT_PARAM = os.environ.get(
    "WILDAWM_VISION_LLM_REASONING_CHAT_PARAM", "reasoning_effort").strip()

_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

mcp = FastMCP("vision")


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
    body_obj = {
        "model": model,
        "max_completion_tokens": MAX_TOKENS,
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
    body = json.dumps(body_obj).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        base_url + "/chat/completions",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        if exc.code == 400 and "multimodal" in detail.lower():
            return None, (
                f"!the configured vision model '{model}' at {base_url} is not "
                f"multimodal. Point WILDAWM_VISION_LLM_MODEL_NAME/"
                f"WILDAWM_VISION_LLM_ENDPOINT at a "
                f"vision-capable model. {detail}"
            )
        if exc.code in (401, 403):
            return None, f"!auth failed (HTTP {exc.code}) at {base_url}: {detail}"
        if exc.code != 429 and 400 <= exc.code < 500:
            return None, (
                f"!vision request rejected (HTTP {exc.code}) at {base_url}: {detail}"
            )
        return None, f"HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"connection/timeout error: {exc}"

    msg = ((payload.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content") or msg.get("reasoning") or msg.get("reasoning_content") or ""
    text = text.strip() if isinstance(text, str) else ""
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
    if not BASE_URL or not MODEL:
        missing = ", ".join(
            name for name, value in (
                ("WILDAWM_VISION_LLM_ENDPOINT", BASE_URL),
                ("WILDAWM_VISION_LLM_MODEL_NAME", MODEL),
            ) if not value
        )
        return None, (
            f"!vision LLM not configured: set {missing} (in the container "
            "environment, e.g. via the task [environment.env] template or "
            "the vision server's environment) before calling describe_image"
        )
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


def _success_result(
    *,
    text: str,
    path: Path,
    raw: bytes,
    mime: str,
) -> dict[str, object]:
    return {
        "result": text,
        "image_sha256": hashlib.sha256(raw).hexdigest(),
        "image_path": str(path),
        "image_bytes": len(raw),
        "image_mime": mime,
    }


def _describe_image_result(path: str, question: str = "") -> dict[str, object]:
    started = time.monotonic()
    prepared_at: float | None = None
    lock_wait_s = 0.0
    provider_s = 0.0
    retry_sleep_s = 0.0
    attempts = 0
    cache_hit = False

    def timing() -> dict[str, object]:
        now = time.monotonic()
        return {
            "total_s": max(0.0, now - started),
            "prepare_s": max(
                0.0, (prepared_at if prepared_at is not None else now) - started),
            "lock_wait_s": max(0.0, lock_wait_s),
            "provider_s": max(0.0, provider_s),
            "retry_sleep_s": max(0.0, retry_sleep_s),
            "attempts": attempts,
            "cache_hit": cache_hit,
        }

    def error(message: str) -> dict[str, object]:
        value = timing()
        value.update({
            "schema_version": 1,
            "success": False,
            "timed_out": "timeout" in message.casefold()
            or "timed out" in message.casefold(),
        })
        try:
            TIMING_LOG.parent.mkdir(parents=True, exist_ok=True)
            with TIMING_LOG.open("a") as stream:
                stream.write(json.dumps(value, sort_keys=True) + "\n")
        except OSError:
            pass
        return _error_result(message)

    def success(
        *, text: str, path: Path, raw: bytes, mime: str,
    ) -> dict[str, object]:
        value = timing()
        value.update({
            "schema_version": 1,
            "success": True,
            "timed_out": False,
            "image_bytes": len(raw),
        })
        try:
            TIMING_LOG.parent.mkdir(parents=True, exist_ok=True)
            with TIMING_LOG.open("a") as stream:
                stream.write(json.dumps(value, sort_keys=True) + "\n")
        except OSError:
            pass
        return _success_result(text=text, path=path, raw=raw, mime=mime)

    p = Path(path)
    if not p.exists():
        return error(
            f"[describe_image error] file not found: {path!r}. Pass an absolute "
            "path to an existing image file."
        )
    if not p.is_file():
        return error(
            f"[describe_image error] not a regular file: {path!r}.")
    try:
        raw = p.read_bytes()
    except OSError as exc:
        return error(
            f"[describe_image error] cannot read {path!r}: {exc}")
    if not raw:
        return error(
            f"[describe_image error] empty file (0 bytes): {path!r}.")
    if len(raw) > MAX_BYTES:
        return error(
            f"[describe_image error] image too large "
            f"({len(raw) // 1024 // 1024}MB > 20MB): {path!r}."
        )
    mime, mime_error = _mime_for(p, raw)
    if mime is None:
        return error(f"[describe_image error] {mime_error}")

    data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    prompt = question.strip() or (
        "Describe this image objectively and specifically: the main objects, "
        "their colors, and any visible text, labels, or prices. Be concise and "
        "factual; do not speculate."
    )
    cache_key = hashlib.sha256(
        b"\0".join((
            json.dumps([(base, model) for base, _, model in _vision_backends()]).encode(),
            prompt.encode(),
            raw,
        ))
    ).hexdigest()
    prepared_at = time.monotonic()
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = CACHE_DIR / f"{cache_key}.txt"
        if cache_path.exists():
            cached = cache_path.read_text(errors="replace").strip()
            if cached:
                cache_hit = True
                return success(text=cached, path=p, raw=raw, mime=mime)
        lock_path = CACHE_DIR / ".request.lock"
        with lock_path.open("a+") as lock_file:
            lock_started = time.monotonic()
            if not _acquire_lock(lock_file):
                lock_wait_s += time.monotonic() - lock_started
                return error(
                    "[describe_image error] timed out waiting for another "
                    f"vision request after {LOCK_TIMEOUT}s. Proceed using "
                    "other evidence if possible."
                )
            lock_wait_s += time.monotonic() - lock_started
            if cache_path.exists():
                cached = cache_path.read_text(errors="replace").strip()
                if cached:
                    cache_hit = True
                    return success(text=cached, path=p, raw=raw, mime=mime)
            last = ""
            for attempt in range(1, RETRIES + 1):
                attempts = attempt
                provider_started = time.monotonic()
                text, reason = _vision_call(data_uri, prompt)
                provider_s += time.monotonic() - provider_started
                if text is not None:
                    tmp_path = cache_path.with_suffix(".tmp")
                    tmp_path.write_text(text + "\n")
                    os.replace(tmp_path, cache_path)
                    return success(text=text, path=p, raw=raw, mime=mime)
                if reason.startswith("!"):
                    return error(
                        f"[describe_image error] {reason[1:]}")
                last = reason
                if attempt < RETRIES:
                    sleep_started = time.monotonic()
                    time.sleep(min(2 * attempt, 8))
                    retry_sleep_s += time.monotonic() - sleep_started
    except OSError as exc:
        return error(
            f"[describe_image error] local cache/lock failure: {exc}")
    return error(
        f"[describe_image error] configured vision backends failed "
        f"after {RETRIES} attempts: {last}. Could not describe {path!r}; "
        "proceed using other evidence if possible."
    )


def describe_image(path: str, question: str = "") -> str:
    """Plain-text describe_image for the CLI/Python API."""

    return str(_describe_image_result(path, question)["result"])


@mcp.tool(name="describe_image", structured_output=True)
def describe_image_tool(path: str, question: str = "") -> dict[str, object]:
    """Describe an image and return a receipt bound to the exact bytes submitted to the vision backend."""

    return _describe_image_result(path, question)


def main() -> int:
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
