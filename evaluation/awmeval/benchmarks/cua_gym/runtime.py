#!/usr/bin/env python3
"""Start CUA-Gym Hub mock apps and a headless Chromium with Playwright MCP inside one sandbox."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

HUB_ROOT = Path("/opt/cua-gym-hub/websites")
RUNTIME_ROOT = Path("/tmp/cua-gym-runtime")
VITE = Path("/opt/cua-gym-runtime/node_modules/.bin/vite")
CHROMIUM = Path("/opt/pw-browsers/chromium-1228/chrome-linux64/chrome")
APP_NAMES = [line.strip() for line in Path("/opt/cua-gym-apps.txt").read_text().splitlines() if line.strip() and not line.startswith("#")]
APP_PORTS = {name: 18080 + index for index, name in enumerate(APP_NAMES)}


def spawn(name: str, command: list[str], cwd: Path, env: dict | None = None) -> subprocess.Popen:
    (RUNTIME_ROOT / "logs").mkdir(parents=True, exist_ok=True)
    log = (RUNTIME_ROOT / "logs" / f"{name}.log").open("ab", buffering=0)
    return subprocess.Popen(command, cwd=cwd, env={**os.environ, **(env or {})}, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def wait_until(check, process: subprocess.Popen, what: str, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{what} exited during startup; see {RUNTIME_ROOT}/logs")
        try:
            if check():
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"{what} did not become ready")


def http_ok(url: str) -> bool:
    with urllib.request.urlopen(url, timeout=2) as response:
        return 200 <= response.status < 300


def port_open(port: int) -> bool:
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        return True


def start_apps(names: list[str]) -> dict:
    urls = {}
    for name in names:
        root = HUB_ROOT / name
        for stale in (".mock-state.json", ".mock-state.initial.json"):
            (root / stale).unlink(missing_ok=True)
        for stale in (".mock-states", ".mock-files"):
            shutil.rmtree(root / stale, ignore_errors=True)
        port = APP_PORTS[name]
        process = spawn(f"app-{name}", [str(VITE), "preview", "--host", "127.0.0.1", "--port", str(port), "--strictPort"], root, {"NODE_ENV": "production"})
        wait_until(lambda: http_ok(f"http://127.0.0.1:{port}/go?sid=runtime-health"), process, f"app {name}")
        urls[name] = f"http://127.0.0.1:{port}"
    return urls


def start_browser(url: str, allowed_origins: list[str], cdp_port: int = 9222, mcp_port: int = 8931) -> dict:
    profile = RUNTIME_ROOT / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    chrome = spawn("chromium", [str(CHROMIUM), f"--remote-debugging-port={cdp_port}", "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}",
                                "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check", url], RUNTIME_ROOT)
    wait_until(lambda: http_ok(f"http://127.0.0.1:{cdp_port}/json/version"), chrome, "chromium")
    npm_root = subprocess.run(["npm", "root", "--global"], capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    command = ["node", f"{npm_root}/@playwright/mcp/cli.js", "--port", str(mcp_port), "--host", "127.0.0.1", "--cdp-endpoint", f"http://127.0.0.1:{cdp_port}",
               "--shared-browser-context", "--image-responses", "omit", "--output-dir", str(RUNTIME_ROOT / "playwright"), "--save-session"]
    if allowed_origins:
        command += ["--allowed-origins", ";".join(allowed_origins)]
    mcp = spawn("playwright-mcp", command, RUNTIME_ROOT)
    wait_until(lambda: port_open(mcp_port), mcp, "playwright-mcp")
    return {"cdp_url": f"http://127.0.0.1:{cdp_port}", "mcp_url": f"http://127.0.0.1:{mcp_port}/mcp"}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    apps = sub.add_parser("start-apps")
    apps.add_argument("apps", nargs="+")
    browser = sub.add_parser("start-browser")
    browser.add_argument("--url", required=True)
    browser.add_argument("--allowed-origin", action="append", default=[])
    args = parser.parse_args()
    result = start_apps(args.apps) if args.command == "start-apps" else start_browser(args.url, args.allowed_origin)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
