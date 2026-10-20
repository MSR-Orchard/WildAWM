"""Pre-baking: materialize cumulative day-N snapshots by replaying each day's delta.sh serially."""
from __future__ import annotations

import shutil
from pathlib import Path

from tqdm import tqdm

from wildawm import const_config as cfg
from wildawm.synthesis.worlds import anchors
from wildawm.synthesis.worlds import primitives as pri
from wildawm.synthesis.worlds.dayimpl import WORLD_HASH_FILE, _materialize_day, day_script_dir

SCRIPTS_PLACEHOLDER_NOTE = "placeholder: every day snapshot is pre-materialized, so no build scripts are needed"


def master_dir(profile_id: str) -> Path:
    return cfg.WS_MASTERS_DIR / profile_id


def built_days(profile_id: str) -> list[int]:
    """Days that have an authored delta.sh (day_0..day_N)."""
    sd = cfg.WS_SCRIPTS_DIR / profile_id
    if not sd.is_dir():
        return []
    days = []
    for d in sorted(sd.iterdir()):
        if d.is_dir() and d.name.startswith("day_") and (d / "delta.sh").exists():
            try:
                days.append(int(d.name.split("_", 1)[1]))
            except ValueError:
                continue
    return sorted(days)


def prebake(profile_id: str, *, to_day: int | None = None,
            force: bool = False) -> dict:
    """Materialize days 0..N into master snapshots plus days.json; resumable unless ``force``."""
    days = built_days(profile_id)
    if to_day is not None:
        days = [d for d in days if d <= to_day]
    if not days:
        raise RuntimeError(f"no built days for {profile_id} — run `awm syn dayimpl` first.")
    masters = master_dir(profile_id)
    masters.mkdir(parents=True, exist_ok=True)
    (masters / "scripts").mkdir(exist_ok=True)
    (masters / "scripts" / "README.txt").write_text(SCRIPTS_PLACEHOLDER_NOTE + "\n")

    days_map: dict[int, str] = {}
    for k in tqdm(days, desc=f"prebake[{profile_id}]"):
        dest = masters / f"day_{k}"
        if dest.exists() and not force:
            days_map[k] = anchors.day_date(profile_id, k)
            continue
        prev = (masters / f"day_{k - 1}") if k > 0 else None
        if k > 0 and not (prev and prev.is_dir()):
            raise RuntimeError(f"day {k - 1} master missing — prebake is serial; build day {k - 1} first.")
        frozen = anchors.day_date(profile_id, k)
        try:
            _materialize_day(prev, day_script_dir(profile_id, k), frozen, dest)
        except RuntimeError as e:
            shutil.rmtree(dest, ignore_errors=True)
            raise RuntimeError(f"prebake day {k} failed: {e}") from e
        # Replay must reproduce the hash the gate recorded for this day.
        expected = day_script_dir(profile_id, k) / WORLD_HASH_FILE
        if expected.exists():
            got = pri.world_hash(dest)
            want = expected.read_text().strip()
            if got != want:
                shutil.rmtree(dest, ignore_errors=True)
                raise RuntimeError(
                    f"prebake day {k}: materialized world {got[:12]} != the world its gate accepted "
                    f"({want[:12]}). The day N-1 master or this day's scripts changed after the gate; "
                    f"the master is not reproducible from delta.sh and was not written.")
        days_map[k] = frozen
        # Written per day so an interrupted prebake can resume.
        pri.write_days(masters, days_map)
    pri.write_days(masters, days_map)
    return {"days": days, "masters_dir": str(masters), "days_json": str(anchors.days_json_path(profile_id))}


def verify_rebake(profile_id: str) -> dict:
    """Re-materialize every built day in a temp tree and compare each world_hash to its master."""
    import tempfile
    days = built_days(profile_id)
    out: dict[int, dict] = {}
    work = Path(tempfile.mkdtemp(prefix="ws-rebake-"))
    try:
        for k in days:
            # Independent chain: never copy from the master, or the re-bake check is meaningless.
            prev = (work / f"day_{k - 1}") if k > 0 else None
            dest = work / f"day_{k}"
            _materialize_day(prev if (prev and prev.exists()) else None,
                             day_script_dir(profile_id, k),
                             anchors.day_date(profile_id, k), dest)
            master = master_dir(profile_id) / f"day_{k}"
            h_new = pri.world_hash(dest)
            h_master = pri.world_hash(master)
            out[k] = {"match": h_new == h_master, "hash": h_new[:12], "master_hash": h_master[:12]}
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)
