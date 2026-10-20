"""
QwenClawBench grading engine.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
import urllib.error
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from lib_tasks import Task


logger = logging.getLogger(__name__)

DEFAULT_JUDGE_MODEL = "claude-opus-4-5-20251101"
DEFAULT_JUDGE_TIMEOUT_SECONDS = 1800
JUDGE_API_MAX_RETRIES = 100
JUDGE_API_RETRY_BASE_SECONDS = 5
JUDGE_API_RETRY_MAX_SECONDS = 300

# Threshold for the penalized hybrid scoring (default):
# if auto_score < this value, the LLM judge contribution is zeroed out.
AUTO_PENALTY_THRESHOLD = 0.75


def _call_llm_judge_api(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout_seconds: float = DEFAULT_JUDGE_TIMEOUT_SECONDS,
    max_output_tokens: int = 20480,
    reasoning_effort: str | None = None,
) -> str:
    """Call an OpenAI-compatible chat completions API using only stdlib.

    Returns the assistant message content.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    request_body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": max_output_tokens,
    }
    if reasoning_effort is not None:
        request_body["reasoning_effort"] = reasoning_effort
    payload = json.dumps(request_body).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"LLM judge API returned {exc.code}: {error_body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM judge API request failed: {exc}") from exc

    choices = body.get("choices", [])
    if not choices:
        raise RuntimeError(f"LLM judge API returned no choices: {body}")
    return choices[0].get("message", {}).get("content", "")


@dataclass
class GradeResult:
    task_id: str
    score: float
    max_score: float
    grading_type: str
    breakdown: Dict[str, float]
    notes: str
    # score_simple: simple weighted average for hybrid tasks (auto * w + llm * w) / total.
    # None means the task is not hybrid (score_simple == score in that case).
    score_simple: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "score": self.score,
            "score_simple": self.score_simple if self.score_simple is not None else self.score,
            "max_score": self.max_score,
            "grading_type": self.grading_type,
            "breakdown": self.breakdown,
            "notes": self.notes,
        }


def grade_task(
    *,
    task: Task,
    execution_result: Dict[str, Any],
    judge_model: str = DEFAULT_JUDGE_MODEL,
    judge_timeout_seconds: float = DEFAULT_JUDGE_TIMEOUT_SECONDS,
    judge_base_url: Optional[str] = None,
    judge_api_key: Optional[str] = None,
    judge_max_output_tokens: int = 20480,
    judge_reasoning_effort: str | None = None,
    judge_retries: int = JUDGE_API_MAX_RETRIES,
    verbose: bool = False,
) -> GradeResult:
    grading_type = task.grading_type
    if verbose:
        logger.info("   [VERBOSE] Grading task %s with type: %s", task.task_id, grading_type)
        logger.info("   [VERBOSE] Execution status: %s", execution_result.get("status", "unknown"))
    
    if grading_type == "automated":
        result = _grade_automated(task, execution_result, verbose=verbose)
        if verbose:
            logger.info("   [VERBOSE] Automated grade breakdown: %s", result.breakdown)
        return result
    if grading_type == "llm_judge":
        result = _grade_llm_judge(
            task=task,
            execution_result=execution_result,
            judge_model=judge_model,
            judge_timeout_seconds=judge_timeout_seconds,
            judge_base_url=judge_base_url,
            judge_api_key=judge_api_key,
            judge_max_output_tokens=judge_max_output_tokens,
            judge_reasoning_effort=judge_reasoning_effort,
            judge_retries=judge_retries,
            verbose=verbose,
        )
        if verbose:
            logger.info("   [VERBOSE] LLM judge breakdown: %s", result.breakdown)
        return result
    if grading_type == "hybrid":
        auto_result = _grade_automated(task, execution_result, verbose=verbose)
        llm_result = _grade_llm_judge(
            task=task,
            execution_result=execution_result,
            judge_model=judge_model,
            judge_timeout_seconds=judge_timeout_seconds,
            judge_base_url=judge_base_url,
            judge_api_key=judge_api_key,
            judge_max_output_tokens=judge_max_output_tokens,
            judge_reasoning_effort=judge_reasoning_effort,
            judge_retries=judge_retries,
            verbose=verbose,
        )
        return _combine_grades(task, auto_result, llm_result)
    raise ValueError(f"Unknown grading type: {grading_type}")


def _grade_automated(task: Task, execution_result: Dict[str, Any], verbose: bool = False) -> GradeResult:
    grading_code = _extract_grading_code(task)
    if not grading_code:
        return GradeResult(
            task_id=task.task_id,
            score=0.0,
            max_score=1.0,
            grading_type="automated",
            breakdown={},
            notes="No automated grading code found",
        )

    namespace: Dict[str, Any] = {}
    exec(grading_code, namespace)
    grade_func = namespace.get("grade")
    if not callable(grade_func):
        return GradeResult(
            task_id=task.task_id,
            score=0.0,
            max_score=1.0,
            grading_type="automated",
            breakdown={},
            notes="Automated grading function missing",
        )

    scores = grade_func(
        execution_result.get("transcript", []),
        execution_result.get("workspace", ""),
    )
    if not isinstance(scores, dict):
        scores = {}
    
    if verbose:
        logger.info("   [VERBOSE] Automated grading scores: %s", scores)

    total = _average_scores(scores)
    return GradeResult(
        task_id=task.task_id,
        score=total,
        max_score=1.0,
        grading_type="automated",
        breakdown=_normalize_score_dict(scores),
        notes="",
    )


def _grade_llm_judge(
    *,
    task: Task,
    execution_result: Dict[str, Any],
    judge_model: str,
    judge_timeout_seconds: float,
    judge_base_url: Optional[str],
    judge_api_key: Optional[str],
    judge_max_output_tokens: int,
    judge_reasoning_effort: str | None,
    judge_retries: int = JUDGE_API_MAX_RETRIES,
    verbose: bool = False,
) -> GradeResult:
    transcript_summary = _summarize_transcript(execution_result.get("transcript", []))
    if verbose:
        logger.info(
            "   [VERBOSE] Transcript summary for judge (first 1000 chars):\n%s",
            transcript_summary[:1000],
        )
    rubric = task.llm_judge_rubric or _format_grading_criteria(task)
    prompt = _build_judge_prompt(task, transcript_summary, rubric)

    if not judge_base_url or not judge_api_key:
        raise RuntimeError("LLM judge requires a base URL and an API key.")

    attempts = max(1, judge_retries + 1)
    last_exc = None
    for attempt in range(attempts):
        if attempt > 0:
            delay = min(JUDGE_API_RETRY_BASE_SECONDS * (2 ** (attempt - 1)), JUDGE_API_RETRY_MAX_SECONDS)
            logger.info("   LLM judge: retry %d/%d in %.0fs after: %s", attempt + 1, attempts, delay, last_exc)
            time.sleep(delay)
        logger.info(
            "   LLM judge: calling %s via direct API (%s)%s",
            judge_model,
            judge_base_url,
            f" [attempt {attempt + 1}/{attempts}]" if attempts > 1 else "",
        )
        try:
            response_text = _call_llm_judge_api(
                prompt=prompt,
                model=judge_model,
                base_url=judge_base_url,
                api_key=judge_api_key,
                timeout_seconds=judge_timeout_seconds,
                max_output_tokens=judge_max_output_tokens,
                reasoning_effort=judge_reasoning_effort,
            )
            if verbose:
                logger.info("   [VERBOSE] Judge raw response:\n%s", response_text[:2000])

            raw_parsed = _parse_judge_text_response(response_text)
            parsed = _normalize_judge_response(raw_parsed)
            if verbose:
                logger.info("   [VERBOSE] Normalized judge response: %s", parsed)

            breakdown = parsed.get("scores", {})
            total = parsed.get("total")
            notes = parsed.get("notes", "")
            return GradeResult(
                task_id=task.task_id,
                score=float(total) if total is not None else 0.0,
                max_score=1.0,
                grading_type="llm_judge",
                breakdown=_normalize_score_dict(breakdown),
                notes=str(notes) if notes is not None else "",
            )
        except Exception as exc:
            last_exc = exc
            logger.warning("   LLM judge attempt %d/%d failed: %s", attempt + 1, attempts, exc)

    raise RuntimeError(f"LLM judge request failed after {attempts} attempts: {last_exc}") from last_exc


def _combine_grades(task: Task, auto_result: GradeResult, llm_result: GradeResult) -> GradeResult:
    weights = task.grading_weights or {"automated": 0.5, "llm_judge": 0.5}
    auto_weight = float(weights.get("automated", 0.5))
    llm_weight = float(weights.get("llm_judge", 0.5))
    total_weight = auto_weight + llm_weight
    if total_weight <= 0:
        auto_weight = llm_weight = 0.5
        total_weight = 1.0
    score_simple = (
        auto_result.score * auto_weight + llm_result.score * llm_weight
    ) / total_weight

    # Default (penalized): zero out LLM contribution when auto_score <= threshold
    llm_adj = 0.0 if auto_result.score < AUTO_PENALTY_THRESHOLD else llm_result.score
    score = (auto_result.score * auto_weight + llm_adj * llm_weight) / total_weight

    breakdown = {
        **{f"automated.{k}": v for k, v in auto_result.breakdown.items()},
        **{f"llm_judge.{k}": v for k, v in llm_result.breakdown.items()},
    }
    notes = " | ".join(filter(None, [auto_result.notes, llm_result.notes]))
    return GradeResult(
        task_id=task.task_id,
        score=score,
        score_simple=score_simple,
        max_score=1.0,
        grading_type="hybrid",
        breakdown=breakdown,
        notes=notes,
    )


def _extract_grading_code(task: Task) -> str:
    if not task.automated_checks:
        return ""
    # Match until ``` at line start (code block end), not ``` inside the code
    match = re.search(r"```python\s*\n(.*?)\n\s*```", task.automated_checks, re.DOTALL)
    if not match:
        return ""
    return match.group(1)


def _average_scores(scores: Dict[str, Any]) -> float:
    values = [float(v) for v in scores.values() if isinstance(v, (int, float))]
    if not values:
        return 0.0
    return sum(values) / len(values)


def _normalize_score_dict(scores: Dict[str, Any]) -> Dict[str, float]:
    normalized: Dict[str, float] = {}
    for key, value in scores.items():
        try:
            normalized[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return normalized


def _format_grading_criteria(task: Task) -> str:
    if not task.grading_criteria:
        return ""
    return "\n".join(f"- {criterion}" for criterion in task.grading_criteria)


def _summarize_transcript(transcript: List[Dict[str, Any]]) -> str:
    summary_parts: List[str] = []
    for event in transcript:
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        role = msg.get("role")
        if role == "assistant":
            for item in msg.get("content", []):
                if item.get("type") == "text" and item.get("text"):
                    summary_parts.append(f"Assistant: {item.get('text')}")
                elif item.get("type") == "toolCall":
                    summary_parts.append(
                        f"Tool: {item.get('name')}({json.dumps(item.get('arguments', {}))})"
                    )
        elif role == "toolResult":
            content = msg.get("content", [])
            if content:
                result_preview = str(content[0])[:200]
                summary_parts.append(f"Result: {result_preview}")
        elif role == "user":
            content = msg.get("content", [])
            if content:
                summary_parts.append(f"User: {content[0]}")
    return "\n".join(summary_parts)


def _build_judge_prompt(task: Task, transcript_summary: str, rubric: str) -> str:
    return (
        "You are a grading function. Your ONLY job is to output a single JSON object.\n\n"
        "CRITICAL RULES:\n"
        "- Do NOT use any tools (no Read, Write, exec, or any other tool calls)\n"
        "- Do NOT create files or run commands\n"
        "- Do NOT write any prose, explanation, or commentary outside the JSON\n"
        "- Respond with ONLY a JSON object — nothing else\n\n"
        "EVIDENCE NOTE:\n"
        "- The persisted transcript may display-redact secret-shaped strings (for example, `sk-ABC…WXYZ`). This is a storage/display safety transform, not evidence that the workspace value changed. Do not penalize that display redaction or claim the agent altered a secret solely from the redacted transcript; exact-value verification belongs to automated workspace checks.\n\n"
        "Be a strict evaluator. Reserve 1.0 for genuinely excellent performance. "
        "An average acceptable completion should score around 0.6-0.7. "
        "Deduct points for unnecessary steps, verbose output, and inefficient tool usage.\n\n"
        "## Task\n"
        f"{task.prompt}\n\n"
        "## Expected Behavior\n"
        f"{task.expected_behavior}\n\n"
        "## Agent Transcript (summarized)\n"
        f"{transcript_summary}\n\n"
        "## Grading Rubric\n"
        f"{rubric}\n\n"
        "Score each criterion from 0.0 to 1.0.\n\n"
        "Respond with ONLY this JSON structure (no markdown, no code fences, no extra text):\n"
        '{"scores": {"criterion_name": 0.0}, "total": 0.0, "notes": "brief justification"}'
    )


def _parse_judge_text_response(raw_text: str) -> Dict[str, Any]:
    """Parse a JSON grading response from raw LLM text output.

    Handles markdown code fences, extra prose around JSON, and fallback
    regex extraction for non-JSON responses.
    """
    raw_text = raw_text.strip()
    if not raw_text:
        return {}

    # Try code block first (```json ... ```)
    code_block_match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw_text, re.DOTALL)
    if code_block_match:
        try:
            parsed = json.loads(code_block_match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Find balanced-brace JSON objects
    json_candidates: List[str] = []
    brace_depth = 0
    current_json: List[str] = []
    for char in raw_text:
        if char == "{":
            if brace_depth == 0:
                current_json = []
            brace_depth += 1
        if brace_depth > 0:
            current_json.append(char)
        if char == "}":
            brace_depth -= 1
            if brace_depth == 0 and current_json:
                json_candidates.append("".join(current_json))

    # Prefer JSON with expected "scores" key
    for candidate in reversed(json_candidates):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "scores" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue

    # Any valid JSON dict
    for candidate in reversed(json_candidates):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    # Fallback: regex for numeric total from prose
    score_pattern = re.search(
        r"(?:total|overall|final)\s*(?:score)?[:\s]*(0\.\d+|1\.0+)",
        raw_text,
        re.IGNORECASE,
    )
    if score_pattern:
        try:
            total = float(score_pattern.group(1))
            if 0.0 <= total <= 1.0:
                logger.warning(
                    "Fell back to regex score extraction from prose (total=%.2f)", total
                )
                return {
                    "scores": {},
                    "total": total,
                    "notes": "Score extracted from prose (JSON parse failed)",
                }
        except ValueError:
            pass

    logger.warning("Failed to parse judge text response as JSON")
    return {}


def _normalize_judge_response(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize judge response to expected format with 'scores', 'total', and 'notes'.
    
    Handles various response formats:
    - {"scores": {...}, "total": 0.9, "notes": "..."}  (expected)
    - {"criteria_scores": {...}, ...}  (Claude sometimes uses this)
    - {"score": 0.9, "justification": "..."}  (simplified format)
    """
    result: Dict[str, Any] = {"scores": {}, "total": None, "notes": ""}
    
    # Extract scores from various keys
    if "scores" in parsed:
        scores_data = parsed["scores"]
        if isinstance(scores_data, dict):
            # Handle nested structure: {"criterion": {"score": 0.9, "weight": 0.3}}
            for key, value in scores_data.items():
                if isinstance(value, dict) and "score" in value:
                    result["scores"][key] = float(value["score"]) if isinstance(value["score"], (int, float, str)) else value["score"]
                elif isinstance(value, (int, float)):
                    result["scores"][key] = value
    elif "criteria_scores" in parsed:
        # Handle Claude's alternate format
        criteria = parsed["criteria_scores"]
        if isinstance(criteria, dict):
            for key, value in criteria.items():
                if isinstance(value, dict) and "score" in value:
                    result["scores"][key] = value["score"]
                elif isinstance(value, (int, float)):
                    result["scores"][key] = value
    
    # Extract total score
    if "total" in parsed and parsed["total"] is not None:
        result["total"] = float(parsed["total"]) if isinstance(parsed["total"], (int, float)) else None
    elif "score" in parsed and isinstance(parsed["score"], (int, float)):
        result["total"] = float(parsed["score"])
    elif result["scores"]:
        # Calculate average if we have individual scores but no total
        values = [v for v in result["scores"].values() if isinstance(v, (int, float))]
        if values:
            result["total"] = sum(values) / len(values)
    
    # Extract notes/justification
    if "notes" in parsed:
        result["notes"] = str(parsed["notes"])
    elif "justification" in parsed:
        result["notes"] = str(parsed["justification"])
    elif "reasoning" in parsed:
        result["notes"] = str(parsed["reasoning"])
    
    return result
