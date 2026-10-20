#!/usr/bin/env python3
"""Provision clean day worlds for task synthesis."""
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.core.paths import INSTANCES_DIR, PROJECT_ROOT
from wildawm.synthesis import harness
from wildawm.synthesis.tasks import claude_runner as cr
from wildawm.synthesis.tasks import world_patch as wp
from simpleArgParser import parse_args
from wildawm.webharbor import lib as wh

ROOT = PROJECT_ROOT
COMPOSE = ROOT / "docker-compose.unified.yml"
# Host Claude Code credentials, copied into the agent container for claude-code runs.
HOST_CRED = Path(os.environ.get(
    "WILDAWM_CLAUDE_CREDENTIALS",
    str(Path.home() / ".claude" / ".credentials.json"))).expanduser()
AGENT_HOME = "/home/agent"


@dataclass
class ProvisionArgs:
    cmd: str
    name: str
    profile: str = ""
    day: int = 1
    no_agent: bool = False
    world_patch: str = ""

STD_HASH = "$2b$12$jrXZNk07IJem7HFWOqRXmuoBjGy1dOxwtO/XdxU81zuLSmV4rJ4.y"


def project_name(name: str) -> str:
    return f"wildawm-{name}"


def synthesis_agent_kind() -> str:
    """Container profile for sketch/generator: claude-code if either role uses it, else opencode."""
    if (harness.resolve_harness("sketch") == "claude-code"
            or harness.resolve_harness("generator") == "claude-code"):
        return "claude-code"
    return "opencode"


def agent_container(name: str, kind: str | None = None) -> str:
    return f"wildawm-agent-{kind or synthesis_agent_kind()}-{name}"


def webharbor_container(name: str) -> str:
    return f"wildawm-webharbor-{name}"


def instance_dir(name: str) -> Path:
    return INSTANCES_DIR / name


def snapshot_webharbor_served(name: str, dest: Path) -> list[str]:
    """Copy the WebHarbor container's live `instance/<db>` files to ``dest`` for verifiers."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    c = webharbor_container(name)
    got: list[str] = []
    for site in wh.SITE_ORDER:
        dbfile = wh.dbfile_for_site(site)
        r = subprocess.run(["docker", "cp",
                            f"{c}:/opt/WebSyn/{site}/instance/{dbfile}",
                            str(dest / dbfile)], capture_output=True, text=True,
                           timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
        if r.returncode == 0:
            got.append(dbfile)
    return got


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def _chmod_and_retry(func, target, _exc_info):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_chmod_and_retry)
    if path.exists() and shutil.which("sudo"):
        subprocess.run(["sudo", "rm", "-rf", str(path)], check=False,
                       timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    if path.exists():
        raise RuntimeError(f"failed to remove generated instance dir: {path}")


def provision(profile: str, day: int, name: str, *, with_agent: bool = True,
              fresh: bool = True, world_patch: str | Path | None = None) -> dict:
    """Create a day-N instance + env (+ agent container). Idempotent-ish."""
    if fresh:
        teardown(name)
    info = create_and_start_world(profile, day, name, world_patch=world_patch)
    out = {"name": name, "profile": profile, "day": day,
           "project": project_name(name),
           "instance_dir": str(instance_dir(name)), **info}
    if with_agent:
        out["agent_container"] = ensure_agent(name)
        out["prelogin"] = prelogin(name)
    return out


def _dashboard_request(method: str, path: str, body: dict | None = None,
                       timeout: int = cfg.HTTP_MEDIUM_TIMEOUT_S) -> dict:
    url = cfg.SYNTHESIS_DASHBOARD.rstrip("/") + path
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        raise RuntimeError(
            f"dashboard {method} {path} failed HTTP {e.code}: {raw}") from e
    return json.loads(raw) if raw else {}


def create_and_start_world(profile: str, day: int, name: str,
                           webharbor: bool = True,
                           world_patch: str | Path | None = None) -> dict:
    """Create a named world through the synthesis dashboard and start it."""
    patch_dir = wp.resolve_patch_dir(world_patch) if world_patch else None
    body = {"profile": profile, "day": day, "name": name}
    if patch_dir:
        body["world_patch"] = str(patch_dir)
    created = _dashboard_request(
        "POST", "/api/worlds",
        body,
        timeout=cfg.RUNTIME_PROVISION_TIMEOUT_S,
    )
    world_id = created.get("id") or name
    runtime = _dashboard_request(
        "POST", f"/api/worlds/{world_id}/runtime/start",
        {"enable_webharbor": bool(webharbor)},
        timeout=cfg.RUNTIME_PROVISION_TIMEOUT_S,
    )
    return {
        "world": world_id,
        "mcp_port": runtime.get("mcp_port"),
        "webharbor_port": runtime.get("webharbor_port"),
        "worldctl_port": runtime.get("worldctl_port"),
    }


def ensure_agent(name: str) -> str:
    """Bring up the synthesis agent container for the resolved harness."""
    kind = synthesis_agent_kind()
    c = agent_container(name, kind)
    if kind == "claude-code" and not cfg.OPENAI_HOST_PROXY_URL:
        raise RuntimeError(
            "claude-code synthesis agent needs the host-side OpenAI-compatible "
            "proxy endpoint: set WILDAWM_OPENAI_HOST_PROXY_URL, e.g. "
            f"http://{cfg.DOCKER_HOST_GATEWAY}:{cfg.OPENAI_HOST_PROXY_PORT}/v1 "
            "where the proxy listens")
    oc_spec = cfg.model_spec(cfg.OPENCODE_LLM_REF)
    base_url = (cfg.OPENAI_HOST_PROXY_URL if kind == "claude-code"
                else cfg.container_host_url(oc_spec["base_url"]))
    model = oc_spec["api_model"]
    spec_for_reasoning = (
        cfg.model_spec_for_api(base_url, model)
        or (oc_spec if kind == "opencode" else None)
    )
    reasoning_policy = (
        cfg.reasoning_effort_policy(spec_for_reasoning, None, harness=kind)
        if spec_for_reasoning else None
    )
    env = {**os.environ,
           "AWM_HOST_UID": str(os.getuid()), "AWM_HOST_GID": str(os.getgid()),
           "AWM_WORLD_ID": name,
           "AWM_NANOBOT_STATE_DIR": str(cfg.NANOBOT_STATE_DIR),
           "AWM_OPENAI_BASE_URL": base_url,
           "AWM_OPENAI_API_KEY": "not-used",
           "AWM_OPENAI_MODEL": model,
           "OPENAI_BASE_URL": base_url,
           "OPENAI_API_KEY": "not-used", "OPENAI_MODEL": model}
    env.update(cfg.reasoning_effort_env(reasoning_policy))
    env.update(cfg.vision_reasoning_env())
    cr.run_cmd(["docker", "compose", "-f", str(COMPOSE), "-p", project_name(name),
                "--profile", kind, "up", "-d", "--no-deps",
                "--no-recreate", f"agent-{kind}"],
               role="agent_up", env=env, timeout=cfg.RUNTIME_PROVISION_TIMEOUT_S)
    if kind == "claude-code":
        _wait_mcp_registered(c)
        setup_host_auth(c)
    else:
        _wait_browser_mcp(c)
    return c


def _wait_mcp_registered(container: str, timeout: int | None = None) -> None:
    timeout = timeout or cfg.RUNTIME_PROVISION_TIMEOUT_S
    for _ in range(timeout):
        if subprocess.run(["docker", "exec", container, "test", "-f",
                           "/tmp/mcp-registered.flag"],
                          capture_output=True,
                          timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"{container}: MCP registration did not complete")


def _wait_browser_mcp(container: str, timeout: int | None = None) -> None:
    timeout = timeout or cfg.RUNTIME_PROVISION_TIMEOUT_S
    code = (
        "import socket,sys\n"
        "s=socket.socket(); s.settimeout(1)\n"
        f"sys.exit(0 if s.connect_ex(('127.0.0.1',{cfg.BROWSER_MCP_PORT}))==0 else 1)\n"
    )
    for _ in range(timeout):
        if subprocess.run(["docker", "exec", container, "python3", "-c", code],
                          capture_output=True,
                          timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"{container}: browser MCP did not become ready")


def setup_host_auth(container: str) -> None:
    if not HOST_CRED.exists():
        raise RuntimeError(
            f"host Claude credential not found at {HOST_CRED}; point "
            "WILDAWM_CLAUDE_CREDENTIALS at the "
            "Claude Code credentials JSON for the claude-code synthesis "
            "harness")
    u, g = os.getuid(), os.getgid()
    cr.run_cmd(["docker", "exec", container, "sh", "-c",
                f"mkdir -p {AGENT_HOME}/.claude && chown {u}:{g} {AGENT_HOME}/.claude"],
               role="host_auth")
    cr.run_cmd(["docker", "cp", str(HOST_CRED),
                f"{container}:{AGENT_HOME}/.claude/.credentials.json"],
               role="host_auth")
    cr.run_cmd(["docker", "exec", container, "sh", "-c",
                f"chown {u}:{g} {AGENT_HOME}/.claude/.credentials.json && "
                f"chmod 600 {AGENT_HOME}/.claude/.credentials.json"],
               role="host_auth")
    cr.run_cmd(["docker", "exec", "--user", "root", container, "sh", "-c",
                "iptables -P OUTPUT ACCEPT 2>/dev/null; iptables -F OUTPUT 2>/dev/null; "
                "ip6tables -P OUTPUT ACCEPT 2>/dev/null; ip6tables -F OUTPUT 2>/dev/null; true"],
               role="host_auth")


_SITE_DOMAIN = {
    "allrecipes": "www.allrecipes.com", "amazon": "www.amazon.com",
    "apple": "www.apple.com", "arxiv": "arxiv.org", "bbc_news": "www.bbc.com",
    "booking": "www.booking.com", "github": "github.com",
    "google_flights": "flights.google.com", "google_map": "maps.google.com",
    "google_search": "www.google.com", "huggingface": "huggingface.co",
    "wolfram_alpha": "www.wolframalpha.com",
    "cambridge_dictionary": "dictionary.cambridge.org",
    "coursera": "www.coursera.org", "espn": "www.espn.com"}


def _persona_accounts(name: str) -> list[dict]:
    whdir = instance_dir(name) / "webharbor"
    out = []
    for site in wh.SITE_ORDER:
        db = whdir / wh.dbfile_for_site(site)
        if not db.exists() or site not in _SITE_DOMAIN:
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            emails = [e for (e,) in c.execute(
                "SELECT email FROM users WHERE password_hash=?", (STD_HASH,))]
            c.close()
        except sqlite3.DatabaseError:
            continue
        if emails:
            out.append({"domain": _SITE_DOMAIN[site], "email": emails[0],
                        "login_path": "/login"})
    return out


def prelogin(name: str) -> dict:
    """Pre-authenticate the agent's browser and restart Playwright MCP with the storageState."""
    c = agent_container(name)
    accounts = _persona_accounts(name)
    if not accounts:
        return {"accounts": 0}
    helper = Path(__file__).resolve().parent / "_prelogin_in_container.py"
    cr.run_cmd(["docker", "cp", str(helper), f"{c}:/tmp/_prelogin.py"],
               role="prelogin")
    u, g = os.getuid(), os.getgid()
    r = subprocess.run(["docker", "exec", "--user", f"{u}:{g}",
                        "-e", f"HOME={AGENT_HOME}", c,
                        "python3", "/tmp/_prelogin.py", json.dumps(accounts),
                        "/tmp/prelogin-state.json"],
                       capture_output=True, text=True,
                       timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    chrome = subprocess.run(
        ["docker", "exec", c, "sh", "-c",
         "find /opt/pw-browsers -name chrome -type f 2>/dev/null | head -1"],
        capture_output=True, text=True,
        timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S).stdout.strip() \
        or "/opt/pw-browsers/chromium-1223/chrome-linux64/chrome"
    subprocess.run(["docker", "exec", "--user", "root", c, "pkill", "-f",
                    "@playwright/mcp/cli.js"], capture_output=True,
                   timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S)
    time.sleep(1.5)
    cr.run_cmd(["docker", "exec", "-d", "--user", "wildawm-agent",
                "-e", f"HOME={AGENT_HOME}", c,
                "node", "/usr/lib/node_modules/@playwright/mcp/cli.js",
                "--port", str(cfg.BROWSER_MCP_PORT), "--host", "0.0.0.0", "--executable-path", chrome,
                "--headless", "--no-sandbox", "--ignore-https-errors",
                "--isolated", "--storage-state", "/tmp/prelogin-state.json"],
               role="prelogin")
    for _ in range(30):
        if subprocess.run(["docker", "exec", c, "python3", "-c",
                           "import socket,sys;s=socket.socket();"
                           f"s.settimeout({cfg.HTTP_PROBE_TIMEOUT_S});"
                           f"sys.exit(0 if s.connect_ex(('127.0.0.1',{cfg.BROWSER_MCP_PORT}))==0 else 1)"],
                          capture_output=True,
                          timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S).returncode == 0:
            break
        time.sleep(0.5)
    try:
        info = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:                                   # noqa: BLE001
        info = {"raw": r.stdout[-300:], "err": r.stderr[-300:]}
    info["accounts"] = len(accounts)
    return info


def teardown(name: str, *, keep_instance: bool = False) -> None:
    try:
        _dashboard_request("DELETE", f"/api/worlds/{name}", timeout=cfg.RUNTIME_PROVISION_TIMEOUT_S)
    except Exception:
        pass
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), "-p",
                    project_name(name), "--profile", "claude-code",
                    "--profile", "opencode", "--profile", "webharbor",
                    "down", "-v", "--remove-orphans"],
                   capture_output=True, env={**os.environ, "AWM_WORLD_ID": name},
                   timeout=cfg.RUNTIME_SHORT_TIMEOUT_S)
    for kind in ("claude-code", "opencode"):
        subprocess.run(["docker", "rm", "-f", agent_container(name, kind)],
                       capture_output=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    if not keep_instance:
        _remove_tree(instance_dir(name))


if __name__ == "__main__":
    a = parse_args(ProvisionArgs)
    if a.cmd not in {"up", "down"}:
        raise SystemExit("cmd must be 'up' or 'down'")
    if a.cmd == "up" and not a.profile:
        raise SystemExit("--profile is required for up")
    if a.cmd == "up":
        print(json.dumps(provision(a.profile, a.day, a.name,
                                   with_agent=not a.no_agent,
                                   world_patch=a.world_patch or None), indent=2))
    else:
        teardown(a.name)
        print(f"torn down {a.name}")
