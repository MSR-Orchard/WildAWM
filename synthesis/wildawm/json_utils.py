"""Lenient JSON helpers for human- or LLM-edited files; protocol payloads stay on strict json."""

from pathlib import Path
from typing import Any

import json_repair


def loads_jsonc(text: str) -> Any:
    """Parse JSON with JSONC-style comments/trailing commas via json-repair."""
    return json_repair.loads(text)


def load_jsonc(path: str | Path) -> Any:
    """Read and parse a human-edited JSON/JSONC file."""
    return loads_jsonc(Path(path).read_text())
