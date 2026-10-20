#!/usr/bin/env python3
"""Task authoring from a chosen sketch inside the provisioned world container."""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from wildawm.synthesis import harness
from wildawm.synthesis.tasks import claude_runner as cr
from wildawm.synthesis.tasks import vision_shim as vs
from wildawm import const_config as cfg
from wildawm.json_utils import load_jsonc
from wildawm.synthesis.tasks import capability_contract as cap
from wildawm.synthesis.tasks import task_skill_pool as xp
from wildawm.synthesis.worlds import anchors

TASKS_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = TASKS_DIR / "prompts"
CONSTITUTION = PROMPTS_DIR / "generator_constitution.md"
OUT_IN_CONTAINER = "/workspace/.xai_out"

# Paths whose mtimes show the author agent is still active; unquoted so the shell expands them.
ACTIVITY_ROOTS = ("/workspace", "/tmp/awm-oc-*", "$HOME/.claude/projects",
                  "$HOME/.codex", "$HOME/.hermes", "$HOME/.nanobot",
                  "$HOME/.openclaw", "$HOME/.browser-use")

ARTIFACTS = ["task.md", "reason.md", "meta.json", "golden.py"]
_SUPPORT = ["_verify_sdk.py", "llm_judge.py", "capability_contract.py",
            "artifact_lint.py", "_selfcheck.py"]
_COPYOUT_PRESERVE_DIRS = ("_synthesis_log",)


def _seed_summary(seed_dir: Path | None) -> str:
    if not seed_dir or not seed_dir.exists():
        return ""
    files = []
    for rel in ARTIFACTS + ["verifier/rubrics.md", "verifier/verify.skill.md"]:
        if (seed_dir / rel).exists():
            files.append(rel)
    scripts = seed_dir / "verifier" / "scripts"
    if scripts.exists():
        files.extend(
            f"verifier/scripts/{p.name}" for p in sorted(scripts.glob("*.py"))
            if p.name != "_verify_sdk.py"
        )
    if not files:
        return ""
    return (
        "\n\n## Existing partial artifacts were restored\n"
        f"The following files from the previous attempt are already present in `{OUT_IN_CONTAINER}`:\n"
        + "\n".join(f"- `{name}`" for name in files)
        + "\nContinue from these files. Do not repeat broad anchor exploration unless a restored file is clearly inconsistent; fill the missing deliverables, fix anything self-check reports, and finish on ALL GREEN.\n"
    )


def _matching_seed_dir(out_host_dir: Path, sketch: dict) -> Path | None:
    if not out_host_dir.exists():
        return None
    meta_path = out_host_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = load_jsonc(meta_path)
    except ValueError:
        return None
    if meta.get("title") != sketch.get("title"):
        return None
    return out_host_dir


def _prompt(profile: str, day: int, sketch: dict, difficulty: str,
            feedback: str, seed_dir: Path | None = None,
            slug: str = "", capability_contract: str | None = None) -> str:
    failure = (
        f"\n## Previous-stage feedback\n{feedback}\n"
        "Repair the complete package and rerun self-check until ALL GREEN.\n"
        if feedback else ""
    )
    contract_path = PROMPTS_DIR / "capability_contract.md"
    capability_contract = capability_contract or (
        contract_path.read_text() if contract_path.exists()
        else "(capability contract unavailable)"
    )
    image_policy = (
        "The task must be image-decisive. Establish the decisive visual fact with the image tool, store it in meta.json.image_grounding, and state a neutral visual criterion in task.md."
        if cfg.XAI_REQUIRE_IMAGE_DECISIVE else
        "Image grounding is optional. Include meta.json.image_grounding only when the task genuinely depends on a visual fact; otherwise use null."
    )
    slug_rule = (
        f"`meta.json.slug` must be exactly `{slug}`."
        if slug else "Use the reserved output-directory slug."
    )
    return f"""# Author one XAI task package
{failure}

Profile: {profile}
Day: {day}
Authoring difficulty target: {difficulty}
Output directory: {OUT_IN_CONTAINER}

## Reserved sketch
```json
{json.dumps(sketch, indent=1, ensure_ascii=False)}
```

## Live capability and schema contract
{capability_contract}

Verify sketch anchors against the live world. Preserve the reserved task
identity. If a required capability or entity is absent, report the concrete
failure instead of silently switching to a different task.

{xp.prompt_section()}

Write these deliverables before broad exploration:
- `task.md`
- `reason.md`
- `meta.json`
- `golden.py`
- `verifier/rubrics.md`
- `verifier/verify.skill.md`
- at least one `verifier/scripts/*.py`

Then run `python /tmp/_selfcheck.py {OUT_IN_CONTAINER}` and repair every
reported defect until it prints `ALL GREEN`.

Stable requirements:
- `task.md` is a natural, self-contained user request of at most 300 words.
- The task must require indispensable CLI, MCP, and Web work.
- The solver must write and run a script or command-line program over a real
  user-visible source or a real solver-created MCP export. A later MCP or Web
  action must depend on the script result. A separate output artifact is
  optional. If one is required, use a clean workspace-relative path.
- Use only active MCP apps and operations from the live capability contract.
- Discover Web capabilities through the live site. A Web read is valid. A Web
  write must use the public UI and must be verified through a live-discovered,
  persisted AFTER-state surface. Do not guess table or column names.
- Do not impose a fixed interface-hop count. Record the actual intended order
  in `meta.json.interface_flow`, explicitly naming `cli`, `mcp`, and `web`.
- {image_policy}
- Keep private answers and detailed verification logic out of `task.md`.
- Resolve identities and target entities through live schema and natural keys;
  do not hard-code fixture row IDs.
- Verifiers are read-only and use the supplied schema-independent
  `_verify_sdk.py`, baseline/AFTER state, and three verdicts: `completed`,
  `failed`, and `world_imperfect`.
- Clean must fail and golden must complete.
- Do not use traces, screenshots, hidden harness state, or direct database
  mutation as proof that the solver completed a Web or MCP action.
- Do not create `world_patch/` during normal authoring. It is reserved for an
  orchestrator-directed world repair.
- {slug_rule}

Required `meta.json` fields include `title`, `slug`, `difficulty`, `interfaces`,
`interface_flow`, `sites`, `mcp_domains`, `image_grounding`, `natural_keys`,
`cli_code_step`, `web_action_type`, and `web_write_surface`. `interfaces` must be
`["cli","mcp","web"]`. `cli_code_step` must set `requires_code:true`, name
real input sources, describe the program computation, and explain downstream
use. `output_artifact` may be null. `web_action_type` is `read_only` or
`browser_write_flow`; only the latter requires `web_write_surface`.

{_seed_summary(seed_dir)}

Finish with the exact line `ALL GREEN — artifacts ready` only after self-check
passes.
"""

def _lw_env() -> dict[str, str]:
    spec = cfg.model_spec(cfg.LIGHTWEIGHT_JUDGE_MODEL)
    env = {
        "AWM_LIGHTWEIGHT_JUDGE_BASE_URL": cfg.container_host_url(spec["base_url"]),
        "AWM_LIGHTWEIGHT_JUDGE_API_KEY": spec["api_key"],
        "AWM_LIGHTWEIGHT_JUDGE_MODEL": spec["api_model"],
        "AWM_LIGHTWEIGHT_JUDGE_PROVIDER_TYPE": spec["provider_type"],
        "AWM_LIGHTWEIGHT_JUDGE_REF": spec["ref"],
        # Pass only non-secret protocol quirks; the registry may not be mounted in the container.
        "AWM_LIGHTWEIGHT_JUDGE_QUIRKS_JSON": json.dumps(spec.get("quirks") or {}),
        "AWM_LIGHTWEIGHT_JUDGE_REASONING_EFFORT": (
            cfg.LIGHTWEIGHT_JUDGE_REASONING_EFFORT),
        # Keep judge routing independent of the solver's reasoning-effort setting.
        "AWM_REASONING_EFFORT": "",
    }
    models_path = os.environ.get("AWM_MODELS_PATH")
    if models_path:
        env["AWM_MODELS_PATH"] = models_path
    return env


def _gen_done_check(container: str):
    """Nudge-loop completion probe: deliverables on disk and self-check ALL GREEN."""
    uid, gid = os.getuid(), os.getgid()
    present_sh = " && ".join(f"test -s {OUT_IN_CONTAINER}/{a}" for a in ARTIFACTS)
    present_sh += (f" && test -s {OUT_IN_CONTAINER}/verifier/rubrics.md"
                   f" && test -s {OUT_IN_CONTAINER}/verifier/verify.skill.md"
                   f" && ls {OUT_IN_CONTAINER}/verifier/scripts/*.py >/dev/null 2>&1")
    env_flags: list[str] = []
    for k, v in _lw_env().items():
        env_flags += ["-e", f"{k}={v}"]

    def _check() -> tuple[bool, str]:
        p = cr.run_cmd(["docker", "exec", container, "sh", "-c", present_sh],
                       role="gen_done", timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S,
                       check=False)
        if not p.ok:
            return False, "deliverables incomplete"
        sc = cr.run_cmd(
            ["docker", "exec", "--user", f"{uid}:{gid}", *env_flags, container,
             "sh", "-c",
             f"cd /workspace && rm -rf /tmp/_xai_selfcheck_probe && "
             f"python3 /tmp/_selfcheck.py "
             f"--out_dir {OUT_IN_CONTAINER} --snapshot /tmp/_xai_snap "
             f"--scratch /tmp/_xai_selfcheck_probe 2>&1"],
            role="gen_done", timeout=cfg.LLM_MEDIUM_TIMEOUT_S, check=False)
        if "ALL GREEN" in (sc.result_text or ""):
            return True, "deliverables present + self-check ALL GREEN"
        return False, "self-check not ALL GREEN"

    return _check


def _artifact_watchdog(container: str, started_at: float,
                       first_artifact_timeout: int | None = None,
                       pre_artifact_tool_limit: int | None = None):
    first_artifact_timeout = (
        first_artifact_timeout or cfg.LLM_ARTIFACT_TIMEOUT_S)
    idle_timeout = cfg.LLM_ARTIFACT_TIMEOUT_S
    started_epoch = time.time() - (time.monotonic() - started_at)

    def _first_artifact_present() -> bool:
        probe = cr.run_cmd(
            [
                "docker", "exec", container, "sh", "-c",
                f"test -s {OUT_IN_CONTAINER}/task.md || "
                f"test -s {OUT_IN_CONTAINER}/golden.py || "
                f"test -s {OUT_IN_CONTAINER}/meta.json",
            ],
            role="gen_watchdog", timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S, check=False)
        return probe.ok

    def _latest_activity_epoch() -> float | None:
        probe = cr.run_cmd(
            [
                "docker", "exec", container, "sh", "-c",
                f"find {' '.join(ACTIVITY_ROOTS)} -type f "
                "-printf '%T@\\n' 2>/dev/null | sort -nr | head -1",
            ],
            role="gen_watchdog", timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S, check=False)
        if not probe.ok or not probe.result_text.strip():
            return None
        try:
            return float(probe.result_text.strip().splitlines()[0])
        except (ValueError, IndexError):
            return None

    def _pre_artifact_tool_count() -> int | None:
        probe = cr.run_cmd(
            [
                "docker", "exec", container, "python3", "-c",
                (
                    "import datetime,json,pathlib,sys\n"
                    "started=float(sys.argv[1]); n=0\n"
                    "root=pathlib.Path('/home/agent/.claude/projects')\n"
                    "for p in root.glob('**/*.jsonl'):\n"
                    "    try: lines=p.open(errors='replace')\n"
                    "    except OSError: continue\n"
                    "    with lines:\n"
                    "        for line in lines:\n"
                    "            try: e=json.loads(line)\n"
                    "            except Exception: continue\n"
                    "            ts=e.get('timestamp')\n"
                    "            if ts:\n"
                    "                try:\n"
                    "                    epoch=datetime.datetime.fromisoformat(\n"
                    "                        ts.replace('Z','+00:00')).timestamp()\n"
                    "                except ValueError:\n"
                    "                    epoch=started\n"
                    "                if epoch < started: continue\n"
                    "            msg=e.get('message') or {}\n"
                    "            if e.get('type')!='assistant' or msg.get('role')!='assistant':\n"
                    "                continue\n"
                    "            for c in msg.get('content') or []:\n"
                    "                if isinstance(c,dict) and c.get('type')=='tool_use':\n"
                    "                    n+=1\n"
                    "print(n)"
                ),
                str(started_epoch),
            ],
            role="gen_watchdog", timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S, check=False)
        if not probe.ok:
            return None
        try:
            return int(probe.result_text.strip())
        except ValueError:
            return None

    def _evaluate() -> tuple[bool, str]:
        elapsed = time.monotonic() - started_at
        if (pre_artifact_tool_limit is not None
                and pre_artifact_tool_limit > 0
                and elapsed >= cfg.LLM_ARTIFACT_TIMEOUT_S):
            tool_count = _pre_artifact_tool_count()
            if (tool_count is not None and tool_count > pre_artifact_tool_limit
                    and not _first_artifact_present()):
                return (
                    True,
                    "generator pre-artifact tool watchdog fired: "
                    f"{tool_count} tool calls before task.md/golden.py/meta.json",
                )
        if elapsed < min(idle_timeout, first_artifact_timeout):
            return False, ""
        # Any activity counts as alive; LLM_AUTHOR_TIMEOUT_S remains the hard cap.
        latest = _latest_activity_epoch()
        idle_for = (time.time() - max(latest, started_epoch)
                    if latest is not None else elapsed)
        if _first_artifact_present():
            if elapsed >= idle_timeout and idle_for >= idle_timeout:
                return (
                    True,
                    "generator idle watchdog fired: no workspace or "
                    f"harness-session activity for {int(idle_for)}s",
                )
            return False, ""
        if elapsed >= first_artifact_timeout and idle_for >= first_artifact_timeout:
            return (
                True,
                "generator first-artifact watchdog fired: no task.md, golden.py, "
                f"or meta.json was written within {first_artifact_timeout}s and "
                f"no activity for {int(idle_for)}s",
            )
        return False, ""

    # Record the stall reason on the callable so generate() can log it.
    def _check() -> tuple[bool, str]:
        fired, reason = _evaluate()
        if fired:
            _check.fired_reason = reason
        return fired, reason

    _check.fired_reason = ""
    return _check


def setup_container(container: str, profile: str, day: int,
                    seed_dir: Path | None = None) -> str:
    """Seed the container with everything authoring + self-check needs."""
    cr.run_cmd(["docker", "exec", container, "sh", "-c",
                f"rm -rf {OUT_IN_CONTAINER} /tmp/_xai_snap && "
                f"mkdir -p {OUT_IN_CONTAINER}/verifier/scripts"],
               role="gen_setup")
    # A filled scratch copy; the master world is never modified here.
    snap = anchors.inspection_world(profile, day)
    cr.run_cmd(["docker", "cp", str(snap), f"{container}:/tmp/_xai_snap"],
               role="gen_setup", timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    cr.run_cmd(["docker", "cp", str(CONSTITUTION),
                f"{container}:/tmp/generator_constitution.md"], role="gen_setup")
    cr.run_cmd(["docker", "cp", str(vs.DESCRIBE_IMAGE_MCP),
                f"{container}:{vs.CONTAINER_DESCRIBE_IMAGE_MCP}"],
               role="gen_setup")
    xp.stage_readonly(container)
    # Generate the per-world contract without modifying the tracked fallback.
    contract_text = cap.generate_md(snap)
    with tempfile.NamedTemporaryFile(
            mode="w", suffix="-capability-contract.md") as contract_file:
        contract_file.write(contract_text)
        contract_file.flush()
        cr.run_cmd(["docker", "cp", contract_file.name,
                    f"{container}:/tmp/capability_contract.md"], role="gen_setup")
    for mod in _SUPPORT:
        cr.run_cmd(["docker", "cp", str(TASKS_DIR / mod),
                    f"{container}:/tmp/{mod}"], role="gen_setup")
    cr.run_cmd(["docker", "cp", str(TASKS_DIR / "_verify_sdk.py"),
                f"{container}:{OUT_IN_CONTAINER}/verifier/scripts/_verify_sdk.py"],
               role="gen_setup")
    if seed_dir and seed_dir.exists():
        for rel in ARTIFACTS + ["verifier/rubrics.md", "verifier/verify.skill.md"]:
            src = seed_dir / rel
            if src.exists():
                dst = f"{container}:{OUT_IN_CONTAINER}/{rel}"
                cr.run_cmd(["docker", "exec", container, "mkdir", "-p",
                            f"{OUT_IN_CONTAINER}/{Path(rel).parent.as_posix()}"],
                           role="gen_setup", check=False)
                cr.run_cmd(["docker", "cp", str(src), dst],
                           role="gen_setup", check=False)
        scripts = seed_dir / "verifier" / "scripts"
        if scripts.exists():
            for src in sorted(scripts.glob("*.py")):
                if src.name == "_verify_sdk.py":
                    continue
                cr.run_cmd(["docker", "cp", str(src),
                            f"{container}:{OUT_IN_CONTAINER}/verifier/scripts/{src.name}"],
                           role="gen_setup", check=False)
        cr.run_cmd(["docker", "cp", str(TASKS_DIR / "_verify_sdk.py"),
                    f"{container}:{OUT_IN_CONTAINER}/verifier/scripts/_verify_sdk.py"],
                   role="gen_setup")
    cr.run_cmd(["docker", "exec", "--user", "root", container, "chown", "-R",
                f"{os.getuid()}:{os.getgid()}",
                "/tmp/_xai_snap", OUT_IN_CONTAINER], role="gen_setup")
    return contract_text


def _prepare_out_host_dir_for_copyout(out_host_dir: Path) -> None:
    """Clear previous task artifacts but keep the `_synthesis_log/` replay logs."""
    out_host_dir = Path(out_host_dir)
    if not out_host_dir.exists():
        out_host_dir.parent.mkdir(parents=True, exist_ok=True)
        out_host_dir.mkdir(parents=True, exist_ok=True)
        return

    tmp_root = Path(tempfile.mkdtemp(
        prefix=f".{out_host_dir.name}-preserve-",
        dir=str(out_host_dir.parent),
    ))
    preserved: list[tuple[str, Path]] = []
    try:
        for name in _COPYOUT_PRESERVE_DIRS:
            src = out_host_dir / name
            if src.exists():
                dst = tmp_root / name
                shutil.move(str(src), str(dst))
                preserved.append((name, dst))
        shutil.rmtree(out_host_dir)
        out_host_dir.mkdir(parents=True, exist_ok=True)
        for name, src in preserved:
            shutil.move(str(src), str(out_host_dir / name))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def generate(container: str, profile: str, day: int, sketch: dict, *,
             difficulty: str, out_host_dir: Path, feedback: str = "",
             timeout: int | None = None) -> dict:
    """One authoring attempt. Returns {ok, present, meta, call, out_dir}."""
    timeout = timeout or cfg.LLM_AUTHOR_TIMEOUT_S
    out_host_dir = Path(out_host_dir)
    seed_dir = _matching_seed_dir(out_host_dir, sketch)
    contract_text = setup_container(container, profile, day, seed_dir=seed_dir)
    # _lw_env() lets the in-container self-check reach the lightweight judge.
    prompt = _prompt(
        profile, day, sketch, difficulty, feedback, seed_dir=seed_dir,
        slug=out_host_dir.name, capability_contract=contract_text)
    hkind = harness.resolve_harness("generator")
    mcp_config = disallowed_tools = None
    mcp_strict = True
    # The vision shim applies only to claude-code; opencode always gets describe_image MCP.
    if hkind == "claude-code" and cfg.generator_needs_vision_shim(cfg.TASK_GENERATOR_LLM_MODEL):
        prompt += vs.GENERATOR_NOTE
        mcp_config = vs.generator_mcp_config()
        disallowed_tools = vs.generator_disallowed_tools()
        mcp_strict = False
    started_at = time.monotonic()
    watchdog = _artifact_watchdog(container, started_at)
    rec = harness.run_agent(
        prompt, role="generator",
        claude_llm=cr.LLM(model=cfg.TASK_GENERATOR_LLM_MODEL,
                          effort=cfg.TASK_GENERATOR_LLM_EFFORT,
                          route=cfg.TASK_GENERATOR_LLM_ROUTE),
        timeout=timeout, container=container, workdir="/workspace",
        system_prompt_file="/tmp/generator_constitution.md",
        proxy_url=cfg.TASK_GENERATOR_LLM_PROXY, env_extra=_lw_env(), retries=0,
        mcp_config=mcp_config, mcp_strict=mcp_strict,
        disallowed_tools=disallowed_tools, container_mcp=True,
        done_check=_gen_done_check(container),
        stall_check=watchdog,
        stall_poll_s=15)

    _prepare_out_host_dir_for_copyout(out_host_dir)
    cp = cr.run_cmd(["docker", "cp", f"{container}:{OUT_IN_CONTAINER}/.",
                     str(out_host_dir)], role="gen_copyout", check=False)
    scripts = out_host_dir / "verifier" / "scripts"
    if scripts.exists():
        shutil.copy2(TASKS_DIR / "_verify_sdk.py", scripts / "_verify_sdk.py")
        shutil.rmtree(scripts / "__pycache__", ignore_errors=True)
    shutil.rmtree(out_host_dir / "__pycache__", ignore_errors=True)

    meta = {}
    mp = out_host_dir / "meta.json"
    if mp.exists():
        try:
            meta = load_jsonc(mp)
        except ValueError:
            pass
    if sketch.get("area") and "area" not in meta:
        meta["area"] = sketch["area"]
        if mp.exists():
            mp.write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    present = {a: (out_host_dir / a).exists() for a in ARTIFACTS}
    present["verifier/scripts"] = bool(scripts.exists() and
                                       [p for p in scripts.glob("*.py")
                                        if p.name != "_verify_sdk.py"])
    ok = rec.ok and all(present.values()) and cp.ok
    # Embed the full generator transcript (best effort).
    if rec.extra.get("harness") == "opencode":
        transcript = rec.extra.pop("trajectory", None) or []
    else:
        transcript = cr.capture_claude_session_transcript(
            container=container, session_id=rec.session_id)
    call_dict = rec.to_dict()
    if transcript:
        call_dict["session_transcript"] = transcript
        call_dict["session_transcript_events"] = len(transcript)
    return {"ok": ok, "present": present, "meta": meta,
            "out_dir": str(out_host_dir), "call": call_dict,
            "stall_reason": getattr(watchdog, "fired_reason", ""),
            "copy_ok": cp.ok}
