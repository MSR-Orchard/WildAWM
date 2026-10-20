from __future__ import annotations

import json
import shlex
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from huggingface_hub import hf_hub_download

from ...config import ROOT
from ...harness.opencode import REMOTE, opencode_config, spec, upload_assets, vision_mcp
from ...sandbox import endpoint, open_sandbox
from ..base import Benchmark

# tasks.json rows come from `python -m ale_run.tasks.metadata` over third_party/agents-last-exam (upload ale_run first).
INDEX = json.loads((Path(__file__).with_name("tasks.json")).read_text(encoding="utf-8"))
SOURCE = ROOT / "third_party" / "agents-last-exam"
RUNTIME = "/opt/ale-runtime"
CONTROL = "/run/ale-control"
PRIVATE = "/run/ale-private"
NATIVE = "/run/ale-native"
ARCHIVE = "/tmp/ale-native.tar.gz"
DESKTOP_HOME = "/home/user"
# XFCE session and CUA server (port 5000) via the image entrypoint
DESKTOP = (f"/usr/sbin/runuser -u user -- env HOME={DESKTOP_HOME} PATH=/opt/cua-server/.venv/bin:{DESKTOP_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin OPENBLAS_NUM_THREADS=1"
           " OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 ALE_SCREEN_RESOLUTION=1024x768 ALE_ENABLE_DIND=0 /dockerstartup/entrypoint.sh")
CUA_READY = "until curl -fsS --max-time 5 http://127.0.0.1:5000/status >/dev/null 2>&1; do sleep 1; done"
# npm prefix the deployer sets; running it first keeps its 15 s bootstrap timeout clear of cold image reads
NPM_WARMUP = f"/usr/sbin/runuser -u user -- env HOME={DESKTOP_HOME} npm config set prefix {DESKTOP_HOME}/.npm-global"
DATA_DIRS = ("input", "software", "reference")
EXIT_MARGIN = 1800
VALID_RUN = ("completed", "timeout")
SANDBOX_KEYS = ("opencode", "config", "config_home", "workdir", "out", "state", "user", "env", "prompt", "nudge", "marker")  # filled in by the in-sandbox deployer


def missing_data(root: Path) -> list[str]:
    return [task["id"] for task in INDEX["tasks"] if not (root / task["data"] / "input").is_dir()]


def evaluator_environment(task: dict, local: Path) -> dict:
    """WGS scoring reads its truth set from the private task-data copy instead of the agent-visible tree."""
    if task["id"] != "life_sciences/WGS_Variant_Calling":
        return {}
    inputs = local / "input"
    truth = next((inputs / name for name in ("truth_chr17.vcf.gz", "truth_chr17.vcf") if (inputs / name).is_file()), inputs / "truth_chr17.vcf")
    empty = [str(path) for path in (inputs / "chr17.fa", inputs / "eval_region_confident.bed", truth) if not path.is_file() or path.stat().st_size == 0]
    if empty:
        raise FileNotFoundError(f"WGS evaluator data is missing or empty: {empty}")
    return {"WGS_VARIANT_CALLING_EVAL_DATA_DIR": f"{PRIVATE}/task-data/{task['data']}/input"}


def extract(archive: Path, destination: Path) -> None:
    """Unpack regular files and directories only; links and paths escaping the destination are dropped or rejected."""
    with tarfile.open(archive, mode="r:gz") as stream:
        members = []
        for member in stream.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"unsafe path in ALE result archive: {member.name}")
            if member.isfile() or member.isdir():
                members.append(member)
        stream.extractall(destination, members=members, filter="data")


def latest_result(root: Path, task_id: str) -> tuple[Path, dict, dict] | None:
    found = None
    for path in sorted(root.glob("run/*/*/*/v*/*/run.json")):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(run, dict) and (run.get("task") or {}).get("path") == f"tasks/{task_id}":
            found = (path, run)
    if found is None:
        return None
    try:
        evaluation = json.loads((found[0].parent / "eval_result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        evaluation = {"eval_status": "not_executed", "score": None}
    return found[0], found[1], evaluation if isinstance(evaluation, dict) else {}


def redact(text: str, secrets: list[str]) -> str:
    for value in sorted({value for value in secrets if value and len(value) >= 4}, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


class AgentsLastExam(Benchmark):
    name = "ale"
    metric = "mean_score"

    def prepare(self) -> None:
        root = Path(self.options.data_dir)
        if not missing_data(root):
            return
        data = INDEX["data"]
        root.mkdir(parents=True, exist_ok=True)
        archive = Path(hf_hub_download(data["dataset"], data["file"], repo_type="dataset", revision=data["revision"], local_dir=root / ".download", token=self.options.get("hf_token") or None))
        subprocess.run(["tar", "-xzf", str(archive), "-C", str(root)], check=True)
        archive.unlink()
        if missing := missing_data(root):
            raise FileNotFoundError(f"{len(missing)} ALE tasks have no input data under {root}, e.g. {missing[:3]}")

    def tasks(self) -> list[dict]:
        return INDEX["tasks"]

    def control_files(self, sb, task: dict) -> dict:
        """ALE agent, environment and experiment files; the agent part is the shared awmeval OpenCode harness config."""
        cfg = self.cfg
        base_url = endpoint(sb, cfg.agent)
        opencode = opencode_config(cfg, base_url=base_url)
        for provider in opencode["provider"].values():
            for key in ("apiKey", "authToken"):
                provider["options"].pop(key, None)
        if cfg.opencode.get("output_guard", True):
            opencode["plugin"] = [f"{REMOTE}/output_guard.mjs"]
        vision = vision_mcp(cfg, endpoint(sb, cfg.vision))
        vision["environment"].pop("AWM_VISION_API_KEY")
        runner = spec(cfg, opencode="", config="", workdir="", out="", task_text="", marker="", base_url=base_url, config_home=None)
        for key in SANDBOX_KEYS:
            runner.pop(key)
        agent = {
            "harness": "opencode",
            "executor": "sandbox",
            "id": "opencode",
            "model": cfg.agent.model,
            "config": {
                "api_key": "${env:AWMEVAL_ALE_POLICY_API_KEY}",
                "vision_api_key": "${env:AWMEVAL_ALE_VISION_API_KEY}",
                "opencode": opencode,
                "runner": runner,
                "vision": vision,
                "mcp_timeout_ms": int(cfg.opencode.mcp_timeout_ms),
                "assets_dir": REMOTE,
            },
        }
        environment = {
            "provider": "static",
            "endpoint": "http://127.0.0.1:5000",
            "image": "ale-ubuntu22-docker",
            "vm_id": "awmeval",
            "co_located_filesystem": True,
            "desktop_uid": 1000,
            "desktop_gid": 1000,
            "task_data_source": f"co-located:{PRIVATE}/task-data",
            "output_path": "local",
        }
        experiment = {
            "name": "run",
            "agent": f"{CONTROL}/agent.json",
            "environment": f"{CONTROL}/environment.json",
            "tasks": [{"path": task["id"], "variants": [0]}],
            "output": {"root": f"{NATIVE}/ale-runs"},
            "concurrency": 1,
            "cleanup_mode": "delete",
            "wall_time_s": int(cfg.run.task_timeout),
            "auto_resume": False,
            "max_attempts": 1,
        }
        return {"agent.json": agent, "environment.json": environment, "experiment.json": experiment}

    def environment(self, sb, extra: dict) -> dict:
        """Process environment of the in-sandbox ALE run: model keys for the agent profile and the LLM judge used by evaluators."""
        cfg = self.cfg
        judge = endpoint(sb, cfg.judge)
        return {
            "PYTHONPATH": RUNTIME,
            "CUA_TELEMETRY_ENABLED": "false",
            "CUA_TELEMETRY_DISABLED": "1",
            "AWMEVAL_ALE_POLICY_API_KEY": str(cfg.agent.api_key),
            "AWMEVAL_ALE_VISION_API_KEY": str(cfg.vision.api_key),
            "OPENAI_API_KEY": str(cfg.judge.api_key),
            "OPENAI_API_BASE": judge,
            "OPENAI_BASE_URL": judge,
            "LLM_JUDGE_MODEL": str(cfg.judge.model),
            "LLM_JUDGE_REASONING_EFFORT": str(cfg.judge.get("reasoning_effort") or "none"),
            "LLM_JUDGE_MAX_OUTPUT_TOKENS": str(cfg.judge.max_output_tokens),
            **extra,
        }

    def run_task(self, task: dict, out: Path) -> dict:
        cfg, options = self.cfg, self.options
        local = Path(options.data_dir) / task["data"]
        if not (local / "input").is_dir():
            raise FileNotFoundError(f"ALE task data is missing: {local / 'input'} (run `awmeval prepare ale`)")
        extra = evaluator_environment(task, local)
        cpu, memory = (task["cpu"], f"{task['memory_gb']}Gi") if options.get("task_resources", True) else (None, None)
        data = {f"{PRIVATE}/task-data/{task['data']}/{name}": local / name for name in DATA_DIRS if (local / name).is_dir()}
        secrets = [str(cfg.agent.api_key), str(cfg.vision.api_key), str(cfg.judge.api_key)]
        with open_sandbox(cfg, options.image, name=task["id"].split("/")[-1], cpu=cpu, memory=memory, data=data) as sb:
            sb.spawn(DESKTOP, "ale-desktop")
            if not sb.exec(CUA_READY, timeout=int(options.desktop_timeout)).ok:
                raise RuntimeError(f"ALE desktop did not start: {sb.log('ale-desktop', 1500)}")
            sb.exec(NPM_WARMUP, timeout=600).check("warm up npm")
            sb.exec(f"install -d -m 700 {PRIVATE} {PRIVATE}/task-data {CONTROL} {NATIVE} && rm -rf {NATIVE}/* {RUNTIME}/ale_run {RUNTIME}/tasks", timeout=120).check("prepare ALE runtime")
            sb.upload(SOURCE / "ale_run", f"{RUNTIME}/ale_run")
            sb.upload(SOURCE / "tasks", f"{RUNTIME}/tasks")
            upload_assets(sb)
            sb.exec(f"chown -R root:root {RUNTIME}/ale_run {RUNTIME}/tasks && chmod 700 {RUNTIME}/tasks {PRIVATE} {PRIVATE}/task-data && chmod -R a+rX {REMOTE}", timeout=600).check("stage ALE source")
            for name, value in self.control_files(sb, task).items():
                sb.write(f"{CONTROL}/{name}", json.dumps(value, indent=2) + "\n")
            sb.write(f"{CONTROL}/ale.env", "".join(f"export {key}={shlex.quote(value)}\n" for key, value in self.environment(sb, extra).items()))
            sb.exec(f"chmod 700 {CONTROL} {NATIVE} && chmod 600 {CONTROL}/*", timeout=60).check("protect ALE control files")
            run = sb.exec(f"set -a && . {CONTROL}/ale.env && set +a && exec {RUNTIME}/.venv/bin/python -m ale_run run {CONTROL}/experiment.json",
                          timeout=int(cfg.run.task_timeout) + EXIT_MARGIN, cwd=RUNTIME)
            (out / "ale.log").write_text(redact(f"exit code {run.exit_code}\n{run.stdout}\n{run.stderr}", secrets), encoding="utf-8")
            sb.exec(f"tar -C {NATIVE} --exclude='*/.uv-cache' --exclude='*/.agent_runtime_env' -czf {ARCHIVE} .", timeout=int(options.collect_timeout)).check("archive ALE results")
            sb.download(ARCHIVE, out / "native.tar.gz")
        extract(out / "native.tar.gz", out / "native")
        (out / "native.tar.gz").unlink()
        found = latest_result(out / "native" / "ale-runs", task["id"])
        if found is None:
            raise RuntimeError(f"ALE wrote no run.json (exit {run.exit_code}): {redact(run.stderr[-1500:], secrets)}")
        path, meta, evaluation = found
        score = evaluation.get("score")
        status, eval_status = meta.get("status"), evaluation.get("eval_status")
        if eval_status != "success" or not isinstance(score, (int, float)) or isinstance(score, bool) or status not in VALID_RUN:
            error = (meta.get("termination") or {}).get("error") or evaluation.get("error")
            raise RuntimeError(f"invalid ALE result: status={status} eval_status={eval_status} error={json.dumps(error, default=str)[:1500]}")
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        return {
            "score": float(score),
            "run_status": status,
            "eval_status": eval_status,
            "input_tokens": usage.get("total_input_tokens"),
            "output_tokens": usage.get("total_output_tokens"),
            "agent_seconds": (meta.get("timings") or {}).get("duration_s"),
            "run_dir": str(path.parent.relative_to(out)),
        }
