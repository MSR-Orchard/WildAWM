#!/usr/bin/env python3
"""Replayable synthesis log: an ordered event timeline, aggregate summary and raw histories."""
import json
import os
import shutil
import threading
import time
from pathlib import Path

# Bump on breaking changes to the event set or aggregate shape; viewers key off it.
SCHEMA_VERSION = "3.0"


def _utc_iso(ts: float) -> str:
    """ISO-8601 UTC stamp for an epoch seconds value (viewer-friendly axis)."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    except Exception:                                       # noqa: BLE001
        return ""


def _jsonable(obj):
    """Best-effort JSON-serializable form of `obj` (to_dict() or str() fallback); never raises."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict())
        except Exception:                                   # noqa: BLE001
            pass
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


class SynthesisLog:
    """Crash-proof recorder for one task synthesis attempt, bundled under _synthesis_log/."""

    DIRNAME = "_synthesis_log"

    def __init__(self, out_dir, *, profile="", day=None, difficulty="",
                 slug="", enabled=True):
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._seq = 0
        self.timeline: list[dict] = []
        # Budget-bearing loop counters, so a viewer can show "agree_fix 2/3" without the timeline.
        self.counters: dict = {
            "events": 0, "sketch_rounds": 0, "author_attempts": 0,
            "validate_runs": 0, "fixer_runs": 0, "ensemble_draws": 0,
            "ensemble_cells": 0, "ensemble_live_cells": 0, "infra_cells": 0,
            "judge_passes": 0, "agree_fixes": 0, "cave_fixes": 0,
            "probe_runs": 0, "probe_caves": 0, "deepen_used": 0, "ease_used": 0,
            "interface_reauthor_used": 0, "hint_tiers": 0,
            "world_repair_tickets": 0, "infra_resamples": 0,
        }
        self.summary: dict = {}        # aggregate, finalized at flush time
        self.decision: dict = {}       # terminal verdict/reason/band
        self.t0 = time.time()
        self.task = {"profile": profile, "day": day, "difficulty": difficulty,
                     "slug": slug,
                     "started_at": _utc_iso(self.t0), "started_epoch": self.t0}
        # A None/invalid out_dir disables the recorder.
        self.dir = None
        self.gen_dir = None
        self.ensemble_dir = self.judge_dir = self.probe_dir = None
        try:
            if self.enabled and out_dir is not None:
                self.dir = Path(out_dir) / self.DIRNAME
                self.gen_dir = self.dir / "generator"
                self.ensemble_dir = self.dir / "ensemble"
                self.judge_dir = self.dir / "judge"
                self.probe_dir = self.dir / "probe"
        except Exception:                                   # noqa: BLE001
            self.enabled = False

    def event(self, phase: str, type: str, **data) -> dict | None:
        """Append one timestamped timeline event and flush; returns the event or None."""
        if not self.enabled:
            return None
        try:
            with self._lock:
                self._seq += 1
                now = time.time()
                ev = {"seq": self._seq, "phase": phase, "type": type,
                      "t": round(now - self.t0, 3), "epoch": round(now, 3),
                      "iso": _utc_iso(now)}
                # Sanitize each value so an exotic payload cannot break the flush.
                for k, v in data.items():
                    ev[k] = _jsonable(v)
                self.timeline.append(ev)
                self.counters["events"] = len(self.timeline)
                self._flush_locked()
                return ev
        except Exception:                                   # noqa: BLE001
            return None

    def bump(self, name: str, by: int = 1) -> None:
        """Increment a named aggregate counter (HUD value), crash-proof."""
        if not self.enabled:
            return
        try:
            with self._lock:
                self.counters[name] = self.counters.get(name, 0) + by
        except Exception:                                   # noqa: BLE001
            pass

    def set_counter(self, name: str, value) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                self.counters[name] = value
        except Exception:                                   # noqa: BLE001
            pass

    def save_raw(self, subdir: str, name: str, payload) -> str | None:
        """Persist a raw history blob and return its bundle-relative pointer, or None on failure."""
        if not self.enabled or self.dir is None:
            return None
        try:
            safe = _safe(name)
            dest = self.dir / subdir
            dest.mkdir(parents=True, exist_ok=True)
            path = dest / safe
            blob = json.dumps(_jsonable(payload), ensure_ascii=False, indent=1)
            _atomic_write_text(path, blob)
            return f"{subdir}/{safe}"
        except Exception:                                   # noqa: BLE001
            return None

    def save_generator(self, name: str, call) -> str | None:
        """Save a generator-side raw history (sketch/author/fixer CallRecord)."""
        return self.save_raw("generator", name, call)

    def save_stage_json(self, rel_path: str, payload) -> str | None:
        """Persist JSON at a sanitized, structured bundle-relative path."""
        if not self.enabled or self.dir is None:
            return None
        try:
            parts = [_safe(p) for p in Path(rel_path).parts if p not in ("", ".")]
            if not parts:
                return None
            path = self.dir.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            blob = json.dumps(_jsonable(payload), ensure_ascii=False, indent=1)
            _atomic_write_text(path, blob)
            return "/".join(parts)
        except Exception:                                   # noqa: BLE001
            return None

    def save_stage_tree(self, rel_path: str, src) -> str | None:
        """Copy a directory/file into the replay bundle and return its pointer; never raises."""
        if not self.enabled or self.dir is None or not src:
            return None
        try:
            src = Path(src)
            if not src.exists():
                return None
            parts = [_safe(p) for p in Path(rel_path).parts if p not in ("", ".")]
            if not parts:
                return None
            dest = self.dir.joinpath(*parts)
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    dest.unlink(missing_ok=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dest, symlinks=True)
            else:
                shutil.copy2(src, dest)
            return "/".join(parts)
        except Exception:                                   # noqa: BLE001
            return None

    def save_ensemble(self, rel_name: str, payload) -> str | None:
        """Save an ensemble-level JSON record under ensemble/."""
        return self.save_stage_json(f"ensemble/{rel_name}", payload)

    def save_ensemble_cell(self, *, iteration: int, tier: int, cell: str,
                           manifest: dict, before_dir=None, after_dir=None,
                           trajectory_dir=None) -> dict:
        """Archive one solver cell's replay bundle; returns bundle-relative pointers."""
        base = f"ensemble/i{iteration}_t{tier}/{_safe(cell)}"
        ptrs = {
            "manifest": self.save_stage_json(f"{base}/manifest.json", manifest),
            "before": self.save_stage_tree(f"{base}/before", before_dir),
            "after": self.save_stage_tree(f"{base}/after", after_dir),
            "trajectory": self.save_stage_tree(f"{base}/trajectory", trajectory_dir),
        }
        return {k: v for k, v in ptrs.items() if v}

    def save_judge_history(self, *, judge_pass: int, tier: int, cell: str,
                           kind: str, payload) -> str | None:
        """Save lightweight/heavy judge raw trajectory under judge/."""
        rel = f"judge/p{judge_pass}_t{tier}/{_safe(cell)}/{_safe(kind)}.json"
        return self.save_stage_json(rel, payload)

    def save_probe_history(self, *, judge_pass: int, tier: int, cell: str,
                           payload) -> str | None:
        """Save blind/gaslight probe call list under probe/."""
        rel = f"probe/p{judge_pass}_t{tier}/{_safe(cell)}/calls.json"
        return self.save_stage_json(rel, payload)

    def finalize(self, verdict: str, reason: str, *, difficulty_band=None,
                 extra: dict | None = None) -> None:
        """Record the terminal decision and aggregate summary, then flush; call once at exit."""
        if not self.enabled:
            return
        try:
            with self._lock:
                now = time.time()
                self.decision = {"verdict": verdict, "reason": reason,
                                 "difficulty_band": difficulty_band,
                                 "accepted": str(verdict or "").startswith("accept"),
                                 "ended_at": _utc_iso(now), "ended_epoch": round(now, 3),
                                 "wall_seconds": round(now - self.t0, 2)}
                if extra:
                    self.decision.update(_jsonable(extra))
                self.summary = self._build_summary_locked()
                self._flush_locked()
        except Exception:                                   # noqa: BLE001
            pass

    def _build_summary_locked(self) -> dict:
        """Aggregate summary built from the counters and decision."""
        c = self.counters
        return {
            "n_events": len(self.timeline),
            "wall_seconds": round(time.time() - self.t0, 2),
            "sketch_rounds": c.get("sketch_rounds", 0),
            "author_attempts": c.get("author_attempts", 0),
            "validate_runs": c.get("validate_runs", 0),
            "fixer_runs": c.get("fixer_runs", 0),
            "ensemble_draws": c.get("ensemble_draws", 0),
            "ensemble_cells": c.get("ensemble_cells", 0),
            "ensemble_live_cells": c.get("ensemble_live_cells", 0),
            "infra_cells": c.get("infra_cells", 0),
            "infra_resamples": c.get("infra_resamples", 0),
            "hint_tiers_used": c.get("hint_tiers", 0),
            "judge_passes": c.get("judge_passes", 0),
            "agree_fixes": c.get("agree_fixes", 0),
            "cave_fixes": c.get("cave_fixes", 0),
            "probe_runs": c.get("probe_runs", 0),
            "probe_caves": c.get("probe_caves", 0),
            "deepen_used": c.get("deepen_used", 0),
            "ease_used": c.get("ease_used", 0),
            "interface_reauthor_used": c.get("interface_reauthor_used", 0),
            "world_repair_tickets": c.get("world_repair_tickets", 0),
        }

    def to_dict(self) -> dict:
        """The full bundle manifest = the procedure.json contents."""
        return {"schema_version": SCHEMA_VERSION, "task": self.task,
                "summary": self.summary or self._build_summary_locked(),
                "counters": self.counters, "decision": self.decision,
                "timeline": self.timeline}

    def _flush_locked(self) -> None:
        """Atomically rewrite procedure.json under self._lock; never raises."""
        if self.dir is None:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            blob = json.dumps(self.to_dict(), ensure_ascii=False, indent=1)
            _atomic_write_text(self.dir / "procedure.json", blob)
        except Exception:                                   # noqa: BLE001
            pass

    def flush(self) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                self._flush_locked()
        except Exception:                                   # noqa: BLE001
            pass


_TMP_SEQ = 0
_TMP_LOCK = threading.Lock()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically via a unique temp file and os.replace."""
    global _TMP_SEQ
    with _TMP_LOCK:
        _TMP_SEQ += 1
        seq = _TMP_SEQ
    tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.{seq}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _safe(s: str) -> str:
    """Filesystem-safe filename fragment (matches orchestrator._safe_name)."""
    out = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (s or ""))
    return out.strip("_") or "x"


class _NullLog:
    """No-op stand-in for SynthesisLog so call sites need no guards."""

    enabled = False

    def event(self, *a, **k):
        return None

    def bump(self, *a, **k):
        return None

    def set_counter(self, *a, **k):
        return None

    def save_raw(self, *a, **k):
        return None

    def save_generator(self, *a, **k):
        return None

    def save_stage_json(self, *a, **k):
        return None

    def save_stage_tree(self, *a, **k):
        return None

    def save_ensemble(self, *a, **k):
        return None

    def save_ensemble_cell(self, *a, **k):
        return {}

    def save_judge_history(self, *a, **k):
        return None

    def save_probe_history(self, *a, **k):
        return None

    def finalize(self, *a, **k):
        return None

    def flush(self, *a, **k):
        return None

    def to_dict(self):
        return {}


def make_recorder(out_dir, **kw) -> SynthesisLog:
    """Construct a recorder, falling back to a no-op on any error."""
    try:
        return SynthesisLog(out_dir, **kw)
    except Exception:                                       # noqa: BLE001
        return _NullLog()           # type: ignore[return-value]
