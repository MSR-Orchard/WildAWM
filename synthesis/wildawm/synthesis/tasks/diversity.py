#!/usr/bin/env python3
"""LLM diversity gate for task sketches."""
from __future__ import annotations

import json
import re

from wildawm import const_config as cfg
from wildawm.json_utils import loads_jsonc
from wildawm.synthesis.tasks import claude_runner as cr


class DiversityInfrastructureError(RuntimeError):
    """The diversity gate could not produce a parseable decision."""


_PROMPT = """# Select a diverse set of task sketches

Compare every candidate with the accepted and concurrently locked task pool.
Select up to {count} candidates that add the most semantic diversity. Judge the
actual user objective, required artifacts, entities, decisions, and operations.
Different wording does not make two tasks different. Also avoid selecting two
new candidates that duplicate each other.

Do not apply a fixed quota for an area, site, interface order, visual family, or
difficulty. Do not infer a domain taxonomy. Base the decision only on the task
content supplied below.

## Existing task pool
{priors}

## Candidates
{candidates}

Return only this JSON object:
{{"choices":[<0-based candidate indexes, most diverse first>],
  "why":"brief evidence-based selection reason",
  "rejected":[{{"index":0,"overlap":"specific existing or selected task overlap"}}]}}

`choices` must contain distinct valid indexes and may be empty when every
candidate is a near-duplicate.
"""


def _summary(record: dict) -> dict:
    return {
        "slug": record.get("slug"),
        "title": record.get("title"),
        "summary": record.get("summary") or record.get("task_md"),
        "interfaces": record.get("interfaces"),
        "interface_flow": record.get("interface_flow"),
        "sites": record.get("sites"),
        "mcp_domains": record.get("mcp_domains"),
        "image_grounding": record.get("image_grounding"),
        "natural_keys": record.get("natural_keys"),
        "cli_code_step": record.get("cli_code_step"),
        "web_action_type": record.get("web_action_type"),
    }


def pick_many(sketches: list[dict], priors: list[dict], count: int) -> dict:
    """Strict LLM selection; raises DiversityInfrastructureError rather than falling back."""
    count = max(0, min(int(count), len(sketches)))
    if not sketches or count == 0:
        return {"choices": [], "selected": [], "why": "no candidates requested",
                "rejected": [], "call": None}
    if len(sketches) == 1 and not priors:
        return {"choices": [0], "selected": [sketches[0]], "why": "empty pool",
                "rejected": [], "call": None}
    spec = cfg.model_spec(cfg.DIVERSITY_JUDGE_MODEL)
    prompt = _PROMPT.format(
        count=count,
        priors=json.dumps([_summary(p) for p in priors], ensure_ascii=False, indent=1),
        candidates=json.dumps([_summary(s) for s in sketches], ensure_ascii=False, indent=1),
    )
    rec = cr.http_chat(
        spec,
        [{"role": "user", "content": prompt}],
        role="diversity",
        timeout=cfg.LLM_SHORT_TIMEOUT_S,
        retries=2,
    )
    if not rec.ok:
        raise DiversityInfrastructureError(
            f"diversity judge failed after retries: {rec.status}")
    match = re.search(r"\{.*\}", rec.result_text or "", re.S)
    if not match:
        raise DiversityInfrastructureError("diversity judge returned no JSON object")
    try:
        data = loads_jsonc(match.group(0))
    except Exception as exc:  # noqa: BLE001
        raise DiversityInfrastructureError(
            f"diversity judge returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
        raise DiversityInfrastructureError("diversity judge JSON lacks a choices list")
    choices: list[int] = []
    for value in data["choices"]:
        if not isinstance(value, int) or isinstance(value, bool):
            raise DiversityInfrastructureError("diversity judge choice is not an integer")
        if value < 0 or value >= len(sketches):
            raise DiversityInfrastructureError(
                f"diversity judge choice {value} is outside candidate range")
        if value not in choices:
            choices.append(value)
        if len(choices) >= count:
            break
    rejected = data.get("rejected")
    if not isinstance(rejected, list):
        rejected = []
    return {
        "choices": choices,
        "selected": [sketches[i] for i in choices],
        "why": str(data.get("why") or ""),
        "rejected": rejected,
        "call": rec.to_dict(),
    }
