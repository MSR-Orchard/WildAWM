"""Bounded in-container bridge from sandbox exec calls to the sandbox HTTP server."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

import httpx


ALLOWED_ENDPOINTS = frozenset(
    {
        "/exec",
        "/read",
        "/write",
        "/write_b64",
        "/edit",
        "/glob",
        "/grep",
        "/screenshot",
        "/read_media",
        "/describe_image",
        "/download",
        "/health",
    }
)
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 512 * 1024 * 1024
_SANDBOX_BASE_URL = os.environ.get(
    "CLAW_EVAL_SANDBOX_URL",
    "http://127.0.0.1:8080",
)


def _read_request(path: Path) -> dict[str, Any]:
    metadata = path.stat()
    if metadata.st_size > MAX_REQUEST_BYTES:
        raise ValueError("sandbox RPC request exceeds size limit")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("sandbox RPC request must be a JSON object")
    return value


def _atomic_write_response(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("sandbox RPC response exceeds size limit")
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _run(
    endpoint: str,
    request_path: Path,
    response_path: Path,
    timeout_seconds: float,
) -> None:
    request = _read_request(request_path)
    method = "GET" if endpoint == "/health" else "POST"
    kwargs = {} if method == "GET" else {"json": request}
    response = httpx.request(
        method,
        f"{_SANDBOX_BASE_URL}{endpoint}",
        timeout=timeout_seconds,
        follow_redirects=False,
        **kwargs,
    )
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        body = {
            "error": (
                f"Sandbox endpoint {endpoint} returned a non-JSON "
                f"HTTP {response.status_code} response"
            )
        }
    _atomic_write_response(
        response_path,
        {
            "status_code": response.status_code,
            "body": body,
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 4:
        raise ValueError(
            "usage: rpc.py ENDPOINT REQUEST_JSON RESPONSE_JSON TIMEOUT_SECONDS"
        )
    endpoint, raw_request_path, raw_response_path, raw_timeout = arguments
    if endpoint not in ALLOWED_ENDPOINTS:
        raise ValueError(f"unsupported sandbox endpoint: {endpoint}")
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError:
        return 1
    if timeout_seconds <= 0:
        return 1
    try:
        _run(
            endpoint,
            Path(raw_request_path),
            Path(raw_response_path),
            timeout_seconds,
        )
    except Exception as error:
        print(f"sandbox RPC failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
