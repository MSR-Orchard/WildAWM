#!/usr/bin/env python3
"""Run generated Python (verifiers, golden.py) in a throwaway no-network container, not on the host."""
import os
import subprocess
from pathlib import Path

from wildawm import const_config as cfg

IMAGE = os.environ.get("AWM_SANDBOX_IMAGE", "wildawm-agent:latest")

# Fixed container-side mount points keep script argv deterministic.
WS, DATA, BASE, WH, BWH, SCRIPTS = ("/sb/ws", "/sb/data", "/sb/base",
                                    "/sb/wh", "/sb/bwh", "/sb/scripts")


def _mounts(pairs: list[tuple[Path | None, str]]) -> list[str]:
    out: list[str] = []
    for host, cont in pairs:
        if host:
            out += ["-v", f"{Path(host).resolve()}:{cont}:rw"]
    return out


def run_py(script_host: Path, argv: list[str], *,
           workspace: Path, data_dir: Path, baseline_dir: Path | None = None,
           webharbor_dir: Path | None = None,
           baseline_webharbor_dir: Path | None = None,
           timeout: int = cfg.LOCAL_SCRIPT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run one generated script in the sandbox; ``argv`` uses the container paths."""
    script_host = Path(script_host).resolve()
    cmd = ["docker", "run", "--rm", "--network", "none",
           "--memory", "1g", "--pids-limit", "256",
           "--user", f"{os.getuid()}:{os.getgid()}",
           "--entrypoint", "python3",
           *_mounts([(script_host.parent, SCRIPTS), (workspace, WS),
                     (data_dir, DATA), (baseline_dir, BASE),
                     (webharbor_dir, WH), (baseline_webharbor_dir, BWH)]),
           IMAGE, f"{SCRIPTS}/{script_host.name}", *argv]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def check_argv(*, webharbor: bool, baseline_webharbor: bool) -> list[str]:
    """Standard argv for a verifier check inside the sandbox."""
    args = ["--workspace", WS, "--data-dir", DATA, "--baseline-dir", BASE]
    if webharbor:
        args += ["--webharbor-dir", WH]
    if baseline_webharbor:
        args += ["--baseline-webharbor-dir", BWH]
    return args


def golden_argv(*, webharbor: bool, baseline: bool) -> list[str]:
    args = ["--workspace", WS, "--data-dir", DATA]
    if webharbor:
        args += ["--webharbor-dir", WH]
    if baseline:
        args += ["--baseline-dir", BASE]
    return args
