"""Shared host-side web harness: Chromium over CDP, Playwright MCP, and OpenCode inside the web image."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path
from urllib.parse import urlsplit

from wildawm_common import DESCRIBE_IMAGE_MCP

from ..harness.opencode import ASSETS, RUNTIME, VISION_NOTE, continuation_prompt, initial_prompt, run_local, vision_mcp
from ..sandbox import LIVE, STOP

GUARDS = Path(__file__).with_name("web_guards")
MCP_CLI = RUNTIME / "node_modules" / "@playwright" / "mcp" / "cli.js"
MARKER = ".awmeval-complete.md"
VIEWPORT = {"width": 1280, "height": 720}
CONTAINER_PATH = "/workspace/bin:/opt/opencode-web/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
RUNNER_ENV = ("HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "OPENCODE_CONFIG", "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_MODELS_FETCH",
              "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER")
INSTRUCTIONS = """Complete the browser task autonomously. The authenticated Chrome browser is already open at the required starting page. {authentication}The `browser-use` CLI through Bash and the `browser_browser_*` Playwright MCP tools are available for website interaction. There is no general priority between these interfaces; choose based on the operation's required capability and current state. In this benchmark both interfaces control the same authenticated Chrome instance identified by `AWMEVAL_CDP_URL`, so do not launch a separate browser or hardcode a CDP port. Verified browser-use forms include `browser-use open URL`, `browser-use state`, `browser-use click INDEX`, `browser-use input INDEX TEXT`, `browser-use keys KEYS`, `browser-use scroll down --amount 800`, `browser-use screenshot PATH`, and `browser-use close`. Playwright snapshot references are passed as the bare `target` value shown by the latest snapshot. `curl` is available for URLs within the benchmark host set, but it does not automatically inherit browser cookies. The tested model is text-only. If visual evidence is needed, save a screenshot and call `vision_describe_image` with the saved absolute path and a focused question. Do not use webfetch or websearch. Do not navigate away from the specified benchmark environment. Starting URL: {start_url}

{vision_note}"""
BROWSER_USE_WRAPPER = """#!/bin/sh
/opt/opencode-web/bin/python /workspace/opencode_browser_use_guard.py --command "$@" || exit $?
exec /opt/opencode-web/bin/browser-use "$@"
"""
PYTHON_ORIGIN_GUARD = """#!/bin/sh
set -eu
guard=/workspace/opencode_browser_use_guard.py
if [ "$#" -eq 0 ] || [ "${1:-}" = "-" ]; then
  source_file=$(mktemp /tmp/python-source.XXXXXX.py)
  trap 'rm -f "$source_file"' EXIT INT TERM
  cat >"$source_file"
  /usr/bin/python3 "$guard" "$source_file"
  exec /usr/bin/python3 "$source_file"
fi
if [ "$1" = "-c" ] && [ "$#" -ge 2 ]; then
  source_file=$(mktemp /tmp/python-source.XXXXXX.py)
  trap 'rm -f "$source_file"' EXIT INT TERM
  printf '%s' "$2" >"$source_file"
  /usr/bin/python3 "$guard" "$source_file"
elif [ "${1#-}" = "$1" ] && [ -f "$1" ]; then
  /usr/bin/python3 "$guard" "$1"
fi
exec /usr/bin/python3 "$@"
"""
PLAYWRIGHT_ORIGIN_GUARD = """const allowedOrigins = new Set(%s);
const resourceOrigins = new Set(%s);
const requireAllowed = (value, base) => {
  const url = new URL(value, base);
  if (!allowedOrigins.has(url.origin) && url.protocol !== 'data:' && url.protocol !== 'about:')
    throw new Error(`Navigation outside the benchmark origin is blocked: ${url.href}`);
};
export default async ({ page }) => {
  const context = page.context();
  const guardFrame = frame => {
    if (frame.__awmevalOriginGuarded) return;
    frame.__awmevalOriginGuarded = true;
    const goto = frame.goto.bind(frame);
    frame.goto = async (value, options) => { requireAllowed(value, frame.url() || [...allowedOrigins][0]); return goto(value, options); };
  };
  const guardPage = target => {
    if (target.__awmevalOriginGuarded) return;
    target.__awmevalOriginGuarded = true;
    const goto = target.goto.bind(target);
    target.goto = async (value, options) => { requireAllowed(value, target.url() || [...allowedOrigins][0]); return goto(value, options); };
    for (const frame of target.frames()) guardFrame(frame);
    target.on('frameattached', guardFrame);
  };
  for (const target of context.pages()) guardPage(target);
  context.on('page', guardPage);
  const newPage = context.newPage.bind(context);
  context.newPage = async (...args) => { const target = await newPage(...args); guardPage(target); return target; };
  const newCDPSession = context.newCDPSession.bind(context);
  context.newCDPSession = async (...args) => {
    const client = await newCDPSession(...args);
    const send = client.send.bind(client);
    client.send = async (method, params) => {
      if (method === 'Page.navigate' && params?.url) requireAllowed(params.url, [...allowedOrigins][0]);
      return send(method, params);
    };
    return client;
  };
  await context.route('**/*', async route => {
    try {
      const request = route.request();
      const dependency = !request.isNavigationRequest() && ['GET', 'HEAD'].includes(request.method()) && resourceOrigins.has(new URL(request.url()).origin) && allowedOrigins.has(new URL(request.frame().url()).origin);
      if (!dependency) requireAllowed(request.url(), [...allowedOrigins][0]);
      await route.continue();
    } catch {
      await route.abort('blockedbyclient');
    }
  });
};
"""
# Harness-authored tool errors that mark a policy-deadline timeout as an infrastructure failure.
SERVICE_FAILURE_PATTERNS = (
    "429 too many requests", "http 429", "status 429", "status_code 429", "rate limit exceeded", "too many requests",
    "500 internal server error", "502 bad gateway", "503 service unavailable", "504 gateway timeout", "server error",
    "connection reset", "connection aborted", "remotedisconnected", "read timed out", "timed out waiting", "mcp error -32001",
    "failed to start chrome", "cdp was not ready", "browser session startu", "browser launch", "sandbox lost",
    "sandbox error", "sandbox rpc", "lease heartbeat", "mcp server", "vision mcp", "describe_image",
)
_lock = threading.Lock()
_ready: dict[str, str] = {}


def origin(url: str) -> str | None:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def chromium(options) -> str:
    """Chromium executable: `options.chromium` or the browser bundled with the installed Playwright."""
    if options.chromium:
        return str(options.chromium)
    with _lock:
        if "chromium" not in _ready:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                _ready["chromium"] = playwright.chromium.executable_path
        if not Path(_ready["chromium"]).is_file():
            raise RuntimeError(f"Chromium is missing at {_ready['chromium']}; run `uv run playwright install chromium`")
        return _ready["chromium"]


def ensure_image(image: str) -> None:
    with _lock:
        if _ready.get(image):
            return
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
            pulled = subprocess.run(["docker", "pull", image], capture_output=True, text=True)
            if pulled.returncode != 0:
                raise RuntimeError(f"docker pull {image} failed: {pulled.stderr[-1000:]}")
        _ready[image] = image


def orphan_safe(command: list[str]) -> list[str]:
    """Terminate the child when the spawning thread dies without cleanup."""
    return ["setpriv", "--pdeathsig", "TERM", "--", *command] if shutil.which("setpriv") else command


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
            process.wait(timeout=wait)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            continue


class Browser:
    """Host Chromium over CDP driven by async Playwright on a private loop; owns the episode's containers."""

    def __init__(self, executable: str, *, args: list[str] = ()):
        if STOP.is_set():
            raise RuntimeError("evaluation is stopping")
        self.root = Path(tempfile.mkdtemp(prefix="awmeval-chrome-"))
        self.process = self.loop = self.playwright = self.connection = self.context = self.current = self.mcp = None
        self.sessions, self.label, self.closed, self.lock = {}, uuid.uuid4().hex, False, threading.Lock()
        LIVE.add(self)
        try:
            dirs = {name: self.root / name for name in ("profile", "cache", "home", "config", "data", "tmp", "run", "downloads")}
            for directory in dirs.values():
                directory.mkdir(mode=0o700)
            (dirs["config"] / "user-dirs.dirs").write_text(f'XDG_DOWNLOAD_DIR="{dirs["downloads"]}"\n', encoding="utf-8")
            env = {**os.environ, "HOME": str(dirs["home"]), "TMPDIR": str(dirs["tmp"]), "XDG_CACHE_HOME": str(dirs["cache"]), "XDG_CONFIG_HOME": str(dirs["config"]),
                   "XDG_DATA_HOME": str(dirs["data"]), "XDG_RUNTIME_DIR": str(dirs["run"])}
            command = [executable, "--remote-debugging-port=0", f"--user-data-dir={dirs['profile']}", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                       "--disable-gpu", "--no-sandbox", "--password-store=basic", f"--disk-cache-dir={dirs['cache']}", *args, "--headless=new", "about:blank"]
            with (self.root / "chrome.log").open("wb") as log:
                self.process = subprocess.Popen(orphan_safe(command), stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            port_file, deadline = dirs["profile"] / "DevToolsActivePort", time.monotonic() + 30
            while not (port_file.is_file() and port_file.read_text().strip()):
                if self.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"Chromium CDP did not start: {(self.root / 'chrome.log').read_text(errors='replace')[-2000:]}")
                time.sleep(0.1)
            self.cdp_url = f"http://localhost:{port_file.read_text().split()[0]}"
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=lambda loop: (loop.run_forever(), loop.close()), args=(self.loop,), daemon=True).start()
            self.call(self._connect())
        except BaseException:
            self.close()
            raise

    def call(self, coroutine, timeout: float = 60):
        """Run a coroutine on the browser loop with a hard timeout."""
        return asyncio.run_coroutine_threadsafe(asyncio.wait_for(coroutine, timeout), self.loop).result(timeout + 10)

    async def _connect(self) -> None:
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        self.connection = await self.playwright.chromium.connect_over_cdp(self.cdp_url)
        self.context = self.connection.contexts[0]
        self.context.on("page", self._track)
        for page in self.context.pages:
            self._track(page)
            await self.session(page)

    def _track(self, page) -> None:
        self.current = page

        async def dialog(dialog):  # accept alert/confirm/beforeunload and dismiss prompts
            try:
                await (dialog.dismiss() if dialog.type == "prompt" else dialog.accept())
            except Exception:
                pass

        page.on("dialog", dialog)
        page.on("framenavigated", lambda frame: setattr(self, "current", page) if frame == page.main_frame else None)
        page.on("close", lambda _page: self.sessions.pop(page, None))
        self.sessions[page] = asyncio.ensure_future(self._open_session(page))
        self.sessions[page].add_done_callback(lambda future: future.cancelled() or future.exception())

    async def _open_session(self, page):
        session = await self.context.new_cdp_session(page)
        await session.send("Emulation.setDeviceMetricsOverride", {**VIEWPORT, "deviceScaleFactor": 1, "mobile": False})
        return session

    async def session(self, page):
        """Persistent CDP session of a page; it holds the 1280x720 viewport emulation used for screenshots."""
        if page not in self.sessions:
            self.sessions[page] = asyncio.ensure_future(self._open_session(page))
        return await asyncio.shield(self.sessions[page])

    async def page(self):
        """The page the agent most recently opened or navigated, else the newest open page."""
        pages = self.context.pages
        if self.current in pages:
            return self.current
        return pages[-1] if pages else await self.context.new_page()

    async def screenshot(self, page, path: Path) -> None:
        session = await self.session(page)
        data = await session.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False, "optimizeForSpeed": True})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data["data"]))

    async def state(self, page) -> tuple[str, str]:
        """Page title and visible body text (retried while a navigation replaces the document)."""
        for attempt in range(3):
            try:
                return await page.title(), await page.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(1)

    async def _shutdown(self) -> None:
        for step in (self.connection.close if self.connection else None, self.playwright.stop if self.playwright else None):
            try:
                await step() if step else None
            except Exception:
                pass
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def disconnect(self) -> None:
        if self.loop is None:
            return
        try:
            self.call(self._shutdown(), 30)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop = None

    def stop_agent(self) -> None:
        """Stop the Playwright MCP and remove any agent container of this episode."""
        stop(self.mcp)
        leftover = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=awmeval.web={self.label}"], capture_output=True, text=True).stdout.split()
        if leftover:
            subprocess.run(["docker", "rm", "-f", *leftover], capture_output=True)

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.stop_agent()
            self.disconnect()
            stop(self.process)
            shutil.rmtree(self.root, ignore_errors=True)
        finally:
            LIVE.discard(self)

    def __exit__(self, *_exc) -> None:
        self.close()


def start_mcp(cdp_url: str, workspace: Path, log: Path, *, allowed_origins: list[str] = (), init_page: Path | None = None, viewport: bool = False) -> tuple[subprocess.Popen, str]:
    """Host Playwright MCP attached to the shared browser; it saves files inside the agent workspace."""
    if shutil.which("node") is None or not MCP_CLI.is_file():
        raise RuntimeError(f"Playwright MCP needs node and {MCP_CLI}; run scripts/setup_opencode.sh")
    command = ["node", str(MCP_CLI), "--port", "0", "--host", "127.0.0.1", "--cdp-endpoint", cdp_url, "--shared-browser-context", "--image-responses", "omit",
               "--output-dir", str(workspace / ".playwright-mcp"), "--save-session"]
    if allowed_origins:
        command += ["--allowed-origins", ";".join(allowed_origins)]
    if init_page:
        command += ["--init-page", str(init_page)]
    if viewport:
        command += ["--viewport-size", f"{VIEWPORT['width']}x{VIEWPORT['height']}"]
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(orphan_safe(command), cwd=workspace, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if match := re.search(r"Listening on http://[^:\s]+:(\d+)", log.read_text(errors="replace")):
            return process, f"http://localhost:{match.group(1)}/mcp"
        if process.poll() is not None:
            break
        time.sleep(0.1)
    stop(process)
    raise RuntimeError(f"Playwright MCP did not start: {log.read_text(errors='replace')[-2000:]}")


def write_guards(workspace: Path, *, python_guard: bool) -> None:
    """Origin and host guards for curl, browser-use, and (WebArena) python inside the agent container."""
    tools = workspace / "bin"
    tools.mkdir(parents=True, exist_ok=True)
    for name in ("opencode_curl_guard.py", "opencode_browser_use_guard.py"):
        shutil.copyfile(GUARDS / name, workspace / name)
    scripts = {"curl": '#!/bin/sh\nexec python3 /workspace/opencode_curl_guard.py "$@"\n', "browser-use": BROWSER_USE_WRAPPER}
    if python_guard:
        scripts["python3"] = PYTHON_ORIGIN_GUARD
        (tools / "python").symlink_to("python3")
    for name, text in scripts.items():
        (tools / name).write_text(text, encoding="utf-8")
        (tools / name).chmod(0o755)


def start_container(name: str, *, image: str, workspace: Path, out: Path, env: dict, label: str) -> None:
    """Long-lived read-only agent container on the host network; OpenCode runs in it via docker exec."""
    # The host Playwright MCP reports saved files relative to the client root (/workspace), i.e. as ../<host workspace path>/...
    mounts = [(workspace, "/workspace", "rw"), (workspace, workspace, "ro"), (out / "state", out / "state", "rw"), (out / "opencode.json", out / "opencode.json", "ro"),
              (ASSETS, ASSETS, "ro"), (RUNTIME / "config", RUNTIME / "config", "ro"), (RUNTIME / "bin" / "opencode", "/usr/local/bin/opencode", "ro")]
    command = ["docker", "run", "-d", "--name", name, "--label", f"awmeval.web={label}", "--network", "host", "--init", "--user", f"{os.getuid()}:{os.getgid()}",
               "--read-only", "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m", "-w", "/workspace"]
    for source, target, mode in mounts:
        command += ["-v", f"{source}:{target}:{mode}"]
    for key, value in {**env, "PATH": CONTAINER_PATH}.items():
        command += ["-e", f"{key}={value}"]
    started = subprocess.run([*command, "--entrypoint", "sleep", image, "infinity"], capture_output=True, text=True, timeout=1800)
    if started.returncode != 0:
        raise RuntimeError(f"agent container did not start: {started.stderr[-1000:]}")


def write_wrapper(path: Path, *, container: str, workspace: Path) -> Path:
    """`opencode` wrapper running the pinned binary in the episode container (SIGTERM propagates)."""
    variables = " ".join(f"-e {key}" for key in RUNNER_ENV)
    path.write_text(
        "#!/bin/bash\npidfile=/tmp/awmeval-opencode-$$.pid\nargs=()\n"
        f'for value in "$@"; do if [[ "$value" == {shlex.quote(str(workspace))} ]]; then value=/workspace; fi; args+=("$value"); done\n'
        f"trap 'docker exec {container} sh -c \"kill -TERM \\$(cat $pidfile) 2>/dev/null\"; exit 143' INT TERM\n"
        f"docker exec -w /workspace {variables} {container} sh -c 'echo $$ > \"$0\"; exec /usr/local/bin/opencode \"$@\"' \"$pidfile\" \"${{args[@]}}\" &\n"
        "wait $!\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def trajectory(events: list[dict], observations: dict[str, dict]) -> tuple[list[dict], str]:
    """Steps for completed tool calls that have an observation, plus the text after the last step."""
    steps, pending = [], []
    for event in events:
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if part.get("type") in ("text", "reasoning") and isinstance(part.get("text"), str):
            pending.append(part["text"])
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        if part.get("type") != "tool" or state.get("status") not in ("completed", "error"):
            continue
        observation = observations.get(str(part.get("callID") or part.get("id") or ""))
        if observation is None:
            continue
        tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
        error = state.get("error") if state.get("status") == "error" else None
        steps.append({
            "step_number": len(steps) + 1,
            "action": {"model_output": json.dumps({"thinking": "".join(pending).strip(), "action": [{str(part.get("tool") or "unknown"): tool_input}]}, ensure_ascii=False),
                       "results": [json.dumps({"error": error, "extracted_content": state.get("output")}, ensure_ascii=False, default=str)]},
            "observation": observation,
        })
        pending = []
    return steps, "".join(pending).strip()


def done_step(steps: list[dict], thinking: str, final_text: str, observation: dict) -> dict:
    return {"step_number": len(steps) + 1, "observation": observation,
            "action": {"model_output": json.dumps({"thinking": thinking, "action": [{"done": {"text": final_text, "success": True}}]}, ensure_ascii=False),
                       "results": [json.dumps({"is_done": True, "success": True, "extracted_content": final_text}, ensure_ascii=False)]}}


def run_episode(bench, out: Path, browser: Browser, *, task_text: str, start_url: str, allowed_hosts: str, allowed_origins: list[str] = (),
                resource_origins: tuple[str, ...] = (), recover=None, viewport: bool = False) -> tuple[dict, list[dict], dict]:
    """One OpenCode episode against the shared host browser, capturing a screenshot after every completed tool call."""
    cfg, options = bench.cfg, bench.options
    ensure_image(options.image)
    gitignore = RUNTIME / "config" / "opencode" / ".gitignore"  # OpenCode creates it on first start, but the container mounts the config home read-only
    if not gitignore.exists():
        gitignore.write_text("\n".join(("node_modules", "package.json", "package-lock.json", "bun.lock", ".gitignore")), encoding="utf-8")
    workspace = out / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    write_guards(workspace, python_guard=bool(allowed_origins))
    init_page = None
    if allowed_origins:
        init_page = out / "playwright_origin_guard.ts"
        init_page.write_text(PLAYWRIGHT_ORIGIN_GUARD % (json.dumps(list(allowed_origins)), json.dumps(list(resource_origins))), encoding="utf-8")
    browser.mcp, mcp_url = start_mcp(browser.cdp_url, workspace, out / "playwright-mcp.log", allowed_origins=[*allowed_origins, *resource_origins], init_page=init_page, viewport=viewport)
    env = {"AWMEVAL_CDP_URL": browser.cdp_url, "AWMEVAL_START_URL": start_url, "BROWSER_USE_CONFIG_DIR": str(out / "state" / "browser-use" / "config"),
           "ANONYMIZED_TELEMETRY": "false", "PLAYWRIGHT_BROWSERS_PATH": "/opt/pw-browsers", "AWMEVAL_ALLOWED_HOSTS": allowed_hosts, "AWMEVAL_ALLOWED_ORIGINS": ",".join(allowed_origins)}
    container = f"awmeval-web-{re.sub(r'[^a-z0-9]+', '-', out.name.lower())[-24:].strip('-')}-{browser.label[:8]}"
    wrapper = write_wrapper(out / "opencode", container=container, workspace=workspace)
    observations, capture = {}, {"skipped": 0, "error": None}

    async def observe(path: Path) -> dict:
        extra = await recover(browser) if recover else {}
        page = await browser.page()
        await browser.screenshot(page, path)
        return {"screenshot_path": str(path), "url": page.url, "blocked_url": None, **extra}

    def on_event(event: dict) -> None:
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if part.get("type") != "tool" or (part.get("state") or {}).get("status") not in ("completed", "error"):
            return
        if capture["error"]:
            capture["skipped"] += 1
            return
        try:
            observations[str(part.get("callID") or part.get("id") or len(observations))] = browser.call(observe(out / "screenshots" / f"{len(observations):04d}.png"), 30)
        except Exception as error:  # a stuck page must not stall the event reader; later tool calls keep no observation
            capture["error"] = f"{type(error).__name__}: {error}"

    try:
        for name in ("home", "data", "cache", "config"):
            (out / "state" / name).mkdir(parents=True, exist_ok=True)
        (out / "opencode.json").touch()
        # OpenCode runs inside the episode container; `out/state` is mounted there 1:1 rw.
        vision_server = out / "state" / "vision_mcp.py"
        shutil.copyfile(DESCRIBE_IMAGE_MCP, vision_server)
        start_container(container, image=options.image, workspace=workspace, out=out, env=env, label=browser.label)
        mcp = {"browser": {"type": "remote", "url": mcp_url, "enabled": True, "timeout": int(cfg.opencode.mcp_timeout_ms)},
               "vision": vision_mcp(cfg, cfg.vision.base_url, ["/usr/bin/python3", str(vision_server)])}
        instructions = workspace / "EVALUATION.md"  # inside the agent workspace and read-only
        instructions.write_text(INSTRUCTIONS.format(authentication=bench.authentication, start_url=start_url, vision_note=VISION_NOTE).rstrip() + "\n", encoding="utf-8")
        instructions.chmod(0o444)
        result = run_local(cfg, opencode=str(wrapper), workdir=workspace, out=out, task_text=task_text, mcp=mcp, instruction_files=[instructions.name],
                           marker=str(workspace / MARKER), on_event=on_event, prompt=initial_prompt(task_text, MARKER), nudge=continuation_prompt(task_text, MARKER))
    finally:
        browser.stop_agent()
    steps, thinking = trajectory(result["events"], observations)
    if result.get("turns", 0) == 0 and result.get("stop_reason") != "timeout":
        raise RuntimeError("OpenCode produced an empty trajectory")
    return result, steps, {"captured": len(observations), "skipped": capture["skipped"], "error": capture["error"], "thinking": thinking}


def infrastructure_evidence(steps: list[dict]) -> list[str]:
    """Service failures recorded by the harness in tool results (never agent text or page content)."""
    found = []
    for step in steps:
        for item in (step.get("action") or {}).get("results") or []:
            try:
                message = str(json.loads(item).get("error") or "")
            except (ValueError, AttributeError):
                continue
            hit = next((pattern for pattern in SERVICE_FAILURE_PATTERNS if pattern in message.lower()), None) if message and message != "None" else None
            if hit:
                found.append(f"{hit}: {message[:140]}")
    return found


def chat(section, body: dict) -> dict:
    """POST /chat/completions with two retries on connection errors, 408, 409, 429, and 5xx (the OpenAI client policy)."""
    request = urllib.request.Request(section.base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {section.api_key}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=int(section.request_timeout)) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            if attempt == 2 or not (error.code in (408, 409, 429) or error.code >= 500):
                raise JudgeError(error.code, detail) from error
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
        time.sleep(min(8.0, 0.5 * 2 ** attempt))


class JudgeError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail[:1000]}")
        self.status, self.detail = status, detail


def message_text(response: dict) -> str:
    return ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def png_top(data: bytes, height: int) -> bytes:
    """Top `height` rows of a non-interlaced PNG (rows keep their filters because each only references the row above)."""
    chunks, offset = [], 8
    while offset < len(data):
        length, kind = struct.unpack(">I4s", data[offset:offset + 8])
        chunks.append((kind, data[offset + 8:offset + 8 + length]))
        offset += 12 + length
    header = chunks[0][1]
    width, depth, color = struct.unpack(">I", header[:4])[0], header[8], header[9]
    stride = 1 + (width * {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color] * depth + 7) // 8
    pixels = zlib.decompress(b"".join(chunk for kind, chunk in chunks if kind == b"IDAT"))[:stride * height]
    pack = lambda kind, chunk: struct.pack(">I", len(chunk)) + kind + chunk + struct.pack(">I", zlib.crc32(kind + chunk) & 0xFFFFFFFF)
    extra = b"".join(pack(kind, chunk) for kind, chunk in chunks if kind not in (b"IHDR", b"IDAT", b"IEND"))
    return data[:8] + pack(b"IHDR", header[:4] + struct.pack(">I", height) + header[8:]) + extra + pack(b"IDAT", zlib.compress(pixels)) + pack(b"IEND", b"")


def png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        raise ValueError("not a PNG image")
    return struct.unpack(">II", data[16:24])
