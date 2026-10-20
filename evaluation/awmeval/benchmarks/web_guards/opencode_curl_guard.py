#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit


def validate_urls(arguments: list[str], allowed_hosts: set[str], allowed_origins: set[str]) -> None:
    for value in arguments:
        if not value.lower().startswith(("http://", "https://")):
            continue
        parsed = urlsplit(value)
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        host = (parsed.hostname or "").lower()
        if allowed_origins and origin not in allowed_origins:
            raise SystemExit(f"curl: URL is outside the benchmark origin: {origin}")
        if not allowed_origins and allowed_hosts and host not in allowed_hosts:
            raise SystemExit(f"curl: URL is outside the benchmark host set: {host}")


def main() -> None:
    allowed_hosts = {value.strip().lower() for value in os.environ.get("AWMEVAL_ALLOWED_HOSTS", "").split(",") if value.strip()}
    allowed_origins = {value.strip().lower() for value in os.environ.get("AWMEVAL_ALLOWED_ORIGINS", "").split(",") if value.strip()}
    validate_urls(sys.argv[1:], allowed_hosts, allowed_origins)
    os.execv("/usr/bin/curl", ["curl", *sys.argv[1:]])


if __name__ == "__main__":
    main()
