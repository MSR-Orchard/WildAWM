"""run.json termination resolver per LOG_SPEC §4.

Pure helpers:

  - :func:`classify_error`   substring-match an exception against a small
                             category vocabulary.
  - :func:`redact_config`    redact ``*_api_key`` / ``api_key`` from an
                             agent's yaml config before logging.
  - :func:`err_dict`         build the termination.error payload.
"""

from __future__ import annotations

import asyncio
from typing import Any

_CATEGORY_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("rate_limited", ("rate limit", "ratelimit", "429", "too many requests")),
    (
        "vm_quota_exhausted",
        (
            "quota",
            "stockout",
            "resource_exhausted",
            "does not have enough resources",
            "cpus_per_vm_family",
        ),
    ),
    (
        "auth_failed",
        (
            "401",
            "403",
            "authentication_failed",
            "permission denied",
            "unauthorized",
            "forbidden",
            "llm auth failed",
            "user not found",
            "invalid api key",
        ),
    ),
    (
        "gcs_missing",
        ("matched no objects", "no urls matched", "bucketnotfoundexception", "no such object"),
    ),
    (
        "transport_error",
        (
            "connection reset",
            "connection refused",
            "503",
            "service unavailable",
            "deadline exceeded",
            "broken pipe",
            "remote end closed connection",
        ),
    ),
    ("rpc_timeout", ("timeout", "timed out")),
]


def classify_error(exc: BaseException) -> str | None:
    """Return a LOG_SPEC §4 termination.category for ``exc``, or None.

    ``KeyboardInterrupt`` / ``asyncio.CancelledError`` always yield None.
    """
    if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
        return None
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "rpc_timeout"
    msg = str(exc).lower()
    for category, substrings in _CATEGORY_PATTERNS:
        if any(s in msg for s in substrings):
            return category
    return None


def redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        normalized = k.lower()
        if (normalized == "api_key" or normalized.endswith("_api_key")) and isinstance(v, str) and v:
            out[k] = "***"
        else:
            out[k] = v
    return out


def err_dict(exc: BaseException) -> dict[str, Any]:
    """Build the LOG_SPEC §4 termination.error payload from an exception."""
    import traceback

    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
