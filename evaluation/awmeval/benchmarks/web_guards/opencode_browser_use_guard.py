#!/usr/bin/env python3
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from opencode_curl_guard import validate_urls  # noqa: E402


_URL = re.compile(r"https?://[^\s'\"`]+", re.IGNORECASE)
_METADATA_ATTRIBUTE = re.compile(r'''itemtype\s*=\s*\\?(['"])https?://schema\.org/[A-Za-z]+\\?\1''')


def _validate_urls(urls: list[str], allowed_hosts: set[str], allowed_origins: set[str]) -> None:
    try:
        validate_urls(urls, allowed_hosts, allowed_origins)
    except SystemExit as error:
        raise SystemExit(str(error).replace("curl:", "browser-use:", 1)) from error


def validate_source(source: str, allowed_hosts: set[str], allowed_origins: set[str]) -> None:
    if allowed_origins:
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and ast.get_docstring(node) is not None:
                    node.body = node.body[1:]
            source = ast.unparse(tree)  # Comments and docstrings do not issue network requests.
        except SyntaxError:
            pass  # browser-use eval also accepts JavaScript.
        # schema.org itemtype URLs are metadata, not requests; direct navigation is still checked below.
        source = _METADATA_ATTRIBUTE.sub('itemtype="metadata"', source)
    urls = [match.group(0).rstrip(")]},;.") for match in _URL.finditer(source)]
    _validate_urls(urls, allowed_hosts, allowed_origins)


def validate_command(arguments: list[str], allowed_hosts: set[str], allowed_origins: set[str]) -> None:
    commands = {"open", "click", "type", "input", "scroll", "back", "screenshot", "state", "switch", "close-tab", "keys", "select", "eval", "extract", "hover", "dblclick", "rightclick", "cookies", "wait", "get", "python", "run", "task", "session", "tunnel", "sessions", "close", "server", "profile"}
    options_with_values = {"--session", "-s", "--browser", "-b", "--profile", "--api-key", "--template"}
    command_index = None
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in options_with_values:
            index += 2
        elif value.startswith("-"):
            index += 1
        else:
            command_index = index if value in commands else None
            break
    if command_index is None:
        return
    command = arguments[command_index]
    values = arguments[command_index + 1 :]
    if command == "open" and values:
        if not values[0].lower().startswith(("http://", "https://")):
            raise SystemExit("browser-use: navigation URL must use HTTP or HTTPS")
        _validate_urls([values[0]], allowed_hosts, allowed_origins)
    elif command == "eval" and values:
        validate_source(values[0], allowed_hosts, allowed_origins)
    elif command == "python":
        if "--file" in values:
            file_index = values.index("--file") + 1
            if file_index < len(values):
                validate_source(Path(values[file_index]).read_text(encoding="utf-8"), allowed_hosts, allowed_origins)
        elif values and not values[0].startswith("-"):
            validate_source(values[0], allowed_hosts, allowed_origins)


def main() -> None:
    allowed_hosts = {value.strip().lower() for value in os.environ.get("AWMEVAL_ALLOWED_HOSTS", "").split(",") if value.strip()}
    allowed_origins = {value.strip().lower() for value in os.environ.get("AWMEVAL_ALLOWED_ORIGINS", "").split(",") if value.strip()}
    if len(sys.argv) >= 2 and sys.argv[1] == "--command":
        validate_command(sys.argv[2:], allowed_hosts, allowed_origins)
        return
    if len(sys.argv) != 2:
        raise SystemExit("usage: opencode_browser_use_guard.py PYTHON_SOURCE | --command [BROWSER_USE_ARGS ...]")
    validate_source(Path(sys.argv[1]).read_text(encoding="utf-8"), allowed_hosts, allowed_origins)


if __name__ == "__main__":
    main()
