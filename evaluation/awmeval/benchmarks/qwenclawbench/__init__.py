from __future__ import annotations

import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from ...config import ROOT
from ...harness.opencode import VISION_NOTE, HarnessError, initial_prompt, run_agent, vision_mcp
from ...sandbox import STOP, endpoint, open_sandbox
from ..base import Benchmark
from .transcript import finish_reason, to_transcript, usage_totals

HERE = Path(__file__).parent
VENDOR = ROOT / "third_party" / "QwenClawBench"
if str(VENDOR / "scripts") not in sys.path:
    sys.path.insert(0, str(VENDOR / "scripts"))  # vendored upstream modules import each other by bare name
from lib_anomalies import detect_anomalies, execution_error_class  # noqa: E402
from lib_tasks import TaskLoader  # noqa: E402

WORKSPACE = "/home/node/workspace"
TASK_FILE = f"{WORKSPACE}/.awmeval-task.md"
MARKER = "/tmp/qwenclaw-opencode-complete.md"
PROMPT = f"Read {TASK_FILE} and complete the task exactly as written."
NUDGE = f"Continue the original task from the current session state. The authoritative task and completion protocol remain in {TASK_FILE}. Do not restart or re-plan the task."
INSTRUCTIONS = (f"Complete the user's task autonomously in {WORKSPACE}. OpenCode's native shell and file tools are available. There is no general priority between the available shell, file, "
                "and service interfaces; choose based on the operation's required capability and existing state. The tested model is text-only. If visual evidence is needed, use "
                f"`vision_describe_image` with an absolute image path and a focused question. Do not ask the user questions.\n\n{VISION_NOTE}")
INFRA_ANOMALIES = {"EXECUTION_EXCEPTION", "EMPTY_TRANSCRIPT", "ZERO_TOKEN_RESPONSE"}
TRANSCRIPT_API = {"openai": ("openai", "openai-completions"), "anthropic": ("anthropic", "anthropic-messages")}


def stage(task, assets: Path, root: Path) -> None:
    """Initial workspace: fresh plain files in spec order, then the describe-image skill."""
    for item in task.workspace_files:
        if "content" in item:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            continue
        source = next((path for path in (assets / task.task_id / item["source"], assets / item["source"]) if path.exists()), None)
        if source is None:
            raise FileNotFoundError(f"asset {item['source']!r} of {task.task_id} not found under {assets}")
        target = root / item["dest"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, copy_function=shutil.copyfile)
        else:
            shutil.copyfile(source, target)
    skill = root / "skills" / "describe-image"
    shutil.rmtree(skill, ignore_errors=True)
    shutil.copytree(VENDOR / "skills" / "describe-image", skill, copy_function=shutil.copyfile)


def collect(sb, local: Path) -> None:
    """Archive the workspace as the sandbox user, keeping regular files and directories only."""
    archive = f"/tmp/qwenclaw-workspace-{uuid.uuid4().hex}.tar.gz"
    try:
        sb.exec(["tar", "-C", WORKSPACE, "-czf", archive, "."], timeout=300).check("archive workspace")
        payload = sb.read(archive)
    finally:
        sb.exec(["rm", "-f", archive], timeout=30)
    local.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as bundle:
        members = []
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"unsafe workspace archive member {member.name!r}")
            if member.issym() or member.islnk():
                continue
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"unsupported workspace archive member {member.name!r}")
            members.append(member)
        bundle.extractall(local, members=members, filter="data")


def opencode_exit(out: Path) -> dict | None:
    """Agent result when OpenCode only exited non-zero without error events (upstream scores these runs)."""
    result = json.loads((out / "result.json").read_text(encoding="utf-8")) if (out / "result.json").exists() else {}
    if result.get("error") or result.get("returncode") in (0, None) or any(event.get("type") == "error" for event in read_events(out / "events.jsonl")):
        return None
    return result


def read_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


class QwenClawBench(Benchmark):
    name = "qwenclawbench"
    metric = "mean_score"
    pass_threshold = 1.0 - 1e-9

    def __init__(self, cfg):
        super().__init__(cfg)
        self.root = ROOT / str(self.options.data_dir)
        logging.getLogger("lib_tasks").setLevel(logging.WARNING)
        self.specs = {task.task_id: task for task in TaskLoader(self.root / "tasks").load_all_tasks()}
        if not self.specs:
            raise SystemExit(f"no QwenClawBench tasks under {self.root / 'tasks'}")

    def tasks(self) -> list[dict]:
        return [{"id": task_id, "category": task.category, "grading_type": task.grading_type} for task_id, task in self.specs.items()]

    def grade(self, task, out: Path) -> dict:
        """Automated checks and LLM judge in a host child process, against the downloaded workspace."""
        out, judge = out.resolve(), self.cfg.judge
        request = {"scripts": str(VENDOR / "scripts"), "task_file": str(task.file_path), "transcript": str(out / "transcript.json"), "workspace": str(out / "workspace"),
                   "output": str(out / "grade.json"), "judge": {"model": judge.model, "base_url": judge.base_url, "api_key": str(judge.api_key), "timeout": float(judge.request_timeout),
                   "max_output_tokens": int(judge.max_output_tokens), "reasoning_effort": judge.get("reasoning_effort"), "retries": int(self.options.judge_retries)}}
        timeout = int(self.options.grade_timeout)
        with (out / "grade.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, str(HERE / "grade.py")], stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, cwd=out, text=True, start_new_session=True)
            process.stdin.write(json.dumps(request))
            process.stdin.close()
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None and not STOP.is_set() and time.monotonic() < deadline:
                    time.sleep(0.5)
                killed = process.poll() is None
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)  # the grader if still running, and agent programs the checks started
                except OSError:
                    pass
                process.wait()
        if process.returncode != 0 or not (out / "grade.json").exists():
            reason = ("stopped" if STOP.is_set() else f"timed out after {timeout}s") if killed else f"exited {process.returncode}"
            lines = (out / "grade.log").read_text(encoding="utf-8", errors="replace").strip().splitlines()
            raise RuntimeError(f"grading {reason}: {lines[-1] if lines else ''}\n" + "\n".join(lines[-30:]))
        return json.loads((out / "grade.json").read_text(encoding="utf-8"))

    def run_task(self, task: dict, out: Path) -> dict:
        spec = self.specs[task["id"]]
        with tempfile.TemporaryDirectory(prefix="qwenclawbench-") as staging:
            stage(spec, self.root / "assets", Path(staging))
            with open_sandbox(self.cfg, self.options.image, name=task["id"][:10]) as sb:
                sb.upload(staging, WORKSPACE)
                sb.write(TASK_FILE, initial_prompt(spec.prompt, MARKER))
                try:
                    agent = run_agent(sb, self.cfg, task_text=spec.prompt, prompt=PROMPT, nudge=NUDGE, marker=MARKER, workdir=WORKSPACE, out=out / "agent",
                                      mcp={"vision": vision_mcp(self.cfg, endpoint(sb, self.cfg.vision))}, instructions=INSTRUCTIONS, stop_on_finish=("length",))
                except HarnessError:
                    agent = opencode_exit(out / "agent")
                    if agent is None:
                        raise
                sb.exec(["rm", "-f", TASK_FILE], timeout=30)
                collect(sb, out / "workspace")
        events = read_events(out / "agent" / "events.jsonl")
        provider, api = TRANSCRIPT_API.get(self.cfg.agent.get("api", "openai"), TRANSCRIPT_API["openai"])
        effort = self.cfg.agent.get("reasoning_effort")
        transcript = to_transcript(prompt=spec.prompt, events=events, model=self.cfg.agent.model, provider=provider, api=api,
                                   reasoning_effort="off" if effort in (None, "none") else effort, cwd=WORKSPACE)
        (out / "transcript.json").write_text(json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
        # upstream validity rules: infrastructure anomalies are retried and excluded, agent errors (e.g. timeouts) score 0
        timed_out = agent["stop_reason"] == "timeout"
        failed = agent["stop_reason"] == "harness_error" and not (agent["completed"] and events)
        execution = {"status": "timeout" if timed_out else "error" if failed else "success", "exit_code": 124 if timed_out else agent["returncode"] if failed else 0,
                     "timed_out": timed_out, "execution_failed": failed, "execution_time": agent["wall_time_s"],
                     "transcript": transcript, "transcript_length": len(transcript), "usage": usage_totals(transcript), "opencode_terminal_reason": finish_reason(events),
                     "model_output_seen": any((event.get("part") or {}).get("type") in {"text", "reasoning", "tool"} for event in events)}
        anomalies = detect_anomalies(execution, "")
        errors = {item["id"] for item in anomalies["items"] if item["severity"] == "error"}
        conclusion = execution_error_class(execution) or (("infrastructure_error" if errors & INFRA_ANOMALIES else "agent_error") if errors else None)
        if conclusion == "infrastructure_error":
            raise RuntimeError(f"infrastructure anomaly in agent run: {sorted(errors) or agent['stop_reason']}")
        grade = None if conclusion == "agent_error" else self.grade(spec, out)
        if grade is not None:
            anomalies = detect_anomalies(execution, grade["notes"])
        record = {**(grade or {}), "anomalies": anomalies, "concluded_error_class": conclusion, "execution": {key: value for key, value in execution.items() if key != "transcript"}}
        (out / "grading.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        if conclusion is None and anomalies["has_error"]:
            raise RuntimeError(f"grading anomaly: {[item['id'] for item in anomalies['items']]} {grade['notes'][:300]}")
        raw = None if grade is None else grade["score_simple"] if self.options.scoring == "simple" else grade["score"]
        return {"score": 0.0 if grade is None else raw / grade["max_score"], "raw_score": raw, "grading_type": spec.grading_type, "category": spec.category, "error_class": conclusion,
                "anomalies": [item["id"] for item in anomalies["items"]], "stop_reason": agent["stop_reason"], "finish_reason": execution["opencode_terminal_reason"],
                "turns": agent["turns"], "nudges": agent["nudges"], "completed": agent["completed"], "input_tokens": agent["input_tokens"], "output_tokens": agent["output_tokens"]}
