#!/usr/bin/env python3
"""Agent-harness abstraction: run one headless agent turn via Claude Code or OpenCode."""
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

from wildawm import const_config as cfg
from wildawm.synthesis.tasks import claude_runner as cr
from wildawm.synthesis.harness.opencode import (
    OpencodeModel, run_opencode_code, CallRecord)
from wildawm.synthesis.harness.model import (
    ROLES, resolve_harness, resolve_opencode_model, opencode_model_from_ref)
from wildawm.synthesis.harness.nudge import reminder_for

__all__ = ["run_agent", "resolve_harness", "resolve_opencode_model",
           "opencode_model_from_ref", "OpencodeModel", "run_opencode_code",
           "reminder_for", "CallRecord", "ROLES"]


def _wait_claude_container_ready(container: str) -> tuple[bool, str]:
    deadline = time.time() + cfg.AGENT_READY_TIMEOUT_S
    last = ""
    while time.time() < deadline:
        p = subprocess.run(
            ["docker", "exec", container, "bash", "-lc",
             f"test -f /tmp/mcp-registered.flag && "
             f"(exec 3<>/dev/tcp/localhost/{cfg.LLM_PROXY_PORT})"],
            capture_output=True, text=True, timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S)
        if p.returncode == 0:
            return True, ""
        last = (p.stderr or p.stdout or "").strip()
        time.sleep(1)
    return False, last or "claude-code container proxy/mcp readiness timed out"


def _claude_throwaway_env(llm: "cr.LLM", env_extra: dict | None) -> dict:
    env = dict(env_extra or {})
    model = env.get("AWM_OPENAI_MODEL") or env.get("OPENAI_MODEL") or llm.model
    env.update(cfg.claude_code_model_env(model))
    env.update({
        "AWM_AGENT_KIND": "claude-code",
        "AWM_HOST_UID": str(os.getuid()),
        "AWM_HOST_GID": str(os.getgid()),
        "AWM_OPENAI_MODEL": model,
        "OPENAI_MODEL": model,
        "ANTHROPIC_BASE_URL": f"http://localhost:{cfg.LLM_PROXY_PORT}",
        "ANTHROPIC_API_KEY": "not-used",
        "ANTHROPIC_AUTH_TOKEN": "not-used",
    })
    if "AWM_OPENAI_BASE_URL" in env:
        env.setdefault("OPENAI_BASE_URL", env["AWM_OPENAI_BASE_URL"])
    if "AWM_OPENAI_API_KEY" in env:
        env.setdefault("OPENAI_API_KEY", env["AWM_OPENAI_API_KEY"])
    if "OPENAI_BASE_URL" in env:
        env.setdefault("AWM_OPENAI_BASE_URL", env["OPENAI_BASE_URL"])
    if "OPENAI_API_KEY" in env:
        env.setdefault("AWM_OPENAI_API_KEY", env["OPENAI_API_KEY"])
    policy = cr._llm_reasoning_policy(llm, env)
    env.update(cfg.reasoning_effort_env(policy))
    env.setdefault("AWM_LLM_PROXY_PORT", str(cfg.LLM_PROXY_PORT))
    return env


def _run_claude_code_throwaway(
    prompt: str, llm: "cr.LLM", *, role: str, timeout: int,
    cwd: str | None, image: str | None, extra_paths: list[str] | None,
    system_prompt_file: str | None, allowed_tools: str | None,
    output_format: str, env_extra: dict | None, mcp_config: str | None,
    mcp_strict: bool, disallowed_tools: str | None,
    session_id: str | None, resume_session_id: str | None,
    max_turns: int | None, retries: int, abort_on_auth: bool,
    stall_check: Callable[[], tuple[bool, str]] | None,
    interrupt_check: Callable[[], tuple[bool, str]] | None,
    stall_poll_s: float,
    websyn_dir: str | None = None,
) -> CallRecord:
    if not cwd:
        return cr.CallRecord(role=role, status="server", rc=2,
                             stderr_tail="containerized claude-code needs cwd=")
    image = image or cfg.OPENCODE_AGENT_IMAGE
    name = f"wildawm-claude-heavy-{uuid.uuid4().hex[:12]}"
    run_env = _claude_throwaway_env(llm, env_extra)
    cwd_path = Path(cwd).resolve()

    cmd = ["docker", "run", "-d", "--rm", "--name", name,
           "--cap-add", "NET_ADMIN"]
    mounted = set()
    for src, dst, mode in [
        (str(cwd_path), str(cwd_path), "rw"),
        (str(cfg.PROJECT_ROOT), str(cfg.PROJECT_ROOT), "ro"),
    ]:
        cmd += ["-v", f"{src}:{dst}:{mode}"]
        mounted.add(src)
    for p in extra_paths or []:
        src = str(Path(p).resolve())
        if not src or src in mounted:
            continue
        if not os.path.exists(src):
            return cr.CallRecord(
                role=role, status="server", rc=2,
                stderr_tail=f"evidence path missing for claude-code judge: {src}")
        cmd += ["-v", f"{src}:{src}:ro"]
        mounted.add(src)
    # Read-only site tree so the judge can verify web facts at /websyn.
    if websyn_dir:
        cmd += ["-v", f"{websyn_dir}:/websyn:ro"]
    for k, v in run_env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd.append(image)

    started = False
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
        if p.returncode != 0:
            return cr.CallRecord(role=role, status="server", rc=p.returncode,
                                 stderr_tail=(p.stderr or p.stdout))
        started = True
        ready, detail = _wait_claude_container_ready(name)
        if not ready:
            return cr.CallRecord(role=role, status="timeout", rc=124,
                                 stderr_tail=detail)
        rec = cr.run_claude_code(
            prompt, llm, role=role, timeout=timeout,
            container=name, cwd=str(cwd_path), workdir=str(cwd_path),
            as_user=True, system_prompt_file=system_prompt_file,
            allowed_tools=allowed_tools, output_format=output_format,
            proxy_url=None, env_extra=run_env, mcp_config=mcp_config,
            mcp_strict=mcp_strict, disallowed_tools=disallowed_tools,
            session_id=session_id, resume_session_id=resume_session_id,
            max_turns=max_turns, retries=retries, abort_on_auth=abort_on_auth,
            stall_check=stall_check, interrupt_check=interrupt_check,
            stall_poll_s=stall_poll_s)
        rec.extra["containerized"] = True
        rec.extra["container_image"] = image
        return rec
    finally:
        if started:
            subprocess.run(["docker", "rm", "-f", name],
                           capture_output=True, text=True,
                           timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)


def run_agent(prompt: str, *, role: str, claude_llm: "cr.LLM | None" = None,
              timeout: int,
              container: str | None = None, cwd: str | None = None,
              image: str | None = None, extra_paths: list[str] | None = None,
              workdir: str = "/workspace", as_user: bool = True,
              system_prompt_file: str | None = None,
              allowed_tools: str | None = None,
              disallowed_tools: str | None = None,
              output_format: str = "json", proxy_url: str | None = None,
              env_extra: dict | None = None,
              mcp_config: str | None = None, mcp_strict: bool = True,
              session_id: str | None = None,
              resume_session_id: str | None = None,
              max_turns: int | None = None,
              nudge_max: int = 0, nudge_prompt: str | None = None,
              done_check: "Callable[[], tuple[bool, str]] | None" = None,
              retries: int = 1, abort_on_auth: bool = True,
              stall_check: Callable[[], tuple[bool, str]] | None = None,
              interrupt_check: Callable[[], tuple[bool, str]] | None = None,
              stall_poll_s: float = 10.0,
              harness: str | None = None, container_mcp: bool = False,
              opencode_model: OpencodeModel | None = None,
              vision: bool = True,
              vision_note: str | None = None,
              websyn_dir: str | None = None,
              opencode_container_scratch: str | None = None) -> CallRecord:
    """Run one agent turn through the harness resolved for ``role``."""
    harness = harness or resolve_harness(role)
    # Auto-attach the role's completion nudge; keep the resume count finite.
    if not nudge_max:
        nudge_max = cfg.SYNTHESIS_MAX_LLM_TURNS
    if nudge_prompt is None:
        nudge_prompt = reminder_for(role)
    if harness == "claude-code":
        if claude_llm is None:
            raise ValueError(f"role {role!r} resolves to claude-code but no "
                             "claude_llm was supplied")
        if container is None:
            return _run_claude_code_throwaway(
                prompt, claude_llm, role=role, timeout=timeout, cwd=cwd,
                image=image, extra_paths=extra_paths,
                system_prompt_file=system_prompt_file,
                allowed_tools=allowed_tools, output_format=output_format,
                env_extra=env_extra, mcp_config=mcp_config,
                mcp_strict=mcp_strict, disallowed_tools=disallowed_tools,
                session_id=session_id, resume_session_id=resume_session_id,
                max_turns=max_turns, retries=retries,
                abort_on_auth=abort_on_auth, stall_check=stall_check,
                interrupt_check=interrupt_check, stall_poll_s=stall_poll_s,
                websyn_dir=websyn_dir)
        return cr.run_claude_code(
            prompt, claude_llm, role=role, timeout=timeout,
            container=container, cwd=cwd, workdir=workdir, as_user=as_user,
            system_prompt_file=system_prompt_file, allowed_tools=allowed_tools,
            output_format=output_format, proxy_url=proxy_url, env_extra=env_extra,
            mcp_config=mcp_config, mcp_strict=mcp_strict,
            disallowed_tools=disallowed_tools, session_id=session_id,
            resume_session_id=resume_session_id, max_turns=max_turns,
            retries=retries, abort_on_auth=abort_on_auth,
            stall_check=stall_check, interrupt_check=interrupt_check,
            stall_poll_s=stall_poll_s)
    if harness != "opencode":
        raise ValueError(f"unknown synthesis harness {harness!r}")
    model = opencode_model or resolve_opencode_model(role)
    if container is None and image is None:
        image = cfg.OPENCODE_AGENT_IMAGE
    return run_opencode_code(
        prompt, model, role=role, timeout=timeout,
        container=container, image=image, cwd=cwd, workdir=workdir,
        extra_paths=extra_paths, as_user=as_user, container_mcp=container_mcp,
        system_prompt_file=system_prompt_file,
        allowed_tools=allowed_tools, disallowed_tools=disallowed_tools,
        mcp_config=mcp_config, session_id=session_id,
        resume_session_id=resume_session_id, max_turns=max_turns,
        nudge_max=nudge_max, nudge_prompt=nudge_prompt, done_check=done_check,
        retries=retries, abort_on_auth=abort_on_auth, env_extra=env_extra,
        stall_check=stall_check, interrupt_check=interrupt_check,
        stall_poll_s=stall_poll_s, proxy_url=proxy_url,
        output_format=output_format, mcp_strict=mcp_strict, vision=vision,
        vision_note=vision_note,
        websyn_dir=websyn_dir,
        opencode_container_scratch=opencode_container_scratch)
