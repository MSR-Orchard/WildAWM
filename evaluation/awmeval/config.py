from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.yaml"
SECTIONS = ("agent", "vision", "judge", "opencode", "sandbox", "run")


class Config(dict):
    """Nested dict with attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


def wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return Config({key: wrap(item) for key, item in value.items()})
    if isinstance(value, list):
        return [wrap(item) for item in value]
    return value


def deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def set_path(tree: dict, dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    for key in parents:
        tree = tree.setdefault(key, {})
    tree[leaf] = value


def load_tree(paths, sets: list[str] = ()) -> dict:
    tree = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")) or {}
    for path in [paths] if isinstance(paths, (str, Path)) else paths or []:
        tree = deep_merge(tree, yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    for item in sets:
        key, _, raw = item.partition("=")
        set_path(tree, key.strip(), yaml.safe_load(raw))
    return tree


def resolve(tree: dict, benchmark: str) -> Config:
    section = (tree.get("benchmarks") or {}).get(benchmark) or {}
    resolved = {name: tree.get(name) or {} for name in SECTIONS}
    resolved = deep_merge(resolved, {key: value for key, value in section.items() if key in SECTIONS})
    resolved["benchmark"] = {key: value for key, value in section.items() if key not in SECTIONS}
    resolved["name"] = benchmark
    return wrap(resolved)


def load_config(paths, benchmark: str, sets: list[str] = ()) -> Config:
    """Resolve config.example.yaml <- each path <- benchmarks.<name> section for one benchmark; --set always wins."""
    tree = load_tree(paths, [item for item in sets if item.split(".", 1)[0] not in SECTIONS + ("benchmark",)])
    resolved = resolve(tree, benchmark)
    for item in sets:
        key, _, raw = item.partition("=")
        if key.split(".", 1)[0] in SECTIONS + ("benchmark",):
            set_path(resolved, key.strip(), yaml.safe_load(raw))
    return wrap(resolved)
