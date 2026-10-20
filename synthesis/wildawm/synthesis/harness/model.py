#!/usr/bin/env python3
"""Per-role harness and model resolution (AWM_SYNTH_<ROLE>_{HARNESS,MODEL,ENDPOINT,EFFORT})."""
import os

from wildawm import const_config as cfg
from wildawm.synthesis.harness.opencode import OpencodeModel

ROLES = ("sketch", "generator", "heavy_judge", "fixer", "probe",
         "world_patch_generator", "world_diagnostic", "task_skill_writer",
         # World-synthesis roles.
         "profile_creator", "day_planner", "skill_creator", "app_creator",
         "day_implementer", "world_reviewer", "diversity_judge")

_ROLE_PREFIX = {
    "sketch": "SKETCH",
    "generator": "GENERATOR",
    "heavy_judge": "HEAVY_JUDGE",
    "fixer": "FIXER",
    "probe": "PROBE",
    "probe_blind": "PROBE",
    "probe_gaslight": "PROBE",
    "world_patch_generator": "WORLD_PATCH_GENERATOR",
    "world_diagnostic": "WORLD_DIAGNOSTIC",
}


def _role_env(role: str, key: str, default: str = "") -> str:
    prefix = _ROLE_PREFIX.get(role, role.upper())
    return os.environ.get(f"AWM_SYNTH_{prefix}_{key}", default)


def resolve_harness(role: str) -> str:
    """The harness ("opencode" | "claude-code") for a role."""
    return _role_env(role, "HARNESS", cfg.SYNTH_HARNESS)


def opencode_model_from_ref(ref: str | None, *,
                            effort: str | None = None) -> OpencodeModel:
    """Build an OpencodeModel from a 'provider/model' ref or a bare model id."""
    ref = ref or cfg.OPENCODE_LLM_REF
    if "/" in ref:
        spec = cfg.model_spec(ref)
        model, endpoint = spec["api_model"], spec["base_url"]
        api_key, provider_id = spec["api_key"], spec["provider"]
        model_ref = spec["ref"]
    else:
        endpoint_spec = cfg.model_spec(cfg.OPENCODE_LLM_REF)
        model, endpoint = ref, endpoint_spec["base_url"]
        api_key, provider_id = endpoint_spec["api_key"], endpoint_spec["provider"]
        model_ref = f"{provider_id}/{ref}"
        try:
            spec = cfg.model_spec(model_ref)
        except (KeyError, TypeError, ValueError):
            # Unregistered bare model: fall back to the default provider's limits.
            spec = endpoint_spec
    policy = cfg.reasoning_effort_policy(spec, effort, harness="opencode")
    return OpencodeModel(model=model, base_url=cfg.container_host_url(endpoint),
                         upstream_base_url=endpoint,
                         api_key=api_key,
                         effort=(policy or {}).get("effort", ""),
                         provider_id=provider_id,
                         ref=model_ref,
                         reasoning_policy=policy,
                         context_window=spec.get("context_window"))


def resolve_opencode_model(role: str) -> OpencodeModel:
    """Resolve a role's opencode model target, honouring per-role env overrides."""
    model_ref = _role_env(role, "MODEL") or cfg.OPENCODE_LLM_REF
    endpoint_override = _role_env(role, "ENDPOINT")
    if "/" in model_ref:
        spec = cfg.model_spec(model_ref)
        model = spec["api_model"]
        endpoint = endpoint_override or spec["base_url"]
        api_key, provider_id = spec["api_key"], spec["provider"]
        resolved_ref = spec["ref"]
    else:
        endpoint_spec = cfg.model_spec(cfg.OPENCODE_LLM_REF)
        model = model_ref
        endpoint = endpoint_override or endpoint_spec["base_url"]
        api_key, provider_id = endpoint_spec["api_key"], endpoint_spec["provider"]
        resolved_ref = f"{provider_id}/{model_ref}"
        try:
            spec = cfg.model_spec(resolved_ref)
        except (KeyError, TypeError, ValueError):
            spec = endpoint_spec
    effort = _role_env(role, "EFFORT") or None
    policy = cfg.reasoning_effort_policy(spec, effort, harness="opencode")
    return OpencodeModel(model=model, base_url=cfg.container_host_url(endpoint),
                         upstream_base_url=endpoint,
                         api_key=api_key,
                         effort=(policy or {}).get("effort", ""),
                         provider_id=provider_id,
                         ref=resolved_ref,
                         reasoning_policy=policy,
                         context_window=spec.get("context_window"))
