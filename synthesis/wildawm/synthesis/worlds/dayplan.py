"""Day planning: day plan plus shared event skills; day N requires day N-1 to be materialized."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


from wildawm import const_config as cfg
from wildawm.synthesis.worlds import anchors
from wildawm.synthesis.worlds import app_library as app_lib
from wildawm.synthesis.worlds import primitives as pri
from wildawm.synthesis.worlds import runtime as rt
from wildawm.synthesis.worlds import skills as skill_lib
from wildawm.webharbor import lib as wh_lib

CONSTITUTION = Path(__file__).resolve().parent / "constitutions" / "day_planner.md"

# Match bare site DB filenames: delta scripts build the webharbor/ path from a variable.
SITE_DBS = ("allrecipes", "amazon_store", "apple_store", "arxiv", "bbc_news", "booking",
            "cambridge", "coursera", "espn", "github_mirror", "gmaps",
            "google_flights", "google_search", "hf", "wolfram_alpha")
# Services used on at most this share of prior days are flagged as under-used.
UNDERUSED_SHARE = 0.40
# Matches both a bare "site.db" and a "webharbor/site.db" literal.
_DB_FILE_RE = re.compile(r"[\"'](?:[\w./-]*/)?([a-z_]+)\.db[\"']")


def _profile_name_for_plan(profile_dir: Path) -> str:
    """A persona's display name, read the same way dayimpl's leak lint reads it."""
    persona = profile_dir / "persona.md"
    if not persona.exists():
        return ""
    text = persona.read_text(errors="replace")
    m = re.search(r"^\s*[-*]\s*\*\*Name:\*\*\s*(.+?)\s*$", text, re.M)
    if m:
        return m.group(1).strip()
    m = re.search(r"^#\s*Persona Bible\s*[—:-]\s*(.+?)\s*$", text, re.M)
    return m.group(1).strip() if m else ""


def _prior_day_dirs(profile_id: str) -> list[Path]:
    base = cfg.WS_SCRIPTS_DIR / profile_id
    if not base.is_dir():
        return []
    return sorted((d for d in base.iterdir() if d.is_dir() and (d / "delta.sh").exists()),
                  key=lambda d: int(d.name.split("_", 1)[1]) if d.name.split("_", 1)[1].isdigit() else 0)


def coverage(profile_id: str) -> dict:
    """Count prior days that wrote to each MCP app (produces.json) and each site (script scan)."""
    days = _prior_day_dirs(profile_id)
    apps: Counter[str] = Counter()
    sites: Counter[str] = Counter()
    new_apps: Counter[str] = Counter()
    for day_dir in days:
        produces = day_dir / "produces.json"
        touched_apps: set[str] = set()
        if produces.exists():
            try:
                decl = json.loads(produces.read_text(errors="replace"))
            except ValueError:
                decl = {}
            for bucket in ("db_writes", "db_updates"):
                touched_apps |= {str(a) for a in (decl.get(bucket) or {})}
            for app in ((decl.get("overlay") or {}).get("new_apps") or []):
                new_apps[str(app)] += 1
        for app in touched_apps:
            apps[app] += 1
        # Only executable sources: prose may merely mention a site without writing to it.
        blob = "\n".join(f.read_text(errors="replace")
                         for f in day_dir.rglob("*")
                         if f.is_file() and f.suffix in (".sh", ".py"))
        for db in set(_DB_FILE_RE.findall(blob)) & set(SITE_DBS):
            sites[db] += 1
    return {"n_days": len(days), "apps": apps, "sites": sites, "new_apps": new_apps}


def _profile_webharbor_dbs(profile_id: str) -> set[str]:
    accounts_path = cfg.WS_PROFILES_DIR / profile_id / "accounts.json"
    try:
        accounts = json.loads(accounts_path.read_text())
    except (OSError, ValueError):
        return set()
    allowed = set()
    for account in accounts if isinstance(accounts, list) else []:
        site = str(account.get("site") or "") if isinstance(account, dict) else ""
        if not site:
            continue
        try:
            allowed.add(wh_lib.dbfile_for_site(site).removesuffix(".db"))
        except KeyError:
            continue
    return allowed


def _profile_signature(profile_id: str) -> dict:
    path = cfg.WS_PROFILES_DIR / profile_id / "signature.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _roadmap_mcp_apps(profile_id: str) -> set[str]:
    apps = _profile_signature(profile_id).get("mcp_apps") or []
    return {str(app).strip() for app in apps if str(app).strip()}


def _format_coverage(profile_id: str) -> str:
    """Describe prior service use as soft coverage context for the planner."""
    cov = coverage(profile_id)
    n = cov["n_days"]
    roadmap = _roadmap_mcp_apps(profile_id)
    used_roadmap = roadmap & set(cov["apps"])
    unintroduced = sorted(roadmap - used_roadmap)
    if not n:
        return (
            "This is the profile's first day. The initial world contains the 15 WebHarbor websites and no MCP services. "
            f"The persona's grounded multi-day MCP roadmap is: {', '.join(sorted(roadmap))}. "
            "The roadmap is descriptive, not a daily quota. Create only services used by concrete day-0 activities; later roadmap services remain unimplemented until a relevant day first uses them."
        )
    threshold = max(1, int(n * UNDERUSED_SHARE))
    app_lines, cold_apps = [], []
    for app in sorted(set(cov["apps"]) | roadmap):
        used = cov["apps"].get(app, 0)
        status = "not introduced" if used == 0 else f"wrote on {used}/{n} prior days"
        app_lines.append(f"- {app}: {status}")
        if 0 < used <= threshold:
            cold_apps.append(f"{app} ({used}/{n})")
    site_lines, cold_sites = [], []
    eligible_sites = _profile_webharbor_dbs(profile_id)
    for site in SITE_DBS:
        if site not in eligible_sites:
            continue
        used = cov["sites"].get(site, 0)
        site_lines.append(f"- {site}: wrote on {used}/{n} prior days")
        if used <= threshold:
            cold_sites.append(f"{site} ({used}/{n})")
    parts = [
        f"## Service coverage across this persona's {n} prior days (MCP app writes)",
        "\n".join(app_lines),
        "",
        f"Roadmap services not yet introduced: {', '.join(unintroduced) if unintroduced else '(none)' }.",
        "This list is informational. Introduce a service only when a concrete activity requires it.",
        "",
        "## WebHarbor site coverage across the same days (site DB writes)",
        "\n".join(site_lines),
        "",
        "## UNDER-USED",
        f"Active MCP apps: {', '.join(cold_apps) if cold_apps else '(none)' }",
        f"Authenticated persona sites: {', '.join(cold_sites) if cold_sites else '(none; coverage is even)' }",
        "",
        "Use under-used services when they fit the persona's arcs. Introduce a new service only through a concrete activity. Web writes and updates may target only sites present in this persona's accounts.json. A different site may be read signed-out only. Prefer at least one under-used authenticated site unless the persona's arcs make that implausible.",
    ]
    if cov["new_apps"]:
        parts += ["", f"Overlay apps this persona already grew: {', '.join(sorted(cov['new_apps']))}"]
    return "\n".join(parts)


def plan_dir(profile_id: str) -> Path:
    return cfg.WS_PLAN_DIR / profile_id


def plan_file(profile_id: str, day: int) -> Path:
    return plan_dir(profile_id) / f"day_{day}.md"


def skill_manifest_file(profile_id: str, day: int) -> Path:
    return skill_lib.plan_manifest_path(profile_id, day)


def is_materialized(profile_id: str, day: int) -> bool:
    if day < 0:
        return True
    return anchors.master_snapshot_dir(profile_id, day).is_dir()


def _prior_state_dir(profile_id: str, day: int) -> Path | None:
    """The day N-1 cumulative snapshot the planner inspects (None for day 0)."""
    if day <= 0:
        return None
    return anchors.master_snapshot_dir(profile_id, day - 1)


def _done_check_plan(profile_id: str, out_path: Path, skill_manifest: Path):
    def _check() -> tuple[bool, str]:
        if not out_path.exists() or out_path.stat().st_size < 40:
            return False, "day plan missing/too short"
        try:
            manifest = skill_lib.load_plan_manifest(skill_manifest)
        except RuntimeError as exc:
            return False, str(exc)
        event_count = len(manifest.get("events") or [])
        if not 4 <= event_count <= 6:
            return False, f"day plan must contain 4-6 coherent major activities; found {event_count}"
        return True, "day plan and skill mapping written"
    return _check


def synthesize(profile_id: str, day: int, *, force: bool = False,
               steer: str = "") -> Path:
    """Write day_N.md for ``profile_id``; refuses if day N-1 is not materialized."""
    if day > 0 and not is_materialized(profile_id, day - 1):
        raise RuntimeError(
            f"day plan refused: day {day - 1} is not materialized for {profile_id}. "
            f"Days are strictly serial — implement+prebake day {day - 1} first.")
    out = plan_file(profile_id, day)
    skill_manifest = skill_manifest_file(profile_id, day)
    if force:
        app_lib.selection_manifest_path(skill_manifest).unlink(missing_ok=True)
    if out.exists() and skill_manifest.exists() and not force:
        valid, _reason = _done_check_plan(profile_id, out, skill_manifest)()
        if valid:
            # Resume app selection even when package files already exist.
            app_lib.ensure_plan_apps(profile_id, day, skill_manifest)
            skill_lib.ensure_plan_skills(profile_id, day, skill_manifest)
            return out
        app_lib.selection_manifest_path(skill_manifest).unlink(missing_ok=True)
        skill_manifest.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)

    prof = cfg.WS_PROFILES_DIR / profile_id
    persona = (prof / "persona.md").read_text(errors="replace") if (prof / "persona.md").exists() else ""
    accounts = ""
    if (prof / "accounts.json").exists():
        accounts = (prof / "accounts.json").read_text(errors="replace")
    signature = ""
    if (prof / "signature.json").exists():
        signature = (prof / "signature.json").read_text(errors="replace")
    prior_plans = ""
    pd = plan_dir(profile_id)
    if pd.is_dir():
        prior_plans = "\n\n---\n\n".join(
            f"## day_{d.name[4:-3]}\n{d.read_text(errors='replace')}"
            for d in sorted(pd.glob("day_*.md")) if d != out)
    prior_state = _prior_state_dir(profile_id, day)

    container = rt.ensure_creator(cfg.PROJECT_ROOT)
    # Work in a throwaway copy so the agent cannot mutate the day N-1 master.
    cwd = (str(pri.stage_world(prior_state, cfg.SCRATCH / f"ws_plan_{profile_id}_d{day}"))
           if prior_state else str(prof))
    inspect_hint = (
        f"Inspect the REAL day {day - 1} state in the CURRENT DIRECTORY: "
        f"query `data/*.db` and `webharbor/*.db` with sqlite3, read `workspace/`. "
        f"Continue the persona's arcs from this real state."
        if prior_state else
        f"This is day 0 — there is no prior state. The clean initial world contains only the 15 seeded WebHarbor websites; `data/` and `overlay/apps/` are empty. Plan the CLI home, WebHarbor account setup, and only the MCP services required by concrete day-0 activities. Each required MCP service will be implemented from scratch in the shared cross-persona app library before this day is materialized. Do not create a broad service baseline. "
        f"Day 0 must also leave the persona's own credentials somewhere in `workspace/` the way "
        f"this persona would really keep them (an exported password-manager vault, a credentials "
        f"note, a browser profile) — `accounts.json` is invisible to anyone living in the world, "
        f"so without this nobody can ever sign in as the persona and reach their private history.")
    steer_block = f"\n\nSTEERING FOR THIS DAY (honor it, grounded in the persona):\n{steer}" if steer else ""
    coverage_block = _format_coverage(profile_id)
    # NPC names must not collide with other personas, or dayimpl's lint rejects every attempt.
    reserved_names = sorted({
        _profile_name_for_plan(p) for p in cfg.WS_PROFILES_DIR.iterdir()
        if p.is_dir() and p.name != profile_id and not p.name.startswith("_staging")
    } - {""})
    reserved_block = (
        "\n\n## RESERVED NAMES\n"
        "These identities belong to other personas. Anyone you invent must be named outside this "
        "set, and the list itself must not appear in the plan — a plan carrying one of these names "
        "fails every implementation of it.\n"
        + "\n".join(f"- {n}" for n in reserved_names)) if reserved_names else ""

    # Some site ids differ from their DB filename; give the mapping before the plan fixes names.
    site_id_block = "\n".join(f"  - {s}  (webharbor/{wh_lib.dbfile_for_site(s)})"
                              for s in wh_lib.all_sites())

    skill_index = skill_lib.format_library_index()
    app_index = app_lib.format_library_index()
    prompt = f"""# Task: plan day {day} for persona {profile_id}

Write both outputs:
1. Day plan: `{out}`
2. Skill mapping: `{skill_manifest}`

The skill mapping must be JSON with this schema:
```json
{{
  "schema_version": 1,
  "events": [
    {{
      "skill": "reusable_snake_case_id",
      "activity": "generic description of the reusable activity, without persona-specific names",
      "interfaces": ["cli", "mcp", "web"],
      "mcp_apps": ["service_id_used_by_this_activity"],
      "mcp_app_policy": "shared"
    }}
  ]
}}
```
Map exactly 4-6 coherent major activities to shared event skills. Reuse an exact skill id from the library when its semantics fit. Propose a new reusable id when no listed skill fits. Combine causally connected MCP work into one multi-service activity instead of creating one event per service. A later stage creates every missing skill before implementation.

For each event, `mcp_apps` must name exactly the MCP services that activity uses. Use `[]` when the activity has no MCP leg and omit `mcp_app_policy`. For an MCP activity, set `mcp_app_policy` to `shared` when its capability should be reusable across personas; the app creator will inspect the selected implementation and either reuse it or publish a backward-compatible revision. Set it to `local` only for a persona- or world-specific customization that must remain in this world's overlay. Reuse an existing shared app id when its service semantics fit, even when the listed operations need a shared revision. If no shared app fits, choose a generic snake_case service id; the app creator will implement it once and publish it for later days and personas.

SHARED EVENT-SKILL LIBRARY:
{skill_index}

SHARED MCP-APPLICATION LIBRARY:
{app_index}

PERSONA BIBLE:
{persona[:6000]}

PERSONA SIGNATURE:
{signature[:1800]}

ACCOUNTS (username/password per site — the implementer will seed these):
{accounts[:2000]}

AUTHENTICATED SITE RULE: Persisted WebHarbor writes or updates may target only sites listed in ACCOUNTS. The GUI gate has no credential for any other site. A site absent from ACCOUNTS may be read signed-out only and must not receive a signup, save, order, enrollment, booking, history, or other account state.

CANONICAL WEBHARBOR SITE IDS — name sites by these, never by the DB filename you see in `webharbor/`. The plan sets the vocabulary every later artifact inherits, and the id is what the gates accept:
{site_id_block}
{f"PRIOR DAY PLANS (continue these arcs, do not restart):{prior_plans[:6000]}" if prior_plans else ""}

{inspect_hint}{steer_block}

{coverage_block}{reserved_block}

Write a realistic, continuous day-{day} plan across CLI/MCP/Web (see your constitution). Keep to the read budget it sets, and aim that budget at the UNDER-USED services above rather than at the apps this persona already lives in. When done, WRITE both files and reply DONE."""
    dc = _done_check_plan(profile_id, out, skill_manifest)
    try:
        rec = rt.run_stage_agent(
            prompt, role="day_planner", container=container, cwd=cwd,
            constitution=CONSTITUTION, timeout=cfg.WORLD_SYN_PLAN_TIMEOUT_S,
            max_turns=40, done_check=dc)
        if not out.exists():
            raise RuntimeError(
                f"day plan failed for {profile_id} day {day}: "
                f"{rec.status}\n{rec.result_text[:500]}")
        skill_lib.load_plan_manifest(skill_manifest)
        app_lib.ensure_plan_apps(profile_id, day, skill_manifest, container=container)
        skill_lib.ensure_plan_skills(profile_id, day, skill_manifest, container=container)
    finally:
        rt.teardown_creator(cfg.PROJECT_ROOT)
    return out
