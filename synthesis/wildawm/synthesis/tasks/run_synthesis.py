#!/usr/bin/env python3
"""Task synthesis driver: days run concurrently, attempts within a day sequentially; resumable."""
import re
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from simpleArgParser import parse_args

from wildawm import const_config as cfg
from wildawm.synthesis.tasks import orchestrator as orch
from wildawm.synthesis.tasks import tasks_index as tx
from wildawm.synthesis.tasks import xai_pool


@dataclass
class EnsembleCfg:
    """Ensemble solver settings."""
    ensemble_n: int = cfg.ENSEMBLE_N
    solve_timeout_s: int = cfg.SOLVE_TIMEOUT_S


@dataclass
class GateCfg:
    """Acceptance-gate settings."""
    step_gate: int = cfg.STEP_GATE
    live_test_n: int = cfg.LIVE_TEST_N
    bare_medium_max_completed: int = cfg.BARE_MEDIUM_MAX_COMPLETED
    bare_hard_max_completed: int = cfg.BARE_HARD_MAX_COMPLETED
    max_deepen: int = cfg.MAX_DEEPEN
    max_gen_iters: int = cfg.MAX_GEN_ITERS


@dataclass
class SynthArgs:
    profile: str = ""
    days: str = "1-14"
    slots: str = "hard:h1,hard:h2,medium:m1"
    day_parallel: int = 3
    redo_rejected: bool = False
    force: bool = False
    ensemble: EnsembleCfg = field(default_factory=EnsembleCfg)
    gate: GateCfg = field(default_factory=GateCfg)


def parse_days(spec: str) -> list[int]:
    """Parse day ranges such as '1-14', '1,3,5', or '1-3,7'."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return sorted(out)


def slug_prefix(profile: str) -> str:
    """Return a stable collision-resistant auto-slug prefix for a profile."""
    head = (profile or "").split("-", 1)[0].lower()
    if len(head) >= 6 and all(c in "0123456789abcdef" for c in head[:6]):
        return f"{head[:6]}-d"
    normalized = re.sub(r"[^a-z0-9]+", "", (profile or "x").lower())[:6] or "x"
    return f"{normalized}-d"


def parse_slots(template: str, profile: str, day: int) -> list[dict]:
    """Expand a per-day quota template ('difficulty:suffix[:web|:noweb]') into output specs."""
    prefix = slug_prefix(profile)
    slots = []
    for tok in template.split(","):
        parts = tok.split(":")
        diff, suffix = parts[0].strip(), parts[1].strip()
        slug = suffix if suffix.startswith(f"{prefix}{day}-") else f"{prefix}{day}-{suffix}"
        suffix_flags = {p.strip().lower() for p in parts[2:]}
        slots.append({"difficulty": diff, "slug": slug,
                      "require_web": "noweb" not in suffix_flags and "no-web" not in suffix_flags})
    return slots


def _difficulty_quotas(slots: list[dict]) -> dict[str, int]:
    counts = Counter(str(s.get("difficulty") or "").strip().lower() for s in slots)
    invalid = sorted(d for d in counts if d not in {"hard", "medium"})
    if invalid:
        raise ValueError(f"unsupported task difficulty quota(s): {', '.join(invalid)}")
    return {"hard": counts.get("hard", 0), "medium": counts.get("medium", 0)}


def _accepted_result_difficulty(task_dir) -> str | None:
    difficulty = str(tx.prior_entry(task_dir).get("difficulty") or "").strip().lower()
    return difficulty if difficulty in {"hard", "medium"} else None


def apply_overrides(a: SynthArgs) -> None:
    """Apply CLI group values to the shared synthesis config."""
    cfg.ENSEMBLE_N = a.ensemble.ensemble_n
    cfg.SOLVE_TIMEOUT_S = a.ensemble.solve_timeout_s
    cfg.STEP_GATE = a.gate.step_gate
    cfg.LIVE_TEST_N = a.gate.live_test_n
    cfg.BARE_MEDIUM_MAX_COMPLETED = a.gate.bare_medium_max_completed
    cfg.BARE_HARD_MAX_COMPLETED = a.gate.bare_hard_max_completed
    cfg.MAX_DEEPEN = a.gate.max_deepen
    cfg.MAX_GEN_ITERS = a.gate.max_gen_iters


def run_day_slots(profile: str, day: int, slots: list[dict], *,
                  redo_rejected: bool = False, force: bool = False,
                  log=print) -> list[tuple[str, str]]:
    """Run a day's attempts sequentially against its quotas; returns [(slug, verdict)]."""
    priors = tx.gather_global_priors()
    target_quotas = _difficulty_quotas(slots)
    accepted_counts = {"hard": 0, "medium": 0}
    log(
        f"[day {day}] start; {len(priors)} global prior accepted task(s); "
        f"accepted-result quotas hard={target_quotas['hard']} "
        f"medium={target_quotas['medium']}"
    )
    results: list[tuple[str, str]] = []
    pending: list[dict] = []
    for s in slots:
        slug = s["slug"]
        rp = tx.report_path(profile, day, slug)
        if tx.is_accepted(rp) and not force:
            if tx.is_quality_prior(rp.parent):
                classified = _accepted_result_difficulty(rp.parent)
                if classified:
                    accepted_counts[classified] += 1
                log(
                    f"[day {day}] skip {slug}: already accepted"
                    + (f" as {classified}" if classified else "")
                )
                results.append((slug, "accept"))
                continue
            problems = tx.quality_prior_problems(rp.parent)
            preview = "; ".join(problems[:3]) if problems else "unknown quality gate failure"
            log(
                f"[day {day}] redo {slug}: accepted task fails the quality checks "
                f"({preview})"
            )
            pending.append(s)
            continue
        if rp.exists() and not redo_rejected:
            log(f"[day {day}] skip {slug}: prior verdict {tx.verdict_of(rp)} "
                f"(use --redo_rejected to retry)")
            results.append((slug, tx.verdict_of(rp) or "?"))
            continue
        pending.append(s)
    if not pending:
        return results

    owner = f"{cfg.INSTANCE_PREFIX}-xai-{profile}-day{day}-{uuid.uuid4().hex[:8]}"
    try:
        reserve = orch.reserve_day_candidates(
            profile, day, pending, prior_tasks=priors, owner=owner, log=log)
    except Exception as e:  # noqa: BLE001
        reason = f"reserve-many infra failure: {type(e).__name__}: {e}"
        log(f"[day {day}] {reason}")
        results.extend((s["slug"], "error_infra") for s in pending)
        return results
    for dropped in reserve.get("dropped") or []:
        log(f"[day {day}] proposal dropped: {dropped.get('_drop_reason', dropped)}")
    for rej in reserve.get("rejected") or []:
        log(f"[day {day}] reserve rejected {rej.get('title')}: {'; '.join(rej.get('reasons') or [])}")
    locked = reserve.get("locked") or []
    if not locked:
        call = reserve.get("call") or {}
        log(
            f"[day {day}] reserve-many produced no locks: "
            f"reason={reserve.get('reason') or 'none'} "
            f"call_status={call.get('status') or 'n/a'} "
            f"dropped={len(reserve.get('dropped') or [])} "
            f"rejected={len(reserve.get('rejected') or [])}"
        )
        for idx, drop in enumerate((reserve.get("dropped") or [])[:5], 1):
            log(f"[day {day}] dropped[{idx}]: {drop.get('_drop_reason')}")
        for idx, rej in enumerate((reserve.get("rejected") or [])[:5], 1):
            reasons = "; ".join(str(r) for r in (rej.get("reasons") or []))
            log(f"[day {day}] rejected[{idx}]: {reasons}")
    locked_slugs = {r.get("slug") for r in locked}
    for s in pending:
        if s["slug"] not in locked_slugs:
            results.append((s["slug"], "skipped_capacity"))
    abort_after: str | None = None
    for locked_task in locked:
        slug = locked_task["slug"]
        authoring_difficulty = str(
            locked_task.get("difficulty") or "hard").strip().lower()
        remaining = {
            difficulty
            for difficulty, target in target_quotas.items()
            if accepted_counts[difficulty] < target
        }
        if not remaining:
            results.append((slug, "skipped_quota_filled"))
            try:
                xai_pool.release_remote(
                    locked_task["lock_id"], owner=owner,
                    reason="day accepted-result quotas already filled")
            except Exception as e:  # noqa: BLE001
                log(f"[day {day}] lock release failed for {slug}: {e}")
            continue
        rp = tx.report_path(profile, day, slug)
        log(
            f"[day {day}] === adaptive {slug} [authoring={authoring_difficulty}; "
            f"remaining={','.join(sorted(remaining))}; cli+mcp+web reserved] "
            f"(static_accepted_priors={len(priors)} "
            f"lock={locked_task.get('lock_id')}) ==="
        )
        with xai_pool.heartbeat_loop(locked_task.get("lock_id"), owner=owner):
            rep = orch.synthesize_one(
                profile, day, authoring_difficulty, slug,
                prior_tasks=priors, require_web=True,
                reserved_sketch=locked_task,
                allowed_difficulties=remaining)
        v = rep.get("verdict")
        results.append((slug, v))
        classified = str(
            rep.get("difficulty_band") or rep.get("difficulty") or ""
        ).strip().lower()
        log(
            f"[day {day}] {slug}: verdict={v}"
            + (f" classified={classified}" if classified else "")
            + f" — {rep.get('reason','')}"
        )
        if v in tx.ACCEPTED:
            if classified not in remaining:
                raise RuntimeError(
                    f"accepted task {slug} classified {classified!r}, outside "
                    f"remaining day quota {sorted(remaining)}")
            accepted_counts[classified] += 1
            try:
                xai_pool.finalize_remote(locked_task["lock_id"], owner=owner, verdict=v)
            except Exception as e:  # noqa: BLE001
                log(f"[day {day}] lock finalize failed for {slug}: {e}")
            priors.append(tx.prior_entry(rp.parent))
        else:
            try:
                xai_pool.release_remote(locked_task["lock_id"], owner=owner, reason=v or "")
            except Exception as e:  # noqa: BLE001
                log(f"[day {day}] lock release failed for {slug}: {e}")
        if v == "error_infra":
            log(f"[day {day}] ABORT: infra failure — stopping the day")
            abort_after = slug
            break
    if abort_after:
        started = {slug for slug, _ in results}
        for locked_task in locked:
            if locked_task["slug"] in started:
                continue
            try:
                xai_pool.release_remote(
                    locked_task["lock_id"], owner=owner,
                    reason=f"day aborted after {abort_after}")
            except Exception as e:  # noqa: BLE001
                log(f"[day {day}] lock release failed for unstarted {locked_task['slug']}: {e}")
    return results


def run_multi_day(a: SynthArgs, log=print) -> dict:
    days = parse_days(a.days)
    log(f"[driver] profile={a.profile} days={days} day_parallel={a.day_parallel} "
        f"ensemble_n={cfg.ENSEMBLE_N} live_test_n={cfg.LIVE_TEST_N} "
        f"step_gate={cfg.STEP_GATE}")
    summary: dict[int, list] = {}

    def _one_day(day: int):
        slots = parse_slots(a.slots, a.profile, day)
        return day, run_day_slots(a.profile, day, slots,
                                  redo_rejected=a.redo_rejected,
                                  force=a.force,
                                  log=lambda m: log(m))

    with ThreadPoolExecutor(max_workers=max(1, a.day_parallel)) as ex:
        futs = [ex.submit(_one_day, d) for d in days]
        for fu in as_completed(futs):
            try:
                day, res = fu.result()
                summary[day] = res
                log(f"[driver] day {day} DONE: " +
                    ", ".join(f"{s}={v}" for s, v in res))
            except Exception as e:                     # noqa: BLE001
                log(f"[driver] a day failed: {type(e).__name__}: {e}")
    accepted = sum(1 for res in summary.values() for _, v in res
                   if v in tx.ACCEPTED)
    log(f"[driver] ALL DONE — {accepted} accepted across {len(days)} day(s)")
    return {"days": days, "accepted": accepted, "summary": summary}


def main() -> int:
    a = parse_args(SynthArgs)
    if not a.profile:
        raise SystemExit("--profile is required")
    apply_overrides(a)

    def log(m):
        print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)
    rep = run_multi_day(a, log=log)
    return 0 if rep["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
