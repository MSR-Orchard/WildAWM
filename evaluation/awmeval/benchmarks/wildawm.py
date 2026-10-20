from __future__ import annotations

import json
import re
import shlex
import time
import tomllib
import uuid
from pathlib import Path

from huggingface_hub import snapshot_download

from .. import metrics
from ..harness.opencode import remote_mcp, run_agent, vision_mcp
from ..sandbox import endpoint, open_dockerfile_sandbox
from .base import Benchmark

AGENT_PROMPT = "You are an autonomous personal assistant operating inside one disposable WildAWM world. Complete the user's task using the available native, MCP, browser, shell, and file tools. Follow WORLD.md for the world-specific environment and tool interfaces. No browser, shell, or service interface has general priority; choose based on the operation's required capability and existing session state. Do not ask the user questions because no user is available during the run."
VISION_NOTE = "\nYour current model is text-only. The optional `vision_describe_image` tool can read an image or screenshot saved to an absolute path under /workspace or /tmp and return a text description. Browser and shell interfaces are documented in WORLD.md; no interface has general priority.\n"
VISION_NOTE_PATH = "/agent-config/vision_note.md"
# Former set names, still accepted (as subset names and task.toml keys) for one more release.
FORMER_SETS = {"july_30_val": "val33", "july_14_100": "sft100", "sept_08": "rl208", "aug_10": "rl100"}


def set_flag(meta: dict, name: str):
    """task.toml [metadata] set_<name> flag, falling back to the set's former key."""
    name = FORMER_SETS.get(name, name)
    if f"set_{name}" in meta:
        return meta[f"set_{name}"]
    former = next((old for old, new in FORMER_SETS.items() if new == name), None)
    return meta.get(f"set_{former}") if former else None


SUBSETS = {"all": lambda meta: True, "val33": lambda meta: set_flag(meta, "val33") is True, "train": lambda meta: set_flag(meta, "val33") is False}
TEMPLATE = re.compile(r"\$\{([^}:]+)(?::-(.*))?\}")


def task_dirs(root: Path) -> list[Path]:
    """Task packages directly under `root`, or under its task-set folders (deduplicated by name)."""
    found = sorted(root.glob("*/task.toml")) or sorted(root.glob("*/*/task.toml"))
    unique = {}
    for path in found:
        unique.setdefault(path.parent.name, path.parent)
    return [unique[name] for name in sorted(unique)]


class WildAWM(Benchmark):
    name = "wildawm"
    metric = "pass_rate"

    def prepare(self) -> None:
        root = Path(self.options.dataset_dir)
        if not task_dirs(root):
            if not self.options.get("hf_repo"):
                raise SystemExit(f"no task packages under {root}: download wildawm-harbor and set benchmarks.wildawm.dataset_dir (or hf_repo)")
            snapshot_download(self.options.hf_repo, repo_type="dataset", local_dir=root, token=self.options.get("hf_token") or None)
        print(f"{len(self.tasks())} tasks ({self.options.get('subset', 'all')}) under {root}")

    def tasks(self) -> list[dict]:
        root, subset = Path(self.options.dataset_dir), str(self.options.get("subset") or "all")
        keep = SUBSETS.get(subset) or (lambda meta: set_flag(meta, subset) is True)
        tasks = []
        for path in task_dirs(root):
            meta = tomllib.loads((path / "task.toml").read_text(encoding="utf-8")).get("metadata") or {}
            if keep(meta):
                tasks.append({"id": path.name, "path": str(path)})
        if not tasks:
            raise SystemExit(f"no {subset} tasks under {root}; run `awmeval prepare wildawm` or fix benchmarks.wildawm.dataset_dir")
        return tasks

    def task_env(self, sb, env: dict) -> dict:
        """Resolve task.toml `${VAR:-default}` env templates from the config (vision endpoint as seen from the sandbox)."""
        vision = self.cfg.vision
        values = {
            "WILDAWM_VISION_LLM_ENDPOINT": endpoint(sb, vision), "WILDAWM_VISION_LLM_MODEL_NAME": vision.model, "WILDAWM_VISION_LLM_KEY": vision.api_key,
            "WILDAWM_VISION_LLM_REASONING_EFFORT": vision.get("reasoning_effort") or "none", "WILDAWM_DESCRIBE_IMAGE_MAX_TOKENS": vision.get("max_output_tokens"),
            "WILDAWM_DESCRIBE_IMAGE_TIMEOUT": vision.get("request_timeout"), "WILDAWM_DESCRIBE_IMAGE_RETRIES": vision.get("retries"), **(self.options.get("env") or {}),
        }
        resolved = {}
        for key, value in env.items():
            match = TEMPLATE.fullmatch(str(value))
            if match and values.get(match.group(1)) is not None:
                resolved[key] = str(values[match.group(1)])
            elif match and match.group(2) is not None:
                resolved[key] = match.group(2)
            elif match:
                raise ValueError(f"task env {key} needs a value for {match.group(1)} (set benchmarks.wildawm.env)")
            else:
                resolved[key] = str(value)
        return resolved

    def boot(self, sb, command: list[str], check: dict | None, env: dict, out: Path) -> None:
        """Start the image entrypoint in the background and wait for the task healthcheck (Docker HEALTHCHECK semantics)."""
        if command:
            sb.spawn(shlex.join(command), "entrypoint", env=env)
        started, failures = time.monotonic(), 0
        while check and not sb.exec(check["command"], timeout=int(check.get("timeout_sec", 30)), env=env).ok:
            if command and not sb.alive("entrypoint"):
                raise RuntimeError(f"environment entrypoint exited:\n{sb.log('entrypoint')}")
            if time.monotonic() - started < float(check.get("start_period_sec", 0)):
                time.sleep(float(check.get("start_interval_sec", 5)))
                continue
            failures += 1
            if failures >= int(check.get("retries", 3)):
                raise RuntimeError(f"healthcheck failed {failures} times: {check['command']}\n{sb.log('entrypoint')}")
            time.sleep(float(check.get("interval_sec", 5)))
        (out / "boot.log").write_text(sb.log("entrypoint", tail=200_000), encoding="utf-8")

    def agent(self, sb, root: Path, servers: list, env: dict, workdir: str, user: str | None, timeout: int, out: Path) -> dict:
        mcp = {}
        for server in servers:
            if server.get("transport", "stdio") != "stdio":
                mcp[server["name"]] = remote_mcp(server["url"], self.cfg, oauth=False)
            elif server["name"] == "vision":
                mcp["vision"] = vision_mcp(self.cfg, endpoint(sb, self.cfg.vision))
                mcp["vision"]["environment"].update(LD_PRELOAD="", FAKETIME="")  # real clock for TLS to the vision endpoint
            else:
                mcp[server["name"]] = {"type": "local", "command": [server["command"], *server.get("args", [])], "enabled": True, "timeout": int(self.cfg.opencode.mcp_timeout_ms)}
        world = f"{workdir.rstrip('/')}/WORLD.md"
        if user:
            sb.exec(f"cd {shlex.quote(workdir)} && for name in WORLD.md USER.md; do if [ -f $name ]; then chown root:root $name && chmod 444 $name; fi; done", timeout=60).check("protect world files")
        files = [world] if sb.exists(world) else []
        if "vision" in mcp:
            sb.write(VISION_NOTE_PATH, VISION_NOTE)
            files.append(VISION_NOTE_PATH)
        result = run_agent(sb, self.cfg, task_text=(root / "instruction.md").read_text(encoding="utf-8"), workdir=workdir, out=out / "agent", mcp=mcp,
                           instruction_files=files, user=user, env={**env, "OPENCODE_DISABLE_PROJECT_CONFIG": "true"}, agent_prompt=AGENT_PROMPT,
                           marker=f"/tmp/wildawm_completion_{uuid.uuid4().hex}.md", timeout=timeout)
        return {key: result.get(key) for key in ("stop_reason", "turns", "nudges", "completed", "model_calls", "input_tokens", "output_tokens", "wall_time_s")}

    def oracle(self, sb, root: Path, env: dict, workdir: str, timeout: int, out: Path) -> dict:
        sb.upload(root / "solution", "/solution")
        done = sb.exec("bash /solution/solve.sh", timeout=timeout, env=env, cwd=workdir)
        (out / "oracle.log").write_text(done.stdout + "\n" + done.stderr, encoding="utf-8")
        return {"oracle_exit_code": done.exit_code}

    def verify(self, sb, root: Path, timeout: int, env: dict, workdir: str, out: Path) -> dict:
        """Run tests/test.sh after the agent; reward >= 1 means solved, an unscored world (no reward) counts as a failed rollout."""
        sb.upload(root / "tests", "/tests")
        done = sb.exec("mkdir -p /logs/verifier && bash /tests/test.sh > /logs/verifier/test-stdout.txt 2>&1", timeout=timeout, env=env, cwd=workdir)
        folder = out / "verifier"
        sb.download("/logs/verifier", folder)
        rewards = json.loads((folder / "reward.json").read_text()) if (folder / "reward.json").is_file() else None
        if rewards is None and (folder / "reward.txt").is_file():
            rewards = {"reward": float((folder / "reward.txt").read_text().strip())}
        if rewards is None:
            if not (folder / "l1_verdict.json").is_file():
                raise RuntimeError(f"verifier wrote no reward (exit {done.exit_code}): {(folder / 'test-stdout.txt').read_text(errors='replace')[-1500:] if (folder / 'test-stdout.txt').is_file() else ''}")
            verdict = json.loads((folder / "l1_verdict.json").read_text())
            return {"score": 0.0, "unscored": verdict.get("verdict"), "script_errors": verdict.get("script_errors")}
        rewards = rewards if isinstance(rewards, dict) else {"reward": rewards}
        reward = float(rewards.get("reward", 0.0))
        return {"score": 1.0 if reward >= 1.0 else 0.0, "reward": reward, "checks": {key: value for key, value in rewards.items() if key != "reward"}}

    def run_task(self, task: dict, out: Path) -> dict:
        root = Path(task["path"])
        spec = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
        environment = spec.get("environment") or {}
        memory = self.cfg.sandbox.get("memory") or (f"{int(environment['memory_mb'])}Mi" if environment.get("memory_mb") else None)
        timeout = int(self.cfg.run.get("task_timeout") or (spec.get("agent") or {}).get("timeout_sec") or 3600)
        sb, image = open_dockerfile_sandbox(self.cfg, root / "environment", name=task["id"], cpu=self.cfg.sandbox.get("cpu") or environment.get("cpus"), memory=memory,
                                            timeout=int(environment.get("build_timeout_sec") or 3600))
        workdir = environment.get("workdir") or image.workdir or "/"
        with sb:
            env = {**image.env, **self.task_env(sb, environment.get("env") or {})}
            self.boot(sb, image.command, environment.get("healthcheck"), env, out)
            if self.options.get("oracle"):
                result = self.oracle(sb, root, env, workdir, timeout, out)
            else:
                result = self.agent(sb, root, environment.get("mcp_servers") or [], env, workdir, self.options.get("agent_user") or image.user, timeout, out)
            result.update(self.verify(sb, root, int((spec.get("verifier") or {}).get("timeout_sec") or 900), env, workdir, out))
        return result

    def summarize(self, results: list[dict]) -> dict:
        return {**super().summarize(results), **metrics.pass_k_summary(results)}
