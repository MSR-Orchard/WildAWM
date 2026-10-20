#!/usr/bin/env python3
"""Task synthesis orchestrator: sketch, author, validate, ensemble-solve, gate, probe, accept."""
import hashlib
import json
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from simpleArgParser import parse_args

from wildawm import const_config as cfg
from wildawm.json_utils import load_jsonc
from wildawm.synthesis.tasks import claude_runner as cr
from wildawm.synthesis.tasks import provision as prov
from wildawm.synthesis.worlds import anchors
from wildawm.synthesis.tasks import sketch as sk
from wildawm.synthesis.tasks import generator as gen
from wildawm.synthesis.tasks import verifier as vf
from wildawm.synthesis.tasks import llm_judge as lj
from wildawm.synthesis.tasks import judge as jd
from wildawm.synthesis.tasks import reason_helper as rh
from wildawm.synthesis.tasks import ensemble as ens
from wildawm.synthesis.tasks import probe as pb
from wildawm.synthesis.tasks import fixer as fx
from wildawm.synthesis.tasks import world_repair as wr
from wildawm.synthesis.tasks import world_patch as wp
from wildawm.synthesis.tasks import freeze as fz
from wildawm.synthesis.tasks import artifact_lint as alint
from wildawm.synthesis.tasks import synthesis_log as slog
from wildawm.synthesis.tasks import task_skill_pool as xp
from wildawm.synthesis.tasks import diversity as task_diversity
from wildawm.synthesis.tasks import xai_pool
from wildawm.synthesis.tasks import tasks_index as tx
from wildawm.webharbor import lib as wh_lib

AUTHOR_INFRA_STATUSES = {"server", "rate_limit", "timeout", "auth", "quota"}
FIXER_REPAIRABLE_VALIDATE_PREFIXES = (
    "scripts crashed on clean:",
    "clean world did NOT fail",
    "lightweight judge saw clean as",
    "golden.py crashed:",
    "golden world did NOT pass",
    "lightweight judge saw golden as",
)


def _log(msg: str):
    print(f"[synthesis {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _author_failures_are_infra(statuses: list[str], last_val: dict | None) -> bool:
    """Author failures count as infra only if validation never ran."""
    return bool(statuses) and last_val is None and all(
        (s or "") in AUTHOR_INFRA_STATUSES for s in statuses)


def _authoring_validation_defects(defects: list[str]) -> list[str]:
    """Validation defects the verifier fixer must not paper over (they need re-authoring)."""
    out = []
    for defect in defects or []:
        text = str(defect)
        if not text.startswith(FIXER_REPAIRABLE_VALIDATE_PREFIXES):
            out.append(text)
    return out


def _public_task_difficulty_problem(
        before: str | None, after: str | None, *, direction: str) -> str | None:
    """Reject a difficulty re-author that left the public task.md (near-)unchanged."""
    if not before or not after:
        return None
    before_norm = " ".join(re.findall(r"[a-z0-9]+", before.lower()))
    after_norm = " ".join(re.findall(r"[a-z0-9]+", after.lower()))
    if not before_norm or not after_norm:
        return None
    ratio = SequenceMatcher(None, before_norm, after_norm).ratio()
    before_tokens = set(before_norm.split())
    after_tokens = set(after_norm.split())
    overlap = len(before_tokens & after_tokens) / max(1, len(before_tokens | after_tokens))
    if ratio < 0.94 or overlap < 0.92:
        return None
    if direction == "deepen":
        return (
            "task.md did not materially change after a reject_too_easy deepen "
            f"request (text similarity {ratio:.2f}, token overlap {overlap:.2f}). "
            "A too-easy rejection must add or replace public outcome constraints "
            "that increase real task.md-only workload/decision complexity while "
            "staying natural and <=300 words; hidden-only verifier/golden changes "
            "are not enough."
        )
    if direction == "ease":
        return (
            "task.md did not materially change after a reject_too_hard ease "
            f"request (text similarity {ratio:.2f}, token overlap {overlap:.2f}). "
            "A too-hard rejection must simplify or clarify public outcome "
            "constraints enough to reduce real task.md-only difficulty while "
            "preserving every required interface and avoiding answer leakage; "
            "hidden-only verifier/golden changes are not enough."
        )
    raise ValueError(f"unsupported difficulty re-author direction: {direction!r}")


def _skill_event(*, stage: str, profile: str, day: int, difficulty: str,
                      slug: str, out_dir: Path, result: dict | None = None,
                      feedback: str = "") -> dict:
    meta = {}
    try:
        meta = load_jsonc(out_dir / "meta.json")
    except Exception:  # noqa: BLE001
        meta = {}
    return {
        "stage": stage,
        "profile": profile,
        "day": day,
        "difficulty": difficulty,
        "slug": slug,
        "title": (result or {}).get("title") or meta.get("title"),
        "area": (result or {}).get("area") or meta.get("area"),
        "archetype": (result or {}).get("archetype") or meta.get("archetype"),
        "interfaces": (result or {}).get("interfaces") or meta.get("interfaces"),
        "interface_flow": ((result or {}).get("interface_flow")
                           or (result or {}).get("flow")
                           or meta.get("interface_flow")),
        "verdict": (result or {}).get("verdict"),
        "reason": (result or {}).get("reason"),
        "gate": {
            k: (result or {}).get(k)
            for k in (
                "tier", "n_completed", "n_live", "max_depth",
                "bare_gate", "bare_n_completed", "bare_n_live",
                "bare_min_completed", "bare_max_completed", "bypassed",
            )
            if k in (result or {})
        },
        "feedback": feedback[:2000],
    }


def _offer_skill_write(*, rec, report: dict, stage: str, profile: str, day: int,
                       difficulty: str, slug: str, out_dir: Path,
                       result: dict | None = None, feedback: str = "") -> None:
    if not cfg.XAI_SYNTHESIS_SKILLS_ENABLED:
        return
    event = _skill_event(
        stage=stage, profile=profile, day=day, difficulty=difficulty,
        slug=slug, out_dir=out_dir, result=result, feedback=feedback)
    try:
        update = xp.update_from_event(event)
    except Exception as e:  # noqa: BLE001
        update = {"ok": False, "reason": f"{type(e).__name__}: {e}", "applied": []}
    compact = {k: v for k, v in update.items()
               if k not in ("call", "raw") and not k.startswith("_")}
    if update.get("call"):
        hist = rec.save_generator(
            f"task_skill_{stage}_{slug}_{uuid.uuid4().hex[:6]}.json",
            update.get("call"))
        compact["history"] = hist
    report["stages"].setdefault("task_skill", []).append({**event, **compact})
    rec.event("task_skill", "update", iteration=None, stage=stage,
              event=event, update=compact)


def _skill_stage_for_result(result: dict) -> str | None:
    verdict = str((result or {}).get("verdict") or "")
    return {
        "reject_unsolvable": "solvability_failed",
        "reject_verifier_disagree": "judge_disagreement",
        "reject_interface_bypass": "interface_bypass",
        "reject_too_easy": "difficulty_too_easy",
        "reject_too_hard": "difficulty_too_hard",
        "reject_verifier_fragile": "counterfactual_probe_failed",
        "reject_world_imperfect_unresolved": "world_repair_failed",
        "reject_generator": "author_failed",
    }.get(verdict)


def snapshot_dir(profile: str, day: int, task_dir: Path | None = None) -> Path:
    if task_dir and wp.has_patch(task_dir):
        return wp.patched_baseline_dir(profile, day, task_dir)
    # Returns a filled scratch copy; the master world is never mutated.
    return anchors.inspection_world(profile, day)


def clean_copy(profile: str, day: int, dest: Path,
               task_dir: Path | None = None) -> dict:
    src = snapshot_dir(profile, day, task_dir)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    out = {}
    for d in ("workspace", "data", "webharbor", "overlay"):
        s = src / d
        if s.exists():
            shutil.copytree(s, dest / d, symlinks=True)
            out[d] = dest / d
    return out


def _read(p: Path) -> str:
    try:
        return Path(p).read_text()
    except OSError:
        return ""


def _lw_judge(task_md, rubric, rule_result):
    ev = vf.build_lightweight_evidence(rule_result)
    return lj.judge(task_md, rubric, ev,
                    spec=cfg.model_spec(cfg.LIGHTWEIGHT_JUDGE_MODEL))


def _container_workspace_file_index(container: str | None) -> set[str] | None:
    if not container:
        return None
    try:
        r = subprocess.run(
            [
                "docker", "exec", container, "sh", "-lc",
                "find /workspace -type f -printf '%P\\n'",
            ],
            capture_output=True,
            text=True,
            timeout=cfg.SUBPROCESS_FAST_TIMEOUT_S,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def _webharbor_site_contract_problems(
    record: dict, *, snapshot: Path | None, where: str,
) -> list[str]:
    registry = wh_lib.site_registry()
    webharbor_dir: Path | None = None
    if snapshot is not None:
        candidate_dir = Path(snapshot) / "webharbor"
        if candidate_dir.is_dir():
            webharbor_dir = candidate_dir
            registry = [
                row for row in registry
                if (webharbor_dir / row["dbfile"]).is_file()
            ]
    valid = {str(row["id"]) for row in registry}
    registry_by_id = {str(row["id"]): row for row in registry}
    problems: list[str] = []
    sites = record.get("sites") or []
    if not isinstance(sites, list):
        sites = [sites]
    for site in sites:
        value = str(site or "").strip()
        if value and value not in valid:
            problems.append(
                f"{where}: site `{value}` is not a canonical active WebHarbor "
                f"site id; use one of {sorted(valid)}, not a dbfile or domain")
    grounding = record.get("image_grounding")
    if isinstance(grounding, dict):
        value = str(grounding.get("site") or "").strip()
        if value and value not in valid:
            problems.append(
                f"{where}: image_grounding.site `{value}` is not a canonical "
                f"active WebHarbor site id; use one of {sorted(valid)}")
    surface = record.get("web_write_surface")
    if isinstance(surface, dict):
        value = str(surface.get("site") or "").strip()
        if value and value not in valid:
            problems.append(
                f"{where}: web_write_surface.site `{value}` is not a canonical "
                f"active WebHarbor site id; use one of {sorted(valid)}")
        table = str(surface.get("state_table") or "").strip()
        if value in registry_by_id and table and webharbor_dir is not None:
            db_path = webharbor_dir / registry_by_id[value]["dbfile"]
            try:
                connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    tables = {
                        str(row[0]) for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%'")
                    }
                finally:
                    connection.close()
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(
                    f"cannot inspect live WebHarbor schema {db_path}: {exc}") from exc
            if table not in tables:
                problems.append(
                    f"{where}: web_write_surface.state_table `{table}` does not "
                    f"exist in live site `{value}` database `{db_path.name}`; "
                    f"available tables are {sorted(tables)}")
            if (
                table in tables
                and str(record.get("web_action_type") or "").strip()
                == "browser_write_flow"
            ):
                query = str(surface.get("baseline_absence_query") or "").strip()
                target = str(surface.get("target_natural_key") or "").strip()
                # Allow one trailing `-- ...` note and one `;`; anything else fails closed.
                query_body = re.sub(r"\s*--[^\r\n]*\s*$", "", query).rstrip()
                query_body = query_body[:-1].rstrip() if query_body.endswith(";") else query_body
                if not query:
                    problems.append(
                        f"{where}: browser_write_flow is missing "
                        "web_write_surface.baseline_absence_query; query the "
                        "exact account-target state and reserve only an absent target")
                elif (
                    len(query) > 4000
                    or not re.match(r"(?is)^select\b", query_body)
                    or ";" in query_body
                    or "--" in query_body
                    or "/*" in query_body
                    or re.search(
                        r"(?i)\b(?:attach|detach|insert|update|delete|replace|"
                        r"create|drop|alter|vacuum|reindex|pragma)\b",
                        query_body,
                    )
                ):
                    problems.append(
                        f"{where}: baseline_absence_query must be one bounded "
                        "read-only SELECT statement")
                elif table and not re.search(
                    rf"(?i)(?<![A-Za-z0-9_]){re.escape(table)}(?![A-Za-z0-9_])",
                    query_body,
                ):
                    problems.append(
                        f"{where}: baseline_absence_query does not reference "
                        f"reserved state_table `{table}`")
                elif target and target.lower() not in query_body.lower():
                    problems.append(
                        f"{where}: baseline_absence_query does not contain the "
                        f"exact target_natural_key `{target}`")
                else:
                    try:
                        connection = sqlite3.connect(
                            f"file:{db_path}?mode=ro", uri=True)
                        try:
                            connection.execute("PRAGMA query_only = ON")
                            connection.set_progress_handler(lambda: 1, 100_000)
                            existing = connection.execute(query_body).fetchmany(2)
                        finally:
                            connection.close()
                    except sqlite3.DatabaseError as exc:
                        problems.append(
                            f"{where}: baseline_absence_query failed against "
                            f"live site `{value}`: {exc}")
                    else:
                        if existing:
                            problems.append(
                                f"{where}: reserved browser-write target already "
                                "exists in the clean baseline according to "
                                "baseline_absence_query; choose a different target "
                                "or write action")
    return problems


def _structural_sketch_precheck(
    sketches: list[dict],
    *,
    workspace_files: set[str] | None = None,
    snapshot: Path | None = None,
) -> tuple[list[dict], list[dict]]:
    ok, dropped = [], []
    for sketch in sketches:
        problems = alint.sketch_xai_contract_problems(sketch)
        problems.extend(_webharbor_site_contract_problems(
            sketch, snapshot=snapshot, where="sketch"))
        problems.extend(alint.sketch_workspace_input_problems(
            sketch, workspace_files=workspace_files, where="sketch"))
        problems.extend(alint.sketch_visual_evidence_problems(
            sketch, where="sketch"))
        if problems:
            dropped.append({**sketch, "_drop_reason": "; ".join(problems)})
        else:
            ok.append(sketch)
    return ok, dropped


def _proposal_vision_calls(call: dict | None) -> int:
    if not isinstance(call, dict):
        return 0
    extra0 = call.get("extra")
    if isinstance(extra0, dict):
        audit0 = extra0.get("xai_day_proposal")
        if isinstance(audit0, dict) and isinstance(audit0.get("vision_call_count"), int):
            return int(audit0["vision_call_count"])
    total = 0

    def walk(obj):
        nonlocal total
        if isinstance(obj, dict):
            if "xai_day_proposal" in obj:
                obj = {k: v for k, v in obj.items() if k != "xai_day_proposal"}
            value = obj.get("n_vision_calls")
            if isinstance(value, int):
                total += value
            extra = obj.get("extra")
            if isinstance(extra, dict):
                walk(extra)
            for key, child in obj.items():
                if key in {"parse_miss_phase", "explore_stop_phase",
                           "continuation_phase"}:
                    walk(child)
        elif isinstance(obj, list):
            for child in obj:
                walk(child)

    walk(call)
    return total


def _proposal_needs_vision_evidence(call: dict | None) -> bool:
    return bool(
        isinstance(call, dict)
        and cfg.XAI_REQUIRE_IMAGE_DECISIVE
    )


def _slot_mix_label(slots: list[dict]) -> str:
    counts: dict[str, int] = {}
    for slot in slots:
        diff = str(slot.get("difficulty") or "").strip().lower()
        counts[diff] = counts.get(diff, 0) + 1
    return ", ".join(f"{count} {diff}" for diff, count in sorted(counts.items())) or "mixed"


def _reservation_candidates(sketches: list[dict], slots: list[dict],
                            profile: str, day: int) -> list[dict]:
    """Map hard-contract sketches onto slots; difficulty is classified later from bare rollouts."""
    out: list[dict] = []
    for sketch_index, sketch in enumerate(sketches):
        proposed_diff = str(sketch.get("difficulty") or "").strip().lower()
        for slot_index, slot in enumerate(slots):
            candidate = dict(sketch)
            candidate["difficulty"] = "hard"
            candidate["difficulty_provisional"] = True
            candidate["proposed_difficulty"] = proposed_diff
            candidate["quota_slot_difficulty"] = str(
                slot.get("difficulty") or "").strip().lower()
            candidate["slug"] = slot["slug"]
            candidate["profile"] = profile
            candidate["day"] = day
            candidate["_sketch_index"] = sketch_index
            candidate["_slot_index"] = slot_index
            out.append(candidate)
    return out


def _save_day_proposal_call(profile: str, day: int, attempt: int,
                            call: dict | None) -> str | None:
    if not call:
        return None
    try:
        root = cfg.LOG_DIR / "xai-day-proposals" / profile / f"day{day}"
        root.mkdir(parents=True, exist_ok=True)
        audit = ((call.get("extra") or {}).get("xai_day_proposal") or {})
        prior_path = str(audit.get("log_path") or "").strip() if isinstance(audit, dict) else ""
        if prior_path:
            path = root / Path(prior_path).name
        else:
            sid = str(call.get("session_id") or "").strip()
            suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", sid) if sid else uuid.uuid4().hex[:8]
            path = root / f"attempt{attempt}-{suffix}.json"
        _annotate_day_proposal_call(call, log_path=str(path))
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(call, indent=2, ensure_ascii=False))
        tmp.replace(path)
        return str(path)
    except Exception:
        return None


def _annotate_day_proposal_call(call: dict | None, **metrics) -> None:
    if not isinstance(call, dict):
        return
    try:
        extra = call.setdefault("extra", {})
        if not isinstance(extra, dict):
            extra = {}
            call["extra"] = extra
        audit = extra.setdefault("xai_day_proposal", {})
        if not isinstance(audit, dict):
            audit = {}
            extra["xai_day_proposal"] = audit
        vision_total = _proposal_vision_calls(call)
        if vision_total:
            extra["n_vision_calls"] = vision_total
            extra["n_vision_calls_total"] = vision_total
            audit.setdefault("vision_call_count", vision_total)
        for key, value in metrics.items():
            audit[key] = value
        if call.get("status") == "ok" and audit.get("parsed_count") == 0:
            audit.setdefault("effective_status", "empty")
            audit.setdefault("empty_reason", "ok response contained no parseable sketches")
        elif audit.get("parsed_count", 0) > 0 and audit.get("effective_status") == "empty":
            audit.pop("effective_status", None)
            audit.pop("empty_reason", None)
    except Exception:  # noqa: BLE001 - proposal audit must never affect synthesis
        return


def _current_prior_tasks(base_priors: list[dict] | None) -> list[dict]:
    """Static priors plus current workspace pool records, to steer sketches off crowded areas."""
    merged: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add_many(records):
        for rec in records or []:
            if not isinstance(rec, dict):
                continue
            key = (
                str(rec.get("status") or ""),
                str(rec.get("title") or "").strip().lower(),
                str(rec.get("summary") or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(rec)

    add_many(base_priors)
    try:
        add_many((xai_pool.list_pool(purge_expired=False) or {}).get("records") or [])
    except Exception:  # noqa: BLE001 - pool hints are advisory only
        pass
    return merged


def reserve_day_candidates(profile: str, day: int, slots: list[dict], *,
                           prior_tasks: list | None = None,
                           owner: str | None = None,
                           log=_log) -> dict:
    """Day-level sketch calls followed by server-side reserve-many; re-explore on zero locks."""
    priors = prior_tasks or []
    owner = owner or f"{cfg.INSTANCE_PREFIX}-xai-day{day}-{uuid.uuid4().hex[:8]}"
    attempts = max(1, int(getattr(cfg, "XAI_DAY_PROPOSAL_ATTEMPTS", 1)))
    dropped_all: list[dict] = []
    rejected_all: list[dict] = []
    locked_all: list[dict] = []
    locked_slugs: set[str] = set()
    last: dict = {"ok": False, "locked": [], "rejected": [],
                  "dropped": [], "reason": "no proposal attempts ran"}

    for attempt in range(1, attempts + 1):
        remaining_slots = [s for s in slots if s.get("slug") not in locked_slugs]
        if not remaining_slots:
            break
        # Small surplus: enough candidates to backfill each slot without exhausting the window.
        n = max(3, len(remaining_slots) * 2)
        gen_inst = (
            f"{cfg.INSTANCE_PREFIX}-sketch-{profile}-day{day}-"
            f"a{attempt}-{uuid.uuid4().hex[:4]}"
        ).lower()
        try:
            log(
                f"[day {day}] provision sketch world for {len(remaining_slots)} slot(s) "
                f"(attempt {attempt}/{attempts})"
            )
            prov.provision(profile, day, gen_inst, with_agent=True)
            gen_c = prov.agent_container(gen_inst)
            log(
                f"[day {day}] sketch world ready; proposing {n} candidate(s) "
                f"(attempt {attempt}/{attempts})"
            )
            dynamic_priors = _current_prior_tasks(priors)
            sres = sk.propose(
                gen_c,
                profile,
                day,
                # Always author against the hard contract; bare rollouts classify difficulty later.
                difficulty="hard",
                priors=dynamic_priors,
                require_web=True,
                n=n,
                timeout=cfg.XAI_DAY_PROPOSAL_TIMEOUT_S,
                session_id=str(uuid.uuid4()),
            )
            call = sres.get("call") or {}
            parsed_count = len(sres.get("sketches") or [])
            vision_calls = _proposal_vision_calls(call)
            vision_required = _proposal_needs_vision_evidence(call)
            _annotate_day_proposal_call(
                call,
                profile=profile,
                day=day,
                attempt=attempt,
                attempts=attempts,
                requested_count=n,
                parsed_count=parsed_count,
                remaining_slot_count=len(remaining_slots),
                target_slot_count=len(slots),
                target_difficulty_mix=_slot_mix_label(slots),
                remaining_difficulty_mix=_slot_mix_label(remaining_slots),
                adaptive_difficulty=True,
                locked_before_count=len(locked_all),
                prior_task_count=len(dynamic_priors),
                timeout_s=cfg.XAI_DAY_PROPOSAL_TIMEOUT_S,
                vision_call_count=vision_calls,
                vision_evidence_required=vision_required,
                vision_evidence_ok=(not vision_required or vision_calls > 0),
            )
            call_log = _save_day_proposal_call(profile, day, attempt, call)
            log(
                f"[day {day}] sketch proposal attempt {attempt}/{attempts} "
                f"status={call.get('status') or 'n/a'} "
                f"parsed={parsed_count}/{n} "
                f"chars={len(str(call.get('result_text') or ''))} "
                f"raw={call_log or 'n/a'}"
            )
            if not sres.get("ok"):
                status = (sres.get("call") or {}).get("status")
                reason = (
                    "sketch proposal returned no parseable sketches"
                    if status == "ok" and parsed_count == 0
                    else f"sketch proposal failed: {status}"
                )
                last = {
                    "ok": False,
                    "locked": locked_all,
                    "rejected": rejected_all,
                    "dropped": dropped_all,
                    "reason": reason,
                    "call": sres.get("call"),
                    "call_log": call_log,
                    "attempts": attempt,
                }
                continue
            if vision_required and vision_calls <= 0:
                dropped = [
                    {**sketch, "_drop_reason": (
                        "sketch proposal produced image-grounded candidates "
                        "without any vision_describe_image evidence")}
                    for sketch in (sres.get("sketches") or [])
                ]
                dropped_all.extend(dropped)
                _annotate_day_proposal_call(
                    call,
                    contract_eligible_count=0,
                    dropped_count=len(dropped),
                    cumulative_dropped_count=len(dropped_all),
                    candidate_count=0,
                    vision_evidence_ok=False,
                )
                _save_day_proposal_call(profile, day, attempt, call)
                log(
                    f"[day {day}] sketch proposal attempt {attempt}/{attempts} "
                    "dropped all candidates: no vision_describe_image evidence"
                )
                last = {
                    "ok": False,
                    "locked": locked_all,
                    "rejected": rejected_all,
                    "dropped": dropped_all,
                    "reason": "sketch proposal lacked image-read evidence",
                    "call": sres.get("call"),
                    "call_log": call_log,
                    "attempts": attempt,
                }
                continue
            sketches = sres.get("sketches") or []
            workspace_files = _container_workspace_file_index(gen_c)
            elig, dropped = _structural_sketch_precheck(
                sketches,
                workspace_files=workspace_files,
                snapshot=anchors.master_snapshot_dir(profile, day),
            )
            dropped_all.extend(dropped)
            _annotate_day_proposal_call(
                call,
                contract_eligible_count=len(elig),
                dropped_count=len(dropped),
                cumulative_dropped_count=len(dropped_all),
                vision_call_count=_proposal_vision_calls(call),
                vision_evidence_required=_proposal_needs_vision_evidence(call),
                vision_evidence_ok=(
                    not _proposal_needs_vision_evidence(call)
                    or _proposal_vision_calls(call) > 0),
            )
            if dropped:
                for drop in dropped[:5]:
                    log(
                        f"[day {day}] sketch dropped {drop.get('title')!r}: "
                        f"{drop.get('_drop_reason')}"
                    )
            candidates = _reservation_candidates(elig, remaining_slots, profile, day)
            _annotate_day_proposal_call(call, candidate_count=len(candidates))
            if not candidates:
                _save_day_proposal_call(profile, day, attempt, call)
                last = {
                    "ok": False,
                    "locked": locked_all,
                    "rejected": rejected_all,
                    "dropped": dropped_all,
                    "reason": "no proposal sketches satisfied the hard XAI contract",
                    "call": sres.get("call"),
                    "call_log": call_log,
                    "attempts": attempt,
                }
                continue
            diversity_result = task_diversity.pick_many(
                candidates, dynamic_priors, len(remaining_slots))
            call["diversity_judge"] = diversity_result.get("call")
            call["diversity_selection"] = {
                "choices": diversity_result.get("choices") or [],
                "why": diversity_result.get("why") or "",
                "rejected": diversity_result.get("rejected") or [],
            }
            candidates = diversity_result.get("selected") or []
            if not candidates:
                _save_day_proposal_call(profile, day, attempt, call)
                last = {
                    "ok": False,
                    "locked": locked_all,
                    "rejected": rejected_all + (diversity_result.get("rejected") or []),
                    "dropped": dropped_all,
                    "reason": "diversity judge rejected every candidate as a near-duplicate",
                    "call": sres.get("call"),
                    "call_log": call_log,
                    "attempts": attempt,
                }
                continue
            reserve = xai_pool.reserve_many_remote(
                candidates,
                len(remaining_slots),
                owner=owner,
                profile=profile,
                day=day,
            )
            new_locked = reserve.get("locked") or []
            for lock in new_locked:
                slug = lock.get("slug")
                if slug and slug not in locked_slugs:
                    locked_slugs.add(slug)
                    locked_all.append(lock)
            rejected_all.extend(reserve.get("rejected") or [])
            _annotate_day_proposal_call(
                call,
                locked_count=len(new_locked),
                cumulative_locked_count=len(locked_all),
                rejected_count=len(reserve.get("rejected") or []),
                cumulative_rejected_count=len(rejected_all),
                reserve_counts=reserve.get("counts") or {},
                reserve_any_ok=bool(locked_all),
                reserve_complete=len(locked_all) >= len(slots),
                reserve_ok=len(locked_all) >= len(slots),
            )
            _save_day_proposal_call(profile, day, attempt, call)
            log(
                f"[day {day}] reserve-many attempt {attempt}/{attempts} "
                f"locked={len(new_locked)}/{len(remaining_slots)} "
                f"cumulative={len(locked_all)}/{len(slots)} "
                f"rejected={len(reserve.get('rejected') or [])} "
                f"eligible={len(elig)} candidates={len(candidates)}"
            )
            for rej in (reserve.get("rejected") or [])[:5]:
                log(
                    f"[day {day}] reserve rejected {rej.get('title')!r}: "
                    f"{'; '.join(str(r) for r in (rej.get('reasons') or []))}"
                )
            reserve["ok"] = bool(locked_all)
            reserve["owner"] = owner
            reserve["locked"] = locked_all
            reserve["call"] = sres.get("call")
            reserve["call_log"] = call_log
            reserve["dropped"] = dropped_all
            reserve["rejected"] = rejected_all
            reserve["attempts"] = attempt
            if len(locked_all) >= len(slots):
                return reserve
            if new_locked:
                missing = [s.get("slug") for s in slots if s.get("slug") not in locked_slugs]
                log(
                    f"[day {day}] partial reserve; retrying remaining slot(s): "
                    f"{', '.join(str(s) for s in missing)}"
                )
            last = reserve
        except (cr.BatchAbortError, task_diversity.DiversityInfrastructureError):
            raise
        except Exception as e:  # noqa: BLE001 - isolate one bad sketch world
            call = {
                "role": "sketch_provision",
                "status": "infra",
                "rc": None,
                "result_text": "",
                "stdout_tail": "",
                "stderr_tail": traceback.format_exc(),
                "error_type": type(e).__name__,
                "error": str(e),
                "instance": gen_inst,
            }
            _annotate_day_proposal_call(
                call,
                profile=profile,
                day=day,
                attempt=attempt,
                attempts=attempts,
                requested_count=n,
                parsed_count=0,
                remaining_slot_count=len(remaining_slots),
                target_slot_count=len(slots),
                target_difficulty_mix=_slot_mix_label(slots),
                remaining_difficulty_mix=_slot_mix_label(remaining_slots),
                adaptive_difficulty=True,
                locked_before_count=len(locked_all),
                prior_task_count=len(_current_prior_tasks(priors)),
                timeout_s=cfg.XAI_DAY_PROPOSAL_TIMEOUT_S,
            )
            call_log = _save_day_proposal_call(profile, day, attempt, call)
            reason = f"sketch provision/proposal infra failed: {type(e).__name__}: {e}"
            log(
                f"[day {day}] sketch attempt {attempt}/{attempts} infra failure: "
                f"{type(e).__name__}: {e} raw={call_log or 'n/a'}"
            )
            last = {
                "ok": False,
                "locked": locked_all,
                "rejected": rejected_all,
                "dropped": dropped_all,
                "reason": reason,
                "call": call,
                "call_log": call_log,
                "attempts": attempt,
            }
            continue
        finally:
            prov.teardown(gen_inst)

    if locked_all:
        last["ok"] = True
        last["locked"] = locked_all
        last.setdefault("reason", "partial reserve after proposal attempts exhausted")
    return last


def validate_matrix(task_dir: Path, profile: str, day: int, scratch: Path) -> dict:
    """Run the clean→failed / golden→completed matrix on scratch copies."""
    vdir, golden = task_dir / "verifier", task_dir / "golden.py"
    snap = snapshot_dir(profile, day, task_dir)
    task_md = (task_dir / "task.md").read_text()
    rubric = _read(vdir / "rubrics.md")
    defects: list[str] = []
    defects.extend(alint.xai_meta_contract_problems(task_dir))
    try:
        meta = load_jsonc(task_dir / "meta.json")
    except (OSError, ValueError):
        meta = {}
    defects.extend(_webharbor_site_contract_problems(
        meta, snapshot=snap, where="meta.json"))
    defects.extend(alint.active_mcp_domain_problems(task_dir, snap))
    defects.extend(alint.public_task_length_problems(task_dir))
    defects.extend(alint.public_task_leakage_problems(task_dir))
    defects.extend(alint.public_answer_leak_problems(task_dir))
    defects.extend(alint.public_visual_decision_problems(task_dir))
    defects.extend(alint.public_cli_code_requirement_problems(task_dir))
    defects.extend(alint.verifier_sdk_api_problems(task_dir))
    defects.extend(alint.workspace_input_contract_problems(task_dir))
    defects.extend(alint.workspace_existing_claim_problems(task_dir, snap / "workspace"))
    defects.extend(alint.verifier_state_surface_problems(task_dir))

    clean = clean_copy(profile, day, scratch / "clean", task_dir)
    rc = vf.run_verifier(vdir, clean["workspace"], clean["data"], snap,
                         clean.get("webharbor"))
    l2c = _lw_judge(task_md, rubric, rc)
    if rc["script_errors"]:
        defects.append(f"scripts crashed on clean: {rc['script_errors']}")
    if rc["verdict"] != "failed":
        passing = [s["name"] for s in rc["scripts"] if s["verdict"] != "failed"]
        defects.append(f"clean world did NOT fail (got {rc['verdict']}; "
                       f"non-failing: {passing}) — checks must require NEW "
                       f"artifacts vs baseline")
    if l2c["verdict"] not in ("failed", "world_imperfect"):
        defects.append(f"lightweight judge saw clean as {l2c['verdict']} — "
                       f"evidence reads as already-done on an untouched world")

    gold = clean_copy(profile, day, scratch / "golden", task_dir)
    ag = vf.apply_golden(golden, gold["workspace"], gold["data"],
                         gold.get("webharbor"), snap)
    rg = vf.run_verifier(vdir, gold["workspace"], gold["data"], snap,
                         gold.get("webharbor"))
    l2g = _lw_judge(task_md, rubric, rg)
    if not ag["ok"]:
        defects.append(f"golden.py crashed: {ag.get('stderr','')}")
    if rg["verdict"] != "completed":
        failing = [(s["name"], json.dumps(s.get("details", {})))
                   for s in rg["scripts"] if s["verdict"] != "completed"]
        defects.append("golden world did NOT pass (golden+verifier inconsistent): "
                       + " | ".join(f"{n}: {d}" for n, d in failing))
    if l2g["verdict"] != "completed":
        defects.append(f"lightweight judge saw golden as {l2g['verdict']} — "
                       f"scripts must extract the artifact the rubric needs")

    return {"ok": not defects, "defects": defects,
            "detail": {"clean_l1": rc["verdict"], "clean_l2": l2c["verdict"],
                       "golden_apply": ag["ok"], "golden_l1": rg["verdict"],
                       "golden_l2": l2g["verdict"],
                       "clean_scripts": [(s["name"], s["verdict"]) for s in rc["scripts"]],
                       "golden_scripts": [(s["name"], s["verdict"]) for s in rg["scripts"]]}}


def _safe_name(s: str) -> str:
    """Return a filesystem-safe subdirectory name for a harness and model cell id."""
    out = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (s or ""))
    return out.strip("_") or "cell"


def _world_name_stem(profile: str, day: int, slug: str) -> str:
    """Unique, docker-friendly stem for synthesis instances (includes profile and day)."""
    return _safe_name(f"{profile}-d{day}-{slug}").lower()


def _save_judge_trajectories(task_dir: Path, run: "ens.CellRun",
                             lw: dict, hv: dict) -> None:
    """Best-effort archive of both judges' trajectories under `task_dir/judge_trajectories/`."""
    try:
        dest = Path(task_dir) / "judge_trajectories" / _safe_name(run.cell)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "lightweight.json").write_text(
            json.dumps(lw.get("trajectory") or [], ensure_ascii=False, indent=1))
        (dest / "heavy.json").write_text(
            json.dumps(hv.get("trajectory") or [], ensure_ascii=False, indent=1))
    except Exception:                                  # noqa: BLE001
        pass


def _world_fault_evidence(judged: list[dict]) -> dict:
    return {
        "judge_reason": "\n".join(
            f"{j['cell']}: heavy={j.get('heavy_just', '')} lightweight={j.get('lw_just', '')}"
            for j in judged),
        "failing_cells": [j["cell"] for j in judged],
        "verifier_errors": [
            {"cell": j["cell"], "script_errors": j.get("l1_script_errors", []),
             "rule_verdict": j.get("l1")}
            for j in judged
        ],
        "trajectory_refs": [
            {"cell": j["cell"], "before": j["run"].before_dir,
             "after": j["run"].after_dir,
             "trajectory": j["run"].trajectory_dir}
            for j in judged
        ],
        "service_logs": [],
    }


def _validate_patch_for_orchestrator(out_dir: Path, profile: str, day: int,
                                     scratch: Path) -> dict:
    """Validate a generated task-local patch, then re-run validate_matrix for report detail."""
    pv = wp.validate_patch(out_dir, profile, day, scratch / "world_patch_validate")
    if not pv["ok"]:
        return pv
    vm = validate_matrix(out_dir, profile, day, scratch / "world_patch_matrix")
    if not vm["ok"]:
        return {"ok": False, "defects": vm["defects"],
                "detail": {"patch_validation": pv["detail"],
                           "matrix": vm["detail"]}}
    return {"ok": True, "defects": [],
            "detail": {"patch_validation": pv["detail"],
                       "matrix": vm["detail"]}}


def _attempt_world_patch_repair(profile: str, day: int, slug: str,
                                out_dir: Path, scratch: Path, evidence: dict,
                                *, rec, report: dict, reauthor: int,
                                used_iters: int) -> dict:
    remaining = cfg.MAX_WORLD_PATCH_ITERS - used_iters
    if remaining <= 0:
        return {"action": "reject", "used": 0,
                "reason": f"world_imperfect unresolved after {cfg.MAX_WORLD_PATCH_ITERS} task-local repair attempt(s)"}
    base_snapshot = snapshot_dir(profile, day, out_dir)
    req = wp.write_repair_request(
        profile=profile,
        day=day,
        slug=slug,
        task_dir=out_dir,
        evidence=evidence,
        base_snapshot=base_snapshot,
        existing_patch=wp.task_patch_dir(out_dir),
    )
    used = 0
    last_defects: list[str] = []
    for local_i in range(1, remaining + 1):
        repair_iter = used_iters + local_i
        _log(f"[world_patch] repair attempt {repair_iter}/{cfg.MAX_WORLD_PATCH_ITERS}")
        rec.event("world_repair", "repair_attempt_start",
                  iteration=reauthor, repair_iter=repair_iter,
                  max_world_patch_iters=cfg.MAX_WORLD_PATCH_ITERS,
                  request=str(req))
        rr = wp.run_repair_agent(out_dir, req, base_snapshot=base_snapshot)
        used += 1
        report["calls"].append({"world_patch_generator": rr.get("call")})
        hist = rec.save_generator(f"world_patch_i{reauthor}_r{repair_iter}.json",
                                  rr.get("call"))
        report["stages"].setdefault("world_repair", []).append(
            {"repair_iter": repair_iter, "request": str(req),
             "generator_ok": rr.get("ok"), "present": rr.get("present")})
        rec.event("world_repair", "repair_agent",
                  iteration=reauthor, repair_iter=repair_iter,
                  ok=rr.get("ok"), present=rr.get("present"),
                  patch_dir=rr.get("patch_dir"), history=hist)
        if not rr.get("ok"):
            last_defects = ["world_patch_generator did not produce a complete world_patch layout"]
            continue
        pv = _validate_patch_for_orchestrator(
            out_dir, profile, day, scratch / f"world_patch_r{repair_iter}")
        report["stages"].setdefault("world_patch_validate", []).append(
            {"repair_iter": repair_iter, **pv})
        rec.event("world_repair", "patch_validation",
                  iteration=reauthor, repair_iter=repair_iter,
                  ok=pv["ok"], defects=pv["defects"], detail=pv.get("detail"))
        if pv["ok"]:
            fz.stamp(out_dir / "meta.json", snapshot_dir(profile, day, out_dir))
            return {"action": "rerun", "used": used,
                    "reason": "validated task-local world_patch; rerun judges from the patched clean baseline",
                    "validation": pv}
        last_defects = pv["defects"]
        req = wp.write_repair_request(
            profile=profile,
            day=day,
            slug=slug,
            task_dir=out_dir,
            evidence={**evidence, "previous_patch_validation_defects": last_defects},
            base_snapshot=base_snapshot,
            existing_patch=wp.task_patch_dir(out_dir),
        )
    return {"action": "reject", "used": used,
            "reason": f"world_patch validation failed after {used} repair attempt(s): {last_defects}",
            "defects": last_defects}


def _reconcile_judgments(programmatic: str, lightweight: str,
                         agent: str) -> tuple[bool | str, str]:
    """Require three-level consistency: V = V_LLM = V_agent."""
    if "infra" in (lightweight, agent):
        return "infra", "infra"
    if programmatic == lightweight == agent:
        return True, programmatic
    return False, "disagree"


def judge_run(task_dir: Path, run: "ens.CellRun", profile: str, day: int,
              rec: "slog.SynthesisLog | None" = None,
              *, judge_pass: int = 0, tier: int = 0) -> dict:
    """L1 + lightweight + heavy judgment of one ensemble run (3-class)."""
    vdir = task_dir / "verifier"
    task_md = (task_dir / "task.md").read_text()
    rubric = _read(vdir / "rubrics.md")
    skill = _read(vdir / "verify.skill.md")
    after = Path(run.after_dir)
    snap = snapshot_dir(profile, day, task_dir)
    wh = after / "web"
    bwh = snap / "webharbor"
    rd = vf.run_verifier(vdir, after / "workspace", after / "data", snap,
                         wh if wh.is_dir() else None,
                         baseline_webharbor_dir=bwh if bwh.is_dir() else None)
    lw = _lw_judge(task_md, rubric, rd)
    hv = jd.run_judge(Path(run.before_dir), after, task_md, rubric, skill, rd)
    _save_judge_trajectories(task_dir, run, lw, hv)
    l1v, lwv, hvv = rd["verdict"], lw["verdict"], hv["verdict"]
    agree, verdict = _reconcile_judgments(l1v, lwv, hvv)
    out = {"l1": l1v, "l1_script_errors": rd["script_errors"],
           "lw": lwv, "heavy": hvv, "agree": agree, "verdict": verdict,
           "lw_just": (lw.get("justification") or ""),
           "heavy_just": (hv.get("justification") or ""),
           "heavy_too_strict": hv.get("too_strict", []),
           "heavy_status": hv.get("status"), "lw_status": lw.get("status"),
           "lw_confidence": lw.get("confidence"), "heavy_confidence": hv.get("confidence"),
           "lw_tokens": {"in": lw.get("input_tokens"), "out": lw.get("output_tokens")},
           "heavy_call": hv.get("call"), "rule_result": rd}
    if rec is not None and getattr(rec, "enabled", False):
        # Archive raw judge histories to the replay bundle; keep pointers for the timeline.
        out["lw_history"] = rec.save_judge_history(
            judge_pass=judge_pass, tier=tier, cell=run.cell,
            kind="lightweight", payload=lw.get("trajectory") or [])
        out["heavy_history"] = rec.save_judge_history(
            judge_pass=judge_pass, tier=tier, cell=run.cell,
            kind="heavy", payload=hv.get("trajectory") or [])
    return out


def synthesize_one(profile: str, day: int, difficulty: str, slug: str, *,
                   prior_tasks: list | None = None, require_web: bool = True,
                   gen_inst: str | None = None,
                   reserved_sketch: dict | None = None,
                   allowed_difficulties: set[str] | None = None) -> dict:
    priors = prior_tasks or []
    out_dir = cfg.TASKS_OUT / profile / f"day{day}" / slug
    scratch = cfg.SCRATCH / f"{profile}-day{day}-{slug}"
    scratch.mkdir(parents=True, exist_ok=True)
    allowed_difficulties = _normalize_allowed_difficulties(
        allowed_difficulties, fallback=difficulty)
    report = {"profile": profile, "day": day, "difficulty": difficulty,
              "authoring_difficulty": difficulty,
              "allowed_difficulties": sorted(allowed_difficulties),
              "slug": slug, "stages": {}, "calls": []}
    gen_inst = gen_inst or f"{cfg.INSTANCE_PREFIX}-gen-{_world_name_stem(profile, day, slug)}"
    auto_lock_info: dict | None = None
    # Best-effort replayable synthesis log under out_dir/_synthesis_log/.
    rec = slog.make_recorder(out_dir, profile=profile, day=day,
                             difficulty=difficulty, slug=slug)
    rec.event("init", "synthesis_start", priors=len(priors),
              require_web=require_web,
              authoring_difficulty=difficulty,
              allowed_difficulties=sorted(allowed_difficulties),
              budgets={
                  "sketch_rounds": cfg.MAX_SKETCH_ROUNDS,
                  "gen_iters": cfg.MAX_GEN_ITERS,
                  "deepen": cfg.MAX_DEEPEN,
                  "ease": cfg.MAX_EASE,
                  "agree_fix": cfg.MAX_AGREE_FIX,
                  "interface_reauthor": cfg.MAX_INTERFACE_REAUTHOR,
                  "cave_fix": cfg.MAX_CAVE_FIX,
                  "post_fix_validate_fix": cfg.MAX_POST_FIX_VALIDATE_FIXES,
                  "ensemble_n": cfg.ENSEMBLE_N,
                  "live_test_n": cfg.LIVE_TEST_N,
                  "judge_infra_resample": cfg.MAX_JUDGE_INFRA_RESAMPLE,
                  "step_gate": cfg.STEP_GATE,
                  "world_patch": cfg.MAX_WORLD_PATCH_ITERS,
              })

    try:
        _log(f"[1/9] provision generator world ({profile} day{day})")
        rec.event("provision", "generator_world_start", instance=gen_inst)
        prov.provision(profile, day, gen_inst, with_agent=True)
        gen_c = prov.agent_container(gen_inst)
        rec.event("provision", "generator_world_ready", instance=gen_inst,
                  container=gen_c)

        # 2. Sketch + diversity gate.
        chosen, feedback, last_val = None, "", None
        if reserved_sketch:
            chosen = dict(reserved_sketch)
            report["stages"]["reservation"] = {
                "lock_id": chosen.get("lock_id"),
                "owner": chosen.get("owner"),
                "title": chosen.get("title"),
            }
            rec.event("sketch", "reserved", title=chosen.get("title"),
                      area=chosen.get("area"), summary=chosen.get("summary"),
                      interfaces=chosen.get("interfaces"),
                      flow=chosen.get("interface_flow") or chosen.get("flow"),
                      lock_id=chosen.get("lock_id"), owner=chosen.get("owner"))
        else:
            sketch_session_id = str(uuid.uuid4())
            for sround in range(1, cfg.MAX_SKETCH_ROUNDS + 1):
                _log(f"[2/9] sketch round {sround}: propose + diversity")
                rec.bump("sketch_rounds")
                dynamic_priors = _current_prior_tasks(priors)
                sres = sk.propose(gen_c, profile, day, difficulty=difficulty,
                                  priors=dynamic_priors, require_web=require_web, n=4,
                                  session_id=sketch_session_id if sround == 1 else None,
                                  resume_session_id=sketch_session_id if sround > 1 else None)
                # Thread the harness's actual session id: opencode mints its own, ignoring ours.
                _sid = (sres.get("call") or {}).get("session_id")
                if _sid:
                    sketch_session_id = _sid
                report["calls"].append({"sketch": sres.get("call")})
                sk_hist = rec.save_generator(f"sketch_r{sround}.json", sres.get("call"))
                if not sres["ok"]:
                    rec.event("sketch", "propose_failed", round=sround,
                              status=(sres.get("call") or {}).get("status"),
                              history=sk_hist)
                    continue
                sketches = sres["sketches"]
                elig, dropped = _structural_sketch_precheck(
                    sketches,
                    workspace_files=_container_workspace_file_index(gen_c),
                    snapshot=anchors.master_snapshot_dir(profile, day),
                )
                candidates = []
                for cand in elig:
                    c = dict(cand)
                    c["profile"] = profile
                    c["day"] = day
                    c["slug"] = slug
                    c["difficulty"] = difficulty
                    candidates.append(c)
                diversity_result = task_diversity.pick_many(
                    candidates, dynamic_priors, 1)
                report["calls"].append(
                    {"diversity": diversity_result.get("call")})
                candidates = diversity_result.get("selected") or []
                reserve_owner = (
                    f"{cfg.INSTANCE_PREFIX}-xai-"
                    f"{_world_name_stem(profile, day, slug)}-{uuid.uuid4().hex[:8]}"
                )
                reserve = xai_pool.reserve_many_remote(
                    candidates, 1,
                    owner=reserve_owner,
                    profile=profile, day=day)
                locked = reserve.get("locked") or []
                chosen_lock = locked[0] if locked else None
                if chosen_lock:
                    auto_lock_info = {"lock_id": chosen_lock.get("lock_id"),
                                      "owner": reserve_owner}
                report["stages"].setdefault("diversity", []).append(
                    {"round": sround, "n_sketches": len(sres["sketches"]),
                     "area_dropped": [d.get("_drop_reason") for d in dropped],
                     "chosen": (chosen_lock or {}).get("title"),
                     "why": diversity_result.get("why") or "",
                     "rejected": ((diversity_result.get("rejected") or [])
                                  + (reserve.get("rejected") or []))})
                rec.event("sketch", "propose_and_diversity", round=sround,
                          n_sketches=len(sres["sketches"]),
                          titles=[s.get("title") for s in sres["sketches"]],
                          area_dropped=[d.get("_drop_reason") for d in dropped],
                          n_eligible=len(elig), chosen_index=None,
                          chosen=(chosen_lock or {}).get("title"),
                          chosen_area=(chosen_lock or {}).get("area"),
                          why=diversity_result.get("why") or "",
                          rejected=((diversity_result.get("rejected") or [])
                                    + (reserve.get("rejected") or [])),
                          history=sk_hist)
                if chosen_lock:
                    chosen = chosen_lock
                    break
        if not chosen:
            _offer_skill_write(
                rec=rec, report=report, stage="sketch_rejected",
                profile=profile, day=day, difficulty=difficulty, slug=slug,
                out_dir=out_dir,
                result={"verdict": "skipped_capacity",
                        "reason": "no diverse sketch survived the diversity gate"})
            rec.event("decision", "reject", verdict="skipped_capacity",
                      reason="no diverse sketch survived the diversity gate")
            rec.finalize("skipped_capacity",
                         "no diverse sketch survived the diversity gate")
            return _finish(report, out_dir, scratch, [gen_inst],
                           "skipped_capacity",
                           "no diverse sketch survived the diversity gate")
        rec.event("sketch", "chosen", title=chosen.get("title"),
                  area=chosen.get("area"), summary=chosen.get("summary"),
                  interfaces=chosen.get("interfaces"),
                  flow=chosen.get("interface_flow") or chosen.get("flow"),
                  why_hard=chosen.get("why_hard"))
        _offer_skill_write(
            rec=rec, report=report, stage="sketch_accepted",
            profile=profile, day=day, difficulty=difficulty, slug=slug,
            out_dir=out_dir,
            result={**chosen, "verdict": "accepted",
                    "reason": "diversity gate selected sketch"},
            feedback=json.dumps(chosen, ensure_ascii=False)[:2000])

        # Outer re-author loop: deepen, ease, or restructure; each trigger has its own budget.
        deepen_feedback, result = "", None
        deepen_used, ease_used, interface_reauthor_used = 0, 0, 0
        reauthor_dir = None
        difficulty_reference_task_md: str | None = None
        max_reauthors = cfg.MAX_DEEPEN + cfg.MAX_EASE + cfg.MAX_INTERFACE_REAUTHOR
        for reauthor in range(max_reauthors + 1):
            reauthored = reauthor > 0
            rec.event("reauthor", "iteration_start", iteration=reauthor,
                      reauthored=reauthored, direction=reauthor_dir,
                      deepen_used=deepen_used, ease_used=ease_used,
                      interface_reauthor_used=interface_reauthor_used,
                      title=chosen.get("title"))
            # 3-4. Author + validate + fix.
            validated, feedback = False, deepen_feedback
            author_failure_statuses: list[str] = []
            for attempt in range(1, cfg.MAX_GEN_ITERS + 1):
                _log(f"[3/9] author attempt {attempt}"
                     f"{f' ({reauthor_dir} {reauthor})' if reauthored else ''}: "
                     f"'{chosen.get('title')}'")
                rec.bump("author_attempts")
                rec.event("author", "start", iteration=reauthor,
                          attempt=attempt, reauthored=reauthored,
                          direction=reauthor_dir,
                          title=chosen.get("title"), feedback=feedback)
                g = gen.generate(gen_c, profile, day, chosen, difficulty=difficulty,
                                 out_host_dir=out_dir, feedback=feedback)
                report["calls"].append({"generate": g.get("call")})
                au_hist = rec.save_generator(
                    f"author_i{reauthor}_a{attempt}.json", g.get("call"))
                if not g["ok"]:
                    author_failure_statuses.append(
                        str((g.get("call") or {}).get("status") or ""))
                    present = g.get("present") or {}
                    # Log watchdog cuts; otherwise the attempt reappears with no recorded reason.
                    _log(f"[3/9] author attempt {attempt} did not deliver "
                         f"(status={(g.get('call') or {}).get('status') or 'n/a'}, "
                         f"present={sum(1 for v in present.values() if v)}/{len(present)})"
                         + (f" -> {g['stall_reason']}" if g.get("stall_reason") else ""))
                    if not any(present.values()):
                        feedback = (
                            "The previous author attempt timed out before writing any "
                            "deliverable. Do not browse again unless a named anchor is "
                            "contradictory. Reuse any anchors/image facts already found "
                            "in this running container, and make your next action a "
                            "single write batch for task.md, reason.md, meta.json, "
                            "golden.py, and verifier files. Then run self-check until "
                            "ALL GREEN."
                        )
                    else:
                        feedback = (f"Deliverables incomplete or run failed "
                                    f"({g['call'].get('status')}). present={present}. "
                                    f"Write ALL files and finish on ALL GREEN.")
                    report["stages"].setdefault("author", []).append(
                        {"attempt": attempt, "reauthor": reauthor,
                         "direction": reauthor_dir, "ok": False,
                         "stall_reason": g.get("stall_reason") or "",
                         "status": g["call"].get("status")})
                    rec.event("author", "authoring_failed", iteration=reauthor,
                              attempt=attempt, reauthored=reauthored,
                              direction=reauthor_dir,
                              status=(g.get("call") or {}).get("status"),
                              stall_reason=g.get("stall_reason") or "",
                              present=g.get("present"), history=au_hist)
                    _offer_skill_write(
                        rec=rec, report=report, stage="author_failed",
                        profile=profile, day=day, difficulty=difficulty,
                        slug=slug, out_dir=out_dir,
                        result={"verdict": "author_failed",
                                "reason": feedback},
                        feedback=feedback)
                    continue
                rec.event("author", "authoring_ok", iteration=reauthor,
                          attempt=attempt, reauthored=reauthored,
                          direction=reauthor_dir,
                          title=(g.get("meta") or {}).get("title", chosen.get("title")),
                          present=g.get("present"), history=au_hist)
                drift_defects = alint.sketch_author_drift_problems(out_dir, chosen)
                if drift_defects:
                    last_val = {"ok": False, "defects": drift_defects,
                                "detail": {"sketch_author_drift": True}}
                    report["stages"].setdefault("validate", []).append(
                        {"attempt": attempt, "reauthor": reauthor,
                         "direction": reauthor_dir,
                         "defects": drift_defects,
                         "sketch_author_drift": True})
                    rec.event("author", "sketch_author_drift",
                              iteration=reauthor, attempt=attempt,
                              reauthored=reauthored, direction=reauthor_dir,
                              defects=drift_defects)
                    feedback = (
                        "Author output drifted away from the reserved sketch. "
                        "Re-author the task while preserving the sketch's "
                        "declared Web behavior, MCP app, image policy, and "
                        "CLI program/input contract. "
                        "If a core sketch anchor is impossible, do not invent "
                        "a different task; fail this sketch via feedback.\n"
                        "Drift defects:\n" + "\n".join(f"- {d}" for d in drift_defects)
                    )
                    _offer_skill_write(
                        rec=rec, report=report, stage="author_selfcheck_failed",
                        profile=profile, day=day, difficulty=difficulty,
                        slug=slug, out_dir=out_dir,
                        result={"verdict": "author_drift",
                                "reason": "; ".join(drift_defects)},
                        feedback=feedback)
                    continue
                if (reauthor_dir in {"deepen", "ease"}
                        and difficulty_reference_task_md is not None):
                    try:
                        public_task = (out_dir / "task.md").read_text()
                    except FileNotFoundError:
                        public_task = ""
                    difficulty_problem = _public_task_difficulty_problem(
                        difficulty_reference_task_md, public_task,
                        direction=reauthor_dir)
                    if difficulty_problem:
                        last_val = {"ok": False, "defects": [difficulty_problem],
                                    "detail": {"public_task_changed": False}}
                        report["stages"].setdefault("validate", []).append(
                            {"attempt": attempt, "reauthor": reauthor,
                             "direction": reauthor_dir,
                             "defects": [difficulty_problem],
                             "public_task_changed": False})
                        rec.event("author", "public_task_unchanged",
                                  iteration=reauthor, attempt=attempt,
                                  reauthored=reauthored, direction=reauthor_dir,
                                  defect=difficulty_problem)
                        if reauthor_dir == "deepen":
                            rewrite = (
                                "Rewrite task.md itself with denser but natural public "
                                "outcome constraints: more real sub-artifacts, more "
                                "cross-checks, more item loops, and a harder grounded "
                                "decision."
                            )
                        else:
                            rewrite = (
                                "Rewrite task.md itself to simplify or clarify the "
                                "decisive public outcome constraints and reduce "
                                "incidental brittleness, while preserving CLI, MCP, "
                                "Web, image grounding, and the reserved task identity."
                            )
                        feedback = (
                            f"{reauthor_dir.title()} re-author failed before validation "
                            "because the public solver prompt did not materially "
                            f"change.\n{difficulty_problem}\n{rewrite} Keep it <=300 "
                            "words and update reason.md, meta.json, golden.py, rubrics, "
                            "verify.skill.md, and verifier scripts consistently."
                        )
                        continue
                _log(f"[4/9] validate matrix (clean→fail, golden→pass) attempt {attempt}")
                rec.bump("validate_runs")
                val = validate_matrix(out_dir, profile, day, scratch)
                last_val = val
                report["stages"].setdefault("validate", []).append(
                    {"attempt": attempt, "reauthor": reauthor,
                     "direction": reauthor_dir, **val["detail"],
                     "defects": val["defects"]})
                rec.event("validate", "matrix", iteration=reauthor, attempt=attempt,
                          post_fix=False, ok=val["ok"], defects=val["defects"],
                          detail=val["detail"])
                if val["ok"]:
                    validated = True
                    report["stages"]["author_meta"] = g.get("meta", {})
                    break
                skill_stage = (
                    "initial_unsatisfaction_failed"
                    if any("clean world did NOT fail" in str(defect)
                           for defect in val["defects"])
                    else "author_selfcheck_failed"
                )
                _offer_skill_write(
                    rec=rec, report=report, stage=skill_stage,
                    profile=profile, day=day, difficulty=difficulty,
                    slug=slug, out_dir=out_dir,
                    result={"verdict": "validation_failed",
                            "reason": "; ".join(val["defects"])},
                    feedback="\n".join(val["defects"]))
                authoring_defects = _authoring_validation_defects(val["defects"])
                if authoring_defects:
                    _log(
                        f"    {len(val['defects'])} validation defect(s), "
                        f"{len(authoring_defects)} authoring/public → re-author"
                    )
                    rec.event("validate", "authoring_defect",
                              iteration=reauthor, attempt=attempt,
                              defects=authoring_defects,
                              all_defects=val["defects"])
                    feedback = (
                        "Validation found authoring/public task contract defects "
                        "that a verifier-only fixer cannot repair. Re-author the "
                        "full task.md/reason.md/meta.json/golden.py/rubric/"
                        "verifier package consistently.\nValidation defects:\n"
                        + "\n".join(val["defects"])
                    )
                    continue
                _log(f"    {len(val['defects'])} defect(s) → fixer")
                rec.bump("fixer_runs")
                fr = fx.fix(out_dir, defects=val["defects"],
                            baseline_dir=snapshot_dir(profile, day, out_dir),
                            completed_after_dirs=[])
                report["calls"].append({"fixer_validate": fr.get("call")})
                fx_hist = rec.save_generator(
                    f"fixer_validate_i{reauthor}_a{attempt}.json", fr.get("call"))
                rec.event("validate", "fixer", iteration=reauthor, attempt=attempt,
                          tag="validate", ok=fr["ok"], defects=val["defects"],
                          changed=fr.get("changed_files"), history=fx_hist)
                if fr["ok"]:
                    rec.bump("validate_runs")
                    val2 = validate_matrix(out_dir, profile, day, scratch)
                    report["stages"]["validate"].append(
                        {"attempt": attempt, "post_fix": True, **val2["detail"],
                         "defects": val2["defects"], "changed": fr["changed_files"]})
                    rec.event("validate", "matrix", iteration=reauthor,
                              attempt=attempt, post_fix=True, ok=val2["ok"],
                              defects=val2["defects"], detail=val2["detail"],
                              changed=fr["changed_files"])
                    if val2["ok"]:
                        validated = True
                        report["stages"]["author_meta"] = g.get("meta", {})
                        break
                    feedback = (
                        "Validation failed after verifier fixer. Re-author the full "
                        "task/verifier/golden package; do not keep the broken fixed "
                        "verifier.\nPost-fix validation defects:\n"
                        + "\n".join(val2["defects"])
                        + "\nOriginal validation defects:\n"
                        + "\n".join(val["defects"])
                    )
                else:
                    feedback = (
                        "Validation failed and verifier fixer did not complete. "
                        "Re-author the full task/verifier/golden package.\n"
                        + "\n".join(val["defects"])
                    )
            if not validated:
                if _author_failures_are_infra(author_failure_statuses, last_val):
                    rsn = (f"author infra failed after {cfg.MAX_GEN_ITERS} "
                           f"attempts: statuses={author_failure_statuses}")
                    rec.event("decision", "abort", verdict="error_infra",
                              reason=rsn, iteration=reauthor,
                              author_statuses=author_failure_statuses)
                    rec.finalize("error_infra", rsn)
                    return _finish(report, out_dir, scratch, [gen_inst],
                                   "error_infra", rsn)
                rsn = (f"validation matrix unmet after {cfg.MAX_GEN_ITERS} "
                       f"attempts: {(last_val or {}).get('defects')}")
                rec.event("decision", "reject", verdict="reject_generator",
                          reason=rsn, iteration=reauthor,
                          defects=(last_val or {}).get("defects"))
                _offer_skill_write(
                    rec=rec, report=report, stage="terminal_reject",
                    profile=profile, day=day, difficulty=difficulty,
                    slug=slug, out_dir=out_dir,
                    result={"verdict": "reject_generator", "reason": rsn},
                    feedback="\n".join((last_val or {}).get("defects") or []))
                rec.finalize("reject_generator", rsn)
                return _finish(report, out_dir, scratch, [gen_inst],
                               "reject_generator", rsn)

            # 5. Stamp world digest + derive reason_helper.
            fz.stamp(out_dir / "meta.json", snapshot_dir(profile, day, out_dir))
            meta = load_jsonc(out_dir / "meta.json")
            helper = rh.build((out_dir / "task.md").read_text(),
                              _read(out_dir / "reason.md"), meta)
            (out_dir / "reason_helper.md").write_text(helper["helper"])
            report["stages"]["reason_helper"] = {k: v for k, v in helper.items()
                                                 if k != "helper"}
            rec.event("reason_helper", "derived", iteration=reauthor,
                      source=helper.get("source"),
                      leak_lint_ok=rh.final_lint_ok(helper),
                      detail={k: v for k, v in helper.items() if k != "helper"})

            # 6-9. Ensemble + gates.
            result = run_ensemble_and_gate(
                profile, day, difficulty, slug, out_dir,
                meta, report, scratch, require_web,
                rec=rec, reauthor=reauthor,
                allowed_difficulties=allowed_difficulties)
            if result["verdict"] == "reject_too_easy" and deepen_used < cfg.MAX_DEEPEN:
                try:
                    difficulty_reference_task_md = (out_dir / "task.md").read_text()
                except FileNotFoundError:
                    difficulty_reference_task_md = None
                deepen_used += 1
                rec.bump("deepen_used")
                rec.event("deepen", "trigger", iteration=reauthor,
                          trigger_verdict="reject_too_easy",
                          max_depth_llm_calls=result.get("max_depth"),
                          step_gate=cfg.STEP_GATE, deepen_used=deepen_used,
                          budget_remaining=cfg.MAX_DEEPEN - deepen_used,
                          reason=result.get("reason"))
                _log(f"[deepen] too-easy → re-author deeper (budget {cfg.MAX_DEEPEN - deepen_used})")
                if result.get("bare_gate"):
                    bare_cells = result.get("bare_cells") or []
                    deepen_feedback = (
                        "## ⚠️ PUBLIC TASK TOO EASY — deepen intrinsic difficulty\n"
                        f"The task.md-only bare gate completed too reliably: "
                        f"{result.get('bare_n_completed')}/{result.get('bare_n_live')} "
                        f"agents completed it, but the remaining "
                        f"{result.get('target_difficulty') or difficulty} quota requires "
                        f"{result.get('bare_min_completed')}.."
                        f"{result.get('bare_max_completed')} completions.\n\n"
                        f"Bare gate evidence: {bare_cells}\n\n"
                        "Keep task.md natural, self-contained, and sufficient. Do not "
                        "hide requirements in reason_helper/meta or make the public task "
                        "ambiguous. Increase intrinsic workload or decision complexity "
                        "using capabilities and evidence from the live world. The next "
                        "task.md must be materially different from the rejected one; "
                        "hidden-only verifier/golden edits will be rejected before "
                        "validation. Re-author the whole package "
                        "consistently: task.md, reason.md, meta.json, golden.py, "
                        "verifier/rubrics.md, verify.skill.md, and verifier scripts. "
                        "Self-check to ALL GREEN.")
                else:
                    deepen_feedback = (
                        "## ⚠️ TOO SHALLOW — deepen this task\n"
                        f"An ensemble of agents all COMPLETED your task in fewer than "
                        f"{cfg.STEP_GATE} LLM calls. Keep the same grounded objective but "
                        "redesign its intrinsic workload or decision complexity from live "
                        "world evidence. Do not add arbitrary interface hops or hidden "
                        "requirements. This must be a consistent "
                        "structural rewrite of task.md, reason.md, meta.json, golden.py, "
                        "rubrics, verify.skill.md, and verifier scripts; do not merely add "
                        "or remove wording, and do not keep task.md semantically the "
                        "same. Self-check to ALL GREEN.")
                rec.event("deepen", "feedback", iteration=reauthor,
                          direction="deepen", feedback=deepen_feedback)
                _offer_skill_write(
                    rec=rec, report=report, stage="deepen", profile=profile,
                    day=day, difficulty=difficulty, slug=slug, out_dir=out_dir,
                    result=result, feedback=deepen_feedback)
                reauthor_dir = "deepen"
                continue
            # Too hard or unsolved by >=2 hinted cells: ease/clarify and rerun every gate.
            if (((result["verdict"] == "reject_unsolvable"
                  and result.get("n_failed_live", 0) >= 2)
                 or result["verdict"] == "reject_too_hard")
                    and ease_used < cfg.MAX_EASE):
                try:
                    difficulty_reference_task_md = (out_dir / "task.md").read_text()
                except FileNotFoundError:
                    difficulty_reference_task_md = None
                ease_used += 1
                rec.bump("ease_used")
                rec.event("ease", "trigger", iteration=reauthor,
                          trigger_verdict=result["verdict"],
                          n_failed_live=result.get("n_failed_live"),
                          ease_used=ease_used,
                          budget_remaining=cfg.MAX_EASE - ease_used,
                          reason=result.get("reason"))
                _log(f"[ease] too-hard ({result.get('n_failed_live')} cells failed) → "
                     f"re-author easier (budget {cfg.MAX_EASE - ease_used})")
                if result.get("bare_gate"):
                    deepen_feedback = (
                        "## ⚠️ PUBLIC TASK TOO HARD FOR ITS TARGET BAND\n"
                        f"The task.md-only bare gate completed "
                        f"{result.get('bare_n_completed')}/"
                        f"{result.get('bare_n_live')} rollouts, but the remaining "
                        f"{result.get('target_difficulty') or difficulty} quota requires "
                        f"{result.get('bare_min_completed')}.."
                        f"{result.get('bare_max_completed')} completions.\n\n"
                        f"Bare gate evidence: {result.get('bare_cells') or []}\n\n"
                        "Keep the task cross-interface and non-trivial, but make its "
                        "decisive steps more reachable and unambiguous. Reduce incidental "
                        "brittleness without leaking answers or removing required interface "
                        "dependencies. Re-author task.md, reason.md, meta.json, golden.py, "
                        "rubrics, verify.skill.md, and verifier scripts consistently; "
                        "self-check to ALL GREEN.")
                else:
                    deepen_feedback = (
                        "## ⚠️ TOO HARD — no agent could solve this; make it clearly solvable\n"
                        "Multiple capable agents attempted your task end-to-end and ALL FAILED "
                        "(judged not-completed) even with the existence hints. The task must stay "
                        "cross-interface and non-trivial, but a careful agent has to be able to "
                        "FINISH it. Keep the same grounded objective, make every decisive "
                        "criterion unambiguous, reduce incidental brittleness, and make sure "
                        "every required input and result is reachable through the tools and clearly "
                        "specified in task.md. For browser write tasks, inspect failed-run evidence "
                        "carefully: if agents reached a UI success state but the declared WebHarbor "
                        "state table had no new `web_new_rows(...)` row, this is NOT ordinary "
                        "difficulty. It means the chosen website write surface is invalid or "
                        "non-persistent in this world. Switch to a different persisted "
                        "site/action/table and update task.md, reason.md, meta.json, golden.py, "
                        "rubrics, verify.skill.md, and verifier scripts consistently; never add "
                        "a browser trace/session fallback. Re-author and self-check to ALL GREEN.")
                rec.event("ease", "feedback", iteration=reauthor,
                          direction="ease", feedback=deepen_feedback)
                _offer_skill_write(
                    rec=rec, report=report, stage="ease", profile=profile,
                    day=day, difficulty=difficulty, slug=slug, out_dir=out_dir,
                    result=result, feedback=deepen_feedback)
                reauthor_dir = "ease"
                continue
            if (result["verdict"] == "reject_interface_bypass"
                    and interface_reauthor_used < cfg.MAX_INTERFACE_REAUTHOR):
                interface_reauthor_used += 1
                rec.bump("interface_reauthor_used")
                bypassed = result.get("bypassed") or []
                rec.event("interface_reauthor", "trigger", iteration=reauthor,
                          trigger_verdict="reject_interface_bypass",
                          interface_reauthor_used=interface_reauthor_used,
                          budget_remaining=
                              cfg.MAX_INTERFACE_REAUTHOR - interface_reauthor_used,
                          bypassed=bypassed,
                          reason=result.get("reason"))
                _log("[interface] completed trajectory bypassed required interface(s) "
                     f"→ structural re-author (budget "
                     f"{cfg.MAX_INTERFACE_REAUTHOR - interface_reauthor_used})")
                deepen_feedback = (
                    "## ⚠️ REQUIRED INTERFACE BYPASS — structural re-author\n"
                    "At least one completed solver trajectory was judged completed "
                    "without actually using all required interfaces {cli, web, mcp}.\n\n"
                    f"Bypass evidence: {bypassed}\n\n"
                    "This means the task package permits a shortcut. Re-author the "
                    "whole package so each interface is indispensable in the real "
                    "workflow and the verifier rejects shortcut completions. You may "
                    "change task.md, reason.md, meta.json, golden.py, rubrics, "
                    "verify.skill.md, and verifier scripts together. Do NOT loosen "
                    "the all-three-interface requirement, do NOT hide required steps "
                    "only in reason_helper/meta, and do NOT make task.md ambiguous. "
                    "Make the task naturally require the declared Web work, writing and "
                    "running a CLI program, and real MCP work. If image-decisive mode is "
                    "enabled, keep the visual decision load-bearing. Then self-check to ALL GREEN.")
                rec.event("interface_reauthor", "feedback", iteration=reauthor,
                          direction="interface", feedback=deepen_feedback)
                _offer_skill_write(
                    rec=rec, report=report, stage="interface_reauthor",
                    profile=profile, day=day, difficulty=difficulty, slug=slug,
                    out_dir=out_dir, result=result, feedback=deepen_feedback)
                reauthor_dir = "interface"
                continue
            # Terminal verdict or exhausted re-author budget: leave the outer loop.
            rec.event("reauthor", "loop_exit", iteration=reauthor,
                      verdict=result.get("verdict"), reason=result.get("reason"),
                      deepen_used=deepen_used, ease_used=ease_used,
                      interface_reauthor_used=interface_reauthor_used)
            break

        band = _difficulty_band(result)
        if result.get("verdict") in tx.ACCEPTED and band in {"hard", "medium"}:
            _persist_classified_difficulty(
                out_dir, report, classified=band,
                authoring_difficulty=difficulty)
        prov.teardown(gen_inst)
        gate_skill_stage = _skill_stage_for_result(result)
        if gate_skill_stage:
            _offer_skill_write(
                rec=rec, report=report, stage=gate_skill_stage,
                profile=profile, day=day,
                difficulty=(band if band in {"hard", "medium"} else difficulty),
                slug=slug, out_dir=out_dir, result=result)
        _offer_skill_write(
            rec=rec, report=report,
            stage=("terminal_accept" if result.get("verdict") in tx.ACCEPTED
                   else "terminal_reject"), profile=profile, day=day,
            difficulty=(band if band in {"hard", "medium"} else difficulty),
            slug=slug, out_dir=out_dir, result=result)
        rec.event("decision", _terminal_decision_type(result["verdict"]),
                  verdict=result["verdict"], reason=result["reason"],
                  difficulty_band=band, deepen_used=deepen_used,
                  ease_used=ease_used,
                  interface_reauthor_used=interface_reauthor_used)
        rec.finalize(result["verdict"], result["reason"], difficulty_band=band,
                     extra={"deepen_used": deepen_used, "ease_used": ease_used,
                            "interface_reauthor_used": interface_reauthor_used,
                            "max_depth_llm_calls": result.get("max_depth"),
                            "n_completed": result.get("n_completed"),
                            "n_live": result.get("n_live"),
                            "tier": result.get("tier")})
        return _finish(report, out_dir, scratch, [gen_inst], result["verdict"],
                       result["reason"])
    except cr.BatchAbortError as e:
        report["stages"]["batch_abort"] = e.record.to_dict()
        rec.event("decision", "abort", verdict="error_infra",
                  reason=f"BATCH ABORT (auth/quota): {e}",
                  record=e.record.to_dict())
        rec.finalize("error_infra", f"BATCH ABORT (auth/quota): {e}")
        return _finish(report, out_dir, scratch, [gen_inst], "error_infra",
                       f"BATCH ABORT (auth/quota): {e}")
    except task_diversity.DiversityInfrastructureError as e:
        reason = f"diversity gate infrastructure failed after retries: {e}"
        rec.event("decision", "abort", verdict="error_infra", reason=reason)
        rec.finalize("error_infra", reason)
        return _finish(report, out_dir, scratch, [gen_inst], "error_infra", reason)
    except Exception as e:                              # noqa: BLE001
        tb = traceback.format_exc()
        rec.event("decision", "error", verdict="error",
                  reason=f"{type(e).__name__}: {e}", traceback=tb)
        rec.finalize("error", f"{type(e).__name__}: {e}")
        return _finish(report, out_dir, scratch, [gen_inst], "error",
                       f"{type(e).__name__}: {e}\n{tb}")
    finally:
        if auto_lock_info and report.get("verdict"):
            try:
                if str(report.get("verdict", "")).startswith("accept"):
                    xai_pool.finalize_remote(
                        auto_lock_info["lock_id"],
                        owner=auto_lock_info["owner"],
                        verdict=report.get("verdict", ""),
                    )
                else:
                    xai_pool.release_remote(
                        auto_lock_info["lock_id"],
                        owner=auto_lock_info["owner"],
                        reason=report.get("verdict", ""),
                    )
            except Exception as e:  # noqa: BLE001
                _log(f"lock cleanup failed for {auto_lock_info.get('lock_id')}: {e}")


def _normalize_allowed_difficulties(
    allowed: set[str] | list[str] | tuple[str, ...] | None,
    *,
    fallback: str,
) -> set[str]:
    source = {fallback} if allowed is None else allowed
    values = {str(v).strip().lower() for v in source}
    invalid = sorted(values - {"hard", "medium"})
    if invalid or not values:
        detail = ", ".join(invalid) if invalid else "empty set"
        raise RuntimeError(f"invalid allowed difficulty quota: {detail}")
    return values


def _persist_classified_difficulty(
    out_dir: Path,
    report: dict,
    *,
    classified: str,
    authoring_difficulty: str,
) -> None:
    """Persist the rollout-derived class as the task's authoritative difficulty."""
    meta_path = out_dir / "meta.json"
    meta = load_jsonc(meta_path)
    meta["difficulty"] = classified
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    report["authoring_difficulty"] = authoring_difficulty
    report["difficulty"] = classified
    report["difficulty_band"] = classified
    if isinstance(report.get("stages", {}).get("author_meta"), dict):
        report["stages"]["author_meta"]["difficulty"] = classified


def _configured_bare_completion_range(difficulty: str,
                                      n_bare_rollouts: int) -> tuple[int, int]:
    """Return the configured absolute success-count band for bare rollouts."""
    diff = str(difficulty or "").strip().lower()
    hard_max = int(cfg.BARE_HARD_MAX_COMPLETED)
    medium_min = int(cfg.BARE_MEDIUM_MIN_COMPLETED)
    medium_max = int(cfg.BARE_MEDIUM_MAX_COMPLETED)
    if hard_max < 0 or medium_min < 0 or medium_max < medium_min:
        raise RuntimeError(
            "invalid bare difficulty bands: require 0 <= hard_max and "
            "0 <= medium_min <= medium_max")
    if hard_max + 1 != medium_min:
        raise RuntimeError(
            "invalid bare difficulty bands: require contiguous, non-overlapping "
            "classification with bare_hard_max_completed + 1 == "
            "bare_medium_min_completed")
    if diff == "hard":
        selected_min, selected_max = 0, hard_max
    elif diff == "medium":
        selected_min, selected_max = medium_min, medium_max
    else:
        raise RuntimeError(
            f"unsupported task difficulty for bare gate: {difficulty!r}")
    if n_bare_rollouts > 0 and selected_max > n_bare_rollouts:
        raise RuntimeError(
            f"bare {diff} band [{selected_min}, {selected_max}] exceeds "
            f"live_test_n={n_bare_rollouts}; configure the completion bands "
            "for the selected bare rollout count")
    return selected_min, selected_max


def _classify_bare_difficulty(n_completed: int, n_bare_rollouts: int) -> str:
    """Classify an attempt from agreed task.md-only rollout completions."""
    _hard_min, hard_max = _configured_bare_completion_range(
        "hard", n_bare_rollouts)
    if n_completed <= hard_max:
        return "hard"
    medium_min, medium_max = _configured_bare_completion_range(
        "medium", n_bare_rollouts)
    if medium_min <= n_completed <= medium_max:
        return "medium"
    if n_completed > medium_max:
        return "too_easy"
    raise RuntimeError(
        f"bare completion count {n_completed} falls outside configured "
        f"hard [0,{hard_max}] and medium [{medium_min},{medium_max}] bands")


def _counterfactual_probe_sample(
    rollouts: list[dict], *, rate: float, sample_key: str,
) -> list[dict]:
    """Select a stable unstratified fraction of final binary rollouts."""
    population = [
        rollout for rollout in rollouts
        if rollout.get("verdict") in {"completed", "failed"}
    ]
    if not population or rate <= 0:
        return []
    count = min(len(population), math.ceil(rate * len(population)))
    ranked = sorted(
        population,
        key=lambda rollout: hashlib.sha256(
            f"{sample_key}\0{rollout.get('cell')}".encode()).hexdigest(),
    )
    return ranked[:count]


def run_ensemble_and_gate(profile, day, difficulty, slug, out_dir, meta,
                          report, scratch, require_web, *, rec=None,
                          reauthor=0, world_patch_iters: int = 0,
                          allowed_difficulties: set[str] | None = None) -> dict:
    rec = rec or slog._NullLog()
    allowed_difficulties = _normalize_allowed_difficulties(
        allowed_difficulties, fallback=difficulty)
    task_md = (out_dir / "task.md").read_text()
    helper = (out_dir / "reason_helper.md").read_text()
    snap = snapshot_dir(profile, day, out_dir)
    active_patch = wp.task_patch_dir(out_dir) if wp.has_patch(out_dir) else None
    cum_failed_live = 0          # judged-failed cells across ALL tiers (ease signal)
    # Model/harness pairs tried in the current hint tier; reset when the tier escalates.
    tried_cells: set = set()
    bare_gate_live: list = []    # task.md-only runs, judged alongside the accept tier
    bundle_by_cell: dict[str, dict] = {}

    if cfg.LIVE_TEST_N > 0:
        for allowed_difficulty in sorted(allowed_difficulties):
            _configured_bare_completion_range(
                allowed_difficulty, cfg.LIVE_TEST_N)

    def _run_group(run: "ens.CellRun") -> str:
        return getattr(run, "prompt_group", "") or "hinted"

    def _revalidate(tag):
        v = validate_matrix(out_dir, profile, day, scratch)
        report["stages"].setdefault("validate", []).append({tag: True, **v["detail"]})
        rec.event("validate", "matrix", iteration=reauthor, tag=tag,
                  post_fix=True, ok=v["ok"], defects=v["defects"], detail=v["detail"])
        return v

    def _run_verifier_fixer(defects, completed_afters, tag,
                            *, validation_repair_iter: int | None = None):
        fr = fx.fix(out_dir, defects=defects, baseline_dir=snap,
                    completed_after_dirs=completed_afters)
        call_key = f"fixer_{tag}"
        if validation_repair_iter is not None:
            call_key = f"{call_key}_validate_fix{validation_repair_iter}"
        report["calls"].append({call_key: fr.get("call")})
        report["stages"].setdefault("fix", []).append(
            {"tag": tag, "validation_repair_iter": validation_repair_iter,
             "defects": defects, "changed": fr.get("changed_files")})
        rec.bump("fixer_runs")
        fx_hist = rec.save_generator(
            f"{call_key}_i{reauthor}_{len(report['stages'].get('fix', []))}.json",
            fr.get("call"))
        rec.event("fix", "improve_verifier", iteration=reauthor, tag=tag,
                  validation_repair_iter=validation_repair_iter,
                  ok=fr.get("ok"), defects=defects,
                  changed=fr.get("changed_files"),
                  n_completed_afters=len(completed_afters), history=fx_hist)
        return fr

    def _improve_verifier(defects, completed_afters, tag):
        """Harden the verifier and re-validate it; agent world-states are not re-run."""
        fixes = [_run_verifier_fixer(defects, completed_afters, tag)]
        validation = _revalidate(f"after_{tag}")
        validation_repairs = 0
        while (not validation["ok"]
               and validation_repairs < cfg.MAX_POST_FIX_VALIDATE_FIXES):
            validation_repairs += 1
            repair_defects = [
                "post-fix clean/golden validation failed; repair the verifier "
                "contract before judging ensemble outputs: " + d
                for d in validation["defects"]
            ]
            _log("[fix] post-fix validation failed -> repair clean/golden "
                 f"contract {validation_repairs}/"
                 f"{cfg.MAX_POST_FIX_VALIDATE_FIXES}")
            rec.event("fix", "post_fix_validation_failed",
                      iteration=reauthor, tag=tag,
                      validation_repair_iter=validation_repairs,
                      max_validation_repairs=cfg.MAX_POST_FIX_VALIDATE_FIXES,
                      defects=validation["defects"],
                      detail=validation.get("detail"))
            fixes.append(_run_verifier_fixer(
                repair_defects, completed_afters, tag,
                validation_repair_iter=validation_repairs))
            validation = _revalidate(
                f"after_{tag}_validate_fix{validation_repairs}")
        if not validation["ok"]:
            rec.event("fix", "post_fix_validation_exhausted",
                      iteration=reauthor, tag=tag,
                      validation_repairs=validation_repairs,
                      max_validation_repairs=cfg.MAX_POST_FIX_VALIDATE_FIXES,
                      defects=validation["defects"],
                      detail=validation.get("detail"))
        return {"ok": bool(validation["ok"]), "fixes": fixes,
                "validation": validation,
                "validation_repairs": validation_repairs}

    def _probe_run(j):
        # Probe the final post-fix rubric, not the pre-ensemble one.
        rubric_txt = _read(out_dir / "verifier" / "rubrics.md")
        pr = pb.run_probe(task_md, rubric_txt, out_dir / "verifier",
                          j["run"].before_dir, j["run"].after_dir, j["verdict"])
        pr["cell"] = j["cell"]
        return j, pr

    def _record_ensemble_runs(draw_runs: list["ens.CellRun"], *, tier: int,
                              draw_name: str, event_type: str = "draw_result",
                              planned_cells: int | None = None,
                              **event_extra) -> None:
        for r in draw_runs:
            tried_cells.add((r.harness, r.model))
        cell_bundles = {}
        for r in draw_runs:
            manifest = _agent_record(r)
            cell_bundles[r.cell] = rec.save_ensemble_cell(
                iteration=reauthor, tier=tier, cell=r.cell, manifest=manifest,
                before_dir=r.before_dir, after_dir=r.after_dir,
                trajectory_dir=r.trajectory_dir)
            bundle_by_cell[r.cell] = cell_bundles[r.cell]
        infra_runs = [r for r in draw_runs if r.status.startswith("infra")]
        rec.bump("ensemble_draws")
        rec.bump("ensemble_cells", len(draw_runs))
        rec.bump("ensemble_live_cells",
                 len([r for r in draw_runs if not r.status.startswith("infra")]))
        rec.bump("infra_cells", len(infra_runs))
        if planned_cells is not None and len(draw_runs) > planned_cells:
            rec.bump("infra_resamples", len(draw_runs) - planned_cells)
        payload = {"iteration": reauthor, "tier": tier,
                   "agents": [{**_agent_record(r),
                               "synthesis_log_bundle": cell_bundles.get(r.cell, {})}
                              for r in draw_runs],
                   **event_extra}
        ensemble_ptr = rec.save_ensemble(
            f"i{reauthor}_t{tier}/{draw_name}.json", payload)
        report["stages"].setdefault("ensemble", []).append(
            {"tier": tier, "draw": draw_name,
             "cells": [r.cell for r in draw_runs],
             "groups": {g: [r.cell for r in draw_runs if _run_group(r) == g]
                        for g in sorted({_run_group(r) for r in draw_runs})},
             "infra": [r.cell for r in infra_runs],
             "runs": [r.to_dict() for r in draw_runs],
             **event_extra})
        rec.event("ensemble", event_type, iteration=reauthor, tier=tier,
                  draw=draw_name, n_runs=len(draw_runs),
                  n_live=len([r for r in draw_runs
                              if _run_group(r) == "hinted"
                              and not r.status.startswith("infra")]),
                  n_infra=len(infra_runs),
                  n_bare_live=len([r for r in draw_runs
                                   if _run_group(r) == "bare"
                                   and not r.status.startswith("infra")]),
                  cells=[{**_agent_summary(r),
                          "bundle": cell_bundles.get(r.cell, {})}
                         for r in draw_runs],
                  infra=[{"cell": r.cell, "status": r.status,
                          "error": (r.error or "")} for r in infra_runs],
                  ensemble_record=ensemble_ptr,
                  **event_extra)

    # Hint-escalation loop: each tier is a new agent run with a stronger hint.
    for tier in (1, 2):
        tried_cells.clear()
        msg = rh.compose_solver_prompt(task_md, helper, tier=tier, meta=meta)
        run_bare_gate = tier == 1 and cfg.LIVE_TEST_N > 0
        bare_target = cfg.LIVE_TEST_N if run_bare_gate else 0
        _log(f"[6/9] ensemble tier-{tier} (resilient, target "
             f"{cfg.ENSEMBLE_N} hinted live"
             f"{f' + {bare_target} bare live' if run_bare_gate else ''})")
        rec.bump("hint_tiers")
        rec.event("ensemble", "tier_start", iteration=reauthor, tier=tier,
                  target_live=cfg.ENSEMBLE_N, bare_target_live=bare_target,
                  excluded=len(tried_cells),
                  hinted_models=cfg.ENSEMBLE_MODELS,
                  difficulty_gate_models=(
                      cfg.DIFFICULTY_GATE_ENSEMBLE_MODELS
                      if run_bare_gate else []),
                  solver_prompt_chars=len(msg))
        if run_bare_gate:
            grouped = ens.run_ensemble_resilient_groups(
                profile, day, slug,
                {
                    "hinted": {
                        "message": msg,
                        "scratch": scratch / f"ens_t{tier}_hinted",
                        "n": cfg.ENSEMBLE_N,
                    },
                    "bare": {
                        "message": task_md.rstrip() + "\n",
                        "scratch": scratch / f"ens_t{tier}_bare",
                        "n": cfg.LIVE_TEST_N,
                        "models": cfg.DIFFICULTY_GATE_ENSEMBLE_MODELS,
                        "completion_probe_task_text": task_md,
                    },
                },
                seed=f"{profile}-{day}-{slug}-t{tier}",
                exclude=list(tried_cells), timeout_s=cfg.SOLVE_TIMEOUT_S,
                log=_log, world_patch=active_patch)
            runs = (grouped.get("hinted") or []) + (grouped.get("bare") or [])
        else:
            runs = ens.run_ensemble_resilient(
                profile, day, slug, msg, scratch / f"ens_t{tier}",
                n=cfg.ENSEMBLE_N, seed=f"{profile}-{day}-{slug}-t{tier}",
                exclude=list(tried_cells), timeout_s=cfg.SOLVE_TIMEOUT_S, log=_log,
                world_patch=active_patch)
            for r in runs:
                r.prompt_group = "hinted"
        planned_cells = cfg.ENSEMBLE_N + bare_target
        _record_ensemble_runs(
            runs, tier=tier, draw_name="sampled", event_type="draw_result",
            planned_cells=planned_cells)
        live = [r for r in runs
                if _run_group(r) == "hinted" and not r.status.startswith("infra")]
        if run_bare_gate:
            bare_gate_live = [
                r for r in runs
                if _run_group(r) == "bare" and not r.status.startswith("infra")
            ]
        if not live:
            hinted_runs = [r for r in runs if _run_group(r) == "hinted"]
            if hinted_runs:
                rsn = (
                    f"all {len(hinted_runs)} hinted ensemble cells failed infra: "
                    f"{[r.error for r in hinted_runs]}"
                )
            else:
                rsn = (
                    f"tier-{tier} ensemble scheduler produced no eligible "
                    "hinted cells; check rollout pool and exclusions"
                )
            rec.event("gate", "abort", verdict="error_infra", reason=rsn,
                      iteration=reauthor, tier=tier)
            return {"verdict": "error_infra", "reason": rsn, "tier": tier}
        if run_bare_gate and len(bare_gate_live) < cfg.LIVE_TEST_N:
            rsn = (f"bare task.md difficulty gate only produced "
                   f"{len(bare_gate_live)}/{cfg.LIVE_TEST_N} live cells; "
                   f"infra={[r.error for r in runs if _run_group(r) == 'bare' and r.status.startswith('infra')]}")
            rec.event("gate", "abort", verdict="error_infra", reason=rsn,
                      iteration=reauthor, tier=tier, gate="bare_difficulty")
            return {"verdict": "error_infra", "reason": rsn, "tier": tier}

        # Verifier-stabilization loop on persisted world-states; any verifier change re-checks all.
        agree_fixes, cave_fixes = 0, 0
        judge_pass = 0
        judge_infra_resamples_used = {"hinted": 0, "bare": 0}
        while True:
            _log(f"[7/9] judge {len(live)} run(s) (L1+lw+heavy) tier{tier} "
                 f"+ {len(bare_gate_live)} bare "
                 f"[agree_fix={agree_fixes} cave_fix={cave_fixes}]")
            judge_pass += 1
            rec.bump("judge_passes")
            judged_all = _judge_all(out_dir, live + bare_gate_live, profile, day, rec,
                                    judge_pass=judge_pass, tier=tier)
            judged = [j for j in judged_all if _run_group(j["run"]) != "bare"]
            bare_judged = [j for j in judged_all if _run_group(j["run"]) == "bare"]
            report["stages"].setdefault("judge", []).append([_jslim(j) for j in judged_all])
            rec.event("judge", "pass", iteration=reauthor, tier=tier,
                      judge_pass=judge_pass, agree_fixes=agree_fixes,
                      cave_fixes=cave_fixes, n=len(judged_all),
                      cells=[{"cell": j["cell"],
                                  "group": _run_group(j["run"]),
                                  "l1": j["l1"], "lw": j["lw"], "heavy": j["heavy"],
                                  "agree": j["agree"], "verdict": j["verdict"],
                                  "steps": j.get("steps"),
                                  "llm_calls": (j["run"].stats or {}).get("llm_calls"),
                                  "trajectory_dir": j["run"].trajectory_dir,
                                  "ensemble_bundle": bundle_by_cell.get(j["cell"], {}),
                                  "lw_history": j.get("lw_history"),
                                  "heavy_history": j.get("heavy_history"),
                                  "script_errors": j["l1_script_errors"]}
                                 for j in judged_all])

            judge_infra = [j for j in judged_all if j["verdict"] == "infra"]
            if judge_infra:
                needs: dict[str, int] = {}
                replaced = []
                for j in judge_infra:
                    group = _run_group(j["run"])
                    needs[group] = needs.get(group, 0) + 1
                    reason = (
                        f"judge infra after retry: lw={j.get('lw')} "
                        f"heavy={j.get('heavy')} l1={j.get('l1')}"
                    )
                    j["run"].status = "infra:judge"
                    j["run"].error = reason
                    replaced.append({"cell": j["cell"], "group": group,
                                     "reason": reason})
                over_budget = [
                    f"{group} needs {count}, used "
                    f"{judge_infra_resamples_used.get(group, 0)}/"
                    f"{cfg.MAX_JUDGE_INFRA_RESAMPLE}"
                    for group, count in needs.items()
                    if judge_infra_resamples_used.get(group, 0) + count
                    > cfg.MAX_JUDGE_INFRA_RESAMPLE
                ]
                if over_budget:
                    rsn = (
                        "judge infra replacement budget exhausted; infra "
                        "cannot be counted as failed: " + "; ".join(over_budget)
                    )
                    rec.event("gate", "abort", verdict="error_infra",
                              reason=rsn, iteration=reauthor, tier=tier,
                              gate="judge_infra_replacement",
                              judge_infra=replaced)
                    return {"verdict": "error_infra", "reason": rsn,
                            "tier": tier}
                bad_ids = {id(j["run"]) for j in judge_infra}
                live = [r for r in live if id(r) not in bad_ids]
                bare_gate_live = [
                    r for r in bare_gate_live if id(r) not in bad_ids
                ]
                for group, count in needs.items():
                    judge_infra_resamples_used[group] = (
                        judge_infra_resamples_used.get(group, 0) + count
                    )
                rec.bump("infra_cells", len(judge_infra))
                rec.bump("infra_resamples", len(judge_infra))
                rec.event("judge", "infra_replacement_start",
                      iteration=reauthor, tier=tier,
                      judge_pass=judge_pass, needs=needs,
                      replaced=replaced,
                      max_judge_infra_resample=
                          cfg.MAX_JUDGE_INFRA_RESAMPLE)
                # Exclude only cells still accepted; a judge-infra cell's pair may be re-drawn.
                replacement_exclude = {
                    (run.harness, run.model)
                    for run in live + bare_gate_live
                }
                repl_specs = {}
                if needs.get("hinted"):
                    repl_specs["hinted"] = {
                        "message": msg,
                        "scratch": scratch / (
                            f"ens_t{tier}_hinted_judge_repl_p{judge_pass}"
                        ),
                        "n": needs["hinted"],
                    }
                if needs.get("bare"):
                    repl_specs["bare"] = {
                        "message": task_md.rstrip() + "\n",
                        "scratch": scratch / (
                            f"ens_t{tier}_bare_judge_repl_p{judge_pass}"
                        ),
                        "n": needs["bare"],
                        "models": cfg.DIFFICULTY_GATE_ENSEMBLE_MODELS,
                        "completion_probe_task_text": task_md,
                    }
                if set(needs) - set(repl_specs):
                    rsn = f"unsupported judge-infra replacement group(s): {needs}"
                    rec.event("gate", "abort", verdict="error_infra",
                              reason=rsn, iteration=reauthor, tier=tier,
                              gate="judge_infra_replacement")
                    return {"verdict": "error_infra", "reason": rsn,
                            "tier": tier}
                grouped_repl = ens.run_ensemble_resilient_groups(
                    profile, day, slug, repl_specs,
                    seed=(f"{profile}-{day}-{slug}-t{tier}-"
                          f"judge-infra-p{judge_pass}-"
                          f"a{agree_fixes}-c{cave_fixes}"),
                    exclude=list(replacement_exclude),
                    timeout_s=cfg.SOLVE_TIMEOUT_S,
                    log=_log, world_patch=active_patch)
                repl_runs = []
                for group in ("hinted", "bare"):
                    repl_runs.extend(grouped_repl.get(group) or [])
                _record_ensemble_runs(
                    repl_runs, tier=tier,
                    draw_name=f"judge_infra_replacement_p{judge_pass}",
                    event_type="judge_infra_replacement",
                    planned_cells=sum(needs.values()),
                    judge_pass=judge_pass, replaced=replaced, needs=needs)
                runs.extend(repl_runs)
                new_live_by_group = {
                    "hinted": len([r for r in repl_runs
                                   if _run_group(r) == "hinted"
                                   and not r.status.startswith("infra")]),
                    "bare": len([r for r in repl_runs
                                 if _run_group(r) == "bare"
                                 and not r.status.startswith("infra")]),
                }
                short = {
                    group: needs[group] - new_live_by_group.get(group, 0)
                    for group in needs
                    if new_live_by_group.get(group, 0) < needs[group]
                }
                if short:
                    rsn = (
                        "judge infra replacement could not replenish live "
                        f"cells: short={short}"
                    )
                    rec.event("gate", "abort", verdict="error_infra",
                              reason=rsn, iteration=reauthor, tier=tier,
                              gate="judge_infra_replacement",
                              judge_infra=replaced)
                    return {"verdict": "error_infra", "reason": rsn,
                            "tier": tier}
                live.extend([
                    r for r in repl_runs
                    if _run_group(r) == "hinted"
                    and not r.status.startswith("infra")
                ])
                bare_gate_live.extend([
                    r for r in repl_runs
                    if _run_group(r) == "bare"
                    and not r.status.startswith("infra")
                ])
                _log(f"[7/9] judge infra replaced {len(judge_infra)} "
                     "cell(s); re-running judge gate")
                continue

            # Every run needs verifier == lightweight == heavy; otherwise improve the verifier.
            bad = ([f"{j['cell']}: programmatic={j['l1']}, lightweight={j['lw']}, "
                    f"heavy={j['heavy']} do not satisfy V = V_LLM = V_agent on the "
                    f"SAME solved world. Make the checks and rubric cite concrete, "
                    f"machine-checkable world state. heavy_just={j['heavy_just']}"
                    for j in judged_all if j["agree"] is False]
                   + [f"{j['cell']}: verifier scripts crashed {j['l1_script_errors']}"
                      for j in judged_all if j["l1_script_errors"]])
            if bad:
                disagree_cells = [{"cell": j["cell"], "l1": j["l1"],
                                   "lw": j["lw"], "heavy": j["heavy"]}
                                  for j in judged_all if j["agree"] is False]
                crash_cells = [j["cell"] for j in judged_all if j["l1_script_errors"]]
                if agree_fixes >= cfg.MAX_AGREE_FIX:
                    rsn = (f"three-level judgment disagreement unresolved after "
                           f"{cfg.MAX_AGREE_FIX} verifier improvements: {bad}")
                    rec.event("agree_fix", "exhausted", iteration=reauthor,
                              tier=tier, agree_fixes=agree_fixes,
                              max_agree_fix=cfg.MAX_AGREE_FIX,
                              disagreements=disagree_cells, crashes=crash_cells)
                    rec.event("gate", "reject",
                              verdict="reject_verifier_disagree", reason=rsn,
                              iteration=reauthor, tier=tier)
                    return {"verdict": "reject_verifier_disagree", "reason": rsn,
                            "tier": tier}
                _offer_skill_write(
                    rec=rec, report=report, stage="judge_disagreement",
                    profile=profile, day=day, difficulty=difficulty,
                    slug=slug, out_dir=out_dir,
                    result={"verdict": "judge_disagreement",
                            "reason": "; ".join(bad)},
                    feedback="\n".join(bad))
                _log(f"[agree_fix] {len(bad)} disagreement/crash -> improve verifier")
                rec.bump("agree_fixes")
                rec.event("agree_fix", "disagreement", iteration=reauthor,
                          tier=tier, judge_pass=judge_pass,
                          agree_fix_iter=agree_fixes + 1,
                          max_agree_fix=cfg.MAX_AGREE_FIX,
                          disagreements=disagree_cells, crashes=crash_cells,
                          defects=bad)
                improved = _improve_verifier(
                    bad, [str(Path(j["run"].after_dir)) for j in judged_all
                          if j["heavy"] == "completed"], "agree")
                agree_fixes += 1
                if not improved["ok"]:
                    rsn = (
                        "post-fix clean/golden validation remained broken after "
                        f"{cfg.MAX_POST_FIX_VALIDATE_FIXES} verifier contract "
                        f"repair attempt(s): {improved['validation']['defects']}"
                    )
                    rec.event("agree_fix", "validation_exhausted",
                              iteration=reauthor, tier=tier,
                              agree_fixes=agree_fixes,
                              max_validation_repairs=
                                  cfg.MAX_POST_FIX_VALIDATE_FIXES,
                              defects=improved["validation"]["defects"])
                    rec.event("gate", "reject",
                              verdict="reject_verifier_disagree", reason=rsn,
                              iteration=reauthor, tier=tier)
                    return {"verdict": "reject_verifier_disagree",
                            "reason": rsn, "tier": tier}
                continue                       # re-verify the SAME world-states
            if judge_pass > 1 or agree_fixes:
                # they now agree after >=1 improvement — record the recovery.
                rec.event("agree_fix", "agreed", iteration=reauthor, tier=tier,
                          judge_pass=judge_pass, agree_fixes=agree_fixes,
                          verdicts=[{"cell": j["cell"], "verdict": j["verdict"]}
                                    for j in judged_all])

            # All runs agree; handle world_imperfect: reproduce, optionally patch, re-judge.
            world_suspects = [x for x in judged_all if x["verdict"] == "world_imperfect"]
            if world_suspects:
                evidence = _world_fault_evidence(world_suspects)
                dispositions = []
                confirmed: list[dict] = []
                unresolved: list[dict] = []
                for j in world_suspects:
                    disp = wr.classify_world_fault(
                        profile, day, slug,
                        {"cell": j["cell"], "heavy_just": j["heavy_just"],
                         "lw_just": j.get("lw_just", ""),
                         "l1": j.get("l1"),
                         "script_errors": j.get("l1_script_errors", [])},
                        clean_snapshot=snap,
                        verifier_dir=out_dir / "verifier",
                    )
                    if disp["disposition"] == "ambiguous":
                        diag = wr.diagnose_ambiguous(out_dir, disp, snap)
                        disp = {**disp, "diagnostic": diag}
                        if diag["disposition"] in wr.DISPOSITIONS:
                            disp["disposition"] = diag["disposition"]
                    dispositions.append(disp)
                    report["stages"].setdefault("world_repair", []).append(disp)
                    rec.event("world_repair", "classification",
                              iteration=reauthor, tier=tier, cell=j["cell"],
                              disposition=disp.get("disposition"), detail=disp,
                              heavy_just=j["heavy_just"])
                    if disp["disposition"] == "confirmed_world_fault":
                        confirmed.append(disp)
                    elif disp["disposition"] == "not_world_fault":
                        j["verdict"] = "failed"
                        j["world_fault_disposition"] = disp
                    else:
                        unresolved.append(disp)
                if confirmed:
                    repair = _attempt_world_patch_repair(
                        profile, day, slug, out_dir, scratch, evidence,
                        rec=rec, report=report, reauthor=reauthor,
                        used_iters=world_patch_iters)
                    if repair["action"] == "rerun":
                        _offer_skill_write(
                            rec=rec, report=report, stage="world_repair",
                            profile=profile, day=day, difficulty=difficulty,
                            slug=slug, out_dir=out_dir,
                            result={"verdict": "repaired",
                                    "reason": repair.get("reason")},
                            feedback=json.dumps(repair, ensure_ascii=False)[:2000])
                        rec.bump("world_repair_tickets")
                        rec.event("world_repair", "rerun_after_patch",
                                  iteration=reauthor, tier=tier,
                                  used_iters=repair.get("used"),
                                  reason=repair.get("reason"))
                        return run_ensemble_and_gate(
                            profile, day, difficulty, slug, out_dir, meta,
                            report, scratch, require_web, rec=rec,
                            reauthor=reauthor,
                            world_patch_iters=world_patch_iters + repair.get("used", 0),
                            allowed_difficulties=allowed_difficulties)
                    rsn = repair["reason"]
                    rec.event("gate", "reject",
                              verdict="reject_world_imperfect_unresolved",
                              reason=rsn, iteration=reauthor, tier=tier)
                    return {"verdict": "reject_world_imperfect_unresolved",
                            "reason": rsn, "tier": tier,
                            "n_completed": 0, "n_live": len(live)}
                if unresolved:
                    rsn = f"ambiguous world_imperfect could not be confirmed or cleared: {unresolved}"
                    rec.event("gate", "reject",
                              verdict="reject_world_imperfect_unresolved",
                              reason=rsn, iteration=reauthor, tier=tier)
                    return {"verdict": "reject_world_imperfect_unresolved",
                            "reason": rsn, "tier": tier,
                            "n_completed": 0, "n_live": len(live)}
                judged = [j for j in judged_all if _run_group(j["run"]) != "bare"]
                bare_judged = [j for j in judged_all if _run_group(j["run"]) == "bare"]

            completed = [j for j in judged if j["verdict"] == "completed"]
            bare_completed = [j for j in bare_judged if j["verdict"] == "completed"]
            n_failed_this = sum(1 for j in judged if j["verdict"] == "failed")

            # Need >=1 completion among all live rollouts; else escalate the hint tier or reject.
            if not completed and not bare_completed:
                cum_failed_live += n_failed_this
                rec.event("gate", "completion", iteration=reauthor, tier=tier,
                          rule="at-least-one-completed", n_live=len(live), n_completed=0,
                          n_bare_live=len(bare_judged), n_bare_completed=0,
                          n_failed=n_failed_this, cum_failed_live=cum_failed_live,
                          verdicts=[{"cell": j["cell"], "verdict": j["verdict"]}
                                    for j in judged],
                          bare_verdicts=[{"cell": j["cell"], "verdict": j["verdict"]}
                                         for j in bare_judged],
                          outcome=("escalate_hint" if tier == 1 else "reject"))
                _offer_skill_write(
                    rec=rec, report=report, stage="solvability_failed",
                    profile=profile, day=day, difficulty=difficulty,
                    slug=slug, out_dir=out_dir,
                    result={"verdict": "reject_unsolvable",
                            "reason": f"tier {tier}: 0 completed overall"},
                    feedback=json.dumps(
                        [{"cell": j["cell"], "verdict": j["verdict"],
                          "heavy": j.get("heavy_just"), "light": j.get("lw_just")}
                         for j in judged + bare_judged],
                        ensure_ascii=False)[:2000])
                if tier == 1:
                    _log("[completion] 0 completed overall @tier-1 "
                         "-> tier-2 hint (re-run)")
                    break                      # -> next tier (new agent run)
                rsn = (f"0 completed across both hint tiers and all bare "
                       f"rollouts; {cum_failed_live} capable hinted cell(s) "
                       f"judged failed")
                rec.event("gate", "reject", verdict="reject_unsolvable",
                          reason=rsn, iteration=reauthor, tier=tier,
                          n_failed_live=cum_failed_live)
                return {"verdict": "reject_unsolvable", "reason": rsn,
                        "n_failed_live": cum_failed_live, "tier": tier,
                        "n_completed": 0, "n_live": len(live)}

            # Completed trajectories must actually use all of cli/mcp/web.
            required_ifaces = {"cli", "mcp", "web"}
            bypassed = []
            for j in completed + bare_completed:
                used = set((j["run"].stats or {}).get("interfaces") or [])
                missing = sorted(required_ifaces - used)
                if missing:
                    bypassed.append({
                        "cell": j["cell"],
                        "group": _run_group(j["run"]),
                        "verdict": j.get("verdict"),
                        "used": sorted(used),
                        "missing": missing,
                        "llm_calls": (j["run"].stats or {}).get("llm_calls"),
                        "trajectory_dir": j["run"].trajectory_dir,
                    })
            rec.event("gate", "interface_use", iteration=reauthor, tier=tier,
                      rule="all-three-interfaces",
                      n_completed=len(completed),
                      n_bare_completed=len(bare_completed),
                      bypassed=bypassed)
            if bypassed:
                rsn = (
                    "completed solver trajectory bypassed required interface(s): "
                    + "; ".join(
                        f"{b['cell']} used {b['used']} missing {b['missing']}"
                        for b in bypassed
                    )
                )
                rec.event("gate", "reject", verdict="reject_interface_bypass",
                          reason=rsn, iteration=reauthor, tier=tier,
                          bypassed=bypassed)
                return {"verdict": "reject_interface_bypass", "reason": rsn,
                        "tier": tier, "n_completed": len(completed),
                        "n_live": len(live), "bypassed": bypassed}

            # Difficulty (depth = proxy-counted LLM calls) before the probe; too easy -> re-author.
            depths = [int(v) for v in ((j["run"].stats or {}).get("llm_calls")
                                       for j in judged) if v is not None]
            max_depth = max(depths) if depths else None
            all_completed = len(completed) == len(judged)
            too_easy = (all_completed and max_depth is not None
                        and max_depth < cfg.STEP_GATE)
            rec.event("gate", "difficulty", iteration=reauthor, tier=tier,
                      rule="hinted-depth-gate", n_live=len(live), n_completed=len(completed),
                      all_completed=all_completed, max_depth_llm_calls=max_depth,
                      step_gate=cfg.STEP_GATE, too_easy=too_easy,
                      depths=depths,
                      per_cell=[{"cell": j["cell"], "verdict": j["verdict"],
                                 "llm_calls": (j["run"].stats or {}).get("llm_calls"),
                                 "steps": j.get("steps")} for j in judged])
            if too_easy:
                report["stages"]["acceptance"] = {
                    "n_live": len(live), "n_completed": len(completed),
                    "max_depth_llm_calls": max_depth, "tier": tier, "too_easy": True}
                rsn = (f"all {len(judged)} completed, max LLM-calls "
                       f"{max_depth} < {cfg.STEP_GATE} — too shallow; deepen")
                rec.event("gate", "reject", verdict="reject_too_easy",
                          reason=rsn, iteration=reauthor, tier=tier,
                          max_depth_llm_calls=max_depth)
                return {"verdict": "reject_too_easy", "reason": rsn,
                        "max_depth": max_depth, "tier": tier,
                        "n_completed": len(completed), "n_live": len(live),
                        "completed_cells": [
                            {"cell": j["cell"],
                             "llm_calls": (j["run"].stats or {}).get("llm_calls"),
                             "steps": j.get("steps"),
                             "interfaces": (j["run"].stats or {}).get("interfaces"),
                             "trajectory_dir": j["run"].trajectory_dir}
                            for j in completed
                        ]}

            bare_k = len(bare_completed)
            classified_difficulty = (
                _classify_bare_difficulty(bare_k, len(bare_judged))
                if bare_judged else difficulty
            )
            bare_cells = [
                {"cell": j["cell"], "verdict": j["verdict"],
                 "llm_calls": (j["run"].stats or {}).get("llm_calls"),
                 "steps": j.get("steps"),
                 "interfaces": (j["run"].stats or {}).get("interfaces"),
                 "completion_probe_done":
                     (j["run"].completion_probe or {}).get("done"),
                 "trajectory_dir": j["run"].trajectory_dir}
                for j in bare_judged
            ]
            target_difficulty = classified_difficulty
            recalibration_verdict = None
            if classified_difficulty == "too_easy":
                target_difficulty = (
                    "hard" if "hard" in allowed_difficulties else "medium")
                recalibration_verdict = "reject_too_easy"
            elif classified_difficulty not in allowed_difficulties:
                if (classified_difficulty == "hard"
                        and "medium" in allowed_difficulties):
                    target_difficulty = "medium"
                    recalibration_verdict = "reject_too_hard"
                elif (classified_difficulty == "medium"
                      and "hard" in allowed_difficulties):
                    target_difficulty = "hard"
                    recalibration_verdict = "reject_too_easy"
                else:
                    raise RuntimeError(
                        f"classified difficulty {classified_difficulty!r} has no "
                        f"available day quota in {sorted(allowed_difficulties)}")
            bare_min, bare_max = _configured_bare_completion_range(
                target_difficulty, len(bare_judged))
            bare_status = (
                "target" if recalibration_verdict is None
                else "quota_full" if classified_difficulty in {"hard", "medium"}
                else "too_easy"
            )
            rec.event(
                "gate", "bare_difficulty", iteration=reauthor, tier=tier,
                rule="task-md-only-adaptive-classification",
                authoring_difficulty=difficulty,
                allowed_difficulties=sorted(allowed_difficulties),
                classified_difficulty=classified_difficulty,
                target_difficulty=target_difficulty,
                n_live=len(bare_judged),
                n_completed=bare_k,
                min_completed=bare_min,
                max_completed=bare_max,
                difficulty_status=bare_status,
                quota_available=recalibration_verdict is None,
                verdicts=[{"cell": j["cell"], "verdict": j["verdict"]}
                          for j in bare_judged],
                per_cell=[{"cell": j["cell"], "verdict": j["verdict"],
                           "l1": j["l1"], "lw": j["lw"],
                           "heavy": j["heavy"], "agree": j["agree"],
                           "llm_calls": (j["run"].stats or {}).get("llm_calls"),
                           "steps": j.get("steps")} for j in bare_judged])
            if recalibration_verdict:
                action = "ease" if recalibration_verdict == "reject_too_hard" else "deepen"
                rsn = (
                    f"task.md-only bare result classified {classified_difficulty} "
                    f"at {bare_k}/{len(bare_judged)} completed, but that day quota "
                    f"is full; {action} toward remaining {target_difficulty} quota "
                    f"(required range {bare_min}..{bare_max})"
                )
                rec.event(
                    "gate", "reject", verdict=recalibration_verdict,
                    reason=rsn, iteration=reauthor, tier=tier,
                    gate="bare_difficulty_quota",
                    classified_difficulty=classified_difficulty,
                    target_difficulty=target_difficulty,
                    allowed_difficulties=sorted(allowed_difficulties),
                    n_bare_completed=bare_k,
                    n_bare_live=len(bare_judged),
                    bare_min_completed=bare_min,
                    bare_max_completed=bare_max)
                return {
                    "verdict": recalibration_verdict, "reason": rsn,
                    "max_depth": max_depth, "tier": tier,
                    "n_completed": len(completed), "n_live": len(live),
                    "n_failed_live": len(bare_judged) - bare_k,
                    "bare_gate": True,
                    "bare_n_completed": bare_k,
                    "bare_n_live": len(bare_judged),
                    "bare_min_completed": bare_min,
                    "bare_max_completed": bare_max,
                    "classified_difficulty": classified_difficulty,
                    "target_difficulty": target_difficulty,
                    "allowed_difficulties": sorted(allowed_difficulties),
                    "bare_cells": bare_cells,
                }

            # Persuasion probe: a cave means a weak verifier/rubric -> improve and re-verify.
            binary_population = [
                j for j in judged_all
                if j["verdict"] in ("completed", "failed")
            ]
            probeable = _counterfactual_probe_sample(
                judged_all,
                rate=cfg.XAI_COUNTERFACTUAL_PROBE_RATE,
                sample_key=(
                    f"{profile}/day{day}/{slug}/reauthor{reauthor}/tier{tier}"
                ),
            )
            _log(
                f"[8/9] probe {len(probeable)}/{len(binary_population)} "
                "final live binary rollout(s) (blind re-judge -> gaslight)"
            )
            rec.event("probe", "start", iteration=reauthor, tier=tier,
                      judge_pass=judge_pass, cave_fixes=cave_fixes,
                      sample_rate=cfg.XAI_COUNTERFACTUAL_PROBE_RATE,
                      population_size=len(binary_population),
                      n_probeable=len(probeable),
                      population=[{"cell": j["cell"], "verdict": j["verdict"],
                                   "group": _run_group(j["run"])}
                                  for j in binary_population],
                      cells=[{"cell": j["cell"], "verdict": j["verdict"],
                              "group": _run_group(j["run"])}
                             for j in probeable])
            caves, probe_defects, probe_infra, probes = [], [], [], []
            for j, pr in _pmap(_probe_run, probeable):
                probes.append({k: v for k, v in pr.items() if k != "calls"})
                report["calls"].append({"probe": pr.get("calls")})
                rec.bump("probe_runs")
                # The probe's raw history is its blind + gaslight call list.
                pr_hist = rec.save_probe_history(
                    judge_pass=judge_pass, tier=tier, cell=j["cell"],
                    payload=pr.get("calls") or [])
                outcome = pr.get("outcome")
                if outcome == "cave":
                    rec.bump("probe_caves")
                    caves.append((j, pr))
                elif outcome in {"blind_mismatch", "refute_judge", "unclear"}:
                    probe_defects.append((j, pr))
                elif outcome == "infra":
                    probe_infra.append((j, pr))
                rec.event("probe", "result", iteration=reauthor, tier=tier,
                          judge_pass=judge_pass, cell=j["cell"],
                          real_verdict=j["verdict"],
                          blind_verdict=pr.get("blind_verdict"),
                          blind_agrees=pr.get("blind_agrees"),
                          stated=pr.get("stated"), outcome=outcome,
                          caved_files=pr.get("caved_files"),
                          justification=(pr.get("justification") or ""),
                          history=pr_hist)
            report["stages"].setdefault("probe", []).append(probes)
            if probe_infra:
                rsn = (
                    "probe infra after retries; cannot accept or count as failed: "
                    + "; ".join(
                        f"{j['cell']} stage={pr.get('stage')} status={pr.get('status')}"
                        for j, pr in probe_infra
                    )
                )
                rec.event("gate", "abort", verdict="error_infra", reason=rsn,
                          iteration=reauthor, tier=tier, gate="probe",
                          probe_infra=[{"cell": j["cell"],
                                        "stage": pr.get("stage"),
                                        "status": pr.get("status")}
                                       for j, pr in probe_infra])
                return {"verdict": "error_infra", "reason": rsn,
                        "tier": tier, "n_completed": len(completed),
                        "n_live": len(live), "max_depth": max_depth}
            if caves or probe_defects:
                _offer_skill_write(
                    rec=rec, report=report,
                    stage="counterfactual_probe_failed",
                    profile=profile, day=day, difficulty=difficulty,
                    slug=slug, out_dir=out_dir,
                    result={"verdict": "probe_failed",
                            "reason": f"{len(caves)} cave(s), {len(probe_defects)} defect(s)"},
                    feedback=json.dumps(
                        [{"cell": j["cell"], "verdict": j["verdict"],
                          "probe": {k: v for k, v in pr.items() if k != "calls"}}
                         for j, pr in caves + probe_defects],
                        ensure_ascii=False)[:2000])
                if cave_fixes >= cfg.MAX_CAVE_FIX:
                    cells = [j["cell"] for j, _ in caves + probe_defects]
                    rsn = (f"probe unresolved after {cfg.MAX_CAVE_FIX} "
                           f"verifier improvements: {cells}")
                    rec.event("cave_fix", "exhausted", iteration=reauthor,
                              tier=tier, cave_fixes=cave_fixes,
                              max_cave_fix=cfg.MAX_CAVE_FIX,
                              caved_cells=[c[0]["cell"] for c in caves],
                              probe_defect_cells=[d[0]["cell"]
                                                  for d in probe_defects])
                    rec.event("gate", "reject",
                              verdict="reject_verifier_fragile", reason=rsn,
                              iteration=reauthor, tier=tier)
                    return {"verdict": "reject_verifier_fragile", "reason": rsn,
                            "tier": tier, "n_completed": len(completed),
                            "n_live": len(live), "max_depth": max_depth}
                defects = [
                    f"pressure probe on {j['cell']}: the verifier CAVED (edited "
                    f"{pr['caved_files']}) under a false-signal challenge — it is "
                    f"fragile. Re-author the checks to assert hard, citable world "
                    f"evidence so they need not change under pressure. "
                    f"{pr['justification']}"
                    for j, pr in caves
                ]
                defects.extend(
                    f"pressure probe on {j['cell']}: blind/gaslight outcome "
                    f"{pr.get('outcome')} means the automated verdict "
                    f"{j['verdict']} is not robustly supported. Blind verdict="
                    f"{pr.get('blind_verdict')}; stated={pr.get('stated')}. "
                    "Improve the verifier/rubric so an independent judge reaches "
                    "the same verdict from hard, citable world evidence. "
                    f"{pr.get('justification') or ''}"
                    for j, pr in probe_defects
                )
                _log(f"[cave_fix] {len(caves)} cave(s), "
                     f"{len(probe_defects)} probe defect(s) -> improve verifier")
                rec.bump("cave_fixes")
                rec.event("cave_fix", "probe_defect", iteration=reauthor, tier=tier,
                          cave_fix_iter=cave_fixes + 1, max_cave_fix=cfg.MAX_CAVE_FIX,
                          caved=[{"cell": j["cell"], "caved_files": pr.get("caved_files")}
                                 for j, pr in caves],
                          probe_defects=[{"cell": j["cell"],
                                          "outcome": pr.get("outcome"),
                                          "blind_verdict": pr.get("blind_verdict"),
                                          "stated": pr.get("stated")}
                                         for j, pr in probe_defects],
                          defects=defects)
                completed_after_dirs = [str(Path(j["run"].after_dir))
                                        for j in completed]
                for j, pr in probe_defects:
                    if pr.get("blind_verdict") == "completed":
                        completed_after_dirs.append(str(Path(j["run"].after_dir)))
                completed_after_dirs = list(dict.fromkeys(completed_after_dirs))
                improved = _improve_verifier(
                    defects, completed_after_dirs, "probe")
                cave_fixes += 1
                if not improved["ok"]:
                    rsn = (
                        "post-fix clean/golden validation remained broken after "
                        f"{cfg.MAX_POST_FIX_VALIDATE_FIXES} verifier contract "
                        f"repair attempt(s): {improved['validation']['defects']}"
                    )
                    rec.event("cave_fix", "validation_exhausted",
                              iteration=reauthor, tier=tier,
                              cave_fixes=cave_fixes,
                              max_validation_repairs=
                                  cfg.MAX_POST_FIX_VALIDATE_FIXES,
                              defects=improved["validation"]["defects"])
                    rec.event("gate", "reject",
                              verdict="reject_verifier_fragile", reason=rsn,
                              iteration=reauthor, tier=tier)
                    return {"verdict": "reject_verifier_fragile",
                            "reason": rsn, "tier": tier,
                            "n_completed": len(completed),
                            "n_live": len(live), "max_depth": max_depth}
                continue                       # re-verify (verifier changed)

            # All conditions hold: agreement, hinted completion, difficulty band, probe resisted.
            report["stages"]["acceptance"] = {
                "n_live": len(live), "n_completed": len(completed),
                "bare_n_live": len(bare_judged),
                "bare_n_completed": len(bare_completed),
                "bare_min_completed": bare_min,
                "bare_max_completed": bare_max,
                "difficulty_band": classified_difficulty,
                "authoring_difficulty": difficulty,
                "allowed_difficulties": sorted(allowed_difficulties),
                "verdicts": [j["verdict"] for j in judged],
                "max_depth_llm_calls": max_depth, "tier": tier,
                "agree_fixes": agree_fixes, "cave_fixes": cave_fixes}
            verdict = "accept_after_fix" if (agree_fixes or cave_fixes) else "accept"
            rsn = (f"{len(completed)}/{len(live)} completed; "
                   f"V==V_LLM==V_agent all; "
                   f"bare {len(bare_completed)}/{len(bare_judged)} completed "
                   f"classified {classified_difficulty} "
                   f"(band {bare_min}..{bare_max}); "
                   f"probe resisted; max LLM-calls {max_depth}; tier {tier}; "
                   f"fixes(agree={agree_fixes},cave={cave_fixes})")
            rec.event("gate", "accept", iteration=reauthor, tier=tier,
                      verdict=verdict, n_live=len(live), n_completed=len(completed),
                      bare_n_live=len(bare_judged),
                      bare_n_completed=len(bare_completed),
                      bare_min_completed=bare_min,
                      bare_max_completed=bare_max,
                      difficulty_band=classified_difficulty,
                      authoring_difficulty=difficulty,
                      allowed_difficulties=sorted(allowed_difficulties),
                      verdicts=[{"cell": j["cell"], "verdict": j["verdict"]}
                                for j in judged],
                      max_depth_llm_calls=max_depth, agree_fixes=agree_fixes,
                      cave_fixes=cave_fixes,
                      probe_outcomes=[{"cell": p.get("cell"), "outcome": p.get("outcome")}
                                      for p in probes])
            return {"verdict": verdict, "reason": rsn, "tier": tier,
                    "n_completed": len(completed), "n_live": len(live),
                    "bare_n_completed": len(bare_completed),
                    "bare_n_live": len(bare_judged),
                    "bare_min_completed": bare_min,
                    "bare_max_completed": bare_max,
                    "difficulty_band": classified_difficulty,
                    "classified_difficulty": classified_difficulty,
                    "authoring_difficulty": difficulty,
                    "allowed_difficulties": sorted(allowed_difficulties),
                    "max_depth": max_depth, "agree_fixes": agree_fixes,
                    "cave_fixes": cave_fixes}
        # stabilize loop broke out (escalate) -> continue to next tier

    rsn = "hint tiers exhausted"
    rec.event("gate", "reject", verdict="reject_unsolvable", reason=rsn,
              iteration=reauthor, n_failed_live=cum_failed_live)
    return {"verdict": "reject_unsolvable", "reason": rsn,
            "n_failed_live": cum_failed_live, "n_completed": 0}


def _attach(jr: dict, run) -> dict:
    jr["cell"], jr["run"] = run.cell, run
    jr["steps"] = (run.stats or {}).get("steps")
    return jr


def _agent_summary(run: "ens.CellRun") -> dict:
    """Compact per-agent summary for a timeline event."""
    st = run.stats or {}
    probe = getattr(run, "completion_probe", {}) or {}
    return {"cell": run.cell, "harness": run.harness, "model": run.model,
            "group": getattr(run, "prompt_group", "") or "hinted",
            "status": run.status, "seconds": run.seconds,
            "attempts": getattr(run, "attempts", 1),
            "llm_calls": st.get("llm_calls"), "steps": st.get("steps"),
            "tool_calls": st.get("tool_calls"), "jumps": st.get("jumps"),
            "interfaces": st.get("interfaces"),
            "budget_end": st.get("budget_end"),
            "completion_probe_done": probe.get("done"),
            "completion_probe_nudges": probe.get("nudges_used")}


def _agent_record(run: "ens.CellRun") -> dict:
    """Full per-agent record for the agents/ bundle file (verdicts live on judge events)."""
    return {**_agent_summary(run), "world_id": run.world_id,
            "before_dir": run.before_dir, "after_dir": run.after_dir,
            "trajectory_dir": run.trajectory_dir, "error": run.error,
            "reply": (run.reply or ""),
            "completion_probe": getattr(run, "completion_probe", {}) or {}}


def _pmap(fn, items: list):
    """Run fn over items concurrently, preserving input order."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=len(items)) as ex:
        return list(ex.map(fn, items))


def _judge_all(out_dir, live, profile, day, rec=None, *, judge_pass=0,
               tier=0) -> list:
    """Judge every live run concurrently, re-judging any `infra` verdict once."""
    judged = _pmap(lambda r: _attach(
        judge_run(out_dir, r, profile, day, rec, judge_pass=judge_pass, tier=tier), r), live)
    stuck = [j for j in judged if j["verdict"] == "infra"]
    if stuck:
        _log(f"[7/9] re-judging {len(stuck)} run(s) that hit judge-infra")
        fixed = {j2["cell"]: j2 for j2 in
                 _pmap(lambda j: _attach(
                     judge_run(out_dir, j["run"], profile, day, rec,
                               judge_pass=judge_pass, tier=tier), j["run"]),
                       stuck)}
        judged = [fixed.get(j["cell"], j) if j["verdict"] == "infra" else j
                  for j in judged]
    return judged


def _jslim(j: dict) -> dict:
    # Drop bulky/non-serializable fields before persisting to the report.
    return {k: v for k, v in j.items()
            if k not in ("rule_result", "run", "heavy_call")}


def _difficulty_band(result: dict) -> str | None:
    """Difficulty band for the replay HUD, derived from the configured bare-rollout completion band."""
    v = result.get("verdict") or ""
    if v.startswith("reject_too_easy"):
        return "too_easy"
    if v.startswith("reject_unsolvable"):
        return "unsolvable"
    if v.startswith("reject_too_hard"):
        return "too_hard"
    if v in ("reject_verifier_disagree", "reject_verifier_fragile"):
        return "verifier_defect"
    if v == "reject_interface_bypass":
        return "interface_bypass"
    if v == "reject_world_imperfect_unresolved":
        return "world_imperfect"
    if not str(v).startswith("accept"):
        return None
    return result.get("difficulty_band")


def _terminal_decision_type(verdict: str | None) -> str:
    v = str(verdict or "")
    if v.startswith("accept"):
        return "accept"
    if v == "error_infra":
        return "abort"
    if v == "error":
        return "error"
    return "reject"


def _finish(report, out_dir, scratch, instances, verdict, reason) -> dict:
    report["verdict"] = verdict
    report["reason"] = reason
    try:
        if out_dir.exists():
            (out_dir / "_synthesis_report.json").write_text(json.dumps(report, indent=2))
    except Exception:                                  # noqa: BLE001
        pass
    for inst in instances:
        try:
            prov.teardown(inst)
        except Exception:                              # noqa: BLE001
            pass
    shutil.rmtree(scratch, ignore_errors=True)
    _log(f"[9/9] verdict={verdict} — {reason}")
    return report


@dataclass
class OrchArgs:
    profile: str = ""
    day: int = -1
    difficulty: str = "medium"
    slug: str = ""
    prior: str = ""
    require_web: bool = True


def main() -> int:
    a = parse_args(OrchArgs)
    if not a.profile or a.day < 0 or not a.slug:
        sys.exit("--profile, --day (>=0) and --slug are required")
    prior = load_jsonc(a.prior) if a.prior and Path(a.prior).exists() else []
    rep = synthesize_one(a.profile, a.day, a.difficulty, a.slug,
                         prior_tasks=prior, require_web=a.require_web)
    print("\n" + json.dumps(rep, indent=2)[:6000])
    return 0 if rep.get("verdict", "").startswith("accept") else 1


if __name__ == "__main__":
    raise SystemExit(main())
