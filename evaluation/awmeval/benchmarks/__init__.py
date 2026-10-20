from __future__ import annotations

import importlib

BENCHMARKS = {
    "online_mind2web": "online_mind2web:OnlineMind2Web",
    "webarena": "webarena:WebArena",
    "qwenclawbench": "qwenclawbench:QwenClawBench",
    "claweval": "claweval:ClawEval",
    "cua_gym": "cua_gym:CUAGym",
    "osworld_verified": "osworld_verified:OSWorldVerified",
    "osworld2": "osworld2:OSWorld2",
    "ale": "ale:AgentsLastExam",
    "wildawm": "wildawm:WildAWM",
}


def load(name: str):
    if name not in BENCHMARKS:
        raise SystemExit(f"unknown benchmark {name!r}; choose from: {', '.join(BENCHMARKS)}")
    module, cls = BENCHMARKS[name].split(":")
    return getattr(importlib.import_module(f"{__name__}.{module}"), cls)
