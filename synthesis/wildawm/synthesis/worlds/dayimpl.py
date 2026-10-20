"""Day implementation: author a day's delta.sh, verify it through the layered gate and repair on failure."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tqdm import tqdm

from wildawm import const_config as cfg
from wildawm.synthesis.worlds import anchors
from wildawm.synthesis.worlds import app_library as app_lib
from wildawm.synthesis.worlds import overlay as ov
from wildawm.synthesis.worlds import primitives as pri
from wildawm.synthesis.worlds import runtime as rt
from wildawm.synthesis.worlds import skills as skill_lib
from wildawm.synthesis.worlds.dayplan import plan_file, skill_manifest_file, is_materialized
from wildawm.webharbor import lib as wh_lib

# The world_hash the gate accepted; prebake checks the master against it.
WORLD_HASH_FILE = ".world_hash"

IMPL_CONST = Path(__file__).resolve().parent / "constitutions" / "day_implementer.md"
REVIEW_CONST = Path(__file__).resolve().parent / "constitutions" / "world_reviewer.md"

# Per-site history page proving synthesized history renders; unmapped sites use "/".
_SITE_HISTORY_PATH = {
    "amazon": "/account/orders",
    "coursera": "/",
    "booking": "/",
    "allrecipes": "/",
    "github": "/",
}
_DELIVERABLE_TEXT_EXTS = {".sh", ".py", ".json", ".md", ".txt", ".yaml", ".yml"}


def script_dir(profile_id: str) -> Path:
    return cfg.WS_SCRIPTS_DIR / profile_id


def day_script_dir(profile_id: str, day: int) -> Path:
    return script_dir(profile_id) / f"day_{day}"


@dataclass
class GateResult:
    lint: list[str] = field(default_factory=list)
    determinism_checked: bool = False
    determinism: bool = False
    determinism_detail: str = ""
    world_hash: str = ""
    produces_checked: bool = False
    produces_ok: bool = False
    produces_problems: list[str] = field(default_factory=list)
    credentials_checked: bool = False
    credentials_ok: bool = False
    credentials_problems: list[str] = field(default_factory=list)
    gui_checked: bool = False
    gui_ok: bool = False
    gui_detail: str = ""
    overlay_mcp: list[str] = field(default_factory=list)
    overlay_webharbor: list[str] = field(default_factory=list)
    reviewer_checked: bool = False
    reviewer_accept: bool = False
    reviewer_problems: list[str] = field(default_factory=list)
    skill_feedback: list[dict] = field(default_factory=list)
    evolved_skills: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.lint
            and self.determinism_checked and self.determinism
            and self.produces_checked and self.produces_ok
            and self.credentials_checked and self.credentials_ok
            and self.gui_checked and self.gui_ok
            and self.reviewer_checked and self.reviewer_accept
        )

    def evidence(self) -> str:
        lines = []
        if self.lint:
            lines.append("LINT FAILURES:\n" + "\n".join("  - " + x for x in self.lint))
        if self.determinism_checked and not self.determinism:
            lines.append("DETERMINISM FAILED: " + self.determinism_detail)
        if self.produces_checked and not self.produces_ok:
            lines.append("PRODUCES CHECK FAILED:\n" + "\n".join("  - " + x for x in self.produces_problems))
        if self.credentials_checked and not self.credentials_ok:
            lines.append("CREDENTIALS NOT DISCOVERABLE:\n"
                         + "\n".join("  - " + x for x in self.credentials_problems))
        if self.gui_checked and not self.gui_ok:
            lines.append("GUI LOGIN/RENDER FAILED: " + self.gui_detail)
        if self.overlay_mcp:
            lines.append("OVERLAY MCP loaded: " + ", ".join(self.overlay_mcp))
        if self.overlay_webharbor:
            lines.append("OVERLAY webharbor patched+rendered: " + ", ".join(self.overlay_webharbor))
        if self.reviewer_checked and not self.reviewer_accept:
            lines.append("REVIEWER REJECTED:\n" + "\n".join("  - " + x for x in self.reviewer_problems))
        if self.evolved_skills:
            lines.append("EVOLVED SHARED SKILLS: " + ", ".join(self.evolved_skills))
        return "\n".join(lines) or "ALL GATES PASSED"


def _materialize_day(prev_snap: Path | None, day_dir: Path, frozen: str, dest: Path) -> Path:
    """Build day N at ``dest``: copy day N-1, replay delta.sh, then accumulate the overlay."""
    pri.stage_world(prev_snap, dest)
    r = rt.run_delta_sh(day_dir, dest, frozen_date=frozen)
    if r.returncode != 0:
        raise RuntimeError(
            f"delta.sh failed (rc={r.returncode})\n--stdout--\n{r.stdout[-2000:]}\n--stderr--\n{r.stderr[-2000:]}")
    if (Path(day_dir) / "overlay").is_dir():
        ov.merge_overlay(Path(day_dir) / "overlay", dest)
    return dest


def _done_check_impl(day_dir: Path, prepared_apps=()):
    def _check() -> tuple[bool, str]:
        for f in ("delta.sh", "produces.json", "state_delta.md"):
            p = day_dir / f
            if not p.exists() or p.stat().st_size == 0:
                return False, f"{f} missing/empty"
        for prepared in prepared_apps:
            package = day_dir / "overlay" / "apps" / prepared.app_id
            problems = app_lib.app_problems(package, prepared.app_id)
            if problems:
                return False, f"pre-staged app {prepared.app_id} is missing/invalid: {'; '.join(problems[:4])}"
        try:
            json.loads((day_dir / "produces.json").read_text(errors="replace"))
        except Exception as e:  # noqa: BLE001 - keep authoring until JSON is valid
            return False, f"produces.json invalid: {type(e).__name__}: {e}"
        delta = day_dir / "delta.sh"
        try:
            r = subprocess.run(
                ["bash", "-n", str(delta)],
                capture_output=True,
                text=True,
                timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"delta.sh bash syntax check failed: {type(e).__name__}: {e}"
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()
            return False, f"delta.sh bash syntax error: {detail[:500]}"
        lint = pri.lint_delta(day_dir)
        if lint:
            return False, "lint failed: " + "; ".join(lint[:5])
        return True, "deliverables present and syntax/lint checks passed"
    return _check


def _profile_name(profile_dir: Path) -> str:
    persona = profile_dir / "persona.md"
    if not persona.exists():
        return ""
    text = persona.read_text(errors="replace")
    m = re.search(r"^\s*[-*]\s*\*\*Name:\*\*\s*(.+?)\s*$", text, re.M)
    if m:
        return m.group(1).strip()
    m = re.search(r"^#\s*Persona Bible\s*[—-]\s*(.+?)\s*$", text, re.M)
    return m.group(1).strip() if m else ""


def _forbidden_identities(profile_id: str) -> list[tuple[str, str]]:
    """(needle, owning profile) for every other persona's id and display name."""
    needles: list[tuple[str, str]] = []
    for prof in sorted(cfg.WS_PROFILES_DIR.iterdir()):
        if not prof.is_dir() or prof.name == profile_id or prof.name.startswith("_staging"):
            continue
        needles.append((prof.name, prof.name))
        name = _profile_name(prof)
        if name:
            needles.append((name, prof.name))
    return needles


def _cross_profile_leaks(profile_id: str, day_dir: Path) -> list[str]:
    """Catch copied sibling-world scripts before they become baked state."""
    needles = _forbidden_identities(profile_id)
    if not needles:
        return []

    problems: list[str] = []
    for p in day_dir.rglob("*"):
        if not p.is_file() or p.suffix not in _DELIVERABLE_TEXT_EXTS:
            continue
        rel = p.relative_to(day_dir).as_posix()
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for needle, source_profile in needles:
            if needle and needle in text:
                problems.append(
                    f"cross-profile leakage: {rel} mentions {needle!r} from {source_profile}")
                break
    return problems


def _author(profile_id: str, day: int, day_dir: Path, prev_snap: Path | None,
             frozen: str, feedback: str, steer: str = "") -> bool:
    """Run the day_implementer; True if delta.sh, produces.json and state_delta.md were written."""
    if day_dir.exists():
        shutil.rmtree(day_dir)
    day_dir.mkdir(parents=True)
    plan = plan_file(profile_id, day).read_text(errors="replace")
    manifest = skill_manifest_file(profile_id, day)
    skill_lib.validate_plan_skills(profile_id, manifest)
    app_lib.validate_plan_apps(profile_id, manifest)
    selections = app_lib.load_plan_app_selections(manifest)
    prepared_apps = app_lib.prepare_day_apps(profile_id, day, manifest, day_dir, prev_snap)
    refs = skill_lib.plan_skill_refs(manifest)
    skill_paths = "\n".join(
        f"- {ref.skill_id}: {ref.path} (activity: {ref.activity})"
        for ref in refs)
    app_requirements = app_lib.plan_app_requirements(manifest)
    active_apps = app_lib.active_world_apps(prev_snap)
    prepared_by_id = {prepared.app_id: prepared for prepared in prepared_apps}
    app_paths = "\n".join(
        f"- {req.app_id}: {selections[req.app_id].path} (selected shared revision {selections[req.app_id].revision}; policy={selections[req.app_id].policy}; "
        f"{'already active in this world' if req.app_id in active_apps and req.app_id not in prepared_by_id else prepared_by_id[req.app_id].change + ' package pre-staged in ' + str(day_dir / 'overlay' / 'apps' / req.app_id)}; "
        f"activities: {'; '.join(req.activities)})"
        for req in app_requirements) or "(none)"
    prof = cfg.WS_PROFILES_DIR / profile_id
    accounts = ""
    if (prof / "accounts.json").exists():
        accounts = (prof / "accounts.json").read_text(errors="replace")
    inspect_hint = (
        f"Inspect the REAL day {day - 1} state in the CURRENT DIRECTORY: "
        f"query `data/*.db` and `webharbor/*.db` with `sqlite3` (tables, columns, max ids, counterparties), "
        f"read `workspace/` and `overlay/`. Write idempotent, frozen-literal inserts that continue this real state. "
        f"The CURRENT DIRECTORY is your private scratch copy of day {day - 1}, so you may run your delta here "
        f"to test it; the gate replays it from a pristine day {day - 1} regardless."
        if prev_snap else
        f"This is day 0 — no prior state. The CURRENT DIRECTORY is a seeded day-0 world: "
        f"the 15 WebHarbor sites (`webharbor/<site>.db`) are ALREADY initialized with the FULL real "
        f"catalog (products/courses/hotels/articles/... from the pinned seed cache). Query them with "
        f"`sqlite3` to see the real table schemas + catalog rows. Do NOT create or populate any catalog "
        f"tables (products/courses/hotels/etc.) — they already exist. INSERT the persona's `users` "
        f"login row per site (password_bcrypt from accounts.json, INSERT OR IGNORE so it is idempotent "
        f"on the seeded users table), plus whatever pre-existing account baseline the day plan specifies "
        f"(enrolment progress, saved/followed items, wishlists, notebooks, starred repos) — a real person "
        f"arrives at day 0 with a lived-in account, and the L4 reviewer rejects the day if the plan "
        f"promises that state and the tables are empty. What day 0 must NOT invent is new transactions "
        f"(a fresh order/booking/purchase made on day 0); those belong to later days. The initial `data/` and `overlay/apps/` are empty. Create databases only for the MCP services listed in MATCHED SHARED MCP APPLICATIONS, using their generated schemas. Do not create any other service database. Also create the CLI home needed by the plan.")
    fb = (f"\n\nPREVIOUS ATTEMPT GATE EVIDENCE (fix each failure):\n{feedback}") if feedback else ""
    steer_block = (f"\n\nSTEERING FOR THIS DAY (honor it, grounded in the persona):\n{steer}") if steer else ""
    # Multi-word display names only: invented people collide with personas by name, not by profile id.
    reserved_names = sorted({n for n, _ in _forbidden_identities(profile_id) if " " in n})
    reserved_block = "\n".join(f"  - {n}" for n in reserved_names) or "  (none)"
    # Give the id-to-filename mapping instead of letting the implementer guess it.
    site_id_block = "\n".join(f"  - {s}  (webharbor/{wh_lib.dbfile_for_site(s)})"
                              for s in wh_lib.all_sites())
    isolation = f"""

WORLD ISOLATION RULE (critical):
- Do NOT read, copy, diff, adapt, or use any sibling synthesized world scripts,
  plans, or snapshots under `worlds/_scripts/*`, `worlds/_plan/*`, or
  `worlds/instances/*` except the CURRENT profile/day paths named in this prompt
  and the CURRENT working world state.
- Read MCP schemas only from the matched shared app paths listed in this prompt or from active `overlay/apps/` in the current world. Query live `data/*.db` and `webharbor/*.db` for state. Never use source-tree reference apps or another
  persona's `day_0` as a template.
- Your deliverables must be for `{profile_id}` only and must not mention other
  profile ids or persona names.

RESERVED NAMES — these identities belong to other personas. Never give one to
anyone you invent here, and never write the list itself into a deliverable; the
lint reads both the same way.
{reserved_block}
"""
    prompt = f"""# Task: implement day {day} for persona {profile_id} (frozen date: {frozen})

Write your deliverables into: `{day_dir}` (the `_scripts/{profile_id}/day_{day}` dir).
That is: `delta.sh`, deterministic `*.py` sub-scripts, `produces.json`, `state_delta.md`, and `overlay/` if this day introduces/patches code. delta.sh will be replayed hermetically (--network none, frozen git dates) over the day N-1 world; it must reproduce day N identically every time.

DAY PLAN (day_{day}.md):
{plan[:8000]}

MATCHED SHARED EVENT SKILLS:
{skill_paths}

Read every matched `skill.md` before authoring. Apply its reusable scenarios, parameters, preconditions, and consistency checks to this day's concrete world state. The day plan and live world remain authoritative for persona-specific values. If a skill conflicts with the real schema or state, follow reality and leave evidence for the reviewer so the shared skill can evolve.

MATCHED SHARED MCP APPLICATIONS:
{app_paths}

These are the only MCP services this day may use. Read each selected revision before writing its database rows. Packages marked `new` or `upgrade` are already copied into this day's deliverable overlay before authoring; preserve them. For `policy=shared`, use the selected shared revision as written. For `policy=local`, you may modify the pre-staged copy for this world without changing the shared library. An already-active package may also receive a world-specific bug fix in this day's overlay. Do not create unrelated service databases. If a package is an upgrade, invoke each required package migration from delta.sh before writing activity rows; `Base.metadata.create_all` is not a migration.

ACCOUNTS (password_bcrypt is the fixed-salt hash to write into webharbor users.password_hash):
{accounts[:3000]}

CANONICAL WEBHARBOR SITE IDS — the only keys `web_writes`/`web_accounts` accept. The DB filename in `webharbor/` is often NOT the id (that is why this list is given to you rather than left to inference):
{site_id_block}
{inspect_hint}{fb}{steer_block}
{isolation}

Follow your constitution exactly. Read reality first — TARGETED: query only the tables/columns you will write to (a few `sqlite3` reads for schemas + max ids + counterparties), do NOT browse the whole catalog. Then write deterministic, idempotent, frozen-literal code. When done, reply DONE."""
    container = rt.ensure_creator(cfg.PROJECT_ROOT)
    # Author against a throwaway copy: test-running the delta would mutate the day N-1 master.
    cwd = str(pri.stage_world(prev_snap, cfg.SCRATCH / f"ws_author_{profile_id}_d{day}"))
    rec = rt.run_stage_agent(
        prompt, role="day_implementer", container=container, cwd=cwd,
        constitution=IMPL_CONST, timeout=cfg.WORLD_SYN_IMPLEMENT_TIMEOUT_S,
        max_turns=120, done_check=_done_check_impl(day_dir, prepared_apps))
    rt.teardown_creator(cfg.PROJECT_ROOT)
    complete = (day_dir / "delta.sh").exists() and (day_dir / "produces.json").exists()
    if complete:
        app_lib.finalize_day_apps(profile_id, day, manifest, day_dir, prev_snap, prepared_apps)
    return complete


# The solver never sees accounts.json; look for credentials in the persona's own files.
_CRED_SEARCH_DIRS = ("workspace",)
_CRED_MAX_BYTES = 2_000_000  # skip binaries and archives
_CRED_MIN_SITES = 3  # a single account is not a credential store


def check_credentials_discoverable(profile_id: str, world_dir: Path) -> tuple[bool, list[str]]:
    """L2b: check the persona's web passwords are discoverable inside the world itself."""
    accounts_p = cfg.WS_PROFILES_DIR / profile_id / "accounts.json"
    if not accounts_p.exists():
        return True, []
    try:
        accounts = json.loads(accounts_p.read_text(errors="replace"))
    except ValueError as exc:
        return False, [f"accounts.json unreadable: {exc}"]
    wanted = {str(a.get("site")): str(a.get("password") or "") for a in accounts if a.get("password")}
    if not wanted:
        return True, []
    haystack = []
    for sub in _CRED_SEARCH_DIRS:
        root = world_dir / sub
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            try:
                if f.stat().st_size > _CRED_MAX_BYTES:
                    continue
                haystack.append(f.read_text(errors="replace"))
            except OSError:
                continue
    blob = "\n".join(haystack)
    found = sorted(site for site, pw in wanted.items() if pw and pw in blob)
    need = min(_CRED_MIN_SITES, len(wanted))
    if len(found) >= need:
        return True, []
    missing = sorted(set(wanted) - set(found))
    return False, [
        f"{len(found)}/{len(wanted)} site passwords are discoverable under workspace/ "
        f"(need at least {need}); found for: {', '.join(found) or 'none'}",
        f"not discoverable: {', '.join(missing[:10])}",
        "An agent in this world cannot log in as the persona, so tasks can only ever "
        "'create a new account' and the persona's private history becomes unreachable. "
        "Write the persona's real credentials into the world the way that persona would "
        "actually keep them (an exported password-manager vault under Documents/, a "
        "credentials note, or a browser profile) using the exact passwords from "
        "accounts.json, and keep it deterministic.",
    ]


def _selected_app_boundary_errors(profile_id: str, day: int, produces: dict) -> list[str]:
    """Reject MCP writes to services that the immutable day selection did not pin."""
    try:
        selected = set(app_lib.load_plan_app_selections(skill_manifest_file(profile_id, day)))
    except RuntimeError as exc:
        return [f"selected-app boundary could not load day selection: {exc}"]
    declared: set[str] = set()
    for bucket in ("db_writes", "db_updates"):
        values = produces.get(bucket) or {}
        if isinstance(values, dict):
            declared.update(str(app_id) for app_id in values)
    extras = sorted(declared - selected)
    if not extras:
        return []
    return [
        "selected-app boundary: produces.json declares writes to unselected MCP "
        f"service(s): {', '.join(extras)}; selected: {', '.join(sorted(selected)) or '(none)'}"
    ]


def _run_gates(profile_id: str, day: int, day_dir: Path,
               prev_snap: Path | None, frozen: str) -> GateResult:
    g = GateResult()
    g.lint = pri.lint_delta(day_dir)
    g.lint += _cross_profile_leaks(profile_id, day_dir)
    ovdir = day_dir / "overlay"
    if ovdir.is_dir():
        g.lint += ov.overlay_code_safety_errors(ovdir)
    if g.lint:
        return g
    produces = pri.load_produces(day_dir / "produces.json")
    # L1 determinism: two replays must hash identically (work dir must be container-visible).
    work = cfg.SCRATCH / f"ws_gate_{profile_id}_d{day}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        tmpA = work / "A"
        tmpB = work / "B"
        try:
            _materialize_day(prev_snap, day_dir, frozen, tmpA)
            _materialize_day(prev_snap, day_dir, frozen, tmpB)
        except RuntimeError as e:
            g.determinism_checked = True
            g.determinism = False
            g.determinism_detail = f"delta.sh failed: {e}"
            return g
        ha, hb = pri.world_hash(tmpA), pri.world_hash(tmpB)
        g.determinism_checked = True
        g.determinism = (ha == hb)
        g.determinism_detail = "" if ha == hb else f"hash A={ha[:12]} != B={hb[:12]}"
        if not g.determinism:
            return g
        g.world_hash = ha
        # L2 produces check.
        g.produces_checked = True
        g.produces_ok, g.produces_problems = pri.check_produces(
            produces, tmpA, prev_snap, day_dir=day_dir)
        g.produces_problems.extend(_selected_app_boundary_errors(profile_id, day, produces))
        g.produces_ok = not g.produces_problems
        if not g.produces_ok:
            return g
        # L2b: the world itself must hand the solver a way to log in.
        g.credentials_checked = True
        g.credentials_ok, g.credentials_problems = check_credentials_discoverable(
            profile_id, tmpA)
        if not g.credentials_ok:
            return g
        # L3: boot the day-N world live and verify.
        g.gui_checked = True
        g.gui_ok, g.gui_detail, g.overlay_mcp, g.overlay_webharbor = _verify_live(
            profile_id, tmpA, produces)
        # L4: reviewer agent.
        g.reviewer_checked = True
        (g.reviewer_accept,
         g.reviewer_problems,
         g.skill_feedback) = _run_reviewer(
            profile_id, day, tmpA, prev_snap, produces)
        return g
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _verify_live(profile_id: str, world_dir: Path, produces: dict):
    """Boot the day-N world and verify GUI login, overlay MCP apps and patched site routes."""
    gui_ok, gui_detail = False, ""
    mcp_loaded: list[str] = []
    wh_sites: list[str] = []
    accounts = pri.load_accounts(cfg.WS_PROFILES_DIR / profile_id)
    try:
        # Load only the sites the gate exercises; declared names may be DB filenames.
        target_sites = sorted(
            {pri.resolve_site(t["site"]) for t in (produces.get("web_accounts") or []) if t.get("site")}
            | set(ov.list_overlay_webharbor_sites(world_dir)))
        if pri.account_for(accounts, "amazon") and "amazon" not in target_sites:
            target_sites = sorted(set(target_sites) | {"amazon"})
        lw = rt.boot_live_world(world_dir, fresh=True, with_mcp=True,
                                load_sites=target_sites or None)
    except Exception as e:
        return False, f"live boot failed: {e}", mcp_loaded, wh_sites
    try:
        expected_mcp = sorted(path.stem for path in (world_dir / "data").glob("*.db"))
        if lw.mcp_host_port:
            apps = rt.gateway_apps(lw.mcp_host_port)
            mcp_loaded = list(apps.get("loaded", []))
        missing_mcp = sorted(set(expected_mcp) - set(mcp_loaded))
        for site in ov.list_overlay_webharbor_sites(world_dir):
            status, body = _wh_fetch(lw.webharbor_container, site, "/")
            if str(status).startswith("2") and len(body) > 100:
                wh_sites.append(site)
        # Accept if any target renders; always include amazon as a known login path.
        targets = list(produces.get("web_accounts") or [])
        amz = pri.account_for(accounts, "amazon")
        if amz and not any(t.get("site") == "amazon" for t in targets):
            targets.insert(0, {"site": "amazon", "username": amz.get("username")})
        # Fall back to the persona's real accounts when no declared target has a login.
        if accounts and not any(pri.account_for(accounts, t.get("site")) for t in targets):
            targets = targets + [{"site": a["site"], "username": a["username"]}
                                 for a in accounts[:3]]
        rendered = False
        details = []
        known_sites = sorted({a.get("site") for a in accounts if a.get("site")})
        for t in targets[:4]:
            # Downstream lookups are keyed by canonical id.
            site = pri.resolve_site(t.get("site") or "")
            acc = pri.account_for(accounts, site)
            if not acc:
                # List the persona's known sites so a mis-declared target is distinguishable.
                details.append(f"{site}:SKIPPED(no account; known={','.join(known_sites)})")
                continue
            hist = _SITE_HISTORY_PATH.get(site, "/")
            status, body = _wh_login_fetch(
                lw.webharbor_container, site, hist,
                acc.get("email") or acc.get("username"), acc.get("password", ""))
            authed = str(status).startswith("2") and len(body) > 200 and 'name="password"' not in body
            details.append(f"{site}:{status}:{'ok' if authed else 'FAIL'}")
            if authed:
                rendered = True
        if rendered and not missing_mcp:
            gui_ok, gui_detail = True, "; ".join(details)
        else:
            failures = []
            if not rendered:
                failures.append("no history page rendered authed: " + "; ".join(details))
            if missing_mcp:
                failures.append(f"MCP services not loaded: {missing_mcp}; loaded={mcp_loaded}")
            gui_ok, gui_detail = False, "; ".join(failures)
    finally:
        rt.teardown_live_world(lw)
    return gui_ok, gui_detail, mcp_loaded, wh_sites


def _wh_fetch(wh_container: str, site: str, path: str):
    from wildawm.webharbor import control as whc
    return whc.fetch(wh_container, site, path)


def _wh_login_fetch(wh_container: str, site: str, path: str, email: str, password: str):
    from wildawm.webharbor import control as whc
    return whc.login_and_fetch(wh_container, site, path=path, email=email,
                               password=password, login_path="/login")


def _run_reviewer(profile_id: str, day: int, world_dir: Path,
                  prev_snap: Path | None, produces: dict
                  ) -> tuple[bool, list[str], list[dict]]:
    """L4: host-side reviewer agent over the materialized day-N world."""
    verdict_path = world_dir.parent / f"review_day{day}.json"
    if verdict_path.exists():
        verdict_path.unlink()
    plan = plan_file(profile_id, day).read_text(errors="replace")
    manifest_path = skill_manifest_file(profile_id, day)
    refs = skill_lib.plan_skill_refs(manifest_path)
    skill_index = "\n".join(
        f"- {ref.skill_id}: {ref.path} (planned activity: {ref.activity})"
        for ref in refs)
    try:
        selections = app_lib.load_plan_app_selections(manifest_path)
    except RuntimeError:
        selections = {}
    app_index = ("\n".join(
        f"- {app_id}: {selection.path} (revision {selection.revision}; policy={selection.policy})"
        for app_id, selection in sorted(selections.items()))
        if selections else "\n".join(
            f"- {req.app_id}: {req.path}"
            for req in app_lib.plan_app_requirements(manifest_path))) or "(none)"
    summary = _world_summary(world_dir)
    prompt = f"""# Task: review day {day} for persona {profile_id}

Inspect the materialized day-N world in the CURRENT DIRECTORY (`{world_dir}`): query `data/*.db` + `webharbor/*.db` with sqlite3, read `workspace/` and `overlay/`. Compare against the plan.

DAY PLAN (day_{day}.md):
{plan}

PRODUCES DECLARATION:
{json.dumps(produces, indent=2)[:2000]}

MATCHED SHARED EVENT SKILLS:
{skill_index}

MATCHED SHARED MCP APPLICATIONS:
{app_index}

Read the matched skills and application contracts. If a genuine defect comes from reusable skill guidance that is missing, vague, or wrong, return a targeted `skill_feedback` entry for that skill. Do not propose a shared-skill change for a one-off implementation error.

WORLD SUMMARY (row counts / files — verify these yourself by querying):
{summary}

Write your verdict as a single JSON object to: `{verdict_path}` (exactly the schema in your constitution). Then reply DONE."""
    container = rt.ensure_creator(cfg.PROJECT_ROOT)

    def _dc():
        if not verdict_path.exists():
            return False, "verdict not written"
        try:
            value = json.loads(verdict_path.read_text())
        except Exception as exc:  # noqa: BLE001
            return False, f"verdict is not valid JSON: {exc}"
        if not isinstance(value, dict) or not isinstance(value.get("accept"), bool):
            return False, "verdict must contain a boolean accept field"
        return True, "valid verdict written"

    failures: list[str] = []
    j = None
    try:
        for attempt in range(1, 4):
            verdict_path.unlink(missing_ok=True)
            try:
                rec = rt.run_stage_agent(
                    prompt, role="world_reviewer", container=container,
                    cwd=str(world_dir), constitution=REVIEW_CONST,
                    timeout=cfg.LLM_MEDIUM_TIMEOUT_S, max_turns=40,
                    done_check=_dc)
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    f"attempt {attempt}: {type(exc).__name__}: {exc}")
                continue
            try:
                candidate = json.loads(verdict_path.read_text())
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    f"attempt {attempt}: status={rec.status}; invalid verdict: {exc}")
                continue
            if not isinstance(candidate, dict) or not isinstance(candidate.get("accept"), bool):
                failures.append(
                    f"attempt {attempt}: status={rec.status}; missing boolean accept")
                continue
            j = candidate
            break
    finally:
        rt.teardown_creator(cfg.PROJECT_ROOT)
    if j is None:
        raise RuntimeError(
            "world reviewer infrastructure failed after retries: "
            + "; ".join(failures))
    persistent_path = day_script_dir(profile_id, day) / f"review_day{day}.json"
    try:
        persistent_path.write_text(json.dumps(j, indent=2, ensure_ascii=False) + "\n")
    except OSError:
        # The audit copy is best-effort; never fail a passed gate over it.
        pass
    feedback = j.get("skill_feedback")
    if not isinstance(feedback, list):
        feedback = []
    return bool(j.get("accept")), list(j.get("problems", [])), feedback


def _world_summary(world_dir: Path) -> str:
    import sqlite3
    lines = []
    for sub in ("data", "webharbor"):
        d = world_dir / sub
        if not d.is_dir():
            continue
        for db in sorted(d.glob("*.db")):
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                tabs = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                counts = {t: con.execute(f"SELECT count(*) FROM '{t}'").fetchone()[0] for t in tabs[:12]}
                con.close()
                lines.append(f"{sub}/{db.name}: {counts}")
            except Exception as e:
                lines.append(f"{sub}/{db.name}: err {e}")
    ws = world_dir / "workspace"
    if ws.is_dir():
        files = [str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file()][:40]
        lines.append(f"workspace files ({len(files)} shown): {files}")
    return "\n".join(lines[:60])


def synthesize(profile_id: str, day: int, *, force: bool = False,
               max_iters: int | None = None, steer: str = "") -> Path:
    """Implement day N (author, gate, bounded repair); returns the day_N script dir on success."""
    max_iters = max_iters or cfg.WORLD_SYN_MAX_IMPLEMENT_ITERS
    if not plan_file(profile_id, day).exists():
        raise RuntimeError(f"day {day} plan missing for {profile_id} — run `awm syn dayplan` first.")
    if day > 0 and not is_materialized(profile_id, day - 1):
        raise RuntimeError(
            f"day impl refused: day {day - 1} not materialized for {profile_id}. Days are strictly serial.")
    manifest_path = skill_manifest_file(profile_id, day)
    skill_lib.load_plan_manifest(manifest_path)
    skill_lib.validate_plan_skills(profile_id, manifest_path)
    app_lib.ensure_plan_apps(profile_id, day, manifest_path)
    out = day_script_dir(profile_id, day)
    # Skip a built day only if its gate passed, so crashed half-built days are redone.
    if out.exists() and (out / ".gate_passed").exists() and not force:
        skill_lib.record_usage(profile_id, day, manifest_path)
        app_lib.record_usage(profile_id, day, manifest_path)
        return out
    prev_snap = anchors.master_snapshot_dir(profile_id, day - 1) if day > 0 else None
    frozen = anchors.day_date(profile_id, day)
    evidence = ""
    last_gate = None
    for it in tqdm(range(max_iters), desc=f"dayimpl[{profile_id}/d{day}]"):
        ok = _author(profile_id, day, out, prev_snap, frozen, evidence, steer=steer)
        if not ok:
            evidence = "day_implementer did not produce delta.sh + produces.json. " \
                       "Inspect the day N-1 world and write the full deliverable set."
            print(f"[dayimpl {profile_id} d{day} iter {it}] AUTHOR FAILED\n{evidence}")
            continue
        last_gate = _run_gates(profile_id, day, out, prev_snap, frozen)
        print(f"[dayimpl {profile_id} d{day} iter {it}] GATE OK={last_gate.ok}")
        if last_gate.skill_feedback:
            try:
                last_gate.evolved_skills = skill_lib.evolve_from_feedback(
                    profile_id, day, manifest_path, last_gate.skill_feedback)
            except Exception as exc:  # noqa: BLE001 - skill feedback must not disappear silently
                last_gate.reviewer_accept = False
                last_gate.reviewer_problems.append(
                    f"shared skill evolution failed: {type(exc).__name__}: {exc}")
        if not last_gate.ok:
            print(last_gate.evidence())
        if last_gate.ok:
            skill_lib.record_usage(profile_id, day, manifest_path)
            app_lib.record_usage(profile_id, day, manifest_path)
            (out / ".gate_passed").write_text(f"day {day} gate passed iter {it}\n")
            if last_gate.world_hash:
                (out / WORLD_HASH_FILE).write_text(last_gate.world_hash)
            # Accepted days no longer need their staged scratch worlds; failed days keep them.
            for staged in (cfg.SCRATCH / f"ws_author_{profile_id}_d{day}",
                           cfg.SCRATCH / f"ws_plan_{profile_id}_d{day}"):
                shutil.rmtree(staged, ignore_errors=True)
            return out
        evidence = last_gate.evidence()
        shutil.rmtree(out, ignore_errors=True)
    raise RuntimeError(f"day impl failed after {max_iters} iterations for {profile_id} day {day}:\n{evidence}")
