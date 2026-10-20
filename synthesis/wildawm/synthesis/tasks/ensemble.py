#!/usr/bin/env python3
"""Ensemble solving: sampled (model x harness) cells each solve a task in a fresh world."""
import json
import random
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.cli.nudge import agent_container_name, completion_probe_loop
from wildawm.synthesis.tasks import provision as prov
from wildawm.synthesis.tasks import world_patch as wp


def _api(method: str, path: str, body: dict | None = None, *,
         timeout: int = cfg.HTTP_MEDIUM_TIMEOUT_S) -> dict:
    url = cfg.DASHBOARD.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _proxy_llm_calls(harness: str, name: str, *, reset: bool = False) -> int | None:
    """Read (optionally reset) the in-container proxy's LLM-call counter; harness-agnostic depth."""
    container = f"wildawm-agent-{harness}-{name}"
    url = f"http://localhost:{cfg.LLM_PROXY_PORT}/stats" + ("?reset=1" if reset else "")
    try:
        out = subprocess.run(
            ["docker", "exec", container, "curl", "-s", "-m",
             str(cfg.HTTP_PROBE_TIMEOUT_S), url],
            capture_output=True, text=True, timeout=cfg.HTTP_SHORT_TIMEOUT_S)
        if out.returncode == 0 and out.stdout.strip():
            return int(json.loads(out.stdout).get("llm_calls"))
    except Exception:                                   # noqa: BLE001
        pass
    return None


def _capture_trajectory(harness: str, name: str, session: str, dest: Path,
                        structured: list,
                        proxy_trajectory: dict | None = None) -> None:
    """Archive a cell's trajectory bundle (structured, raw, images, proxy log); never raises."""
    container = f"wildawm-agent-{harness}-{name}"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "structured.json").write_text(json.dumps(structured or [], indent=1))
    except Exception:                                   # noqa: BLE001
        return
    try:  # raw harness-native transcript
        out = subprocess.run(
            ["docker", "exec", container, "curl", "-s",
             f"http://localhost:{cfg.AGENT_ADAPTER_PORT}/raw?session_id={session}"],
            capture_output=True, text=True, timeout=cfg.HTTP_SHORT_TIMEOUT_S)
        if out.returncode == 0 and out.stdout.strip().startswith("{"):
            ro = json.loads(out.stdout)
            (dest / f"raw.{ro.get('ext', 'txt')}").write_text(ro.get("raw", "") or "")
    except Exception:                                   # noqa: BLE001
        pass
    try:  # proxy-captured images
        (dest / "images").mkdir(exist_ok=True)
        subprocess.run(["docker", "cp", f"{container}:/tmp/xai_proxy_images/.",
                        str(dest / "images")],
                       capture_output=True, timeout=cfg.DOCKER_COMMAND_TIMEOUT_S)
    except Exception:                                   # noqa: BLE001
        pass
    try:  # canonical proxy trajectory
        proxy = proxy_trajectory if proxy_trajectory is not None else \
            _proxy_trajectory(container)
        if proxy is not None:
            (dest / "proxy_trajectory.json").write_text(
                json.dumps(proxy, ensure_ascii=False))
    except Exception:                                   # noqa: BLE001
        pass


_CLI_NAMES = {
    "bash", "write", "read", "shell", "exec_command", "apply_patch",
    "edit", "multiedit", "glob", "grep", "notebookedit", "str_replace_editor",
    "run_command", "text_editor", "ls", "view", "create_file", "edit_file",
    "write_file", "read_file", "execute", "terminal",
    "execute_code", "search_files", "read_files", "list_files", "write_files",
}
_WEB_NAMES = ("browser", "navigate", "screenshot", "vision", "page_", "click",
              "goto", "web_", "playwright")
_MCP_CALL_HINT = re.compile(r"mcp_call\.py\s+([a-z][a-z0-9_]*)|mcp__([a-z_]+)__")
# Non-local URLs in shell calls count as Web; local gateway calls are MCP.
_WEB_HINT = re.compile(r"https?://(?!(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::|/))", re.I)
_WEB_CLI_HINT = re.compile(r"\bbrowser-use\b|describe_image_mcp\.py")


def interface_of(name: str, arguments: str = "") -> str | None:
    """Map a tool call to an agent interface (cli / mcp / web) across harness naming conventions."""
    n = (name or "").lower()
    if n.startswith("mcp__browser__") or any(w in n for w in _WEB_NAMES):
        return "web"
    if n.startswith("mcp__") or n.startswith("mcp_"):
        return "mcp"
    # OpenCode flattens MCP calls as ``<server>_<tool>``, repeating the server name.
    if re.match(r"^(.+?)_\1_", n):
        return "mcp"
    if n in _CLI_NAMES or n.split("_")[0] in ("bash", "shell"):
        # Shell calls can reach MCP or Web, so classify their arguments first.
        a = arguments or ""
        if _MCP_CALL_HINT.search(a):
            return "mcp"
        if _WEB_HINT.search(a) or _WEB_CLI_HINT.search(a):
            return "web"
        return "cli"
    return None  # planning/meta tools


def trajectory_stats(history: list[dict]) -> dict:
    steps = sum(1 for h in history
                if h.get("type") == "message" and h.get("role") == "assistant")
    tool_calls, iface_seq, unknown = 0, [], set()
    for h in history:
        if h.get("type") != "tool_call":
            continue
        tool_calls += 1
        args = h.get("arguments")
        args_s = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) \
            else str(args or "")
        iface = interface_of(h.get("name", ""), args_s)
        if iface:
            iface_seq.append(iface)
        else:
            unknown.add(h.get("name", "?"))
    jumps = sum(1 for i in range(1, len(iface_seq))
                if iface_seq[i] != iface_seq[i - 1])
    return {"steps": steps, "tool_calls": tool_calls, "jumps": jumps,
            "interfaces": sorted(set(iface_seq)),
            "unknown_tools": sorted(unknown)[:12]}


def _proxy_tool_fields(call: dict) -> tuple[str, str]:
    fn = call.get("function") if isinstance(call, dict) else None
    if isinstance(fn, dict):
        name = fn.get("name") or call.get("name") or ""
        args = fn.get("arguments")
    else:
        name = call.get("name") if isinstance(call, dict) else ""
        args = call.get("arguments") if isinstance(call, dict) else ""
    if isinstance(args, (dict, list)):
        args_s = json.dumps(args, ensure_ascii=False)
    else:
        args_s = str(args or "")
    return str(name or ""), args_s


def trajectory_stats_from_proxy(proxy: dict) -> dict:
    """Interface stats from the proxy trajectory (fallback when harness history is lost)."""
    sessions = proxy.get("sessions") if isinstance(proxy, dict) else {}
    steps, tool_calls, iface_seq, unknown = 0, 0, [], set()
    if not isinstance(sessions, dict):
        sessions = {}
    for session_steps in sessions.values():
        if not isinstance(session_steps, list):
            continue
        for step in session_steps:
            if not isinstance(step, dict):
                continue
            out = step.get("output") or {}
            if isinstance(out, dict):
                steps += 1
                calls = out.get("tool_calls") or []
            else:
                calls = []
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                tool_calls += 1
                name, args_s = _proxy_tool_fields(call)
                iface = interface_of(name, args_s)
                if iface:
                    iface_seq.append(iface)
                else:
                    unknown.add(name or "?")
    jumps = sum(1 for i in range(1, len(iface_seq))
                if iface_seq[i] != iface_seq[i - 1])
    return {"steps": steps, "tool_calls": tool_calls, "jumps": jumps,
            "interfaces": sorted(set(iface_seq)),
            "unknown_tools": sorted(unknown)[:12]}


def _proxy_trajectory(container: str) -> dict | None:
    try:
        out = subprocess.run(
            ["docker", "exec", container, "curl", "-s",
             f"http://localhost:{cfg.LLM_PROXY_PORT}/trajectory"],
            capture_output=True, text=True, timeout=cfg.HTTP_SHORT_TIMEOUT_S)
        if out.returncode == 0 and out.stdout.strip().startswith("{"):
            return json.loads(out.stdout)
    except Exception:                                   # noqa: BLE001
        pass
    return None


def _merge_proxy_stats(stats: dict, proxy_stats: dict | None) -> dict:
    if not proxy_stats:
        return stats
    current_tools = stats.get("tool_calls")
    current_interfaces = stats.get("interfaces") or []
    proxy_tools = proxy_stats.get("tool_calls")
    if (not current_tools or not current_interfaces
            or (proxy_tools or 0) > (current_tools or 0)):
        merged = dict(stats)
        for key in ("steps", "tool_calls", "jumps", "interfaces", "unknown_tools"):
            merged[key] = proxy_stats.get(key)
        merged["stats_source"] = "proxy_trajectory"
        return merged
    return stats


def _merge_phase_stats(task_stats: dict | None,
                       final_stats: dict | None) -> dict:
    """Merge task-solve and completion-probe stats so a session reset cannot erase evidence."""
    if not task_stats:
        return dict(final_stats or {})
    if not final_stats:
        return dict(task_stats)
    merged = dict(final_stats)
    for key in ("steps", "tool_calls", "jumps"):
        values = [v for v in (task_stats.get(key), final_stats.get(key))
                  if isinstance(v, (int, float))]
        merged[key] = max(values) if values else None
    merged["interfaces"] = sorted(
        set(task_stats.get("interfaces") or [])
        | set(final_stats.get("interfaces") or []))
    merged["unknown_tools"] = sorted(
        set(task_stats.get("unknown_tools") or [])
        | set(final_stats.get("unknown_tools") or []))[:12]
    merged["stats_source"] = "task_solve+completion_probe"
    return merged


def snapshot_world(world_id: str, dest: Path) -> Path:
    """Copy the instance's judgeable state (workspace, data, web DBs) to ``dest`` on the host."""
    inst = prov.instance_dir(world_id)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for d in ("workspace", "data"):
        s = inst / d
        if s.exists():
            shutil.copytree(s, dest / d, symlinks=True)
    web = dest / "web"
    got = prov.snapshot_webharbor_served(world_id, web)
    if not got:
        shutil.rmtree(web, ignore_errors=True)
    return dest


def cells_for(*, harnesses: list[str] | None = None,
              models: list[str] | None = None) -> list[tuple[str, str]]:
    """Build a harness×model cell pool, excluding chronically-dead harnesses."""
    hs = harnesses if harnesses is not None else cfg.ENSEMBLE_HARNESSES
    ms = models if models is not None else cfg.ENSEMBLE_MODELS
    if not hs or not ms:
        missing = []
        if not ms:
            missing.append("WILDAWM_ENSEMBLE_MODELS (models.json refs)")
        if not hs:
            missing.append("WILDAWM_ENSEMBLE_HARNESSES (harness names)")
        raise RuntimeError(
            "the ensemble solver pool is not configured: set "
            + " and ".join(missing)
            + " (or AWM_ENSEMBLE_MODELS/AWM_ENSEMBLE_HARNESSES, or the "
              "ensemble_models/ensemble_harnesses keys of "
              "WILDAWM_SYNTHESIS_CONFIG); see configs/synthesis.example.jsonc")
    return [(h, m) for h in hs for m in ms if h not in cfg.DEAD_HARNESSES]


def all_cells() -> list[tuple[str, str]]:
    """The live (model × harness) pool, excluding chronically-dead harnesses."""
    return cells_for()


def sample_cells(n: int, *, seed: str, pool: list[tuple[str, str]] | None = None,
                 exclude: list[tuple[str, str]] = ()) -> list[tuple[str, str]]:
    """Uniform, seed-deterministic sample of (model x harness) cells."""
    pool = [c for c in (pool or all_cells()) if c not in set(exclude)]
    rng = random.Random(seed)
    return rng.sample(pool, min(n, len(pool)))


_MODEL_HEALTH_CACHE: dict[str, tuple[bool, str]] = {}


def _models_url(base_url: str) -> str:
    """Return the provider's OpenAI-compatible /models endpoint."""
    parsed = urllib.parse.urlparse(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    if path.endswith("/v1"):
        path = f"{path}/models"
    else:
        path = f"{path}/v1/models"
    return urllib.parse.urlunparse(parsed._replace(path=path, query="", fragment=""))


def model_health(model_ref: str) -> tuple[bool, str]:
    """Fast endpoint/model availability check so dead endpoints are skipped, not timed out."""
    if not cfg.ENSEMBLE_MODEL_PREFLIGHT:
        return True, "preflight disabled"
    if model_ref in _MODEL_HEALTH_CACHE:
        return _MODEL_HEALTH_CACHE[model_ref]
    try:
        spec = cfg.model_spec(model_ref)
        req = urllib.request.Request(
            _models_url(spec["base_url"]),
            headers={"Authorization": f"Bearer {spec.get('api_key', '')}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=cfg.HTTP_PROBE_TIMEOUT_S) as r:
            payload = json.loads(r.read() or b"{}")
        ids = {str(m.get("id", "")) for m in payload.get("data", []) if isinstance(m, dict)}
        api_model = str(spec.get("api_model") or "")
        if ids and api_model not in ids:
            result = False, f"model {api_model!r} not listed by provider"
        else:
            result = True, "ok"
    except Exception as e:  # noqa: BLE001
        result = False, f"{type(e).__name__}: {e}"
    _MODEL_HEALTH_CACHE[model_ref] = result
    return result


def _preflight_cells(cells: list[tuple[str, str]], *, log=print
                     ) -> tuple[list[tuple[str, str]], list["CellRun"]]:
    if not cfg.ENSEMBLE_MODEL_PREFLIGHT:
        return cells, []
    by_model = sorted({m for _, m in cells})
    unhealthy: dict[str, str] = {}
    for model in by_model:
        ok, reason = model_health(model)
        if not ok:
            unhealthy[model] = reason
            log(f"    ensemble preflight skip: {model} unavailable ({reason})")
    if not unhealthy:
        return cells, []
    healthy_cells = [(h, m) for h, m in cells if m not in unhealthy]
    skipped = []
    for h, m in cells:
        if m not in unhealthy:
            continue
        skipped.append(CellRun(
            harness=h,
            model=m,
            status="infra:model_unavailable",
            error=unhealthy[m],
        ))
    return healthy_cells, skipped


@dataclass
class CellRun:
    harness: str
    model: str
    world_id: str = ""
    status: str = "ok"  # ok | infra:<detail>
    seconds: float = 0.0
    reply: str = ""
    before_dir: str = ""
    after_dir: str = ""
    stats: dict = field(default_factory=dict)
    error: str = ""
    attempts: int = 1  # >1 when retried on transient infra
    raw_history: list = field(default_factory=list)  # full structured transcript
    trajectory_dir: str = ""  # structured + raw + images
    prompt_group: str = ""  # hinted | bare | ...
    completion_probe: dict = field(default_factory=dict)

    @property
    def cell(self) -> str:
        return f"{self.harness}+{self.model}"

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d.pop("raw_history", None)
        d["cell"] = self.cell
        return d


# Infra statuses a fresh world might fix.
_RETRYABLE_INFRA = ("infra:http_502", "infra:http_500", "infra:http_503",
                    "infra:exception", "infra:completion_probe")


def run_cell(profile: str, day: int, slug: str, idx: int, harness: str,
             model: str, message: str, scratch: Path, *,
             timeout_s: int | None = None, log=print,
             retries: int | None = None, max_turns: int | None = None,
             world_patch: str | Path | None = None,
             completion_probe_task_text: str | None = None) -> CellRun:
    """Solve one cell, retrying transient infra failures in a fresh world."""
    retries = cfg.CELL_RETRY if retries is None else retries
    run = None
    for attempt in range(retries + 1):
        run = _run_cell_once(profile, day, slug, idx if attempt == 0 else f"{idx}r{attempt}",
                             harness, model, message, scratch, timeout_s=timeout_s,
                             max_turns=max_turns, log=log,
                             world_patch=world_patch,
                             completion_probe_task_text=completion_probe_task_text)
        run.attempts = attempt + 1
        if not run.status.startswith("infra"):
            return run
        if not any(run.status.startswith(p) for p in _RETRYABLE_INFRA) or attempt >= retries:
            return run
        log(f"    [cell {idx}] {harness}+{model} {run.status} → retry "
            f"{attempt + 1}/{retries} (fresh world)")
    return run


def _run_cell_once(profile: str, day: int, slug: str, idx, harness: str,
                   model: str, message: str, scratch: Path, *,
                   timeout_s: int | None = None, max_turns: int | None = None,
                   log=print, world_patch: str | Path | None = None,
                   completion_probe_task_text: str | None = None) -> CellRun:
    timeout_s = timeout_s or cfg.SOLVE_TIMEOUT_S
    max_turns = cfg.SOLVE_MAX_TURNS if max_turns is None else max_turns
    run = CellRun(harness=harness, model=model)
    name = f"{cfg.INSTANCE_PREFIX}-{slug}-c{idx}-{uuid.uuid4().hex[:4]}".lower()
    t0 = time.time()
    deadline = t0 + float(timeout_s)
    timing_stats: dict[str, float] = {}
    try:
        body = {"profile": profile, "day": day, "name": name}
        patch_dir = wp.resolve_patch_dir(world_patch) if world_patch else None
        if patch_dir:
            body["world_patch"] = str(patch_dir)
        created = _api("POST", "/api/worlds", body,
                       timeout=cfg.RUNTIME_PROVISION_TIMEOUT_S)
        run.world_id = created["id"]
        _api("POST", f"/api/worlds/{run.world_id}/runtime/start",
             {"enable_webharbor": True}, timeout=cfg.RUNTIME_PROVISION_TIMEOUT_S)
        _api("POST", f"/api/worlds/{run.world_id}/agents/{harness}/start",
             {"model": model}, timeout=cfg.RUNTIME_PROVISION_TIMEOUT_S)
        run.before_dir = str(snapshot_world(run.world_id,
                                            scratch / f"c{idx}_before"))
        session = str(uuid.uuid4())
        _proxy_llm_calls(harness, name, reset=True)  # fence the solve's LLM calls
        log(f"    [cell {idx}] {harness}+{model} solving (≤{timeout_s}s)…")
        budget_end = None
        base_t0 = time.time()
        try:
            chat = _api("POST", f"/api/worlds/{run.world_id}/agent/chat",
                        {"message": message, "kind": harness, "session_id": session,
                         "timeout_s": timeout_s, "max_turns": max_turns},
                        timeout=cfg.agent_chat_timeout(timeout_s, hops=4))
            run.reply = chat.get("content") or ""
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode(errors="replace")
            except Exception:                               # noqa: BLE001
                pass
            m = re.search(r"force-ended \((max_turns|timeout)\)", detail)
            if not m:
                raise  # real crash: infra, resample
            # A budget force-end is a valid result: capture the partial trajectory and judge it.
            budget_end = m.group(1)
            run.reply = ""
        timing_stats["base_seconds"] = round(time.time() - base_t0, 3)
        task_history: list[dict] = []
        task_proxy: dict | None = None
        task_stats: dict | None = None
        tdir = scratch / f"c{idx}_trajectory"
        if completion_probe_task_text:
            # Capture the task turn first; some adapters replace history on the next chat.
            try:
                hist = _api("GET", f"/api/worlds/{run.world_id}/agent/history"
                                   f"?session_id={session}&kind={harness}",
                            timeout=cfg.RUNTIME_SHORT_TIMEOUT_S)
                task_history = hist if isinstance(hist, list) else \
                    hist.get("history") or hist.get("messages") or []
            except Exception:                           # noqa: BLE001
                task_history = []
            task_proxy = _proxy_trajectory(f"wildawm-agent-{harness}-{name}")
            task_stats = _merge_proxy_stats(
                trajectory_stats(task_history),
                trajectory_stats_from_proxy(task_proxy)
                if task_proxy is not None else None,
            )
            _capture_trajectory(
                harness, name, session, tdir / "task_solve", task_history,
                proxy_trajectory=task_proxy,
            )
            probe_t0 = time.time()

            def _probe_chat(prompt: str, *, timeout_s: int | None = None) -> dict:
                probe_timeout = max(1, int(timeout_s or max(1, deadline - time.time())))
                return _api(
                    "POST", f"/api/worlds/{run.world_id}/agent/chat",
                    {"message": prompt, "kind": harness, "session_id": session,
                     "timeout_s": probe_timeout, "max_turns": max_turns},
                    timeout=cfg.agent_chat_timeout(probe_timeout, hops=4))

            run.completion_probe = completion_probe_loop(
                chat_func=_probe_chat,
                container=agent_container_name(name, harness),
                task_text=completion_probe_task_text,
                max_nudges=cfg.TEST_NUDGE_MAX,
                deadline=deadline,
                log=lambda msg: log(f"    [cell {idx}] {msg}"),
                echo_reply=False,
            )
            timing_stats["probe_seconds"] = round(time.time() - probe_t0, 3)
            if (not run.completion_probe.get("done")
                    and run.completion_probe.get("errors")
                    and not run.completion_probe.get("budget_exhausted")):
                run.status = "infra:completion_probe"
                run.error = "; ".join(run.completion_probe.get("errors") or [])
        llm_calls = _proxy_llm_calls(harness, name)
        run.after_dir = str(snapshot_world(run.world_id,
                                           scratch / f"c{idx}_after"))
        try:
            hist = _api("GET", f"/api/worlds/{run.world_id}/agent/history"
                               f"?session_id={session}&kind={harness}",
                        timeout=cfg.RUNTIME_SHORT_TIMEOUT_S)
            entries = hist if isinstance(hist, list) else \
                hist.get("history") or hist.get("messages") or []
            run.raw_history = entries
            run.stats = trajectory_stats(entries)
        except Exception as e:                          # noqa: BLE001
            run.stats = {"steps": None, "tool_calls": None, "jumps": None,
                         "interfaces": [], "history_error": str(e)}
        proxy = _proxy_trajectory(f"wildawm-agent-{harness}-{name}")
        run.stats = _merge_proxy_stats(
            run.stats,
            trajectory_stats_from_proxy(proxy) if proxy is not None else None,
        )
        if completion_probe_task_text:
            run.stats = _merge_phase_stats(task_stats, run.stats)
        run.stats.update(timing_stats)
        run.stats["llm_calls"] = llm_calls
        if budget_end:
            run.status = "budget"
            run.stats["budget_end"] = budget_end
        if (run.completion_probe or {}).get("budget_exhausted"):
            run.stats["budget_end"] = (
                run.stats.get("budget_end") or "completion_probe_time_budget"
            )
        # Final adapter view at the root; task_solve/ keeps the pre-probe transcript.
        _capture_trajectory(harness, name, session, tdir, run.raw_history,
                            proxy_trajectory=proxy)
        run.trajectory_dir = str(tdir)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")
        except Exception:                               # noqa: BLE001
            pass
        run.status, run.error = f"infra:http_{e.code}", detail
    except Exception as e:                              # noqa: BLE001
        run.status, run.error = "infra:exception", str(e)
    finally:
        run.seconds = round(time.time() - t0, 1)
        if run.world_id:
            try:
                _api("DELETE", f"/api/worlds/{run.world_id}",
                     timeout=cfg.RUNTIME_SHORT_TIMEOUT_S)
            except Exception:                           # noqa: BLE001
                pass
    return run


def run_ensemble_resilient(profile: str, day: int, slug: str, message: str,
                           scratch: Path, *, n: int, seed: str,
                           exclude: list[tuple[str, str]] = (),
                           timeout_s: int | None = None, log=print,
                           world_patch: str | Path | None = None) -> list[CellRun]:
    """Resample replacements for infra cells until N live runs; returns all runs."""
    scratch.mkdir(parents=True, exist_ok=True)
    used = set(exclude)
    available, skipped = _preflight_cells(
        [c for c in all_cells() if c not in used], log=log)
    runs: list[CellRun] = list(skipped)
    for r in skipped:
        used.add((r.harness, r.model))
    live = 0
    rng_round = 0
    batch = sample_cells(n, seed=seed, pool=available, exclude=list(used))
    extra_budget = cfg.MAX_INFRA_RESAMPLE
    while batch:
        for h, m in batch:
            used.add((h, m))
        log(f"    ensemble draw: {[f'{h}+{m}' for h, m in batch]}")
        with ThreadPoolExecutor(max_workers=len(batch)) as ex:
            futs = [ex.submit(run_cell, profile, day, slug,
                              f"{rng_round}_{i}", h, m, message, scratch,
                              timeout_s=timeout_s, log=log,
                              world_patch=world_patch)
                    for i, (h, m) in enumerate(batch)]
            batch_runs = [f.result() for f in futs]
        runs.extend(batch_runs)
        live += sum(1 for r in batch_runs if not r.status.startswith("infra"))
        if live >= n or extra_budget <= 0:
            break
        need = min(n - live, extra_budget)
        extra_budget -= need
        rng_round += 1
        batch = sample_cells(need, seed=f"{seed}-r{rng_round}",
                             pool=available, exclude=list(used))
        if not batch:
            break
    return runs


def run_ensemble_resilient_groups(profile: str, day: int, slug: str,
                                  groups: dict[str, dict], *,
                                  seed: str,
                                  exclude: list[tuple[str, str]] = (),
                                  timeout_s: int | None = None, log=print,
                                  world_patch: str | Path | None = None
                                  ) -> dict[str, list[CellRun]]:
    """Run several independent ensemble draws in one scheduler pass."""
    used = set(exclude)
    results: dict[str, list[CellRun]] = {}
    available_by_group: dict[str, list[tuple[str, str]]] = {}
    for name, spec in groups.items():
        raw_pool = cells_for(
            harnesses=spec.get("harnesses"),
            models=spec.get("models"),
        )
        available, skipped = _preflight_cells(
            [c for c in raw_pool if c not in used], log=log)
        available_by_group[name] = available
        results[name] = []
        for r in skipped:
            r.prompt_group = name
            results[name].append(r)
            used.add((r.harness, r.model))
    live_counts = {name: 0 for name in groups}
    extra_budget = {name: cfg.MAX_INFRA_RESAMPLE for name in groups}
    rounds = {name: 0 for name in groups}

    for spec in groups.values():
        Path(spec["scratch"]).mkdir(parents=True, exist_ok=True)

    while True:
        jobs: list[tuple[str, int, str, str]] = []
        for name, spec in groups.items():
            target = max(0, int(spec.get("n", 0)))
            if live_counts[name] >= target:
                continue
            if rounds[name] == 0:
                need = target - live_counts[name]
            else:
                need = min(target - live_counts[name], extra_budget[name])
                extra_budget[name] -= need
            if need <= 0:
                continue
            batch = sample_cells(
                need,
                seed=f"{seed}-{name}-r{rounds[name]}",
                pool=available_by_group[name],
                exclude=list(used),
            )
            rounds[name] += 1
            if not batch:
                continue
            base_idx = len(results[name])
            for offset, (h, m) in enumerate(batch):
                used.add((h, m))
                jobs.append((name, base_idx + offset, h, m))

        if not jobs:
            break

        log("    ensemble draw: " + str([
            f"{name}:{h}+{m}" for name, _idx, h, m in jobs
        ]))
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = []
            for name, idx, h, m in jobs:
                spec = groups[name]
                futs.append((name, ex.submit(
                    run_cell, profile, day, slug, f"{name}_{idx}", h, m,
                    spec["message"], Path(spec["scratch"]),
                    timeout_s=timeout_s, log=log, world_patch=world_patch,
                    completion_probe_task_text=spec.get(
                        "completion_probe_task_text"))))
            for name, fut in futs:
                run = fut.result()
                run.prompt_group = name
                results[name].append(run)
                if not run.status.startswith("infra"):
                    live_counts[name] += 1

        if all(live_counts[name] >= max(0, int(spec.get("n", 0)))
               or extra_budget[name] <= 0
               for name, spec in groups.items()):
            break

    return results
