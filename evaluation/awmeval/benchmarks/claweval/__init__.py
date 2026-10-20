from __future__ import annotations

import base64
import inspect
import io
import json
import logging
import math
import sys
import tarfile
import time
import uuid
from pathlib import Path

from ...config import ROOT
from ...harness.opencode import AGENT_PROMPT, INTERACTIVE_AGENT_PROMPT, REMOTE, VISION_NOTE, continuation_prompt, initial_prompt, install, opencode_config, spec
from ...sandbox import STOP, Sandbox, SandboxError, endpoint, open_sandbox
from ...sandbox.base import RUN_DIR
from ..base import Benchmark

VENDOR = ROOT / "third_party" / "claw-eval"
TASKS = VENDOR / "tasks"
if str(VENDOR / "src") not in sys.path:
    sys.path.insert(0, str(VENDOR / "src"))

from claw_eval.graders.llm_judge import LLMJudge  # noqa: E402
from claw_eval.graders.registry import get_grader  # noqa: E402
from claw_eval.models.scoring import compute_task_score, is_pass  # noqa: E402
from claw_eval.models.task import TaskDefinition  # noqa: E402
from claw_eval.models.trace import DimensionScores, GradingResult, TraceEnd  # noqa: E402
from claw_eval.runner.deadline import TaskDeadline, TaskDeadlineExceeded  # noqa: E402
from claw_eval.task_requirements import MissingAssetError, resolve_required_assets, sync_official_fixtures  # noqa: E402
from claw_eval.trace.reader import load_trace  # noqa: E402
from claw_eval.trace.writer import TraceWriter  # noqa: E402

log = logging.getLogger("awmeval")
FIXTURE_REPO = "claw-eval/Claw-Eval"
FIXTURE_REVISION = "ca978fd82edb77d52f26f4ccf3f9684a8df84341"
IMAGE_OPENCODE = "/opt/opencode/opencode"
POD_ROOT = "/opt/claw-eval"
POD_AGENT_DIR = "/var/lib/claw-eval-agent"
PROFILE = f"{POD_AGENT_DIR}/profile.json"
RESULT = f"{POD_AGENT_DIR}/result.json"
PROGRESS = f"{POD_AGENT_DIR}/progress.jsonl"
SERVER_URL = "http://127.0.0.1:18080"
SERVER_LOG = "claweval-sandbox-server"
AGENT_LOG = "claweval-pod-agent"
MARKER = ".awmeval-complete.md"
DISABLED_BUILTINS = ("bash", "read", "edit", "glob", "grep")
FETCH_MARGIN = 120.0
POLL_SECONDS = 10
POLL_FAILURES = 6
SERVICE_FIXTURE_MAX_BYTES = 2 * 1024 * 1024
JUDGE_RETRIES = 3
USER_AGENT_RETRIES = 3
STARTUP_FAILURES = ("startup timeout", "no model events", "no events", "failed to inspect opencode", "failed to start opencode")
AGENT_SIDE_FAILURES = ("opencode exited", "taskdeadlineexceeded", "task deadline exceeded", "task deadline exhausted", "max turns", "max_turns", "context exhausted", "context_exhausted")
INSTRUCTIONS = "Complete the CLAWEval task autonomously. The benchmark filesystem and commands are remote: use `sandbox_*` MCP tools for every shell, read, write, edit, glob, grep, download, or media operation required by the task. Use `task_*` MCP tools for task-specific services. There is no general priority between the available remote service interfaces; choose based on the operation's required capability and existing state. Use `vision_describe_image` when image evidence is needed. The local OpenCode `write` tool is reserved only for the completion marker named in the completion protocol. {interaction}\n\n{vision}"
ASK = "If clarification is required, ask it in your final text and wait for the simulated user's next message."
NO_ASK = "Do not ask the user questions."


def agent_texts(task: TaskDefinition) -> tuple[str, str]:
    """Task prompt (with attachment paths) and the EVALUATION.md instructions."""
    attachments = [f"/workspace/{value}" for value in task.prompt.attachments]
    note = "\n\nAttached task files inside the remote sandbox:\n" + "\n".join(f"- {value}" for value in attachments) if attachments else ""
    interaction = ASK if task.user_agent.enabled else NO_ASK
    return task.prompt.text.rstrip() + note, INSTRUCTIONS.format(interaction=interaction, vision=VISION_NOTE)


def agent_side(message: str) -> bool:
    """True when a reported failure means the agent side ended the trial (scored 0, not retried)."""
    text = message.lower()
    return not any(marker in text for marker in STARTUP_FAILURES) and any(marker in text for marker in AGENT_SIDE_FAILURES)


def vision_failure(dispatches) -> str | None:
    """A DescribeImage call that failed in the vision backend (HTTP >= 500) makes the trial unscorable."""
    for dispatch in dispatches:
        if dispatch.tool_name == "DescribeImage" and dispatch.response_status >= 500:
            body = dispatch.response_body
            detail = body.get("error") if isinstance(body, dict) else body
            return f"DescribeImage infrastructure failure ({dispatch.response_status})" + (f": {detail}" if detail else "")
    return None


def put(sb: Sandbox, files: list[tuple[str, Path]]) -> None:
    """Copy local files to absolute sandbox paths through one tar archive."""
    if not files:
        return
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for remote, local in files:
            info = archive.gettarinfo(str(local), arcname=remote.lstrip("/"))
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = 0o644
            with open(local, "rb") as stream:
                archive.addfile(info, stream)
    staged = f"{RUN_DIR}/put-{uuid.uuid4().hex}.tar"
    sb.write(staged, buffer.getvalue())
    sb.exec(f"tar -xf {staged} -C / && rm -f {staged}", timeout=1800).check("unpack task files")


def rpc(sb: Sandbox, path: str, payload: dict, timeout: int) -> dict:
    """POST to the in-pod sandbox server through its bundled rpc.py bridge."""
    token = uuid.uuid4().hex
    request, response = f"/tmp/claw-eval-rpc-{token}.request.json", f"/tmp/claw-eval-rpc-{token}.response.json"
    sb.write(request, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    try:
        sb.exec(["python", "/opt/sandbox/rpc.py", path, request, response, str(float(timeout) + 10)], timeout=int(timeout) + 40, env={"CLAW_EVAL_SANDBOX_URL": SERVER_URL}).check(f"sandbox rpc {path}")
        envelope = json.loads(sb.read(response))
    finally:
        sb.exec(["rm", "-f", request, response], timeout=30)
    if not isinstance(envelope, dict) or type(envelope.get("status_code")) is not int or not isinstance(envelope.get("body"), dict):
        raise SandboxError(f"sandbox rpc {path} returned an invalid envelope")
    return envelope["body"]


def snapshot_read(body: dict) -> dict:
    frames = body.get("frames")
    if frames and not body.get("encoding") and frames[0].get("image_b64"):
        return {"content": frames[0]["image_b64"], "encoding": "base64", "mime_type": frames[0].get("mime_type", "image/png")}
    return body


def save_snapshot(snapshot: dict, folder: Path) -> None:
    """Persist env_snapshot files and command outputs next to the trace."""
    if not snapshot:
        return
    folder.mkdir(parents=True, exist_ok=True)
    index, commands = {"files": [], "commands": []}, []
    for key, entry in sorted(snapshot.items()):
        if key.startswith("file:"):
            path = key[len("file:"):]
            target = folder / Path(path).name
            try:
                if entry.get("encoding") == "base64" and entry.get("content"):
                    target.write_bytes(base64.b64decode(entry["content"]))
                    index["files"].append({"container_path": path, "saved_as": target.name, "size": target.stat().st_size})
                elif entry.get("error"):
                    index["files"].append({"container_path": path, "error": entry["error"]})
                else:
                    target.write_text(entry.get("content", ""), encoding="utf-8")
                    index["files"].append({"container_path": path, "saved_as": target.name})
            except Exception as error:
                index["files"].append({"container_path": path, "error": str(error)})
        elif key.startswith("cmd:"):
            record = {"cmd": key[len("cmd:"):], "exit_code": entry.get("exit_code"), "stdout": entry.get("stdout") or "", "stderr": entry.get("stderr") or "", "error": entry.get("error")}
            commands.append(record)
            index["commands"].append({**record, "stdout": record["stdout"][:2000], "stderr": record["stderr"][:2000]})
    (folder / "snapshot_commands.json").write_text(json.dumps(commands, indent=2, ensure_ascii=False), encoding="utf-8")
    (folder / "snapshot_index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def grade(grader, messages, dispatches, task, *, audit_data, judge, media_events, env_snapshot) -> tuple[DimensionScores, list[dict]]:
    """Call grader.grade with the optional parameters its signature accepts."""
    params = inspect.signature(grader.grade).parameters
    kwargs = {"audit_data": audit_data, "judge": judge}
    if "media_events" in params:
        kwargs["media_events"] = media_events
    if "env_snapshot" in params and env_snapshot is not None:
        kwargs["env_snapshot"] = env_snapshot
    judge.reset_call_log()
    return grader.grade(messages, dispatches, task, **kwargs), judge.get_call_log()


class ClawEval(Benchmark):
    name = "claweval"
    metric = "pass_rate"
    pass_threshold = 0.75

    @property
    def data_dir(self) -> Path:
        return Path(self.options.data_dir)

    def prepare(self) -> None:
        marker = self.data_dir / f".fixtures-{FIXTURE_REVISION}"
        if not marker.is_file():
            report = sync_official_fixtures(FIXTURE_REPO, self.options.get("hf_token") or None, self.data_dir)
            marker.write_text(json.dumps({"revision": report.archive_revision, "sha256": report.archive_sha256, "files": report.extracted_files}), encoding="utf-8")
        missing = [task["id"] for task in self.tasks() if not self.assets_ready(task["id"])]
        log.info("claweval fixtures in %s; tasks with missing assets: %s", self.data_dir / "tasks", missing or "none")

    def assets_ready(self, task_id: str) -> bool:
        try:
            self.assets(TaskDefinition.from_yaml(TASKS / task_id / "task.yaml"))
            return True
        except FileNotFoundError:
            return False

    def tasks(self) -> list[dict]:
        rows = []
        for path in sorted(TASKS.glob("*/task.yaml")):
            task = TaskDefinition.from_yaml(path)
            rows.append({"id": task.task_id, "category": task.category, "user_agent": task.user_agent.enabled, "services": [service.name for service in task.services]})
        return rows

    def assets(self, task: TaskDefinition) -> list:
        try:
            return resolve_required_assets(task, self.data_dir / "tasks" / task.task_id, self.data_dir)
        except MissingAssetError as error:
            raise FileNotFoundError(f"{task.task_id}: missing fixtures {error.paths} under {self.data_dir}; run `awmeval prepare claweval`") from None

    def service_files(self, task: TaskDefinition) -> list[tuple[str, Path]]:
        """Fixtures the in-pod mock services read: env-referenced files plus the task's fixture tree (files up to 2 MiB)."""
        relative = [value for service in task.services for value in (service.env or {}).values() if isinstance(value, str) and value.startswith("tasks/")]
        tree = self.data_dir / "tasks" / task.task_id / "fixtures"
        if tree.is_dir():
            relative += [str(path.relative_to(self.data_dir)) for path in sorted(tree.rglob("*")) if path.is_file() and path.stat().st_size <= SERVICE_FIXTURE_MAX_BYTES]
        return [(f"{POD_ROOT}/{value}", self.data_dir / value) for value in sorted(set(relative)) if (self.data_dir / value).is_file()]

    def start_server(self, sb: Sandbox) -> None:
        sb.upload(VENDOR / "src" / "claw_eval" / "sandbox", "/opt/sandbox")
        sb.spawn(["python", "/opt/sandbox/server.py", "--host", "127.0.0.1", "--port", "18080"], name=SERVER_LOG)
        probe = ["python", "-c", f"import urllib.request; urllib.request.urlopen('{SERVER_URL}/health', timeout=2).read()"]
        for _ in range(60):
            if sb.exec(probe, timeout=30).ok:
                return
            time.sleep(0.5)
        raise SandboxError(f"sandbox server did not become healthy: {sb.log(SERVER_LOG)}")

    def profile(self, sb: Sandbox, task: TaskDefinition, *, runtime: tuple[str, str | None], trace_id: str, budget: float, timeout: int) -> tuple[dict, str]:
        """Pod-agent profile: OpenCode config and runner spec from the core harness plus the task's tools and services."""
        cfg, judge, vision = self.cfg, self.cfg.judge, self.cfg.vision
        executable, config_home = runtime
        task_text, instructions = agent_texts(task)
        interactive = bool(task.user_agent.enabled and judge.get("api_key"))
        base_url = endpoint(sb, cfg.agent)
        artifacts = f"{POD_AGENT_DIR}/artifacts/{task.task_id}"
        config = opencode_config(cfg, base_url=base_url, instructions=["EVALUATION.md"], disabled_tools=DISABLED_BUILTINS, agent_prompt=INTERACTIVE_AGENT_PROMPT if interactive else AGENT_PROMPT)
        if config_home:
            config["plugin"] = [f"{REMOTE}/output_guard.mjs"]
        runner = spec(cfg, opencode=executable, config=f"{artifacts}/opencode.json", workdir="/workspace", out=artifacts, task_text=task_text, marker=f"/workspace/{MARKER}", base_url=base_url,
                      config_home=config_home, prompt=initial_prompt(task_text, MARKER), nudge=continuation_prompt(task_text, MARKER), timeout=int(budget))
        runner["state"] = f"{POD_AGENT_DIR}/state"
        provenance = {"reasoning_effort": cfg.agent.get("reasoning_effort"), "context_window": int(cfg.agent.context_window), "max_output_tokens": int(cfg.agent.max_output_tokens),
                      "request_timeout_seconds": float(cfg.agent.request_timeout), "task_timeout_seconds": timeout, "sandbox_backend": cfg.sandbox.backend, "sandbox_image": self.options.image,
                      "sandbox_cpu": str(cfg.sandbox.cpu), "sandbox_memory": str(cfg.sandbox.memory), "judge_model": judge.model, "user_agent_model": judge.model,
                      "vision_tool_enabled": True, "vision_tool_model": vision.model, "vision_tool_transport": "sandbox_server"}
        profile = {
            "trace_id": trace_id,
            "trace_path": f"{POD_AGENT_DIR}/trace/trace.jsonl",
            "timeout_seconds": budget,
            "sandbox_url": SERVER_URL,
            "model_id": cfg.agent.model,
            "trace_provenance": provenance,
            "vision_tool": {"enabled": True, "model_id": vision.model, "base_url": endpoint(sb, vision), "api_key": str(vision.api_key), "reasoning_effort": vision.get("reasoning_effort"),
                            "request_timeout_seconds": float(vision.request_timeout), "retry_attempts": max(1, int(vision.get("retries", 3)) + 1), "max_output_tokens": int(vision.max_output_tokens)},
            "task": {
                "task_id": task.task_id,
                "task_text": task_text,
                "instructions": instructions,
                "tools": [{"name": tool.name, "canonical_name": tool.name, "description": tool.description, "input_schema": tool.input_schema} for tool in task.tools],
                "endpoint_map": {name: {"url": item.url, "method": item.method} for name, item in task.get_endpoint_map().items()},
                "user_agent": {"enabled": interactive, "persona": task.user_agent.persona, "max_rounds": task.user_agent.max_rounds, "model_id": judge.model, "api_key": str(judge.get("api_key") or ""),
                               "base_url": endpoint(sb, judge), "max_output_tokens": int(judge.max_output_tokens), "request_timeout_seconds": float(judge.request_timeout), "retry_attempts": USER_AGENT_RETRIES},
            },
            "services": [{"name": item.name, "command": item.command, "port": int(item.port), "health_check": item.health_check, "health_check_method": item.health_check_method,
                          "ready_timeout": int(item.ready_timeout), "reset_endpoint": item.reset_endpoint, "env": dict(item.env or {})} for item in task.services],
            "mock_today": task.environment.mock_today,
            "opencode_config": config,
            "runner": runner,
        }
        return profile, artifacts

    def wait_agent(self, sb: Sandbox, seconds: float) -> str:
        check = f"if test -e {RESULT}; then echo done; elif kill -0 $(cat {RUN_DIR}/logs/{AGENT_LOG}.pid) 2>/dev/null; then echo alive; else echo dead; fi"
        limit, failures = time.monotonic() + seconds, 0
        while time.monotonic() < limit:
            if STOP.is_set():
                raise SandboxError("evaluation is stopping")
            time.sleep(POLL_SECONDS)
            try:
                state = sb.exec(check, timeout=60).stdout.strip()
            except Exception as error:
                state = f"poll failed: {error}"
            if state in ("done", "dead"):
                return "done" if state == "done" or sb.exists(RESULT) else "dead"
            failures = 0 if state == "alive" else failures + 1
            if failures >= POLL_FAILURES:
                raise SandboxError(f"cannot read the pod agent state: {state[-500:]}")
        return "timeout"

    def fetch(self, sb: Sandbox, artifacts: str, out: Path) -> None:
        agent = out / "agent"
        try:
            sb.download(artifacts, agent)
        except Exception as error:
            log.warning("cannot download agent artifacts: %s", error)
        agent.mkdir(parents=True, exist_ok=True)
        for remote, name in ((f"{RUN_DIR}/logs/{AGENT_LOG}.log", "entry.log"), (PROGRESS, "progress.jsonl"), (f"{RUN_DIR}/logs/{SERVER_LOG}.log", "sandbox-server.log"),
                             ("/tmp/sandbox-request-log.jsonl", "sandbox-request-log.jsonl"), ("/workspace/EVALUATION.md", "EVALUATION.md")):
            try:
                (agent / name).write_bytes(sb.read(remote))
            except Exception:
                pass

    def episode(self, sb: Sandbox, task: TaskDefinition, deadline: TaskDeadline, timeout: int, out: Path) -> tuple[dict, Path]:
        """Run the pod agent detached, wait for its result, then fetch the trace and append TraceEnd."""
        runtime = install(sb, self.cfg, binary=IMAGE_OPENCODE)
        sb.exec(f"rm -rf {POD_ROOT} && mkdir -p {POD_ROOT}/src {POD_AGENT_DIR}/trace {POD_AGENT_DIR}/artifacts", timeout=120).check("prepare pod directories")
        sb.upload(VENDOR / "src" / "claw_eval", f"{POD_ROOT}/src/claw_eval")
        sb.upload(VENDOR / "mock_services", f"{POD_ROOT}/mock_services")
        put(sb, self.service_files(task))
        remaining = deadline.remaining()
        budget = min(max(60.0, remaining - FETCH_MARGIN), remaining - 1.0)
        if budget <= 60.0:
            raise TaskDeadlineExceeded("task deadline exhausted before in-pod agent start")
        trace_id = str(uuid.uuid4())
        profile, artifacts = self.profile(sb, task, runtime=runtime, trace_id=trace_id, budget=budget, timeout=timeout)
        sb.write(PROFILE, json.dumps(profile, ensure_ascii=False))
        searxng = self.options.get("searxng_url")
        env = {"PYTHONPATH": f"{POD_ROOT}/src:{REMOTE}", "CLAWEVAL_POD_PROFILE": PROFILE, **({"SEARXNG_URL": sb.url(searxng)} if searxng else {"WEB_SEARCH_BACKENDS": "ddgs"})}
        sb.spawn(["python3", "-m", "claw_eval.runner.pod_agent", PROFILE], name=AGENT_LOG, env=env, cwd=POD_AGENT_DIR)
        state = self.wait_agent(sb, budget + FETCH_MARGIN / 2)
        self.fetch(sb, artifacts, out)
        if state != "done":
            tail = sb.log(AGENT_LOG, 2000)
            if state == "timeout":
                raise TimeoutError(f"in-pod agent did not finish within the task deadline: {tail}")
            raise RuntimeError(f"in-pod agent exited without a result: {tail}")
        status = json.loads(sb.read(RESULT))
        trace_path = out / "trace.jsonl"
        try:
            trace_path.write_bytes(sb.read(profile["trace_path"]))
        except Exception as error:
            raise RuntimeError(f"in-pod trace fetch failed ({status.get('error') or error})") from None
        error = status.get("error")
        tokens_in, tokens_out, wall = int(status.get("input_tokens") or 0), int(status.get("output_tokens") or 0), float(status.get("wall_time_s") or 0.0)
        with TraceWriter(trace_path) as writer:
            writer.write_event(TraceEnd(trace_id=trace_id, total_turns=int(status.get("turns") or 0), model_input_tokens=tokens_in, model_output_tokens=tokens_out, input_tokens=tokens_in,
                                        output_tokens=tokens_out, total_tokens=tokens_in + tokens_out, wall_time_s=wall, other_time_s=wall, stop_reason=status.get("stop_reason"),
                                        failure_modes=[error] if error else [], user_agent_rounds=int(status.get("conversation_followups_used") or 0),
                                        user_agent_max_rounds=task.user_agent.max_rounds if task.user_agent.enabled else 0, user_agent_done=bool(status.get("user_agent_done"))))
        return status, trace_path

    def snapshot(self, sb: Sandbox, task: TaskDefinition, deadline: TaskDeadline) -> dict:
        """Run env_snapshot_commands, then read env_snapshot_files through the sandbox server (bounded by the task deadline)."""
        limit = getattr(task.environment, "env_snapshot_timeout", 10)
        snapshot: dict = {}
        try:
            for command in task.env_snapshot_commands:
                seconds = max(1, math.ceil(deadline.clamp(limit)))
                snapshot[f"cmd:{command}"] = rpc(sb, "/exec", {"command": command, "timeout_seconds": seconds}, seconds)
            for pattern in task.env_snapshot_files:
                seconds = max(1, math.ceil(deadline.clamp(limit)))
                if "*" in pattern or "?" in pattern:
                    for item in rpc(sb, "/glob", {"pattern": pattern, "max_files": 50}, seconds).get("files", []):
                        snapshot[f"file:{item['path']}"] = snapshot_read(rpc(sb, "/read", {"path": item["path"]}, seconds))
                else:
                    snapshot[f"file:{pattern}"] = snapshot_read(rpc(sb, "/read", {"path": pattern}, seconds))
        except TaskDeadlineExceeded:
            log.warning("%s: task deadline reached during env snapshot", task.task_id)
        return snapshot

    def run_task(self, task: dict, out: Path) -> dict:
        definition = TaskDefinition.from_yaml(TASKS / task["id"] / "task.yaml")
        timeout = definition.effective_timeout_seconds(int(self.cfg.run.task_timeout))
        deadline = TaskDeadline.start(timeout)
        assets = self.assets(definition)
        with open_sandbox(self.cfg, self.options.image, name=definition.task_id) as sb:
            self.start_server(sb)
            put(sb, [(item.destination, item.source) for item in assets if not item.grader_only])
            status, trace_path = self.episode(sb, definition, deadline, timeout, out)
            error = status.get("error")
            if error and not agent_side(error):
                raise RuntimeError(error)
            if not error:
                put(sb, [(item.destination, item.source) for item in assets if item.grader_only])
                snapshot = self.snapshot(sb, definition, deadline)
        usage = {"turns": status.get("turns"), "stop_reason": status.get("stop_reason"), "input_tokens": status.get("input_tokens"), "output_tokens": status.get("output_tokens"),
                 "user_agent_rounds": status.get("conversation_followups_used"), "completed": status.get("completed")}
        if error:
            return {"score": 0.0, "completion": 0.0, "robustness": 0.0, "communication": 0.0, "safety": 1.0, "concluded_error_class": "agent_error", "agent_error": error, **usage}
        save_snapshot(snapshot, out / "trace_snapshot")
        fixtures = self.data_dir / "tasks" / definition.task_id
        for relative in definition.local_grader_files:
            source = fixtures / relative
            snapshot[f"local_file:{relative}"] = {"encoding": "base64", "content": base64.b64encode(source.read_bytes()).decode("ascii")} if source.is_file() else {"error": f"not found: {source}"}
        start, messages, dispatches, media_events, end, audit_data = load_trace(trace_path)
        if reason := vision_failure(dispatches):
            raise RuntimeError(reason)
        judge = self.cfg.judge
        llm = LLMJudge(model_id=judge.model, api_key=str(judge.api_key), base_url=judge.base_url, max_output_tokens=int(judge.max_output_tokens),
                       request_timeout_seconds=float(judge.request_timeout), retry_attempts=JUDGE_RETRIES, reasoning_effort=judge.get("reasoning_effort"))
        definition.task_file = str(fixtures / "task.yaml")  # graders resolve fixtures/ next to task_file
        grader = get_grader(definition.task_id, tasks_dir=TASKS, task_dir=TASKS / definition.task_id)
        scores, judge_calls = grade(grader, messages, dispatches, definition, audit_data=audit_data, judge=llm, media_events=media_events, env_snapshot=snapshot)
        task_score = compute_task_score(scores)
        user_agent_meta = {"rounds_used": end.user_agent_rounds, "max_rounds": end.user_agent_max_rounds, "done_reached": end.user_agent_done} if end and end.user_agent_rounds > 0 else {}
        with TraceWriter(trace_path) as writer:
            writer.write_event(GradingResult(trace_id=start.trace_id, task_id=definition.task_id, scores=DimensionScores(completion=scores.completion, robustness=scores.robustness,
                               communication=scores.communication, safety=scores.safety), task_score=task_score, passed=is_pass(task_score), judge_calls=judge_calls, user_agent_meta=user_agent_meta))
        return {"score": task_score, "completion": scores.completion, "robustness": scores.robustness, "communication": scores.communication, "safety": scores.safety,
                "judge_calls": len(judge_calls), **usage}
