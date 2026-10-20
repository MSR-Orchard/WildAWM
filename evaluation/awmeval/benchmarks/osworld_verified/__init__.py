from __future__ import annotations

import json
import time
from pathlib import Path

from ...harness.opencode import REMOTE, run_agent, vision_mcp
from ...sandbox import endpoint, open_sandbox
from ..base import Benchmark

HERE = Path(__file__).parent
SUBSET = json.loads((HERE / "tasks.json").read_text(encoding="utf-8"))
ROWS = {row["metadata"]["osworld_id"]: row for row in SUBSET["tasks"]}
TASK_DIR = "/run/awmeval-task"
GROUND_TRUTH_PYTHON = "/opt/awmeval-ground-truth/venv/bin/python"
AGENT_PYTHON = "/opt/awmeval-agent/venv/bin/python"
ACTION_LOG = "/tmp/lite-osworld-actions.jsonl"
CDP_URL = "http://127.0.0.1:1337"
RESULT_PREFIX = "AWMEVAL_LITE_OSWORLD_RESULT="
SESSION = {"DISPLAY": ":1", "HOME": "/home/user", "XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
DESKTOP_ENV = {"VNC_RESOLUTION": "1920x1080", "VNC_PW": "password", "VNCOPTIONS": "-disableBasicAuth"}
START_DESKTOP = """set -euo pipefail
if ! pgrep -x supervisord >/dev/null; then nohup setsid /usr/bin/supervisord -c /etc/supervisor/supervisord.conf </dev/null >/tmp/lite-osworld-supervisord.log 2>&1 & fi
for i in $(seq 1 300); do
  [ ! -f /tmp/gnome-failed ] || { cat /tmp/gnome-failed >&2; exit 10; }
  if [ -f /tmp/gnome-ready ] && curl -fsS http://127.0.0.1:5000/accessibility >/dev/null 2>&1; then break; fi
  sleep 1
done
[ -f /tmp/gnome-ready ]
curl -fsS http://127.0.0.1:5000/accessibility >/dev/null
test "$(DISPLAY=:1 /opt/env/bin/xdotool getdisplaygeometry)" = "1920 1080"
runuser -u user -- test ! -r /opt/awmeval-ground-truth
runuser -u user -- test ! -r /opt/awmeval-ground-truth/cua-lite/lite/gym/envs/lite/osworld/data/eval.jsonl
"""
PROMPT = "{instruction}\n\nComplete the task fully, verify the final state, call osworld_done, and provide a concise final response."
WORLD = """# Lite.OSWorld evaluation

Complete the user's task in the current 1920x1080 GNOME desktop. This is the evaluated LiteCUA Lite.OSWorld container.

## Tools

There is no general priority between the available desktop, browser, shell, and file interfaces; choose based on the operation's required capability and existing session state.

- OpenCode's built-in bash and filesystem tools run as the evaluated desktop user in this same container. HOME is `/home/user`. You may use them when direct shell work faithfully satisfies the instruction.
- The tested policy model is text-only. If visual evidence is needed, use `osworld_screenshot`, then `vision_describe_image` with the returned PNG path before choosing visual coordinates.
- Use the `osworld_*` mouse, keyboard, accessibility, wait, done, and fail tools for visible desktop interaction.
- For Chrome tasks, the `browser-use` CLI and `browser_browser_*` Playwright MCP tools attach to the evaluated visible Chrome session when its CDP endpoint is active. They change the same browser state scored by Lite.OSWorld. Use `browser-use state` or `browser_browser_snapshot` before semantic clicks. For Playwright tools, pass only the bare exact reference token shown by the latest snapshot as `target` (for example `target="e52"` for `[ref=e52]`) and put the human-readable label in `element`; never pass `ref=e52`, the complete snapshot line, or prose as `target`. Playwright key chords use names such as `Control+H`.
- Chrome, LibreOffice, GIMP, VLC, Thunderbird, VS Code, and the standard Lite.OSWorld desktop applications are installed.

## Evaluation controls

Do not inspect benchmark task definitions, evaluator source, expected artifacts, `/opt/awmeval-ground-truth`, or `/run/awmeval-task`. Those protected paths are unreadable to the desktop user. Do not call hidden setup/evaluator control routes to infer expected state. The Lite.OSWorld evaluator is the sole source of reward.

Call `osworld_done` only after every requested state is complete and verified. Call `osworld_fail` only when the task is genuinely infeasible.
"""


def chrome_page(sb) -> str | None:
    """URL of the first web page when the evaluated Chrome exposes CDP ('' without one), else None."""
    done = sb.exec(["curl", "-fsS", "--max-time", "10", f"{CDP_URL}/json"], timeout=30)
    if not done.ok and done.exit_code not in range(1, 100):  # curl's own errors mean no CDP; others are sandbox failures
        raise RuntimeError(f"Chrome CDP probe failed (exit {done.exit_code}): {done.stderr[-500:]}")
    try:
        targets = json.loads(done.stdout) if done.ok else None
    except json.JSONDecodeError:
        targets = None
    if not isinstance(targets, list):
        return None
    pages = [target for target in targets if isinstance(target, dict) and target.get("type") == "page" and not str(target.get("url") or "").startswith("chrome://")]
    return str(pages[0].get("url") or "") if pages else ""


class OSWorldVerified(Benchmark):
    name = "osworld_verified"
    metric = "mean_score"

    def tasks(self) -> list[dict]:
        return [{"id": task_id, "domain": row["metadata"]["others"]["domain"]} for task_id, row in ROWS.items()]

    def evaluator(self, sb, out: Path, command: str, timeout: int) -> dict:
        done = sb.exec([GROUND_TRUTH_PYTHON, f"{TASK_DIR}/evaluator.py", command, "--task", f"{TASK_DIR}/task.json"], timeout=timeout,
                       env={"EASYOCR_MODULE_PATH": "/opt/awmeval-ground-truth/easyocr"})
        (out / f"{command}.log").write_text(done.stdout + "\n" + done.stderr, encoding="utf-8")
        lines = [line[len(RESULT_PREFIX):] for line in done.stdout.splitlines() if line.startswith(RESULT_PREFIX)]
        result = json.loads(lines[-1]) if done.ok and lines else {}
        if result.get("status") != "ok":
            raise RuntimeError(f"Lite.OSWorld {command} failed (exit {done.exit_code}): {(done.stderr or done.stdout)[-1000:]}")
        return result

    def run_task(self, task: dict, out: Path) -> dict:
        row, options = ROWS[task["id"]], self.options
        mcp_timeout = int(self.cfg.opencode.mcp_timeout_ms)
        with open_sandbox(self.cfg, options.image, name=task["id"][:8]) as sb:
            sb.exec(START_DESKTOP, timeout=360, env=DESKTOP_ENV).check("start Lite.OSWorld desktop")
            sb.exec(f"install -d -m 700 {TASK_DIR} && install -d -m 700 -o user -g user /workspace", timeout=60).check("create task directories")
            sb.write(f"{TASK_DIR}/task.json", json.dumps(row, ensure_ascii=False) + "\n", mode=0o600)
            sb.write(f"{TASK_DIR}/evaluator.py", (HERE / "evaluator.py").read_bytes(), mode=0o600)
            self.evaluator(sb, out, "setup", int(options.setup_timeout))
            page = chrome_page(sb)
            sb.write(f"{REMOTE}/osworld_desktop_mcp.py", (HERE / "desktop_mcp.py").read_bytes(), mode=0o644)
            mcp = {
                "osworld": {"type": "local", "command": [AGENT_PYTHON, f"{REMOTE}/osworld_desktop_mcp.py"], "enabled": True, "timeout": mcp_timeout,
                            "environment": {**SESSION, "AWMEVAL_WORKSPACE": "/workspace", "AWMEVAL_ACTION_LOG": ACTION_LOG}},
                "vision": vision_mcp(self.cfg, endpoint(sb, self.cfg.vision)),
            }
            env = {**SESSION, "XDG_CONFIG_HOME": "/home/user/.config", "PLAYWRIGHT_BROWSERS_PATH": "/opt/pw-browsers", "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
            if page is not None:
                mcp["browser"] = {"type": "local", "enabled": True, "timeout": mcp_timeout, "environment": {"HOME": "/home/user", "PLAYWRIGHT_BROWSERS_PATH": "/opt/pw-browsers"},
                                  "command": ["/usr/local/bin/node", "/usr/local/lib/node_modules/@playwright/mcp/cli.js", "--cdp-endpoint", CDP_URL, "--shared-browser-context",
                                              "--image-responses", "omit", "--output-dir", "/workspace/playwright", "--save-session"]}
                env.update(AWMEVAL_CDP_URL=CDP_URL, BU_CDP_URL=CDP_URL, **({"AWMEVAL_START_URL": page} if page else {}))
            sb.write("/workspace/WORLD.md", WORLD, mode=0o444)
            sb.exec("chown user:user /workspace/WORLD.md", timeout=60).check("own WORLD.md")
            prompt = PROMPT.format(instruction=str(row["instruction"]).strip())
            agent = run_agent(sb, self.cfg, task_text=prompt, prompt=prompt, workdir="/workspace", out=out / "agent", mcp=mcp, instruction_files=["/workspace/WORLD.md"],
                              user="user", env=env)
            time.sleep(float(options.settle_seconds))
            result = self.evaluator(sb, out, "evaluate", int(options.reward_timeout))
            if sb.exists(ACTION_LOG):
                sb.download(ACTION_LOG, out / "actions.jsonl")
        score = float(result["score"])
        if not 0.0 <= score <= 1.0:
            raise RuntimeError(f"Lite.OSWorld evaluator returned score outside [0, 1]: {score}")
        return {"score": score, "domain": task["domain"], "browser_cdp": page is not None, "turns": agent["turns"], "stop_reason": agent["stop_reason"], "completed": agent["completed"]}
