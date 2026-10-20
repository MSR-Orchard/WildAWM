#!/usr/bin/env python3
"""Boot a WebHarbor container for a world, load its ``webharbor/<dbfile>`` seeds, and render pages."""
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from wildawm.const_config import (
    DOCKER_COMMAND_TIMEOUT_S,
    HTTP_PROBE_TIMEOUT_S,
    HTTP_SHORT_TIMEOUT_S,
    RUNTIME_SHORT_TIMEOUT_S,
    WEBHARBOR_CTRL_PORT,
)
from simpleArgParser import parse_args_with_commands
from wildawm.webharbor import lib as wh


def container_name(world_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in world_id)
    return f"wildawm-creator-{safe}"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("timeout", DOCKER_COMMAND_TIMEOUT_S)
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def is_running(name: str) -> bool:
    r = _run(["docker", "inspect", "-f", "{{.State.Running}}", name])
    return r.returncode == 0 and r.stdout.strip() == "true"


def _exec_py(
        name: str, code: str,
        timeout: int = DOCKER_COMMAND_TIMEOUT_S) -> tuple[int, str, str]:
    r = _run(["docker", "exec", name, "python3", "-c", code], timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def control_health(name: str) -> dict | None:
    code = (
        "import urllib.error,urllib.request,sys\n"
        "try:\n"
        f" r=urllib.request.urlopen('http://127.0.0.1:{WEBHARBOR_CTRL_PORT}/health',"
        f"timeout={HTTP_PROBE_TIMEOUT_S})\n"
        " sys.stdout.write(r.read().decode())\n"
        "except urllib.error.HTTPError as e:\n"
        " body=e.read().decode()\n"
        " sys.stdout.write(body if body else 'ERR:HTTP '+str(e.code))\n"
        "except Exception as e:\n"
        " sys.stdout.write('ERR:'+str(e))\n"
    )
    rc, out, _ = _exec_py(name, code)
    if rc == 0 and out and not out.startswith("ERR:"):
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"raw": out}
    return None


def wait_ready(name: str, timeout: int = RUNTIME_SHORT_TIMEOUT_S) -> bool:
    """Wait until the control plane is up and reports all sites alive."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running(name):
            time.sleep(1)
            continue
        h = control_health(name)
        if h:
            # Accept once /health responds and a strong majority of sites are alive.
            sites = h.get("sites") or h
            try:
                alive = sum(1 for v in sites.values()
                            if (isinstance(v, dict) and v.get("alive")) or v in (True, "alive", "ok"))
                if alive >= 12:
                    return True
            except AttributeError:
                return True  # unfamiliar response shape, but it responded
        time.sleep(2)
    return False


def boot(world_path: Path | str, *, image: str = wh.WEBHARBOR_IMAGE,
         name: str | None = None, fresh: bool = False,
         ready_timeout: int = RUNTIME_SHORT_TIMEOUT_S,
         load_sites: list[str] | None = None,
         extra_run_args: list[str] | None = None) -> str:
    """Ensure a WebHarbor container runs for ``world_path`` with its seeds loaded; returns its name."""
    world_path = Path(world_path).resolve()
    name = name or container_name(world_path.name)

    if fresh and is_running(name):
        teardown(name)

    if not is_running(name):
        _run(["docker", "rm", "-f", name])
        r = _run([
            "docker", "run", "-d", "--name", name,
            "--label", "wildawm-creator=1",
            *(extra_run_args or []),
            image,
        ])
        if r.returncode != 0:
            raise RuntimeError(f"docker run failed: {r.stderr.strip()}")
        if not wait_ready(name, ready_timeout):
            raise RuntimeError(
                f"webharbor container {name} did not become ready; "
                f"logs: {_run(['docker','logs','--tail','40',name]).stdout}")

    load_and_reset(name, world_path, sites=load_sites)
    return name


def load_and_reset(name: str, world_path: Path | str,
                   sites: list[str] | None = None) -> list[str]:
    """Copy the world's ``webharbor/<dbfile>`` seeds into the container and reset those sites."""
    world_path = Path(world_path).resolve()
    whdir = wh.webharbor_dir(world_path)
    candidates = sites or wh.all_sites()
    loaded: list[str] = []
    for s in candidates:
        dbfile = wh.dbfile_for_site(s)
        src = whdir / dbfile
        if not src.exists():
            continue
        wh._apply_seed_patches(s, src)
        dest = f"{name}:/opt/WebSyn/{s}/instance_seed/{dbfile}"
        r = _run(["docker", "cp", str(src), dest])
        if r.returncode != 0:
            raise RuntimeError(f"docker cp seed for {s} failed: {r.stderr.strip()}")
        reset_site(name, s)
        loaded.append(s)
    return loaded


def reset_site(name: str, site: str) -> bool:
    """Reset a site from instance_seed/ and respawn it; True on a 2xx response."""
    code = (
        "import urllib.request,sys\n"
        f"req=urllib.request.Request('http://127.0.0.1:{WEBHARBOR_CTRL_PORT}/reset/{site}',method='POST')\n"
        "try:\n"
        f" r=urllib.request.urlopen(req,timeout={HTTP_SHORT_TIMEOUT_S});"
        " sys.stdout.write(str(r.status))\n"
        "except urllib.error.HTTPError as e:\n"
        " sys.stdout.write('HTTP'+str(e.code))\n"
        "except Exception as e:\n"
        " sys.stdout.write('ERR:'+str(e))\n"
    )
    rc, out, _ = _exec_py(name, code, timeout=HTTP_SHORT_TIMEOUT_S)
    ok = rc == 0 and out.strip().startswith("2")
    time.sleep(1.5)
    return ok


def restart_site(name: str, site: str) -> bool:
    """Respawn a site's Flask worker to pick up patched code, keeping its DB; True on 2xx."""
    code = (
        "import urllib.request,sys\n"
        f"req=urllib.request.Request('http://127.0.0.1:{WEBHARBOR_CTRL_PORT}/restart/{site}',method='POST')\n"
        "try:\n"
        f" r=urllib.request.urlopen(req,timeout={HTTP_SHORT_TIMEOUT_S});"
        " sys.stdout.write(str(r.status))\n"
        "except urllib.error.HTTPError as e:\n"
        " sys.stdout.write('HTTP'+str(e.code))\n"
        "except Exception as e:\n"
        " sys.stdout.write('ERR:'+str(e))\n"
    )
    rc, out, _ = _exec_py(name, code, timeout=HTTP_SHORT_TIMEOUT_S)
    ok = rc == 0 and out.strip().startswith("2")
    time.sleep(1.5)
    return ok


def fetch(
        name: str, site: str, path: str = "/",
        timeout: int = HTTP_SHORT_TIMEOUT_S) -> tuple[int, str]:
    """GET a site path from inside the container; returns (status, body)."""
    port = wh.port_for_site(site)
    if not path.startswith("/"):
        path = "/" + path
    code = (
        "import urllib.request,sys\n"
        f"u='http://127.0.0.1:{port}{path}'\n"
        "try:\n"
        f" r=urllib.request.urlopen(u,timeout={HTTP_SHORT_TIMEOUT_S})\n"
        " sys.stdout.write(str(r.status)+'\\n'+r.read().decode('utf-8','replace'))\n"
        "except urllib.error.HTTPError as e:\n"
        " sys.stdout.write(str(e.code)+'\\n'+e.read().decode('utf-8','replace'))\n"
        "except Exception as e:\n"
        " sys.stdout.write('0\\nERR:'+str(e))\n"
    )
    rc, out, err = _exec_py(name, code, timeout=timeout + 10)
    if rc != 0:
        return 0, f"EXEC_ERR:{err.strip()}"
    status, _, body = out.partition("\n")
    try:
        return int(status), body
    except ValueError:
        return 0, body


def login_and_fetch(name: str, site: str, *, path: str,
                    email: str, password: str,
                    login_path: str = "/login",
                    email_field: str = "email",
                    password_field: str = "password",
                    timeout: int = HTTP_SHORT_TIMEOUT_S) -> tuple[int, str]:
    """Log in with a cookie jar, then GET ``path`` as that user; returns (status, body)."""
    port = wh.port_for_site(site)
    if not path.startswith("/"):
        path = "/" + path
    # Fetch the login page first for the WTForms csrf_token and session cookie, as a browser would.
    code = (
        "import urllib.request,urllib.parse,http.cookiejar,sys,re\n"
        f"base='http://127.0.0.1:{port}'\n"
        "cj=http.cookiejar.CookieJar()\n"
        "op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),\n"
        "    urllib.request.HTTPRedirectHandler())\n"
        "try:\n"
        f" lg=op.open(base+'{login_path}',timeout={HTTP_SHORT_TIMEOUT_S}).read().decode('utf-8','replace')\n"
        " m=re.search(r'name=\"csrf_token\"[^>]*value=\"([^\"]+)\"',lg)\n"
        " tok=m.group(1) if m else ''\n"
        f" fields={{'{email_field}':{email!r},'{password_field}':{password!r}}}\n"
        " if tok: fields['csrf_token']=tok\n"
        " data=urllib.parse.urlencode(fields).encode()\n"
        f" op.open(base+'{login_path}',data=data,timeout={HTTP_SHORT_TIMEOUT_S})\n"
        f" r=op.open(base+'{path}',timeout={HTTP_SHORT_TIMEOUT_S})\n"
        " sys.stdout.write(str(r.status)+'\\n'+r.read().decode('utf-8','replace'))\n"
        "except urllib.error.HTTPError as e:\n"
        " sys.stdout.write(str(e.code)+'\\n'+e.read().decode('utf-8','replace'))\n"
        "except Exception as e:\n"
        " sys.stdout.write('0\\nERR:'+str(e))\n"
    )
    rc, out, err = _exec_py(name, code, timeout=timeout + 10)
    if rc != 0:
        return 0, f"EXEC_ERR:{err.strip()}"
    status, _, body = out.partition("\n")
    try:
        return int(status), body
    except ValueError:
        return 0, body


def query_db(name: str, site: str, sql: str) -> str:
    """Run a read-only SQL query against the live instance DB inside the container."""
    dbfile = wh.dbfile_for_site(site)
    code = (
        "import sqlite3,sys,json\n"
        f"c=sqlite3.connect('/opt/WebSyn/{site}/instance/{dbfile}')\n"
        "try:\n"
        f" rows=c.execute({sql!r}).fetchall()\n"
        " sys.stdout.write(json.dumps(rows, default=str))\n"
        "except Exception as e:\n"
        " sys.stdout.write('ERR:'+str(e))\n"
    )
    _, out, _ = _exec_py(name, code)
    return out


def teardown(name: str) -> None:
    _run(["docker", "rm", "-f", name])


def teardown_all() -> int:
    r = _run(["docker", "ps", "-aq", "--filter", "label=wildawm-creator=1"])
    ids = [x for x in r.stdout.split() if x]
    if ids:
        _run(["docker", "rm", "-f", *ids])
    return len(ids)


@dataclass
class BootArgs:
    world: str
    fresh: bool = False


@dataclass
class FetchArgs:
    world: str
    site: str
    path: str = "/"


@dataclass
class ReloadArgs:
    world: str
    sites: list[str] = field(default_factory=list)


@dataclass
class DownArgs:
    world: str = ""
    all: bool = False


def _main() -> None:
    command, args = parse_args_with_commands({
        "boot": BootArgs,
        "fetch": FetchArgs,
        "reload": ReloadArgs,
        "down": DownArgs,
    }, description="WebHarbor boot/render control")
    if command == ("boot",):
        name = boot(args.world, fresh=args.fresh)
        print(f"booted {name}")
        h = control_health(name)
        print("health:", json.dumps(h)[:200] if h else None)
    elif command == ("fetch",):
        name = boot(args.world)
        status, body = fetch(name, args.site, args.path)
        print(f"HTTP {status}  ({len(body)} bytes)")
        sys.stdout.write(body[:4000])
    elif command == ("reload",):
        name = container_name(Path(args.world).resolve().name)
        loaded = load_and_reset(name, args.world, sites=args.sites or None)
        print("reloaded:", loaded)
    elif command == ("down",):
        if args.all:
            print(f"removed {teardown_all()} creator containers")
        else:
            if not args.world:
                raise SystemExit("--world is required unless --all is set")
            teardown(container_name(Path(args.world).resolve().name))
            print("down")


if __name__ == "__main__":
    _main()
