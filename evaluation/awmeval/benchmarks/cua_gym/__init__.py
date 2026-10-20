from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

from huggingface_hub import snapshot_download

from ...harness.opencode import remote_mcp, run_agent, vision_mcp
from ...sandbox import endpoint, open_sandbox
from ..base import Benchmark

HERE = Path(__file__).parent
SUBSET = json.loads((HERE / "tasks.json").read_text(encoding="utf-8"))
APP_NAMES = (
    "discord_mock", "facebook_mock", "github_mock", "gitlab_mock", "gmail_mock", "google_calendar_mock", "google_docs_mock", "google_drive_mock",
    "google_sheets_mock", "hubspot_mock", "instacart_mock", "instagram_mock", "jira_mock", "linkedin_mock", "microsoft_teams_mock", "monday_mock",
    "notion_mock", "outlook_web_mock", "pinterest_mock", "postman_mock", "reddit_mock", "salesforce_mock", "shopify_admin_mock", "slack_mock",
    "stripe_dashboard_mock", "trello_mock", "twitter_mock", "uber_eats_mock", "wechat_mock",
)
APP_PORTS = {name: 18080 + index for index, name in enumerate(APP_NAMES)}
TEXT_SUFFIXES = {".json", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"}
REWARD = re.compile(r"(?:REWARD|FINAL_REWARD|SCORE)\s*:\s*([01](?:\.\d+)?)", re.IGNORECASE)
CONTROL_PATTERNS = (
    (re.compile(r"https?://[^/\s'\"`|;]+/(?:go|post|state|upload)(?:[?#'\"\s]|$)", re.IGNORECASE), "control-plane route"),
    (re.compile(r"\b(?:fetch|requests\.(?:get|post|put|delete)|urllib\.request\.(?:urlopen|Request))\s*\(\s*[fFrR]*['\"`]\s*/(?:go|post|state|upload)(?:[?#'\"`\s]|$)", re.IGNORECASE), "control-plane route"),
    (re.compile(r"https?://[^/\s'\"`|;]+/files(?:[/?#'\"\s]|$)", re.IGNORECASE), "control-plane file route"),
    (re.compile(r"/(?:run/cua-task|run/cua-control|opt/cua-gym-hub)(?:/|\b)", re.IGNORECASE), "protected benchmark path"),
    (re.compile(r"(?:reward\.py|initial_setup\.py|task_web_sid|reward_judge\.py)", re.IGNORECASE), "protected benchmark artifact"),
    (re.compile(r"(?:/v1/models|(?:127\.0\.0\.1|localhost):[0-9]+/v1(?:/|\b))", re.IGNORECASE), "direct model endpoint access"),
)
BROWSER_STORAGE = re.compile(r"\b(?:localStorage|sessionStorage|indexedDB|document\.cookie)\b", re.IGNORECASE)
BROWSER_USE_STORAGE = re.compile(r"/usr/local/bin/browser-use\s+(?:eval|python|cookies?|get\s+cookies?)\b", re.IGNORECASE)
EVAL_TOOLS = {"browser_browser_evaluate", "browser_browser_run_code", "browser_browser_run_code_unsafe"}
BROWSER_USE_GUARD = """#!/bin/sh
set -eu
for argument in "$@"; do
    case "$argument" in
        eval|python|cookies|cookie) echo "browser storage and arbitrary browser evaluation are disabled" >&2; exit 64 ;;
    esac
done
exec /usr/local/bin/browser-use "$@"
"""
WORLD = """# CUA-Gym Web evaluation

Complete the user's task autonomously through the local mock websites. The tested model is text-only.

## Browser tools

There is no general priority between the available browser, shell, and file interfaces; choose based on the operation's required capability and existing session state. Browser state, cookies, and login sessions are not automatically shared between browser-use and Playwright MCP, so keep one stateful web flow in the same browser interface.

- Use the `browser-use` CLI through bash for interactive browser work: `browser-use open URL`, `browser-use state`, `browser-use click INDEX`, `browser-use input INDEX TEXT`, `browser-use keys KEY`, `browser-use scroll down --amount 800`, and `browser-use screenshot /tmp/name.png`.
- The Playwright MCP server is available through `browser_browser_*` tools and starts at the task URL. Snapshot element references are passed in the tools' `target` field.
- The tested model is text-only. If visual evidence is needed, save a screenshot under `/tmp` or `/workspace`, then call `vision_describe_image` with the absolute image path and a focused question.
- Standard OpenCode shell and filesystem tools are available.

## Evaluation controls

Use only visible website UI for task actions. Do not access CUA-Gym control-plane routes such as `/go`, `/post`, `/state`, `/upload`, or `/files`. Do not inspect or modify localStorage, sessionStorage, IndexedDB, cookies, task setup files, reward files, protected runtime files, or model/judge APIs. These restrictions are enforced from the saved trajectory.

## Local websites

{websites}

Initial task URL: {start_url}
"""


def materialize(task_dir: Path, apps: list[str]) -> bytes:
    """Task bundle as tar.gz with `__CUA_GYM_<APP>_URL__` placeholders bound to the local app ports."""
    values = {"__CUA_GYM_MOCK_APP_URL_TEMPLATE__": "http://127.0.0.1:{mock_name}"}
    for app, port in APP_PORTS.items():
        token = app.removesuffix("_mock").upper()
        for alias in {token, app.upper()}:
            values[f"__CUA_GYM_{alias}_URL__"] = f"http://127.0.0.1:{port}"
            values[f"__CUA_GYM_{alias}_HOST__"] = f"127.0.0.1:{port}"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for source in sorted(task_dir.iterdir()):
            if source.name.startswith("._") or source.name == ".DS_Store" or not source.is_file():
                continue
            data = source.read_bytes()
            if source.suffix in TEXT_SUFFIXES:
                text = data.decode("utf-8")
                for placeholder, value in values.items():
                    text = text.replace(placeholder, value)
                unresolved = sorted(set(re.findall(r"__CUA_GYM_[A-Z0-9_]+__", text)))
                if unresolved:
                    raise ValueError(f"unresolved URL placeholders in {source.name}: {unresolved}")
                data = text.encode()
            info = tarfile.TarInfo(source.name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def violations(events_path: Path) -> list[dict]:
    """Benchmark-control violations (control-plane routes, browser storage, protected files) found in the trajectory."""
    found = []
    for number, line in enumerate(events_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        try:
            part = json.loads(line).get("part") or {}
        except (ValueError, AttributeError):
            continue
        state = part.get("state") if part.get("type") == "tool" and isinstance(part.get("state"), dict) else None
        if state is None:
            continue
        tool, value = str(part.get("tool") or ""), state.get("input")
        text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value or "")
        exit_code = (state.get("metadata") or {}).get("exit")
        if state.get("status") == "error" or (isinstance(exit_code, int) and exit_code != 0):
            continue
        match = BROWSER_USE_STORAGE.search(text)
        if match is None and tool in EVAL_TOOLS:
            match = BROWSER_STORAGE.search(text)
            if match is None:
                found.append({"line": number, "tool": tool, "rule": "arbitrary browser evaluation", "excerpt": text[:200]})
        if match is not None:
            found.append({"line": number, "tool": tool, "rule": "browser storage", "excerpt": text[max(0, match.start() - 80):match.end() + 120]})
        for pattern, label in CONTROL_PATTERNS:
            if match := pattern.search(text):
                found.append({"line": number, "tool": tool, "rule": label, "excerpt": text[max(0, match.start() - 80):match.end() + 120]})
    return found


class CUAGym(Benchmark):
    name = "cua_gym"
    metric = "mean_score"

    def prepare(self) -> None:
        root = Path(self.options.data_dir)
        if (root / "tasks").is_dir():
            return
        cache = Path(snapshot_download("xlangai/CUA-Gym", repo_type="dataset", revision=SUBSET["revision"], allow_patterns=["artifacts/cua_gym_tasks_v1.tar.zst"], token=self.options.get("hf_token") or None))
        (root / "tasks").mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "--zstd", "-xf", str(cache / "artifacts" / "cua_gym_tasks_v1.tar.zst"), "-C", str(root / "tasks")], check=True)

    def tasks(self) -> list[dict]:
        return SUBSET["tasks"]

    def reward(self, sb, out: Path, phase: str) -> float:
        result = sb.exec(["python3", "reward.py"], cwd="/run/cua-task", timeout=int(self.options.reward_timeout))
        output = result.stdout + "\n" + result.stderr
        (out / f"reward-{phase}.log").write_text(output, encoding="utf-8")
        scores = REWARD.findall(output)
        if result.exit_code != 0 or not scores:
            raise RuntimeError(f"{phase} reward failed: {output[-500:]}")
        return float(scores[-1])

    def run_task(self, task: dict, out: Path) -> dict:
        apps = task["apps"]
        task_dir = Path(self.options.data_dir) / "tasks" / task["id"]
        instruction = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))["instruction"]
        with open_sandbox(self.cfg, self.options.image, name=task["id"][:8]) as sb:
            sb.write("/tmp/cua-task.tgz", materialize(task_dir, apps))
            sb.upload(HERE / "runtime.py", "/tmp/awmeval/cua_gym_runtime.py")
            sb.exec("install -d -m 700 /run/cua-task && tar -xzf /tmp/cua-task.tgz -C /run/cua-task && install -d -o agent -g agent /workspace"
                    f" && python3 /tmp/awmeval/cua_gym_runtime.py start-apps {' '.join(apps)}", timeout=300).check("start CUA-Gym apps")
            setup = sb.exec(["python3", "/run/cua-task/initial_setup.py"], timeout=int(self.options.setup_timeout), env={"CUA_GYM_START_URL_FILE": "/tmp/cua-gym-start-url.txt"})
            (out / "setup.log").write_text(setup.stdout + "\n" + setup.stderr, encoding="utf-8")
            setup.check("CUA-Gym initial setup")
            sid = sb.read("/tmp/task_web_sid").decode().strip()
            start_url = f"http://127.0.0.1:{APP_PORTS[apps[0]]}/?sid={sid}"
            origins = " ".join(f"--allowed-origin http://127.0.0.1:{APP_PORTS[app]}" for app in apps)
            sb.exec(f"python3 /tmp/awmeval/cua_gym_runtime.py start-browser --url '{start_url}' {origins}", timeout=180).check("start browser")
            baseline = self.reward(sb, out, "baseline")
            if baseline > 1e-9:
                raise RuntimeError(f"task baseline reward is {baseline}, expected 0")
            sb.write("/opt/cua-gym-shims/browser-use", BROWSER_USE_GUARD, mode=0o755)
            sb.exec("chmod -R go-rwx /run/cua-task /opt/cua-gym-hub", timeout=600).check("protect task files")
            websites = "\n".join(f"- `{app}`: http://127.0.0.1:{APP_PORTS[app]}/?sid={sid}" for app in apps)
            prompt = instruction.strip() + "\n\nComplete the task fully through the website UI, then provide a concise final response."
            mcp = {"browser": remote_mcp("http://localhost:8931/mcp", self.cfg), "vision": vision_mcp(self.cfg, endpoint(sb, self.cfg.vision))}
            agent = run_agent(sb, self.cfg, task_text=prompt, prompt=prompt, workdir="/workspace", out=out / "agent", mcp=mcp, user="agent",
                              instructions=WORLD.format(websites=websites, start_url=start_url), env={"PATH": "/opt/cua-gym-shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                              disabled_tools=tuple(sorted(EVAL_TOOLS)))
            raw = self.reward(sb, out, "final")
        found = violations(out / "agent" / "events.jsonl")
        return {"score": 0.0 if found else raw, "raw_reward": raw, "violations": found, "apps": apps, "turns": agent["turns"], "stop_reason": agent["stop_reason"]}
