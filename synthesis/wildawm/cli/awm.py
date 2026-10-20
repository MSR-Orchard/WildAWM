#!/usr/bin/env python3
"""awm — WildAWM operator CLI: a REST client for the dashboard plus synthesis commands."""

import enum
import json as _json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from tabulate import tabulate  # noqa: E402
from tqdm import tqdm  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from simpleArgParser import parse_args_with_commands  # noqa: E402

from wildawm import const_config as awm_cfg
from wildawm.const_config import (
    DEFAULT_REASONING_EFFORT,
    DEFAULT_AGENT_MAX_TURNS,
    DEFAULT_AGENT_TURN_TIMEOUT_S,
    DASHBOARD as DEFAULT_DASHBOARD,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    LOG_DIR as ROOT_LOG_DIR,
    HTTP_MEDIUM_TIMEOUT_S,
    LOCAL_SCRIPT_TIMEOUT_S,
    OUTPUTS_DIR,
    RUNTIME_HEAVY_TIMEOUT_S,
    RUNTIME_PROVISION_TIMEOUT_S,
    RUNTIME_SHORT_TIMEOUT_S,
    SUBPROCESS_FAST_TIMEOUT_S,
    WORLD_SYN_MAX_IMPLEMENT_ITERS,
    agent_chat_timeout,
)
from wildawm.cli.nudge import (
    agent_container_name,
    completion_probe_loop,
    initial_completion_contract,
    new_completion_marker_path,
)
from wildawm.synthesis.worlds import profile as ws_profile
from wildawm.synthesis.worlds import dayplan as ws_dayplan
from wildawm.synthesis.worlds import dayimpl as ws_dayimpl
from wildawm.synthesis.worlds import prebake as ws_prebake
from wildawm.synthesis.worlds import orchestrator as ws_world
from wildawm.synthesis.worlds import anchors as ws_anchors
from wildawm.core.paths import PROJECT_ROOT
from wildawm.json_utils import load_jsonc
from wildawm.synthesis.tasks import run_synthesis as task_synthesis
from wildawm.synthesis.tasks import tasks_index as tx
from wildawm.synthesis.tasks import verifier as vf
from wildawm.verify_stats import (
    aggregate_stats,
    compact_stats_line,
    stats_from_manifest,
    stats_from_verify_response,
    table_stats_cells,
)

LOG_DIR = ROOT_LOG_DIR / "cli"
LOG_DIR.mkdir(parents=True, exist_ok=True)


_LOG_PATH = LOG_DIR / f"awm-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
_log_fh = open(_LOG_PATH, "a")  # noqa: SIM115
_LOG_LOCK = threading.RLock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _LOG_LOCK:
        print(line, flush=True)
        _log_fh.write(line + "\n")
        _log_fh.flush()


def die(msg: str, code: int = 1):
    log(f"ERROR: {msg}")
    sys.exit(code)


class Client:
    def __init__(self, base: str, *, headers: dict[str, str] | None = None):
        self.base = base.rstrip("/")
        self.headers = dict(headers or {})

    @staticmethod
    def _refused(e) -> bool:
        """Connection refused means the request was never sent, so any method may be retried."""
        return isinstance(e, ConnectionRefusedError) or \
            isinstance(getattr(e, "reason", None), ConnectionRefusedError)

    def _req(self, method: str, path: str, body=None,
             timeout: float = HTTP_MEDIUM_TIMEOUT_S,
             retries: int = 1):
        """`retries` caps attempts; GETs retry 5xx/timeouts, POST/DELETE only connection refused."""
        url = self.base + path
        data = _json.dumps(body).encode() if body is not None else None
        retries = max(1, min(int(retries), 5))
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, method=method,
                                         headers={
                                             "Content-Type": "application/json",
                                             **self.headers,
                                         })
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                    return _json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                retryable = method == "GET" and e.code in (500, 502, 503, 504)
                if not retryable or attempt == retries - 1:
                    detail = e.read().decode("utf-8", "ignore")
                    raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                retryable = method == "GET" or self._refused(e)
                if not retryable or attempt == retries - 1:
                    raise RuntimeError(
                        f"{method} {path} -> {e} (is the dashboard up at {self.base}?)")
            time.sleep(min(2 ** attempt + 1, 30))

    def get(self, path, timeout=HTTP_MEDIUM_TIMEOUT_S, retries=5):
        return self._req("GET", path, None, timeout, retries)

    def post(self, path, body=None, timeout=HTTP_MEDIUM_TIMEOUT_S, retries=5):
        return self._req("POST", path, body if body is not None else {}, timeout, retries)

    def delete(self, path, timeout=HTTP_MEDIUM_TIMEOUT_S, retries=5):
        return self._req("DELETE", path, None, timeout, retries)


def out(cfg, obj):
    """Print raw JSON when --json, else return for the handler's own summary."""
    if getattr(cfg, "json", False):
        print(_json.dumps(obj, indent=2))
    return obj


# `claude-code` has a hyphen, so Harness uses the functional Enum API.
Harness = enum.Enum("Harness", {
    "nanobot": "nanobot",
    "opencode": "opencode",
    "claude-code": "claude-code",
    "codex": "codex",
    "hermes": "hermes",
    "openclaw": "openclaw",
})


class RestoreState(enum.Enum):
    after = "after"
    before = "before"


class VerifyMode(enum.Enum):
    lightweight = "lightweight"
    heavy = "heavy"


def _ev(v, default: str = "") -> str:
    """A choice field's wire string: Enum -> its value; None/'' -> default."""
    if isinstance(v, enum.Enum):
        return v.value
    return v if v else default


@dataclass
class Base:
    dashboard: str = DEFAULT_DASHBOARD   # dashboard base URL (or AWM_DASHBOARD)
    json: bool = False                   # emit raw JSON instead of a summary


@dataclass
class Dashboard:
    port: int = DASHBOARD_PORT   # dashboard listen port (or AWM_DASHBOARD_PORT)
    host: str = DASHBOARD_HOST   # dashboard listen host (or AWM_DASHBOARD_HOST)


# Shared field groups; simpleArgParser flattens them into the same --llm/--task/... flags.
@dataclass
class Model:
    llm: str = ""            # model: bare name or provider/model (default: registry)
    judge_model: str = ""    # LLM-judge model (default: registry)
    reasoning_effort: str = DEFAULT_REASONING_EFFORT  # override registry default reasoning effort
    context_window: int = 0   # harness context tokens; 0 = models.json value
    max_output_tokens: int = 32768  # per-request harness output tokens; 0 = backend default


@dataclass
class Turn:
    timeout: int = DEFAULT_AGENT_TURN_TIMEOUT_S  # per-turn wall-clock budget (s); 0=off
    max_turns: int = DEFAULT_AGENT_MAX_TURNS     # per-turn assistant response budget; 0=off


@dataclass
class RunSpec:
    task: str = ""           # run this task id (-> task.md)
    prompt: str = ""         # run this natural-language prompt (XOR --task)
    use_simple_task: bool = True  # for XAI tasks, send simple_task.txt to the agent when present
    archive: bool = False    # save run to outputs/
    verify: bool = False     # run verify (implies archive; task only)
    teardown: bool = False   # destroy the world after the run
    episode: bool = False    # sugar = archive + verify + teardown
    mode: VerifyMode = VerifyMode.lightweight  # verify mode
    out: str = str(OUTPUTS_DIR)  # archive root dir
    run_name: str = ""       # override the archive folder name (default: <ts>__<task>__<kind>__<model>)


@dataclass
class WorldList(Base):
    pass


@dataclass
class WorldCreate(Base):
    # initial state (profile+day) OR restore from an archive (path)
    profile: str = ""        # profile name (required unless --path)
    day: int = -1            # starting day N (required unless --path)
    name: str = ""           # optional explicit instance name
    path: str = ""           # restore from an archived run folder (outputs/<run>)
    state: RestoreState = RestoreState.after  # restore which snapshot
    # runtime
    docker_up: bool = True   # start containers after create (no = create only)
    webharbor: bool = True   # bring up WebHarbor when starting
    # optional one-shot agent run (delegates to `agent run`)
    harness: Harness | None = None  # agent harness (omit = no agent run)
    model: Model = field(default_factory=Model)    # --llm / --judge_model
    turn: Turn = field(default_factory=Turn)        # --timeout / --max_turns
    run: RunSpec = field(default_factory=RunSpec)   # --task / --prompt / --archive / ...


@dataclass
class WorldId(Base):
    world: str = ""          # world id (required)


@dataclass
class WorldStart(Base):
    world: str = ""          # world id (required)
    webharbor: bool = True   # bring up WebHarbor


@dataclass
class WorldReset(Base):
    world: str = ""          # world id (required)
    # optional rebase to a different initial state (else this world's own)
    profile: str = ""
    day: int = -1
    path: str = ""
    state: RestoreState = RestoreState.after


@dataclass
class WorldReplicate(Base):
    world: str = ""          # source world id (required, must exist)
    n: int = 1               # how many clones
    start: bool = True       # start each clone after cloning
    webharbor: bool = True   # bring up WebHarbor on each clone


@dataclass
class WorldAttach(Base):
    world: str = ""          # world id (required)
    harness: Harness | None = None  # which agent container (default: any running one)


@dataclass
class AgentRun(Base):
    world: str = ""          # target running world (required)
    harness: Harness | None = None  # agent harness (required)
    resume: str = ""         # continue this session id (multi-turn)
    session: str = ""        # session id (default <world>:<harness>)
    model: Model = field(default_factory=Model)    # --llm / --judge_model
    turn: Turn = field(default_factory=Turn)        # --timeout / --max_turns
    run: RunSpec = field(default_factory=RunSpec)   # --task / --prompt / --archive / ...


@dataclass
class AgentHistory(Base):
    world: str = ""          # world id (required)
    harness: Harness | None = None  # agent harness (required)
    session: str = ""        # session id (default <world>:<harness>)


@dataclass
class AgentChat(Base):
    world: str = ""          # world id (required)
    harness: Harness | None = None  # agent harness (required)
    llm: str = ""            # model: switch the agent to this before chatting
    reasoning_effort: str = DEFAULT_REASONING_EFFORT  # override registry default reasoning effort
    context_window: int = 0   # harness context tokens; 0 = models.json value
    session: str = ""        # session id (default <world>:<harness>)


@dataclass
class ViewWorld(Base):
    profile: str = ""        # profile name ("" -> all profiles; else per-day breakdown)


@dataclass
class ViewTask(Base):
    world: str = ""          # world id ("" -> global registry)


@dataclass
class ViewModel(Base):
    pass


@dataclass
class Verify(Base):
    dir: str = ""            # an outputs/<run> archive OR a directory of archives (verify all under it). required
    mode: VerifyMode = VerifyMode.lightweight  # lightweight (verifier+LLM) | heavy (agent-as-a-judge)
    judge_model: str = ""    # LLM-judge model override; heavy otherwise uses the configured synthesis heavy_judge default
    out_name: str = ""       # write verdict to verify/<out_name>.json (default: verdict_<mode>)
    parallel: int = -1       # batch concurrency (spawn pool); <0 -> mode default (16 lightweight / 8 heavy)
    resume: bool = True      # skip runs whose target verdict file already exists
    force: bool = False      # recompute even when --resume would skip

    def post_process(self):
        # Heavy runs an agentic judge per run, so it defaults to a lower fan-out; --parallel wins.
        if self.parallel < 0:
            self.parallel = 8 if self.mode == VerifyMode.heavy else 16




# Synthesis commands call Python directly; `syn task` also needs the dashboard up.
@dataclass
class SynTask(Base):
    profile: str = ""            # persona name (required)
    day: int = -1                # single day-state (or use --days)
    days: str = ""               # comma list, e.g. "1,2,4" (overrides --day)
    tasks: str = ""              # explicit slot spec for ONE day: diff:slug[:web],... (else auto)
    difficulty: str = "hard,hard,medium"  # per-day accepted-result quota when --tasks is omitted
    count: int = 3               # auto-spec slots per day when --tasks omitted
    require_web: bool = True      # XAI tasks require image-decisive web by default; false emits :noweb
    parallel: int = 3            # day concurrency
    force: bool = False          # rerun accepted task outputs


# World synthesis: per-day delta.sh + overlay; days run strictly serially.
@dataclass
class SynProfile(Base):
    name: str = ""              # persona display name (required); profile id = <slug>-<name>
    max_proposals: int = 3      # diversity-gate re-propose budget
    force: bool = False         # regenerate even if the profile already exists
    steer: str = ""             # optional hint to aim the persona (e.g. at under-sampled sites)

@dataclass
class SynDayPlan(Base):
    profile: str = ""           # profile id, e.g. <slug>-<name> (required)
    day: int = 0                # day to plan (required; day 0 = initial world setup)
    force: bool = False         # overwrite an existing day_N.md
    steer: str = ""             # optional hint to guide the day's plan

@dataclass
class SynDayImpl(Base):
    profile: str = ""           # profile id (required)
    day: int = 0                # day to implement (required)
    max_iters: int = 0          # 0 → WORLD_SYN_MAX_IMPLEMENT_ITERS
    force: bool = False         # re-author even if day_N already done
    steer: str = ""             # optional hint to guide the day's implementation

@dataclass
class SynPrebake(Base):
    profile: str = ""           # profile id (required)
    to_day: int = -1            # materialize through this day (-1 = all built days)
    force: bool = False         # re-materialize existing masters
    verify_rebake: bool = False # also run the re-bake stability check

@dataclass
class SynWorld(Base):
    profile: str = ""           # profile id (required)
    to_day: int = 0             # build days 0..to_day (serial)
    force: bool = False         # regenerate
    force_plan: bool = False   # force only the plan stage
    force_impl: bool = False   # force only the impl stage


@dataclass
class SynExportHarbor(Base):
    """Export accepted tasks to Harbor task packages (awm syn export-harbor)."""
    tasks_dir: str = ""         # accepted-task tree (default AWM_TASKS_XAI_DIR)
    masters_dir: str = ""       # worlds/_masters (default AWM_WS_MASTERS_DIR)
    seed_cache: str = ""       # webharbor seed cache (default AWM_WEBHARBOR_SEED_CACHE_DIR)
    out_dir: str = ""           # output root (default AWM_OUTPUTS_DIR/harbor-packages)
    eval_source_root: str = "" # optional gateway source override (default: this source release)
    node_archive: str = ""      # pinned playwright Node archive (default WILDAWM_HARBOR_NODE_ARCHIVE)
    workers: int = 8            # blob/digest worker processes
    stages: str = "A,B,C,D"     # pipeline stages
    only: str = ""              # comma list of slugs (restrict emission)


class Top(enum.Enum):
    dashboard = "dashboard"
    world = "world"
    agent = "agent"
    view = "view"
    verify = "verify"
    syn = "syn"


class WorldCmd(enum.Enum):
    list = "list"
    create = "create"
    start = "start"
    stop = "stop"
    status = "status"
    reset = "reset"
    destroy = "destroy"
    replicate = "replicate"
    attach = "attach"


class AgentCmd(enum.Enum):
    run = "run"
    history = "history"
    chat = "chat"


class ViewCmd(enum.Enum):
    world = "world"
    task = "task"
    model = "model"


class SynCmd(enum.Enum):
    world = "world"
    task = "task"
    profile = "profile"
    dayplan = "dayplan"
    dayimpl = "dayimpl"
    prebake = "prebake"
    export_harbor = "export-harbor"


COMMANDS = {
    Top.dashboard: Dashboard,
    Top.world: {
        WorldCmd.list: WorldList,
        WorldCmd.create: WorldCreate,
        WorldCmd.start: WorldStart,
        WorldCmd.stop: WorldId,
        WorldCmd.status: WorldId,
        WorldCmd.reset: WorldReset,
        WorldCmd.destroy: WorldId,
        WorldCmd.replicate: WorldReplicate,
        WorldCmd.attach: WorldAttach,
    },
    Top.agent: {
        AgentCmd.run: AgentRun,
        AgentCmd.history: AgentHistory,
        AgentCmd.chat: AgentChat,
    },
    Top.view: {
        ViewCmd.world: ViewWorld,
        ViewCmd.task: ViewTask,
        ViewCmd.model: ViewModel,
    },
    Top.syn: {
        SynCmd.world: SynWorld,
        SynCmd.task: SynTask,
        SynCmd.profile: SynProfile,
        SynCmd.dayplan: SynDayPlan,
        SynCmd.dayimpl: SynDayImpl,
        SynCmd.prebake: SynPrebake,
        SynCmd.export_harbor: SynExportHarbor,
    },
    Top.verify: Verify,
}


def h_dashboard(_c, cfg):
    import logging

    import uvicorn

    from wildawm.dashboard import server as dashboard_server

    print(f"WildAWM Dashboard: http://{cfg.host}:{cfg.port}")
    print(f"  Project root: {PROJECT_ROOT}")
    print(f"  Worlds dir:   {awm_cfg.WORLDS_DIR}")
    print(f"  Log file:     {ROOT_LOG_DIR / 'wildawm.dashboard.log'}")
    dashboard_server.log.info("dashboard starting on %s:%s", cfg.host, cfg.port)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if dashboard_server._fh not in logger.handlers:
            logger.addHandler(dashboard_server._fh)
    uvicorn.run(dashboard_server.app, host=cfg.host, port=cfg.port)


def _need(cfg, *fields):
    for f in fields:
        v = getattr(cfg, f)
        if v == "" or v == -1 or v is None:
            die(f"--{f} is required for this command")


def _episode_flags(run):
    """Resolve (archive, verify, teardown); --episode sets all three, --verify implies --archive."""
    ep = run.episode
    verify = ep or run.verify
    archive = ep or verify or run.archive
    teardown = ep or run.teardown
    return archive, verify, teardown


def _resolve_task_md(c, world, task, *, use_simple_task: bool = True):
    """Look up an XAI task's prompt; returns (agent_prompt, source_name)."""
    try:
        d = c.get(f"/api/worlds/{world}/xai-tasks/{task}")
        if d.get("task_md"):
            if use_simple_task and d.get("simple_task_md"):
                return d["simple_task_md"], "simple_task.txt"
            return d["task_md"], "task.md"
    except RuntimeError:
        pass
    die(f"task {task!r} not found (no task.md for it)")


def _backfill_verdict(out_dir, verify_resp: dict) -> None:
    """Patch a verify response into an archive that was exported before verifying."""
    root = Path(out_dir)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    if not root.is_dir():
        log(f"verdict backfill skipped (archive dir not found: {root})")
        return
    verdict = verify_resp.get("verdict")
    vdir = root / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    if verdict is not None:
        (vdir / "verdict.json").write_text(
            _json.dumps(verdict, indent=2, ensure_ascii=False))
    (vdir / "judge_stdout.txt").write_text(
        verify_resp.get("judge_py_stdout") or verify_resp.get("judge_input") or "")
    if isinstance(verify_resp.get("verifier"), dict):
        (vdir / "verifier.json").write_text(
            _json.dumps(verify_resp["verifier"], indent=2, ensure_ascii=False))
    if isinstance(verify_resp.get("judge"), dict):
        (vdir / "judge.json").write_text(
            _json.dumps(verify_resp["judge"], indent=2, ensure_ascii=False))

    man_path = root / "manifest.json"
    man = None
    if man_path.exists():
        man = load_jsonc(man_path)
    stats = stats_from_verify_response(verify_resp, man)
    (vdir / "stats.json").write_text(
        _json.dumps(stats, indent=2, ensure_ascii=False))
    if man is None:
        return
    meta = verify_resp.get("meta") or {}
    man["verdict"] = (verdict or {}).get("verdict") if isinstance(verdict, dict) else None
    man.setdefault("timing", {})["verify"] = meta.get("elapsed_s")
    judge_tok = man.setdefault("tokens", {}).setdefault("judge", {})
    judge_tok["input"] = meta.get("input_tokens")
    judge_tok["output"] = meta.get("output_tokens")
    man["stats"] = stats
    man.setdefault("artifacts", {})["verdict"] = (
        "verify/verdict.json" if verdict is not None else None)
    man.setdefault("artifacts", {})["stats"] = "verify/stats.json"
    man_path.write_text(_json.dumps(man, indent=2, ensure_ascii=False))


def _dashboard_marker_probe(c, world: str, harness: str,
                            marker_path: str) -> tuple[bool, str]:
    """Read a completion marker through the dashboard owning the agent."""
    result = c.post(
        f"/api/worlds/{world}/agents/{harness}/completion-marker",
        {"action": "read", "path": marker_path},
        timeout=RUNTIME_SHORT_TIMEOUT_S,
    )
    return bool(result.get("exists")), result.get("content") or ""


def _dashboard_marker_remove(c, world: str, harness: str,
                             marker_path: str) -> None:
    c.post(
        f"/api/worlds/{world}/agents/{harness}/completion-marker",
        {"action": "remove", "path": marker_path},
        timeout=RUNTIME_SHORT_TIMEOUT_S,
    )


def _terminal_agent_failure_reply(harness: str, error: str) -> str | None:
    text = str(error or "")
    lowered = text.lower()
    if harness == "openclaw" and "assert(!this.paused)" in text:
        return (
            "openclaw model failure: embedded runtime ended with "
            "assert(!this.paused) before producing a final reply"
        )
    if harness == "claude-code" and "tool call could not be parsed" in lowered:
        return "claude-code model failure: tool call could not be parsed"
    if harness == "nanobot" and (
            "http error 504" in lowered or "gateway timeout" in lowered):
        return "nanobot model failure: agent turn exhausted its wall-clock budget"
    context_limit_markers = (
        "maximum context length",
        "context_length_exceeded",
        "context window exceeded",
    )
    if any(marker in lowered for marker in context_limit_markers):
        return f"{harness} model failure: model context window exceeded"
    return None


def _completion_probe_loop(c, world: str, harness: str, session: str,
                           to_s: int, mt: int, task_text: str = "",
                           deadline: float | None = None,
                           reasoning_effort: str = "") -> dict:
    """Post-turn completion probe: a done agent writes its summary to a fresh marker path."""
    def chat_func(prompt: str, *, timeout_s: int | None = None) -> dict:
        probe_timeout = max(1, int(timeout_s or to_s or DEFAULT_AGENT_TURN_TIMEOUT_S))
        return c.post(
            f"/api/worlds/{world}/agent/chat",
            {"message": prompt, "kind": harness, "session_id": session,
             "timeout_s": probe_timeout, "max_turns": mt,
             "reasoning_effort": reasoning_effort or None},
            timeout=agent_chat_timeout(probe_timeout, hops=4))

    return completion_probe_loop(
        chat_func=chat_func,
        container=agent_container_name(world, harness),
        task_text=task_text,
        max_nudges=awm_cfg.TEST_NUDGE_MAX,
        deadline=deadline,
        log=log,
        echo_reply=True,
        marker_probe_func=lambda _container, path: _dashboard_marker_probe(
            c, world, harness, path),
        remove_marker_func=lambda _container, path: _dashboard_marker_remove(
            c, world, harness, path),
    )


def _run_agent(c, cfg, world) -> dict:
    """Attach a harness and run one turn; archive and verify even if the harness crashes."""
    harness = _ev(getattr(cfg, "harness", None))
    if not harness:
        return {}
    run, model = cfg.run, cfg.model
    llm = model.llm
    task = run.task
    prompt = run.prompt
    if task and prompt:
        die("--task and --prompt are mutually exclusive")
    if not task and not prompt:
        die("--harness given but no --task or --prompt to run")
    archive, verify, teardown = _episode_flags(run)
    mode = _ev(run.mode, "lightweight")
    if verify and not task:
        die("--verify needs --task (a free-form prompt has no ground-truth verifier)")
    session = getattr(cfg, "resume", "") or getattr(cfg, "session", "") \
        or f"{world}:{harness}"
    to_s = int(cfg.turn.timeout or 0)
    mt = int(cfg.turn.max_turns or 0)

    # Attach (idempotent; an llm switch re-attaches).
    reasoning_effort = (getattr(model, "reasoning_effort", "") or "").strip()
    context_window = int(getattr(model, "context_window", 0) or 0)
    max_output_tokens = int(getattr(model, "max_output_tokens", 0) or 0)
    log(f"attaching {harness} (llm={llm or 'default'}"
        + (f", reasoning_effort={reasoning_effort}" if reasoning_effort else "")
        + (f", context_window={context_window}" if context_window else "")
        + (f", max_output_tokens={max_output_tokens}" if max_output_tokens else "")
        + f") to {world}...")
    attach_body = {}
    if llm:
        attach_body["model"] = llm
    if reasoning_effort:
        attach_body["reasoning_effort"] = reasoning_effort
    if context_window:
        attach_body["context_window"] = context_window
    if max_output_tokens:
        attach_body["max_output_tokens"] = max_output_tokens
    if to_s:
        attach_body["timeout_s"] = to_s
    c.post(f"/api/worlds/{world}/agents/{harness}/start",
           attach_body,
           timeout=RUNTIME_PROVISION_TIMEOUT_S)

    episode_id = None
    task_prompt_source = "prompt"
    if task:
        message, task_prompt_source = _resolve_task_md(
            c, world, task, use_simple_task=run.use_simple_task)
        log(f"resolved XAI task prompt: {task_prompt_source}")
        if archive or verify:
            ep = c.post(
                f"/api/worlds/{world}/xai-tasks/{task}/episode/start",
                {"use_simple_task": run.use_simple_task},
                timeout=LOCAL_SCRIPT_TIMEOUT_S)
            episode_id = ep["episode_id"]
    else:
        message = prompt
        if archive:
            ep = c.post(f"/api/worlds/{world}/episodes/start",
                        {"label": "prompt"}, timeout=LOCAL_SCRIPT_TIMEOUT_S)
            episode_id = ep["episode_id"]

    initial_completion_marker = ""
    if task and awm_cfg.TEST_NUDGE_MAX > 0:
        initial_completion_marker = new_completion_marker_path()
        message = (
            message.rstrip()
            + "\n\n"
            + initial_completion_contract(initial_completion_marker)
        )

    # A harness crash must not abort the run: the judge scores the final world state.
    log(f"running {harness} (blocking on agent turn; timeout={to_s or 'off'}s "
        f"max_turns={mt or 'off'})...")
    t0 = time.time()
    reply, agent_error = "", None
    terminal_agent_failure = None
    try:
        chat = c.post(f"/api/worlds/{world}/agent/chat",
                      {"message": message, "kind": harness, "session_id": session,
                       "timeout_s": to_s, "max_turns": mt,
                       "reasoning_effort": reasoning_effort or None},
                      timeout=agent_chat_timeout(to_s, hops=4))
        reply = chat.get("content") or ""
    except Exception as e:  # noqa: BLE001  (any transport/harness failure)
        terminal_agent_failure = _terminal_agent_failure_reply(harness, str(e))
        if terminal_agent_failure:
            reply = terminal_agent_failure
        else:
            agent_error = str(e)
    agent_seconds = time.time() - t0
    if agent_error:
        log(f"agent ERRORED after {agent_seconds:.1f}s — archiving + verifying "
            f"the final state anyway: {agent_error}")
    elif terminal_agent_failure:
        log(f"agent reached terminal model failure after {agent_seconds:.1f}s — "
            "archiving + verifying the final state without infra retry: "
            f"{terminal_agent_failure}")
    else:
        log(f"agent finished in {agent_seconds:.1f}s; reply {len(reply)} chars")
    print("\n" + (reply or "(empty)") + "\n")

    completion_probe = {}
    completion_summary = ""
    initial_marker_exists = False
    initial_marker_error = ""
    if initial_completion_marker:
        try:
            initial_marker_exists, completion_summary = _dashboard_marker_probe(
                c, world, harness, initial_completion_marker)
        except Exception as e:  # noqa: BLE001
            initial_marker_error = str(e)
            log(f"initial completion marker probe errored: {e}")
    if initial_marker_error:
        completion_probe = {
            "enabled": False,
            "max_nudges": awm_cfg.TEST_NUDGE_MAX,
            "nudges_used": 0,
            "done": False,
            "marker_path": initial_completion_marker,
            "summary": "",
            "errors": [f"marker_probe: {initial_marker_error}"],
            "replies": [],
            "deadline": (t0 + float(to_s)) if to_s else None,
            "budget_exhausted": False,
            "stop_reason": "infra:marker_probe",
        }
        log("completion probe skipped because marker storage is unavailable")
    elif initial_marker_exists:
        completion_probe = {
            "enabled": True,
            "max_nudges": awm_cfg.TEST_NUDGE_MAX,
            "nudges_used": 0,
            "done": True,
            "marker_path": initial_completion_marker,
            "summary": completion_summary,
            "errors": [],
            "replies": [],
            "deadline": (t0 + float(to_s)) if to_s else None,
            "budget_exhausted": False,
            "stop_reason": "initial_turn",
        }
        log(f"initial completion marker found ({len(completion_summary)} chars); "
            "completion probe not needed")
    elif terminal_agent_failure:
        completion_probe = {
            "enabled": False,
            "max_nudges": awm_cfg.TEST_NUDGE_MAX,
            "nudges_used": 0,
            "done": False,
            "errors": [],
            "replies": [],
            "stop_reason": "terminal_model_failure",
        }
        log("completion probe skipped after terminal model failure")
    elif task and awm_cfg.TEST_NUDGE_MAX > 0 and not agent_error:
        probe_deadline = (t0 + float(to_s)) if to_s else None
        completion_probe = _completion_probe_loop(
            c, world, harness, session, to_s, mt, task_text=message,
            deadline=probe_deadline, reasoning_effort=reasoning_effort)
        completion_summary = completion_probe.get("summary") or ""
        agent_seconds = time.time() - t0
    elif task and awm_cfg.TEST_NUDGE_MAX > 0 and agent_error:
        completion_probe = {
            "enabled": False,
            "max_nudges": awm_cfg.TEST_NUDGE_MAX,
            "nudges_used": 0,
            "done": False,
            "stop_reason": "agent_error",
        }
        log("completion probe skipped after agent error")

    result = {
        "world": world, "harness": harness, "session": session, "reply": reply,
        "completion_probe": completion_probe,
    }
    if completion_summary:
        result["completion_summary"] = completion_summary
    if terminal_agent_failure:
        result["terminal_agent_failure"] = terminal_agent_failure
    if agent_error:
        if completion_probe.get("done"):
            result["agent_error_recovered"] = agent_error
        else:
            result["agent_error"] = agent_error

    # Archive first so the scene survives any agent failure; the verdict is backfilled later.
    out_dir = None
    if archive and episode_id:
        log("archiving run to outputs/ (trajectory + restorable scene)...")
        try:
            exp = c.post(
                f"/api/worlds/{world}/episodes/{episode_id}/export",
                {"task": task or None, "is_xai": True, "kind": harness,
                 "session_id": session, "model": llm or None,
                 "judge_model": model.judge_model or None,
                 "reasoning_effort": reasoning_effort or None,
                 "mode": mode,
                 "agent_seconds": round(agent_seconds, 1),
                 "agent_reply": completion_summary or None,
                 "completion_summary": completion_summary or None,
                 "completion_probe": completion_probe or None,
                 "agent_error": (
                     agent_error if agent_error and not completion_probe.get("done") else None),
                 "agent_error_recovered": (
                     agent_error if agent_error and completion_probe.get("done") else None),
                 "agent_task_source": task_prompt_source,
                 "agent_task_md": message if task else None,
                 "verify": {},
                 "name": run.run_name or None,
                 "out_dir": run.out or None},
                timeout=RUNTIME_PROVISION_TIMEOUT_S)
            out_dir = exp.get("output_dir")
            result["output_dir"] = out_dir
            log(f"archived -> {out_dir}")
        except RuntimeError as e:  # noqa: BLE001
            log(f"archive failed: {e}")

    # Verify, then fold the verdict back into the archive.
    if verify and episode_id:
        log(f"verifying (mode={mode})...")
        vbody = {"mode": mode, "kind": harness, "session_id": session,
                 "agent_seconds": round(agent_seconds, 1)}
        if model.judge_model:
            vbody["judge_model"] = model.judge_model
        try:
            verify_resp = c.post(
                f"/api/worlds/{world}/xai-tasks/{task}/episode/{episode_id}/verify",
                vbody, timeout=RUNTIME_HEAVY_TIMEOUT_S)
            result["verdict"] = (verify_resp.get("verdict") or {}).get("verdict")
            result["stats"] = stats_from_verify_response(verify_resp)
            log(f"VERDICT [{task}/{harness}]: {result.get('verdict')!r}")
            log(f"STATS [{task}/{harness}]: {compact_stats_line(result['stats'])}")
            if out_dir:
                _backfill_verdict(out_dir, verify_resp)
        except RuntimeError as e:  # noqa: BLE001
            log(f"verify failed: {e}")

    if teardown:
        try:
            c.delete(f"/api/worlds/{world}", timeout=RUNTIME_SHORT_TIMEOUT_S)
            log(f"destroyed world {world}")
            result["destroyed"] = True
        except RuntimeError as e:  # noqa: BLE001
            log(f"teardown failed: {e}")
    return result


def h_world_list(c, cfg):
    data = c.get("/api/worlds")
    out(cfg, data)
    if getattr(cfg, "json", False):
        return
    worlds = data.get("worlds", [])
    if not worlds:
        log("(no worlds)")
        return
    rows = [[w["id"], w.get("profile", ""), w.get("started_at_day"),
             w.get("started_at_date", ""),
             "running" if w.get("running") else "stopped",
             w.get("script_count"), w.get("db_count")]
            for w in worlds]
    _emit_table(rows, ["ID", "PROFILE", "DAY", "DATE", "STATUS", "SCRIPTS", "DBS"])
    running = sum(1 for w in worlds if w.get("running"))
    log(f"{len(worlds)} world(s)  ·  {running} running  ·  {len(worlds) - running} stopped")


def h_world_create(c, cfg):
    # Create from (profile, day) or restore from --path.
    if cfg.path:
        body = {"path": cfg.path, "state": _ev(cfg.state, "after")}
        if cfg.name:
            body["name"] = cfg.name
        data = c.post("/api/worlds", body, timeout=RUNTIME_PROVISION_TIMEOUT_S)
        world = data.get("id")
        log(f"restored world: {world} (from {cfg.path}, state={_ev(cfg.state, 'after')})")
    else:
        _need(cfg, "profile", "day")
        body = {"profile": cfg.profile, "day": cfg.day}
        if cfg.name:
            body["name"] = cfg.name
        # Copying the day snapshot can take a while, hence the long timeout.
        data = c.post("/api/worlds", body, timeout=RUNTIME_PROVISION_TIMEOUT_S)
        world = data.get("id")
        log(f"created world: {world}")
    out(cfg, data)

    if not cfg.docker_up:
        log("(--docker_up no) instance created; containers NOT started.")
        return data
    log(f"starting env (webharbor={cfg.webharbor})...")
    rt = c.post(f"/api/worlds/{world}/runtime/start",
                {"enable_webharbor": bool(cfg.webharbor)},
                timeout=RUNTIME_PROVISION_TIMEOUT_S)
    log(f"env up: mcp={rt.get('mcp_port')} webharbor={rt.get('webharbor_port')} "
        f"worldctl={rt.get('worldctl_port')}")

    if cfg.harness:
        res = _run_agent(c, cfg, world)
        if getattr(cfg, "json", False):
            print(_json.dumps(res, indent=2))
        return res
    return data


def h_world_start(c, cfg):
    _need(cfg, "world")
    log(f"starting env world={cfg.world} webharbor={cfg.webharbor}...")
    data = c.post(f"/api/worlds/{cfg.world}/runtime/start",
                  {"enable_webharbor": bool(cfg.webharbor)},
                  timeout=RUNTIME_PROVISION_TIMEOUT_S)
    out(cfg, data)
    log(f"env up: mcp={data.get('mcp_port')} webharbor={data.get('webharbor_port')} "
        f"worldctl={data.get('worldctl_port')}")


def h_world_stop(c, cfg):
    _need(cfg, "world")
    out(cfg, c.post(f"/api/worlds/{cfg.world}/runtime/stop",
                    timeout=RUNTIME_SHORT_TIMEOUT_S))
    log(f"stopped world: {cfg.world}")


def h_world_status(c, cfg):
    _need(cfg, "world")
    data = c.get(f"/api/worlds/{cfg.world}/runtime")
    out(cfg, data)
    if getattr(cfg, "json", False):
        return
    log(f"running={data.get('running')} mcp={data.get('mcp_port')} "
        f"webharbor={data.get('webharbor_port')} worldctl={data.get('worldctl_port')} "
        f"({data.get('worldctl_state')})")
    agents = data.get("agents") or {}
    if agents:
        rows = [[kind, "✓" if a.get("running") else "✗", a.get("state"),
                 a.get("model"), a.get("reasoning_effort") or ""]
                for kind, a in agents.items()]
        _emit_table(rows, ["HARNESS", "RUNNING", "STATE", "MODEL", "EFFORT"])


def h_world_reset(c, cfg):
    _need(cfg, "world")
    body: dict = {}
    if cfg.path:
        body = {"path": cfg.path, "state": _ev(cfg.state, "after")}
    else:
        if cfg.profile:
            body["profile"] = cfg.profile
        if cfg.day is not None and cfg.day >= 0:
            body["day"] = cfg.day
    out(cfg, c.post(f"/api/worlds/{cfg.world}/runtime/reset", body,
                    timeout=RUNTIME_PROVISION_TIMEOUT_S))
    log(f"reset world: {cfg.world}")


def h_world_destroy(c, cfg):
    _need(cfg, "world")
    out(cfg, c.delete(f"/api/worlds/{cfg.world}"))
    log(f"destroyed world: {cfg.world}")


def h_world_replicate(c, cfg):
    _need(cfg, "world")
    log(f"replicating {cfg.world} x{cfg.n} (start={cfg.start}, webharbor={cfg.webharbor})...")
    data = c.post(f"/api/worlds/{cfg.world}/replicate",
                  {"n": cfg.n, "start": bool(cfg.start), "webharbor": bool(cfg.webharbor)},
                  timeout=RUNTIME_HEAVY_TIMEOUT_S)
    out(cfg, data)
    started = set(data.get("started", []))
    created = data.get("created", [])
    if created:
        rows = [[nid, "started" if nid in started else "created"] for nid in created]
        _emit_table(rows, ["REPLICA", "STATUS"])
    log(f"created {len(created)}, started {len(started)}, "
        f"webharbor_captured={data.get('webharbor_captured')}")


def h_world_attach(c, cfg):
    """Exec an interactive bash in the world's agent container (replaces this process)."""
    _need(cfg, "world")
    name = None
    if cfg.harness:
        name = f"wildawm-agent-{_ev(cfg.harness)}-{cfg.world}"
    else:
        r = subprocess.run(
            ["docker", "ps", "--filter", "name=wildawm-agent-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=SUBPROCESS_FAST_TIMEOUT_S)
        cands = [n for n in r.stdout.split() if n.endswith(cfg.world)]
        if not cands:
            die(f"no running agent container for {cfg.world!r} — start one "
                f"(world create --harness X / agent run) or pass --harness.")
        name = cands[0]
    log(f"exec bash into {name}  (Ctrl-D to exit)")
    os.execvp("docker", ["docker", "exec", "-it", name, "bash"])


def h_agent_run(c, cfg):
    _need(cfg, "world", "harness")
    if not cfg.run.task and not cfg.run.prompt:
        die("--task or --prompt is required for `agent run`")
    res = _run_agent(c, cfg, cfg.world)
    if getattr(cfg, "json", False):
        print(_json.dumps(res, indent=2))


def h_agent_history(c, cfg):
    _need(cfg, "world", "harness")
    harness = _ev(cfg.harness)
    session = cfg.session or f"{cfg.world}:{harness}"
    q = f"?session_id={quote(session)}&kind={quote(harness)}"
    data = c.get(f"/api/worlds/{cfg.world}/agent/history{q}")
    out(cfg, data)
    if getattr(cfg, "json", False):
        return
    for h in data.get("history", []):
        t = h.get("type")
        if t == "message":
            print(f"[{h.get('role')}] {h.get('content','')[:400]}")
        elif t == "tool_call":
            print(f"  🔧 {h.get('name')}({h.get('arguments','')[:160]})")
        elif t == "tool_result":
            print(f"     ↳ {str(h.get('content',''))[:200]}")


def h_agent_chat(c, cfg):
    """Ensure the harness is attached, then print a dashboard URL for the chat UI."""
    _need(cfg, "world", "harness")
    harness = _ev(cfg.harness)
    session = cfg.session or f"{cfg.world}:{harness}"
    reasoning_effort = (getattr(cfg, "reasoning_effort", "") or "").strip()
    context_window = int(getattr(cfg, "context_window", 0) or 0)
    log(f"ensuring {harness} attached (llm={cfg.llm or 'default'}"
        + (f", reasoning_effort={reasoning_effort}" if reasoning_effort else "")
        + (f", context_window={context_window}" if context_window else "")
        + ")...")
    body = {}
    if cfg.llm:
        body["model"] = cfg.llm
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    if context_window:
        body["context_window"] = context_window
    c.post(f"/api/worlds/{cfg.world}/agents/{harness}/start",
           body,
           timeout=RUNTIME_PROVISION_TIMEOUT_S)
    base = cfg.dashboard.rstrip("/")
    url = (f"{base}/?world={quote(cfg.world)}&harness={quote(harness)}"
           f"&session={quote(session)}#agent")
    out(cfg, {"world": cfg.world, "harness": harness, "session": session, "url": url})
    if not getattr(cfg, "json", False):
        log("chat session ready — open in your browser:")
        print(f"\n  {url}\n")


def _emit_table(rows, headers):
    """Print a table and mirror it into the CLI log without timestamps."""
    table = tabulate(rows, headers=headers, tablefmt="simple_outline")
    print(table, flush=True)
    _log_fh.write(table + "\n")
    _log_fh.flush()


def _fmt_bytes(n: int) -> str:
    """Human-readable byte size (e.g. 504KB, 1.2MB)."""
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" or n >= 100 else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.0f}GB"


def _day_range(days: list[int]) -> str:
    """Compact a sorted day list to a range string, e.g. [0,1,2,4,5] -> '0-2,4-5'."""
    if not days:
        return "-"
    days = sorted(set(days))
    parts, start, prev = [], days[0], days[0]
    for d in days[1:] + [None]:
        if d == prev + 1:
            prev = d
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = d
    return ",".join(parts)


def h_view_world(c, cfg):
    """List creatable profiles, or the per-day breakdown of one with --profile."""
    data = c.get("/api/profiles")
    out(cfg, data)
    if getattr(cfg, "json", False):
        return
    # Only profiles with a compiled master can host a world.
    profiles = [p for p in data.get("profiles", []) if p.get("master_ready")]

    if cfg.profile:
        p = next((x for x in profiles if x["name"] == cfg.profile), None)
        if p is None:
            die(f"profile {cfg.profile!r} has no compiled master "
                f"(run `awm view world` for the available list)")
        rows = [["✓" if d.get("snapshot_ready") else "✗ bake",
                 d["day"], d.get("date") or "-",
                 d.get("xai_task_count", 0)]
                for d in p.get("days", [])]
        _emit_table(rows, ["SNAP", "DAY", "DATE", "XAI"])
        ready = [d["day"] for d in p.get("days", []) if d.get("snapshot_ready")]
        log(f"{p.get('title') or cfg.profile}: {len(p.get('days', []))} day(s), "
            f"snapshot-ready {_day_range(ready)}  ·  "
            f"create: awm world create --profile {cfg.profile} --day <N>")
        return

    if not profiles:
        log("(no profiles with a compiled master)")
        return
    rows = []
    for p in profiles:
        days = [d["day"] for d in p.get("days", [])]
        rows.append([
            p["name"], (p.get("title") or "")[:28],
            _day_range(days), _day_range(p.get("ready_days", [])),
            sum(d.get("xai_task_count", 0) for d in p.get("days", [])),
            p.get("db_count", 0), _fmt_bytes(p.get("data_bytes", 0)),
            p.get("data_rows", 0),
        ])
    rows.sort(key=lambda r: r[0])
    _emit_table(rows, ["PROFILE", "TITLE", "DAYS", "SNAP-READY",
                       "XAI", "DBs", "DATA", "ROWS"])
    log(f"{len(profiles)} creatable world profile(s)  ·  "
        f"detail: awm view world --profile <name>")


def h_view_task(c, cfg):
    data = c.get(f"/api/worlds/{cfg.world}/xai-tasks" if cfg.world else "/api/xai-tasks")
    out(cfg, data)
    if getattr(cfg, "json", False):
        return
    tasks = data.get("tasks", [])

    # Global listing: one row per (task, eligible profile), using on-disk day readiness.
    if cfg.world:
        rows = [["✓" if t.get("has_assets") else "✗ assets",
                 t["task_name"], t.get("difficulty", ""),
                 "/".join(t.get("interfaces", []))]
                for t in tasks]
        _emit_table(rows, ["READY", "TASK", "DIFF", "INTERFACES"])
        ready = sum(1 for t in tasks if t.get("has_assets"))
        log(f"world {cfg.world}: {ready}/{len(tasks)} task(s) ready")
        return

    rows = []
    summary: dict[str, list[int]] = {}
    for t in tasks:
        name = t["task_name"]
        diff = t.get("difficulty", "")
        ifaces = "/".join(t.get("interfaces", []))
        has_assets = t.get("has_assets", False)
        for rule in t.get("eligible", []) or [{}]:
            prof = rule.get("profile", "-")
            days = rule.get("days", []) or []
            days_avail = rule.get("days_available", [])
            ready = rule.get("ready", False)
            day_str = ",".join(str(d) for d in days) or "-"
            if ready:
                mark = "✓"
            elif not has_assets:
                mark = "✗ assets"
            elif not days_avail:
                mark = "✗ day"
            else:
                mark = "✗"
            rows.append([mark, name, prof, day_str, diff, ifaces])
            s = summary.setdefault(prof, [0, 0])
            s[0] += int(bool(ready))
            s[1] += 1

    rows.sort(key=lambda r: (r[2], r[1]))
    _emit_table(rows, ["READY", "TASK", "PROFILE", "DAY", "DIFF", "INTERFACES"])

    total_ready = sum(s[0] for s in summary.values())
    total = sum(s[1] for s in summary.values())
    sm_rows = [[prof, f"{r}/{n}"] for prof, (r, n) in sorted(summary.items())]
    sm_rows.append(["TOTAL", f"{total_ready}/{total}"])
    print(flush=True)
    _emit_table(sm_rows, ["PROFILE", "READY/TOTAL"])


def h_view_model(c, cfg):
    data = c.get("/api/models")
    out(cfg, data)
    if getattr(cfg, "json", False):
        return
    providers = data.get("providers", [])
    log(f"registry: {data.get('source')}  ·  {data.get('provider_count')} providers  "
        f"·  default: {data.get('default')}")

    prov_rows = [[p["provider"], p.get("provider_type", ""),
                  "✓" if p.get("has_key") else "✗",
                  p.get("model_count"), p.get("base_url", "")]
                 for p in providers]
    _emit_table(prov_rows, ["PROVIDER", "TYPE", "KEY", "MODELS", "BASE URL"])

    # "*" marks the registry default; API ID is shown only when it differs from the name.
    model_rows = []
    total_models = 0
    for p in providers:
        for m in p.get("models", []):
            total_models += 1
            api_id = m["api_model"] if m["api_model"] != m["name"] else ""
            model_rows.append(["*" if m.get("is_default") else "",
                               m["ref"], api_id, p["provider"]])
    print(flush=True)
    _emit_table(model_rows, ["DEF", "MODEL (ref)", "API ID", "PROVIDER"])
    log(f"{len(providers)} provider(s)  ·  {total_models} model(s)  "
        f"·  default: {data.get('default')}")


def _extract_state(archive: Path, into: Path) -> Path | None:
    if not archive.exists():
        return None
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(into)
    label = archive.name.replace(".tar.gz", "")
    cand = into / label
    return cand if cand.is_dir() else into


def _xai_offline_modules():
    """Import dashboard helpers only for offline task re-judging."""
    from wildawm.dashboard import server
    return server


def _flatten_webharbor_into(after_dir: Path, work: Path):
    """Flatten the archived webharbor.tar.gz into the flat <db> layout the verifiers expect."""
    whtar = after_dir / "webharbor.tar.gz"
    if not whtar.exists():
        return None
    whx = work / "whx"
    whx.mkdir(parents=True, exist_ok=True)
    with tarfile.open(whtar) as t:
        t.extractall(whx)
    flat = work / "whflat"
    flat.mkdir(parents=True, exist_ok=True)
    for p in whx.glob("*/instance/*.db"):
        shutil.copy(p, flat / p.name)
    return flat if any(flat.iterdir()) else None


def _verify_task_offline(run_dir, manifest, cfg, server, tdir, before, after, work):
    """Re-judge a task archive offline (heavy: agent-as-a-judge; lightweight: verifier + LLM)."""
    task_name = manifest.get("task", {}).get("name")
    task_md = (tdir / "task.md").read_text()
    rub = tdir / "verifier" / "rubrics.md"
    judge_md = rub.read_text() if rub.exists() else ""
    skill_p = tdir / "verifier" / "verify.skill.md"
    skill_md = skill_p.read_text() if skill_p.exists() else ""
    mode = _ev(getattr(cfg, "mode", None), "lightweight")
    # Baseline is the day-N master snapshot, falling back to the archived 'before'.
    w = manifest.get("world", {}) or {}
    profile, day = w.get("profile"), w.get("started_at_day")
    baseline = None
    if profile and day is not None:
        # Use a scratch copy: the verifier would otherwise mutate the master snapshot.
        snap = ws_anchors.inspection_world(profile, int(day))
        if snap and snap.is_dir():
            baseline = snap
    if baseline is None:
        baseline = before
    try:
        wh = _flatten_webharbor_into(after, work)
        rd = vf.run_verifier(tdir / "verifier", after / "workspace",
                             after / "data", baseline, wh)
        if mode == "heavy":
            log(f"heavy agent-as-a-judge exploring snapshots for {task_name} "
                f"(judge_model={getattr(cfg, 'judge_model', '') or 'default'})...")
            verdict, meta = server._xai_judge_heavy(
                task_md, judge_md, skill_md, before, after, rd,
                getattr(cfg, "judge_model", "") or None)
        else:
            spec = server._models.resolve(getattr(cfg, "judge_model", "") or None)
            verdict, meta = server._xai_judge_lightweight(task_md, judge_md, rd, spec)
    except Exception as e:  # noqa: BLE001  (HTTPException etc. from server funcs)
        die(f"offline {mode} judge failed for {task_name}: {e}")

    out_name = getattr(cfg, "out_name", "") or f"verdict_{mode}"
    stats = stats_from_verify_response({"meta": meta}, manifest)
    out_path = run_dir / "verify" / f"{out_name}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_json.dumps(
        {"verdict": verdict, "meta": meta, "mode": mode, "stats": stats},
        indent=2, ensure_ascii=False))
    v = (verdict or {}).get("verdict") if isinstance(verdict, dict) else verdict
    recorded = run_dir / "verify" / "verdict.json"
    orig = None
    if recorded.exists():
        orig = (load_jsonc(recorded) or {}).get("verdict")
    log(f"[{mode}] VERDICT [{task_name}]: {v!r}   (original lightweight: {orig!r})")
    log(f"[{mode}] STATS [{task_name}]: {compact_stats_line(stats)}")
    print("\n----- reasoning -----")
    print((verdict or {}).get("reasoning", "") if isinstance(verdict, dict) else "(none)")
    print("\n----- stats -----")
    print(compact_stats_line(stats))
    print(f"\nwrote {out_path}")
    if getattr(cfg, "json", False):
        print(_json.dumps({"task": task_name, "mode": mode, "verdict": v,
                           "original": orig, "stats": stats,
                           "out": str(out_path)}, indent=2))
    return {"verdict": v, "stats": stats}


def _verify_one(c, cfg, run_dir: Path):
    """Re-judge one archived XAI run without starting a live world."""
    manifest = load_jsonc(run_dir / "manifest.json")
    task = manifest.get("task", {})
    task_name = task.get("name")
    if task.get("kind") != "xai":
        die("only XAI task archives can be verified")
    log(f"offline verify: task={task_name} mode={_ev(getattr(cfg, 'mode', None), 'lightweight')} dir={run_dir}")

    tmp = Path(tempfile.mkdtemp(prefix="awm-verify-"))
    try:
        after = _extract_state(run_dir / "state" / "after.tar.gz", tmp)
        before = _extract_state(run_dir / "state" / "before.tar.gz", tmp)
        if after is None or before is None:
            die("missing state/before.tar.gz or state/after.tar.gz")
        try:
            server = _xai_offline_modules()
            info = server._load_xai_registry().get(task_name) or {}
            task_dir = Path(server._xai_task_dir(task_name, info))
        except Exception as exc:  # noqa: BLE001
            die(f"could not resolve XAI task directory: {exc}")
        if not (task_dir / "verifier").is_dir():
            die(f"XAI verifier directory is missing: {task_dir / 'verifier'}")
        result = _verify_task_offline(
            run_dir, manifest, cfg, server, task_dir, before, after, tmp)
        log("offline verify complete")
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _discover_run_dirs(root: Path) -> list[Path]:
    """Run folders under `root`: the archive itself, or every child run archive."""
    if (root / "manifest.json").exists():
        return [root]
    if not root.is_dir():
        return []
    return [man.parent for man in sorted(root.glob("*/manifest.json"))]


def _reverify_child_cmd(rd: Path, mode: str, judge_model: str, out_name: str) -> list[str]:
    """Command for a single-run verify child process."""
    cmd = [
        sys.executable, "-m", "wildawm.cli.awm", "verify",
        "--dir", str(rd),
        "--mode", mode,
        "--resume", "false",
        "--force", "true",
        "--parallel", "1",
    ]
    if judge_model:
        cmd += ["--judge_model", judge_model]
    if out_name:
        cmd += ["--out_name", out_name]
    return cmd


def _reverify_one(rd: Path, mode: str, judge_model: str, out_name: str) -> list:
    """Re-judge one archived run in a child process and return [name, verdict, seconds]."""
    t0 = time.time()
    logf = rd / "verify" / f"_reverify_{mode}.log"
    logf.parent.mkdir(parents=True, exist_ok=True)
    verdict = None
    stats = None
    rc = None
    cmd = _reverify_child_cmd(rd, mode, judge_model, out_name)
    with open(logf, "w") as fh:
        try:
            fh.write("$ " + " ".join(cmd) + "\n\n")
            fh.flush()
            proc = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=fh,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=RUNTIME_HEAVY_TIMEOUT_S,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired as e:
            fh.write(f"\n[reverify child timed out after {e.timeout}s]\n")
            rc = 124
        except BaseException as e:  # noqa: BLE001
            fh.write(f"\n[reverify child launch failed] {type(e).__name__}: {e}\n")
            rc = 1
    vfile = rd / "verify" / f"{out_name or ('verdict_' + mode)}.json"
    if vfile.exists():
        try:
            payload = load_jsonc(vfile) or {}
            v = payload.get("verdict")
            verdict = v.get("verdict") if isinstance(v, dict) else v
            stats = payload.get("stats")
        except (ValueError, OSError):
            verdict = None
    if stats is None:
        try:
            stats = stats_from_manifest(load_jsonc(rd / "manifest.json"))
        except Exception:  # noqa: BLE001
            stats = {}
    if verdict is None:
        verdict = "(crashed)" if rc else "(no-verdict)"
    status = "run" if rc == 0 else f"error:{rc}"
    return [rd.name, verdict, round(time.time() - t0, 1), status, stats]


def _verify_output_path(run_dir: Path, mode: str, out_name: str = "") -> Path:
    return run_dir / "verify" / f"{out_name or ('verdict_' + mode)}.json"


def h_verify(c, cfg):
    """Offline re-verify of one archived run, or of every run under a run root in parallel."""
    _need(cfg, "dir")
    root = Path(cfg.dir)
    if not root.is_absolute():
        root = (PROJECT_ROOT / cfg.dir) if not root.exists() else root
    run_dirs = _discover_run_dirs(root)
    if not run_dirs:
        die(f"no run folder(s) with manifest.json under {root} "
            f"(point at an outputs/<run> archive or a directory of archives)")

    t0 = time.time()
    mode = _ev(getattr(cfg, "mode", None), "lightweight")
    if len(run_dirs) == 1:
        target = _verify_output_path(run_dirs[0], mode, cfg.out_name)
        if cfg.resume and not cfg.force and target.exists():
            log(f"verify skip: {run_dirs[0].name} already has {target.name}")
            return
        _verify_one(c, cfg, run_dirs[0])
        log(f"verify done in {time.time() - t0:.1f}s")
        return

    if cfg.resume and not cfg.force:
        before = len(run_dirs)
        skipped = [rd for rd in run_dirs
                   if _verify_output_path(rd, mode, cfg.out_name).exists()]
        run_dirs = [rd for rd in run_dirs
                    if not _verify_output_path(rd, mode, cfg.out_name).exists()]
        if skipped:
            log(f"verify resume: skip {len(skipped)}/{before} run(s) with existing "
                f"{cfg.out_name or ('verdict_' + mode)}.json")
        if not run_dirs:
            log("verify resume: all selected runs already have target verdict files")
            return
    par = max(1, int(cfg.parallel))
    log(f"batch verify: {len(run_dirs)} run(s) under {root}  "
        f"(mode={mode}, parallel={par}, judge={cfg.judge_model or 'default'})")
    rows = []
    with ThreadPoolExecutor(max_workers=par) as ex:
        futs = [ex.submit(_reverify_one, rd, mode, cfg.judge_model, cfg.out_name)
                for rd in run_dirs]
        with tqdm(total=len(futs), desc=f"verify({mode})", unit="run") as bar:
            for fut in as_completed(futs):
                r = fut.result()
                rows.append(r)
                bar.update(1)
                bar.set_postfix_str(f"{r[0]}={r[1]}")
                tqdm.write(f"  [{r[1]:<12}] {r[0]}  {r[2]:.0f}s  "
                            f"{compact_stats_line(r[4])}")
    wall = time.time() - t0
    rows.sort(key=lambda r: r[0])
    print(flush=True)
    _emit_table([[a, b, f"{t:.0f}s", status, *table_stats_cells(stats)]
                 for a, b, t, status, stats in rows],
                ["RUN", f"VERDICT({mode})", "TIME", "STATUS",
                 "LLM", "TOOLS", "TOKENS I/O", "AVG I/O", "AGENT", "JUDGE"])
    counts = Counter(b for _, b, _, _, _ in rows)
    print(flush=True)
    _emit_table([[k, n] for k, n in counts.most_common()] + [["TOTAL", len(rows)]],
                ["VERDICT", "COUNT"])
    totals = aggregate_stats([r[4] for r in rows])
    print(flush=True)
    _emit_table([["TOTAL", *table_stats_cells(totals, item_count=len(rows))]],
                ["SCOPE", "LLM", "TOOLS", "TOKENS I/O", "AVG I/O",
                 "AGENT", "JUDGE", "LLM/TASK", "TOOLS/TASK"])
    proc_min = sum(r[2] for r in rows) / 60
    log(f"batch verify complete: {len(rows)} run(s) in {wall / 60:.1f} min "
        f"(proc-time sum {proc_min:.1f} min) · "
        + " · ".join(f"{k}={n}" for k, n in counts.most_common())
        + f" · {compact_stats_line(totals, item_count=len(rows))}")
    if getattr(cfg, "json", False):
        print(_json.dumps({"total": len(rows), "mode": mode,
                           "counts": dict(counts), "wall_min": round(wall / 60, 1),
                           "stats": totals,
                           "results": [{"run": a, "verdict": b, "seconds": t,
                                        "status": status, "stats": stats}
                                       for a, b, t, status, stats in rows]}, indent=2))


def h_syn_profile(c, cfg):
    """Generate a persona (+ diversity gate, host-side)."""
    _need(cfg, "name")
    res = ws_profile.synthesize(cfg.name, force=cfg.force, max_proposals=cfg.max_proposals,
                                steer=cfg.steer or "")
    log(f"syn profile: id={res.profile_id} accepted={res.accepted} "
        f"diversity={res.diversity} iters={res.iterations} path={res.path}")


def h_syn_dayplan(c, cfg):
    """Plan one day (serial guard: day N-1 must be materialized)."""
    _need(cfg, "profile")
    if cfg.day < 0:
        die("syn dayplan needs --day N (>= 0)")
    p = ws_dayplan.synthesize(cfg.profile, cfg.day, force=cfg.force, steer=cfg.steer or "")
    log(f"syn dayplan: profile={cfg.profile} day={cfg.day} plan={p}")


def h_syn_dayimpl(c, cfg):
    """Implement one day (4-layer gate + repair loop)."""
    _need(cfg, "profile")
    if cfg.day < 0:
        die("syn dayimpl needs --day N (>= 0)")
    max_iters = cfg.max_iters or WORLD_SYN_MAX_IMPLEMENT_ITERS
    p = ws_dayimpl.synthesize(cfg.profile, cfg.day, force=cfg.force,
                              max_iters=max_iters, steer=cfg.steer or "")
    log(f"syn dayimpl: profile={cfg.profile} day={cfg.day} dir={p}")


def h_syn_prebake(c, cfg):
    """Pre-bake cumulative day snapshots into _masters + days.json."""
    _need(cfg, "profile")
    res = ws_prebake.prebake(cfg.profile, to_day=(cfg.to_day if cfg.to_day >= 0 else None),
                             force=cfg.force)
    log(f"syn prebake: profile={cfg.profile} days={res['days']} masters={res['masters_dir']} "
        f"days_json={res['days_json']}")
    if cfg.verify_rebake:
        rb = ws_prebake.verify_rebake(cfg.profile)
        bad = {d: v for d, v in rb.items() if not v["match"]}
        log(f"syn prebake verify_rebake: {len(rb)} days, mismatches={len(bad)}")
        if bad:
            die(f"re-bake mismatch: {bad}")


def h_syn_world(c, cfg):
    """Serial plan→impl→prebake for days 0..to_day."""
    _need(cfg, "profile")
    if cfg.to_day < 0:
        die("syn world needs --to_day N (>= 0)")
    res = ws_world.run_world(cfg.profile, to_day=cfg.to_day, force=cfg.force,
                             force_plan=cfg.force_plan, force_impl=cfg.force_impl)
    for day, r in res.items():
        log(f"syn world: profile={cfg.profile} day={day} plan+impl+prebake done "
            f"→ masters={r['masters']} days_json={r['days_json']}")


def h_syn_export_harbor(c, cfg):
    """Export accepted tasks to Harbor task packages."""
    from types import SimpleNamespace

    from wildawm.synthesis.harbor_export import export_harbor as he

    args = SimpleNamespace(
        tasks_dir=cfg.tasks_dir, masters_dir=cfg.masters_dir,
        seed_cache=cfg.seed_cache, out_dir=cfg.out_dir,
        eval_source_root=cfg.eval_source_root, node_archive=cfg.node_archive,
        workers=cfg.workers, stages=cfg.stages, only=cfg.only)
    rc = he.run_export(args)
    if rc != 0:
        die(f"syn export-harbor failed with exit code {rc}")
    log("syn export-harbor: done (see the emitter's stage logs above)")


def _resolve_days(cfg) -> list[int]:
    if cfg.days:
        return [int(x) for x in cfg.days.split(",") if x.strip() != ""]
    if cfg.day is not None and cfg.day >= 0:
        return [cfg.day]
    die("syn task needs --day N or --days 1,2,4")


def _auto_spec(prefix: str, day: int, difficulty: str, count: int,
               require_web: bool) -> str:
    plan = [p.strip() for p in re.split(r"[,/]+", difficulty or "") if p.strip()]
    if not plan:
        plan = ["hard", "hard", "medium"]
    target_count = max(1, count)
    mixed = len(plan) > 1
    per_diff: dict[str, int] = {}
    slots = []
    for i in range(1, target_count + 1):
        diff = plan[(i - 1) % len(plan)]
        if mixed:
            per_diff[diff] = per_diff.get(diff, 0) + 1
            stem = re.sub(r"[^a-z0-9]+", "", diff.lower()) or "task"
            suffix = f"{stem[0]}{per_diff[diff]}"
        else:
            suffix = f"{diff}-{i}"
        s = f"{diff}:{prefix}{day}-{suffix}"
        if require_web:
            s += ":web"
        else:
            s += ":noweb"
        slots.append(s)
    return ",".join(slots)


def h_syn_task(c, cfg):
    """Synthesize XAI tasks for a profile's day(s); needs the dashboard and built day snapshots."""
    _need(cfg, "profile")
    try:
        # Single attempt: fail fast if the dashboard is down.
        c.get("/api/worlds", timeout=RUNTIME_SHORT_TIMEOUT_S, retries=1)
    except RuntimeError as e:
        die(f"`syn task` needs the dashboard up at {cfg.dashboard} (it provisions "
            f"day instances through it). {e}")
    dashboard = cfg.dashboard.rstrip("/")
    awm_cfg.DASHBOARD = dashboard
    awm_cfg.SYNTHESIS_DASHBOARD = dashboard
    days = _resolve_days(cfg)
    prefix = task_synthesis.slug_prefix(cfg.profile)
    single = len(days) == 1
    day_jobs = []
    for day in days:
        spec = cfg.tasks if (cfg.tasks and single) else \
            _auto_spec(prefix, day, cfg.difficulty, cfg.count, cfg.require_web)
        log(f"syn task day{day}: {spec}")
        slots = task_synthesis.parse_slots(spec, cfg.profile, day)
        runnable = []
        for slot in slots:
            report_path = tx.report_path(cfg.profile, day, slot["slug"])
            if (cfg.force or not tx.is_accepted(report_path)
                    or not tx.is_quality_prior(report_path.parent)):
                runnable.append(slot)
        skipped = len(slots) - len(runnable)
        if skipped:
            log(f"syn task day{day}: {skipped} accepted output(s) count toward quotas")
        if runnable:
            # Pass all slots: accepted outputs count toward the remaining quotas.
            day_jobs.append((day, slots))

    if not day_jobs:
        log("syn task: all selected task outputs are already accepted")
        return

    syn_log_dir = LOG_DIR / "syn-task"
    syn_log_dir.mkdir(parents=True, exist_ok=True)
    run_tag = f"{_safe_name(cfg.profile)}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"

    def _run_day(day: int, slots: list[dict]) -> dict:
        t0 = time.time()
        day_log = syn_log_dir / f"{run_tag}-day{day}.log"

        def live_log(msg: str) -> None:
            line = f"[{time.strftime('%H:%M:%S')}] {msg}"
            with _LOG_LOCK:
                with day_log.open("a") as fh:
                    fh.write(line + "\n")
                print(line, flush=True)
                _log_fh.write(line + "\n")
                _log_fh.flush()

        try:
            res = task_synthesis.run_day_slots(
                cfg.profile, day, slots, redo_rejected=True, force=cfg.force,
                log=live_log)
            return {"day": day, "results": res, "error": None,
                    "elapsed": time.time() - t0, "log": str(day_log)}
        except BaseException as e:  # noqa: BLE001
            return {"day": day, "results": [], "error": e,
                    "elapsed": time.time() - t0, "log": str(day_log)}

    rows, failed = [], []
    with ThreadPoolExecutor(max_workers=max(1, int(cfg.parallel))) as ex:
        futs = {ex.submit(_run_day, day, slots): day for day, slots in day_jobs}
        with tqdm(total=len(futs), desc="syn task days", unit="day") as bar:
            for fut in as_completed(futs):
                r = fut.result()
                rows.append(r)
                if r["error"]:
                    failed.append(r)
                bar.update(1)
                status = "ERROR" if r["error"] else "done"
                bar.set_postfix_str(f"day{r['day']}={status}")
                if r["error"]:
                    tqdm.write(f"[day {r['day']}] ERROR: {r['error']}")
                else:
                    tqdm.write("[day {}] DONE: {}".format(
                        r["day"], ", ".join(f"{s}={v}" for s, v in r["results"])))

    rows.sort(key=lambda r: r["day"])
    _emit_table(
        [[f"day{r['day']}", "ERROR" if r["error"] else "done",
          f"{r['elapsed']:.0f}s",
          ", ".join(f"{s}={v}" for s, v in r["results"]) or str(r["error"] or ""),
          r["log"]]
         for r in rows],
        ["DAY", "STATUS", "TIME", "RESULTS", "LOG"],
    )
    bad_results = [
        f"day{r['day']}/{slug}={verdict}"
        for r in rows
        if not r["error"]
        for slug, verdict in r["results"]
        if verdict not in tx.ACCEPTED
    ]
    if failed or bad_results:
        parts = []
        if failed:
            parts.append("exception day(s): " +
                         ", ".join("day" + str(r["day"]) for r in failed))
        if bad_results:
            parts.append("non-accepted result(s): " + ", ".join(bad_results))
        die("syn task failed: " + "; ".join(parts))


def _safe_name(s: str, fallback: str = "x") -> str:
    """Sanitize a path component (mirror of the dashboard's _safe_component)."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "").strip()).strip("-")
    return s or fallback


HANDLERS = {
    (Top.dashboard,): h_dashboard,
    (Top.world, WorldCmd.list): h_world_list,
    (Top.world, WorldCmd.create): h_world_create,
    (Top.world, WorldCmd.start): h_world_start,
    (Top.world, WorldCmd.stop): h_world_stop,
    (Top.world, WorldCmd.status): h_world_status,
    (Top.world, WorldCmd.reset): h_world_reset,
    (Top.world, WorldCmd.destroy): h_world_destroy,
    (Top.world, WorldCmd.replicate): h_world_replicate,
    (Top.world, WorldCmd.attach): h_world_attach,
    (Top.agent, AgentCmd.run): h_agent_run,
    (Top.agent, AgentCmd.history): h_agent_history,
    (Top.agent, AgentCmd.chat): h_agent_chat,
    (Top.view, ViewCmd.world): h_view_world,
    (Top.view, ViewCmd.task): h_view_task,
    (Top.view, ViewCmd.model): h_view_model,
    (Top.syn, SynCmd.world): h_syn_world,
    (Top.syn, SynCmd.task): h_syn_task,
    (Top.syn, SynCmd.profile): h_syn_profile,
    (Top.syn, SynCmd.dayplan): h_syn_dayplan,
    (Top.syn, SynCmd.dayimpl): h_syn_dayimpl,
    (Top.syn, SynCmd.prebake): h_syn_prebake,
    (Top.syn, SynCmd.export_harbor): h_syn_export_harbor,
    (Top.verify,): h_verify,
}


def main():
    # Accept `syn export-harbor` (enum member names cannot contain '-').
    for i, token in enumerate(sys.argv[:-1]):
        if token == "syn" and sys.argv[i + 1] == "export-harbor":
            sys.argv[i + 1] = "export_harbor"
    command, cfg = parse_args_with_commands(
        commands=COMMANDS,
        description="awm — WildAWM operator CLI (REST client for the dashboard)",
    )
    handler = HANDLERS.get(command)
    if not handler:
        die(f"no handler for command {command}")
    dashboard = getattr(cfg, "dashboard", DEFAULT_DASHBOARD)
    client = Client(dashboard)
    log(f"$ awm {' '.join(x.name for x in command)}  (dashboard={dashboard})")
    try:
        handler(client, cfg)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()
