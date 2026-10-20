"""`awm syn world`: serial plan, implement and pre-bake per day (day N needs day N-1)."""
from __future__ import annotations

from tqdm import tqdm

from wildawm.synthesis.worlds import dayimpl, dayplan, prebake


def run_world(profile_id: str, *, to_day: int, force: bool = False,
              force_plan: bool = False, force_impl: bool = False) -> dict:
    """Plan, implement and pre-bake days 0..to_day for ``profile_id``; return per-day results."""
    if to_day < 0:
        raise ValueError("to_day must be >= 0")
    results = {}
    for day in tqdm(range(0, to_day + 1), desc=f"world[{profile_id}]"):
        plan = dayplan.synthesize(profile_id, day, force=force or force_plan)
        impl = dayimpl.synthesize(profile_id, day, force=force or force_impl)
        bake = prebake.prebake(profile_id, to_day=day, force=False)
        results[day] = {"plan": str(plan), "impl": str(impl),
                        "masters": bake["masters_dir"], "days_json": bake["days_json"]}
    return results
