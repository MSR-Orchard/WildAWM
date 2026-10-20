from __future__ import annotations

import ast
import hashlib
import io
import json
import logging
import shutil
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ...harness.opencode import COMPLETION, CONTINUATION, REMOTE, install, opencode_config, spec, vision_mcp
from ...sandbox import STOP, SandboxError, endpoint, open_sandbox
from ...sandbox.base import RUN_DIR
from ..base import Benchmark

log = logging.getLogger("awmeval")
HERE = Path(__file__).parent
RELEASE = "v2026.08.08"
TASKS_REPO = "xlangai/osworld_v2_tasks"
ASSETS_REPO = "xlangai/osworld_v2_assets_gated"
TASK_HASHES_SHA256 = "42f8f6f8939b8712997d5891456a575f8a2a5f53465e9e3e6747af5d6efd0915"
UTILS_URL = f"https://raw.githubusercontent.com/xlang-ai/OSWorld-V2/{RELEASE}/evaluation_examples/task_class/generated_task_utils.py"
UTILS_SHA256 = "cf0945bc4cd15253631f41dfcd96ca0874116523f07fc34989cce2f23df1bbb0"
TASK_IDS = [f"{index:03d}" for index in range(1, 109)]
GITLAB_TASKS = {"026", "041"}
CLIENT_PASSWORD = "osworld-public-evaluation"
SOURCE = "/opt/osworld-v2"
PYTHON = f"{SOURCE}/.venv/bin/python"
TASK_DIR = "/run/awmeval-task"
ASSETS = "/run/osworld-assets"
NATIVE = "/run/awmeval-native"
WORKSPACE = "/home/awmeval-agent/workspace"
AGENT_USER = "awmeval-agent"
AUX_MCP_TIMEOUT_MS = 240_000
POLL_SECONDS = 30


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def asset_references(task_path: Path, task_id: str) -> tuple[set[str], bool]:
    """Literal `asset(...)` paths referenced by a task class, plus whether any reference is dynamic."""
    references, dynamic = set(), False
    for node in ast.walk(ast.parse(task_path.read_text(encoding="utf-8"), filename=str(task_path))):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "asset" or not node.args:
            continue
        argument, rendered = node.args[0], None
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            rendered = argument.value
        elif isinstance(argument, ast.JoinedStr):
            rendered = ""
            for item in argument.values:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    rendered += item.value
                elif isinstance(item, ast.FormattedValue) and isinstance(item.value, ast.Name) and item.value.id in {"TASK_ID", "task_id"}:
                    rendered += task_id
                else:
                    rendered = None
                    break
        if rendered is None:
            dynamic = True
        else:
            references.add(rendered.lstrip("/"))
    return references, dynamic


def tar_dirs(directories: list[Path]) -> bytes:
    """Uncompressed tar of whole directories owned by root (media assets do not compress)."""
    def owned_by_root(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        return info

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for directory in directories:
            archive.add(directory, arcname=directory.name, filter=owned_by_root)
    return buffer.getvalue()


class OSWorld2(Benchmark):
    name = "osworld2"
    metric = "mean_score"

    def prepare(self) -> None:
        from huggingface_hub import snapshot_download

        logging.getLogger("httpx").setLevel(logging.WARNING)
        token = self.options.get("hf_token") or None
        tasks_dir, assets_dir = Path(self.options.tasks_dir), Path(self.options.assets_dir)
        if self.task_problems(tasks_dir):
            with tempfile.TemporaryDirectory() as temporary:
                snapshot = Path(snapshot_download(TASKS_REPO, repo_type="dataset", revision=RELEASE, allow_patterns=["task_*.py", "manifests/task_hashes.json"], local_dir=temporary, token=token))
                if sha256(snapshot / "manifests" / "task_hashes.json") != TASK_HASHES_SHA256:
                    raise RuntimeError(f"{TASKS_REPO}@{RELEASE} task hash manifest does not match the release pin")
                tasks_dir.mkdir(parents=True, exist_ok=True)
                for path in snapshot.glob("task_*.py"):
                    shutil.copy2(path, tasks_dir / path.name)
                shutil.copy2(snapshot / "manifests" / "task_hashes.json", tasks_dir / "task_hashes.json")
        utils = tasks_dir / "generated_task_utils.py"
        if not utils.is_file() or sha256(utils) != UTILS_SHA256:
            data = urllib.request.urlopen(UTILS_URL, timeout=120).read()
            if hashlib.sha256(data).hexdigest() != UTILS_SHA256:
                raise RuntimeError(f"unexpected content from {UTILS_URL}")
            utils.write_bytes(data)
        problems = self.task_problems(tasks_dir)
        if problems:
            raise RuntimeError(f"OSWorld v2 task classes failed verification: {problems[:5]}")
        snapshot_download(ASSETS_REPO, repo_type="dataset", revision=RELEASE, local_dir=str(assets_dir), token=token)
        log.info("OSWorld v2 %s task classes in %s, assets in %s", RELEASE, tasks_dir, assets_dir)

    @staticmethod
    def task_problems(tasks_dir: Path, task_ids=TASK_IDS) -> list[str]:
        manifest = tasks_dir / "task_hashes.json"
        if not manifest.is_file() or sha256(manifest) != TASK_HASHES_SHA256:
            return [f"missing or modified {manifest}"]
        files = json.loads(manifest.read_text(encoding="utf-8"))["files"]
        problems = []
        for task_id in task_ids:
            path, expected = tasks_dir / f"task_{task_id}.py", files.get(f"task_{task_id}.py") or {}
            if not path.is_file() or path.stat().st_size != expected.get("size") or sha256(path) != expected.get("sha256"):
                problems.append(f"task_{task_id}.py")
        return problems

    def tasks(self) -> list[dict]:
        return [{"id": task_id} for task_id in TASK_IDS]

    def task_inputs(self, task_id: str) -> tuple[Path, Path, list[Path]]:
        """Verified task class, shared task utilities, and the task-scoped asset directories."""
        tasks_dir, assets_dir = Path(self.options.tasks_dir), Path(self.options.assets_dir)
        problems = self.task_problems(tasks_dir, [task_id])
        utils = tasks_dir / "generated_task_utils.py"
        if problems or not utils.is_file():
            raise FileNotFoundError(f"task {task_id} inputs are missing or modified under {tasks_dir} ({problems or ['generated_task_utils.py']}); run `awmeval prepare osworld2`")
        task_file = tasks_dir / f"task_{task_id}.py"
        references, dynamic = asset_references(task_file, task_id)
        missing = sorted(item for item in references if not (assets_dir / item).exists() and not (assets_dir / unquote(item)).exists())
        if dynamic and not (assets_dir / f"task_{task_id}").exists():
            missing.append(f"task_{task_id}/ (dynamic asset reference)")
        if missing:
            raise FileNotFoundError(f"task {task_id} assets are missing from {assets_dir}: {missing[:8]}; run `awmeval prepare osworld2`")
        return task_file, utils, sorted(path for path in assets_dir.glob(f"task_{task_id}*") if path.is_dir())

    def gitlab(self, sb) -> dict:
        """GitLab route for tasks 026/041: an address the sandbox and the QEMU guest can both reach, plus its external host."""
        url, token = self.options.get("gitlab_url"), self.options.get("gitlab_token")
        external = urlsplit(url)
        reach = urlsplit(sb.url(self.options.get("gitlab_sandbox_url") or url))
        port = reach.port or (443 if reach.scheme == "https" else 80)
        resolved = sb.exec(["getent", "ahostsv4", reach.hostname], timeout=60).stdout.split()
        if not resolved:
            raise RuntimeError(f"cannot resolve GitLab host {reach.hostname} inside the sandbox")
        address = resolved[0]
        if address.startswith("127."):
            address = next((item for item in sb.exec(["hostname", "-I"], timeout=60).stdout.split() if "." in item and not item.startswith("127.")), None)
            if address is None:
                raise RuntimeError(f"cannot find a non-loopback sandbox address for GitLab host {reach.hostname}")
        return {"url": f"{reach.scheme}://{address}:{port}", "hostname": external.hostname, "port": external.port or (443 if external.scheme == "https" else 80), "token": str(token)}

    def stage(self, sb, task_id: str, task_file: Path, utils: Path, asset_dirs: list[Path]) -> None:
        sb.exec(f"set -e; install -d -m 700 {TASK_DIR} {ASSETS}; install -d -m 711 {NATIVE}; rm -rf {NATIVE}/* {ASSETS}/*", timeout=900).check("prepare task directories")
        task_class = f"{SOURCE}/evaluation_examples/task_class"
        sb.write(f"{task_class}/task_{task_id}.py", task_file.read_bytes(), mode=0o600)
        sb.write(f"{task_class}/generated_task_utils.py", utils.read_bytes(), mode=0o600)
        if asset_dirs:
            sb.write(f"{TASK_DIR}/assets.tar", tar_dirs(asset_dirs))
            sb.exec(f"tar -C {ASSETS} -xf {TASK_DIR}/assets.tar && rm -f {TASK_DIR}/assets.tar", timeout=1800).check(f"extract task {task_id} assets")
        # Staged files are root-owned already; a recursive chown would copy every image file up into the overlay.
        sb.exec(f"chmod 700 {TASK_DIR} {ASSETS} {SOURCE}/evaluation_examples {SOURCE}/desktop_env/evaluators", timeout=900).check("protect task inputs")

    def inner_config(self, sb, task_id: str, executable: str, config_home: str | None) -> dict:
        cfg, options = self.cfg, self.options
        base_url = endpoint(sb, cfg.agent)
        vision = vision_mcp(cfg, endpoint(sb, cfg.vision))
        vision["timeout"] = AUX_MCP_TIMEOUT_MS
        opencode = opencode_config(cfg, base_url=base_url, mcp={"vision": vision}, instructions=["/workspace/EVALUATION.md"])
        if config_home:
            opencode["plugin"] = [f"{REMOTE}/output_guard.mjs"]
        runner = spec(cfg, opencode=executable, config="", workdir=WORKSPACE, out="", task_text="", marker=f"{WORKSPACE}/.awmeval-complete.md", base_url=base_url,
                      config_home=config_home, user=AGENT_USER, prompt="-", nudge="-")
        judge = cfg.judge
        return {
            "task_id": task_id, "output": NATIVE, "source_dir": SOURCE, "assets_dir": ASSETS, "disk_path": "/System.qcow2", "workspace": WORKSPACE,
            "agent_user": AGENT_USER, "client_password": CLIENT_PASSWORD,
            "osworld": {
                "website_host_suffix": options.website_host_suffix, "guest_ram": options.guest_ram, "guest_cpus": int(options.guest_cpus),
                "qemu_startup_timeout": int(options.qemu_startup_timeout), "startup_wait_seconds": float(options.startup_wait_seconds),
                "settle_seconds": float(options.settle_seconds), "phase_setup_wait_seconds": float(options.phase_setup_wait_seconds),
                "record_video": bool(options.record_video), "respect_task_tool_constraints": bool(options.respect_task_tool_constraints),
                "max_actions": options.get("max_actions"), "default_volume_size": options.get("default_volume_size"),
                "environment": dict(options.get("environment") or {}), "mcp_timeout_ms": int(cfg.opencode.mcp_timeout_ms),
            },
            "judge": {"model": str(judge.model), "base_url": endpoint(sb, judge), "api_key": str(judge.api_key), "max_output_tokens": int(judge.max_output_tokens), "reasoning_effort": judge.get("reasoning_effort")},
            "gitlab": self.gitlab(sb) if task_id in GITLAB_TASKS else None,
            "unctad_forward": options.get("unctad_forward") if task_id == "011" else None,
            "opencode": opencode, "runner": runner, "prompts": {"completion": COMPLETION, "continuation": CONTINUATION},
        }

    def execute(self, sb) -> int:
        """Run the in-sandbox executor detached and poll it, so no single exec has to outlive the trial."""
        script = (f"cd {SOURCE} && PYTHONPATH={REMOTE}:{SOURCE} PYTHONDONTWRITEBYTECODE=1 {PYTHON} -P -m osworld2.executor {TASK_DIR}/config.json"
                  f" > {NATIVE}/inner.log 2>&1; echo $? > {NATIVE}/exit_code.tmp && mv {NATIVE}/exit_code.tmp {NATIVE}/exit_code")
        sb.spawn(script, name="osworld2")
        budget = int(self.cfg.run.task_timeout) + int(self.options.qemu_startup_timeout) + 10800
        deadline, failures = time.monotonic() + budget, 0
        probe = f"cat {NATIVE}/exit_code 2>/dev/null || {{ kill -0 \"$(cat {RUN_DIR}/logs/osworld2.pid)\" 2>/dev/null && echo running; }} || echo gone"
        while True:
            if STOP.wait(POLL_SECONDS):
                raise SandboxError("evaluation is stopping")
            try:
                state = sb.exec(probe, timeout=120).stdout.strip()
                failures = 0
            except Exception:
                failures += 1
                if failures >= 10:
                    raise
                continue
            if state == "running":
                if time.monotonic() > deadline:
                    sb.exec(f"kill -TERM -- -\"$(cat {RUN_DIR}/logs/osworld2.pid)\"; sleep 20; kill -KILL -- -\"$(cat {RUN_DIR}/logs/osworld2.pid)\"", timeout=120)
                    raise TimeoutError(f"OSWorld v2 executor exceeded its {budget}s budget")
                continue
            if state == "gone" or not state.lstrip("-").isdigit():
                raise RuntimeError(f"OSWorld v2 executor exited without a status ({state[-200:]!r})")
            return int(state)

    @staticmethod
    def collect(sb, task_id: str, out: Path) -> None:
        """Copy the trial artifacts to `out`, minus re-creatable agent caches."""
        prune = (f"pkill -KILL -u {AGENT_USER}; find {NATIVE} \\( -type s -o -type p \\) -delete;"
                 f" find {NATIVE} -depth -type d \\( -path '*/state/tmp' -o -path '*/state/home/.npm' \\) -exec rm -rf {{}} + ; true")
        try:
            sb.exec(prune, timeout=600)
            sb.download(NATIVE, out)
        except Exception as error:
            log.warning("%s: collecting artifacts failed: %s", task_id, error)

    def run_task(self, task: dict, out: Path) -> dict:
        task_id, options = task["id"], self.options
        if task_id in {str(item) for item in options.get("unavailable_tasks") or ()}:
            raise RuntimeError(f"task {task_id} is listed in benchmarks.osworld2.unavailable_tasks (its external website is unavailable); recorded as an infrastructure error")
        if task_id in GITLAB_TASKS and not (options.get("gitlab_url") and options.get("gitlab_token")):
            raise RuntimeError(f"task {task_id} needs the self-hosted course GitLab: set benchmarks.osworld2.gitlab_url and gitlab_token")
        task_file, utils, asset_dirs = self.task_inputs(task_id)
        with open_sandbox(self.cfg, options.image, name=task_id) as sb:
            restore = sb.exec("/usr/local/bin/restore-osworld-v2-disk", timeout=1800)
            (out / "disk-restore.log").write_text(restore.stdout + restore.stderr, encoding="utf-8")
            restore.check("restore the OSWorld v2 disk")
            self.stage(sb, task_id, task_file, utils, asset_dirs)
            sb.upload(HERE / "inner", f"{REMOTE}/osworld2")
            executable, config_home = install(sb, self.cfg)
            sb.write(f"{TASK_DIR}/config.json", json.dumps(self.inner_config(sb, task_id, executable, config_home), indent=2), mode=0o600)
            try:
                exit_code = self.execute(sb)
            finally:
                self.collect(sb, task_id, out)
        result_path = out / "result.json"
        if not result_path.is_file():
            raise RuntimeError(f"OSWorld v2 executor exited {exit_code} without result.json; see {out / 'inner.log'}")
        record = json.loads(result_path.read_text(encoding="utf-8"))
        if record.get("status") == "infrastructure_error":
            raise RuntimeError(record.get("error") or f"OSWorld v2 executor exited {exit_code}")
        return {
            "score": float(record["score"]),
            "native_status": record["status"],
            "turns": record.get("opencode_turns"),
            "stop_reason": record.get("stop_reason"),
            "completed": record.get("completion_marker"),
            "actions": record.get("actions"),
            "input_tokens": record.get("input_tokens"),
            "output_tokens": record.get("output_tokens"),
            "agent_seconds": record.get("agent_wall_time_seconds"),
            "phases": len(record.get("phase_results") or []) or None,
        }
