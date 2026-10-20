"""Shared, evolving event-skill library reused across days and personas."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from wildawm import const_config as cfg
from wildawm.synthesis.worlds import runtime as rt
from wildawm.webharbor import lib as wh_lib

SCHEMA_VERSION = 1
SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
REQUIRED_SECTIONS = (
    "Description",
    "Parameters",
    "Scenarios involved",
    "Consistency checks",
    "Preconditions",
    "Possible follow-ups",
)
CONSTITUTION = Path(__file__).resolve().parent / "constitutions" / "skill_creator.md"


@dataclass(frozen=True)
class SkillRef:
    skill_id: str
    activity: str
    interfaces: tuple[str, ...]
    mcp_apps: tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        return skill_path(self.skill_id)


def library_root() -> Path:
    return cfg.WS_EVENTS_DIR


def skill_dir(skill_id: str) -> Path:
    return library_root() / skill_id


def skill_path(skill_id: str) -> Path:
    return skill_dir(skill_id) / "skill.md"


def meta_path(skill_id: str) -> Path:
    return skill_dir(skill_id) / "meta.json"


def plan_manifest_path(profile_id: str, day: int) -> Path:
    return cfg.WS_PLAN_DIR / profile_id / f"day_{day}.skills.json"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


@contextlib.contextmanager
def _library_lock():
    root = library_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".library.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _section_present(text: str, section: str) -> bool:
    return bool(re.search(rf"^##\s+{re.escape(section)}\s*$", text, re.M | re.I))


def skill_problems(text: str, expected_id: str, *,
                   forbidden_terms: tuple[str, ...] = ()) -> list[str]:
    problems: list[str] = []
    if not SKILL_ID_RE.fullmatch(expected_id):
        problems.append(f"invalid skill id {expected_id!r}")
    if len(text.strip()) < 300:
        problems.append("skill.md is too short (<300 characters)")
    heading = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    if not heading or heading.group(1).strip().lower() != expected_id.lower():
        problems.append(f"first H1 must be '# {expected_id}'")
    for section in REQUIRED_SECTIONS:
        if not _section_present(text, section):
            problems.append(f"missing section: ## {section}")
    scenarios = re.search(
        r"^##\s+Scenarios involved\s*$([\s\S]*?)(?=^##\s+|\Z)",
        text,
        re.M | re.I,
    )
    if not scenarios or not re.search(r"^###\s+\S", scenarios.group(1), re.M):
        problems.append("Scenarios involved must contain at least one ### scenario")
    lowered = text.lower()
    for term in forbidden_terms:
        term = str(term or "").strip()
        if len(term) >= 4 and term.lower() in lowered:
            problems.append(f"skill contains persona-specific term {term!r}")
    if re.search(r"(?:^|\s)(?:/home/|/workspace/|worlds/_masters/|worlds/_scripts/)", text):
        problems.append("skill contains a concrete runtime or synthesized-world path")
    return problems


def _description(text: str) -> str:
    match = re.search(
        r"^##\s+Description\s*$([\s\S]*?)(?=^##\s+|\Z)",
        text,
        re.M | re.I,
    )
    if not match:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", match.group(1)) if p.strip()]
    return re.sub(r"\s+", " ", paragraphs[0])[:300] if paragraphs else ""


def _scenario_names(text: str) -> list[str]:
    match = re.search(
        r"^##\s+Scenarios involved\s*$([\s\S]*?)(?=^##\s+|\Z)",
        text,
        re.M | re.I,
    )
    if not match:
        return []
    return [m.group(1).strip()[:80]
            for m in re.finditer(r"^###\s+(.+?)\s*$", match.group(1), re.M)]


def load_meta(skill_id: str) -> dict:
    path = meta_path(skill_id)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def list_skills() -> list[dict]:
    root = library_root()
    if not root.is_dir():
        return []
    rows = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        path = directory / "skill.md"
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        meta = load_meta(directory.name)
        rows.append({
            "id": directory.name,
            "path": str(path),
            "description": _description(text),
            "scenarios": _scenario_names(text),
            "revision": int(meta.get("revision", 1) or 1),
            "uses": list(meta.get("uses") or []),
        })
    return rows


def format_library_index(*, max_chars: int | None = None) -> str:
    rows = list_skills()
    if not rows:
        return "(empty: propose reusable snake_case skill ids for this day's activities)"
    lines = []
    for row in rows:
        scenarios = ", ".join(row["scenarios"]) or "unspecified"
        lines.append(
            f"- {row['id']} (revision {row['revision']}; scenarios: {scenarios}): "
            f"{row['description'] or 'no description'}"
        )
    text = "\n".join(lines)
    limit = max_chars or cfg.WORLD_SYN_SKILL_INDEX_CHARS
    if len(text) > limit:
        return text[:limit] + "\n[skill index truncated; reuse an exact listed id when it fits]"
    return text


def validate_plan_manifest(data: object) -> list[str]:
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["skill manifest must be a JSON object"]
    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    events = data.get("events")
    if not isinstance(events, list) or not events:
        problems.append("events must be a non-empty list")
        return problems
    covered_interfaces: set[str] = set()
    for index, event in enumerate(events):
        prefix = f"events[{index}]"
        if not isinstance(event, dict):
            problems.append(f"{prefix} must be an object")
            continue
        skill_id = str(event.get("skill") or "")
        if not SKILL_ID_RE.fullmatch(skill_id):
            problems.append(f"{prefix}.skill must be a snake_case skill id")
        activity = str(event.get("activity") or "").strip()
        if len(activity) < 20:
            problems.append(f"{prefix}.activity must describe the reusable activity")
        interfaces = event.get("interfaces")
        normalized: set[str] = set()
        if not isinstance(interfaces, list) or not interfaces:
            problems.append(f"{prefix}.interfaces must be a non-empty list")
        else:
            normalized = {str(v).lower() for v in interfaces}
            bad = sorted(normalized - {"cli", "mcp", "web"})
            if bad:
                problems.append(f"{prefix}.interfaces contains invalid values: {bad}")
            covered_interfaces |= normalized & {"cli", "mcp", "web"}
        mcp_apps = event.get("mcp_apps")
        if not isinstance(mcp_apps, list):
            problems.append(f"{prefix}.mcp_apps must be a list")
        else:
            app_ids = [str(v or "").strip() for v in mcp_apps]
            invalid_apps = [app for app in app_ids if not SKILL_ID_RE.fullmatch(app)]
            if invalid_apps:
                problems.append(f"{prefix}.mcp_apps contains invalid app ids: {invalid_apps}")
            web_ids = set(wh_lib.all_sites()) | {wh_lib.dbfile_for_site(site).removesuffix(".db") for site in wh_lib.all_sites()}
            web_as_apps = sorted(set(app_ids) & web_ids)
            if web_as_apps:
                problems.append(f"{prefix}.mcp_apps contains WebHarbor site ids, not MCP services: {web_as_apps}")
            if "mcp" in normalized and not app_ids:
                problems.append(f"{prefix}.mcp_apps must name each MCP service used by the activity")
            if "mcp" not in normalized and app_ids:
                problems.append(f"{prefix}.mcp_apps must be empty when interfaces omits mcp")
        policy = event.get("mcp_app_policy", "shared")
        if policy not in {"shared", "local"}:
            problems.append(f"{prefix}.mcp_app_policy must be shared or local")
        if not event.get("mcp_apps") and "mcp_app_policy" in event:
            problems.append(f"{prefix}.mcp_app_policy is only valid when mcp_apps is non-empty")
    missing = sorted({"cli", "mcp", "web"} - covered_interfaces)
    if missing:
        problems.append(
            "skill mapping must cover the day's CLI, MCP, and Web work; "
            f"missing {missing}")
    return problems


def load_plan_manifest(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid day skill manifest {path}: {exc}") from exc
    problems = validate_plan_manifest(data)
    if problems:
        raise RuntimeError(
            f"invalid day skill manifest {path}: " + "; ".join(problems))
    return data


def plan_skill_refs(path: Path) -> list[SkillRef]:
    data = load_plan_manifest(path)
    refs = []
    for event in data["events"]:
        refs.append(SkillRef(
            skill_id=str(event["skill"]),
            activity=str(event["activity"]).strip(),
            interfaces=tuple(str(v).lower() for v in event["interfaces"]),
            mcp_apps=tuple(str(v).strip() for v in event.get("mcp_apps", [])),
        ))
    return refs


def missing_plan_skills(path: Path) -> list[SkillRef]:
    unique: dict[str, SkillRef] = {}
    for ref in plan_skill_refs(path):
        unique.setdefault(ref.skill_id, ref)
    return [ref for ref in unique.values() if not skill_path(ref.skill_id).is_file()]


def plan_mcp_apps(path: Path) -> list[str]:
    data = load_plan_manifest(path)
    return sorted({
        str(app).strip()
        for event in data["events"]
        for app in event.get("mcp_apps", [])
        if str(app).strip()
    })


def validate_plan_skills(profile_id: str, manifest_path: Path) -> list[Path]:
    paths = []
    forbidden = _profile_terms(profile_id)
    for ref in plan_skill_refs(manifest_path):
        path = ref.path
        if not path.is_file():
            raise RuntimeError(f"day skill mapping references missing skill {ref.skill_id}")
        problems = skill_problems(
            path.read_text(errors="replace"), ref.skill_id,
            forbidden_terms=forbidden,
        )
        if problems:
            raise RuntimeError(f"shared skill {ref.skill_id} is invalid: {problems}")
        paths.append(path)
    return paths


def _profile_terms(profile_id: str) -> tuple[str, ...]:
    terms = [profile_id]
    persona = cfg.WS_PROFILES_DIR / profile_id / "persona.md"
    if persona.exists():
        text = persona.read_text(errors="replace")
        match = re.search(r"^\s*[-*]\s*\*\*Name:\*\*\s*(.+?)\s*$", text, re.M)
        if not match:
            match = re.search(r"^#\s*Persona Bible\s*[—:-]\s*(.+?)\s*$", text, re.M)
        if match:
            terms.append(match.group(1).strip())
    return tuple(terms)


def _done_check_skill(path: Path, skill_id: str, forbidden_terms: tuple[str, ...]):
    def _check() -> tuple[bool, str]:
        if not path.is_file():
            return False, "skill.md missing"
        problems = skill_problems(
            path.read_text(errors="replace"), skill_id,
            forbidden_terms=forbidden_terms,
        )
        return (not problems, "skill is complete" if not problems else "; ".join(problems[:5]))
    return _check


def _stage_dir(skill_id: str, profile_id: str, day: int, purpose: str) -> Path:
    safe_profile = re.sub(r"[^A-Za-z0-9_.-]+", "-", profile_id)
    return library_root() / ".staging" / f"{skill_id}-{safe_profile}-d{day}-{purpose}-{os.getpid()}"


def _skill_prompt(ref: SkillRef, profile_id: str, day: int, output: Path, *,
                  existing_text: str = "", feedback: str = "") -> str:
    mode = "Revise the existing reusable skill" if existing_text else "Create a new reusable skill"
    existing = f"\n\nEXISTING SKILL:\n```markdown\n{existing_text}\n```" if existing_text else ""
    feedback_block = f"\n\nREVIEWER FEEDBACK TO GENERALIZE:\n{feedback}" if feedback else ""
    app_paths = "\n".join(f"- {app}: {cfg.WS_APPS_DIR / app}" for app in ref.mcp_apps) or "(none)"
    return f"""# {mode}: {ref.skill_id}

Write the complete skill to `{output}`.

Reusable activity: {ref.activity}
Interfaces involved: {', '.join(ref.interfaces)}
MCP applications involved:
{app_paths}
Observed while synthesizing profile `{profile_id}` day {day}; this provenance is context only and MUST NOT appear in the skill.

The skill is shared across every persona. Express names, dates, ids, products, files, and account details as parameters or lookup rules. Do not copy persona-specific values from the day plan. Inspect schemas only in the matched shared MCP application paths above. Do not read source-tree reference applications. Do not modify any world, plan, profile, or existing library file.{existing}{feedback_block}

Follow the skill_creator constitution. Write all required sections, validate that the file contains no persona-specific identity or synthesized-world path, then reply DONE."""


def _publish_new(ref: SkillRef, staged: Path, profile_id: str, day: int) -> Path:
    text = staged.read_text()
    problems = skill_problems(text, ref.skill_id, forbidden_terms=_profile_terms(profile_id))
    if problems:
        raise RuntimeError(f"generated skill {ref.skill_id} is invalid: {problems}")
    final = skill_dir(ref.skill_id)
    with _library_lock():
        if skill_path(ref.skill_id).is_file():
            return skill_path(ref.skill_id)
        final.mkdir(parents=True, exist_ok=True)
        _atomic_write(final / "skill.md", text)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "id": ref.skill_id,
            "revision": 1,
            "created_by": {"profile": profile_id, "day": day},
            "last_updated_by": {"profile": profile_id, "day": day},
            "uses": [],
            "content_sha256": _sha256_text(text),
        }
        _atomic_write(final / "meta.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    return final / "skill.md"


def _generate_missing(ref: SkillRef, profile_id: str, day: int,
                      container: str) -> Path:
    stage = _stage_dir(ref.skill_id, profile_id, day, "create")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    output = stage / "skill.md"
    rec = rt.run_stage_agent(
        _skill_prompt(ref, profile_id, day, output),
        role="skill_creator",
        container=container,
        cwd=str(stage),
        constitution=CONSTITUTION,
        timeout=cfg.WORLD_SYN_SKILL_TIMEOUT_S,
        max_turns=cfg.WORLD_SYN_SKILL_MAX_TURNS,
        done_check=_done_check_skill(output, ref.skill_id, _profile_terms(profile_id)),
    )
    if not output.is_file():
        raise RuntimeError(
            f"skill creator failed for {ref.skill_id}: {rec.status}\n{rec.result_text[:500]}")
    try:
        return _publish_new(ref, output, profile_id, day)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def ensure_plan_skills(profile_id: str, day: int, manifest_path: Path, *,
                       container: str | None = None) -> list[Path]:
    refs = plan_skill_refs(manifest_path)
    owned_container = container is None
    if container is None and any(not skill_path(ref.skill_id).is_file() for ref in refs):
        container = rt.ensure_creator(cfg.PROJECT_ROOT)
    paths: list[Path] = []
    try:
        for ref in refs:
            path = skill_path(ref.skill_id)
            if not path.is_file():
                assert container is not None
                path = _generate_missing(ref, profile_id, day, container)
            paths.append(path)
    finally:
        if owned_container and container is not None:
            rt.teardown_creator(cfg.PROJECT_ROOT)
    validate_plan_skills(profile_id, manifest_path)
    return paths


def _feedback_entries(feedback: object, allowed: set[str]) -> list[dict]:
    if not isinstance(feedback, list):
        return []
    rows = []
    for row in feedback:
        if not isinstance(row, dict):
            continue
        skill_id = str(row.get("skill") or "")
        guidance = str(row.get("guidance") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if skill_id in allowed and guidance:
            rows.append({"skill": skill_id, "guidance": guidance, "reason": reason})
    return rows


def _evolve_one(ref: SkillRef, profile_id: str, day: int, feedback: dict,
                container: str) -> bool:
    current_path = skill_path(ref.skill_id)
    if not current_path.is_file():
        raise RuntimeError(f"cannot evolve missing skill {ref.skill_id}")
    current = current_path.read_text()
    current_sha = _sha256_text(current)
    stage = _stage_dir(ref.skill_id, profile_id, day, "evolve")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    output = stage / "skill.md"
    combined_feedback = str(feedback.get("reason") or "")
    if combined_feedback:
        combined_feedback += "\n"
    combined_feedback += str(feedback["guidance"])
    rec = rt.run_stage_agent(
        _skill_prompt(
            ref, profile_id, day, output,
            existing_text=current,
            feedback=combined_feedback,
        ),
        role="skill_creator",
        container=container,
        cwd=str(stage),
        constitution=CONSTITUTION,
        timeout=cfg.WORLD_SYN_SKILL_TIMEOUT_S,
        max_turns=cfg.WORLD_SYN_SKILL_MAX_TURNS,
        done_check=_done_check_skill(output, ref.skill_id, _profile_terms(profile_id)),
    )
    if not output.is_file():
        raise RuntimeError(
            f"skill evolution failed for {ref.skill_id}: {rec.status}\n{rec.result_text[:500]}")
    revised = output.read_text()
    problems = skill_problems(
        revised, ref.skill_id, forbidden_terms=_profile_terms(profile_id))
    if problems:
        raise RuntimeError(f"evolved skill {ref.skill_id} is invalid: {problems}")
    if revised == current:
        shutil.rmtree(stage, ignore_errors=True)
        return False
    try:
        with _library_lock():
            latest = current_path.read_text()
            if _sha256_text(latest) != current_sha:
                return False
            meta = load_meta(ref.skill_id)
            revision = int(meta.get("revision", 1) or 1)
            history = skill_dir(ref.skill_id) / "history"
            history.mkdir(parents=True, exist_ok=True)
            _atomic_write(history / f"revision_{revision:04d}.md", latest)
            _atomic_write(current_path, revised)
            meta.update({
                "schema_version": SCHEMA_VERSION,
                "id": ref.skill_id,
                "revision": revision + 1,
                "last_updated_by": {"profile": profile_id, "day": day},
                "last_feedback": {
                    "reason": str(feedback.get("reason") or ""),
                    "guidance": str(feedback.get("guidance") or ""),
                },
                "content_sha256": _sha256_text(revised),
            })
            meta.setdefault("created_by", {"profile": profile_id, "day": day})
            meta.setdefault("uses", [])
            _atomic_write(meta_path(ref.skill_id), json.dumps(
                meta, indent=2, ensure_ascii=False) + "\n")
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def evolve_from_feedback(profile_id: str, day: int, manifest_path: Path,
                         feedback: object) -> list[str]:
    refs_by_id = {ref.skill_id: ref for ref in plan_skill_refs(manifest_path)}
    rows = _feedback_entries(feedback, set(refs_by_id))
    if not rows:
        return []
    container = rt.ensure_creator(cfg.PROJECT_ROOT)
    changed = []
    try:
        for row in rows:
            if _evolve_one(refs_by_id[row["skill"]], profile_id, day, row, container):
                changed.append(row["skill"])
    finally:
        rt.teardown_creator(cfg.PROJECT_ROOT)
    return changed


def record_usage(profile_id: str, day: int, manifest_path: Path) -> None:
    usage = {"profile": profile_id, "day": day}
    for skill_id in sorted({ref.skill_id for ref in plan_skill_refs(manifest_path)}):
        with _library_lock():
            path = meta_path(skill_id)
            if not path.is_file():
                raise RuntimeError(f"cannot record usage for missing skill {skill_id}")
            meta = load_meta(skill_id)
            uses = list(meta.get("uses") or [])
            if usage not in uses:
                uses.append(usage)
            meta["uses"] = uses
            _atomic_write(path, json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
