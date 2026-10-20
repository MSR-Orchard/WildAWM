"""LLM-as-judge for subjective communication, action, and visual scoring."""

from __future__ import annotations

import base64
from io import BytesIO
import json
import re
import time
from collections.abc import Callable
from typing import Any

from openai import OpenAI
from PIL import Image, ImageOps
from pydantic import BaseModel

from ..models.trace import _now


class JudgeResult(BaseModel):
    score: float
    reasoning: str


_SYSTEM_PROMPT = """\
You are an evaluation judge for an AI assistant.
You will be given a task prompt, a conversation, a summary of actions taken, and a rubric.
Follow the rubric to score the assistant's response on a 0.0-1.0 scale.
Respond with JSON only: {"score": <float>, "reasoning": "<brief explanation>"}
"""

_ACTIONS_SYSTEM_PROMPT = """\
You are an evaluation judge for an AI agent's actions.
You will be given a task prompt, a record of actions the agent actually performed \
(extracted from the server-side audit log, not from the agent's self-report), \
and a rubric.
Follow the rubric to score the quality of the agent's actions on a 0.0-1.0 scale.
Respond with JSON only: {"score": <float>, "reasoning": "<brief explanation>"}
"""

_VISUAL_SYSTEM_PROMPT = """\
You are a STRICT visual evaluation judge. Your job is to compare candidate images \
against reference images and/or a detailed rubric, then assign a score from 0.0 to 1.0.

CRITICAL RULES:
- You must be HARSH and PRECISE. Do NOT give generous scores.
- If the rubric describes specific content, verify each detail.
- A visually attractive output with wrong content is a failure.
- Only score above 0.5 if the majority of rubric criteria are clearly satisfied.
- Only score above 0.7 if the content is substantially correct with minor issues.
- Only score above 0.9 if the output is nearly perfect.
- Score 0.0-0.2 if the output is mostly wrong or unrecognizable.
- When reference images are provided, compare the candidate directly against them.

Respond with JSON only: {"score": <float>, "reasoning": "<brief explanation>"}
"""

_JSON_SYSTEM_PROMPT = """\
You extract structured data for an evaluation grader.
Follow the user's schema exactly and respond with one JSON object only.
"""

_MAX_VISUAL_IMAGES_PER_REQUEST = 50
_MAX_VISUAL_IMAGE_DIMENSION = 1024
_MAX_VISUAL_IMAGE_BYTES = 192 * 1024


def _uniformly_sample_images(images: list[str], keep: int) -> list[str]:
    if keep >= len(images):
        return list(images)
    if keep <= 0:
        return []
    if keep == 1:
        return [images[len(images) // 2]]
    return [
        images[index * (len(images) - 1) // (keep - 1)]
        for index in range(keep)
    ]


def _limit_visual_images(
    reference_images_b64: list[str],
    candidate_images_b64: list[str],
) -> tuple[list[str], list[str]]:
    reference_count = len(reference_images_b64)
    candidate_count = len(candidate_images_b64)
    total = reference_count + candidate_count
    if total <= _MAX_VISUAL_IMAGES_PER_REQUEST:
        return list(reference_images_b64), list(candidate_images_b64)

    if not reference_images_b64:
        return [], _uniformly_sample_images(
            candidate_images_b64,
            _MAX_VISUAL_IMAGES_PER_REQUEST,
        )
    if not candidate_images_b64:
        return _uniformly_sample_images(
            reference_images_b64,
            _MAX_VISUAL_IMAGES_PER_REQUEST,
        ), []

    reference_keep = (
        _MAX_VISUAL_IMAGES_PER_REQUEST * reference_count + total // 2
    ) // total
    reference_keep = max(
        1,
        min(
            reference_count,
            _MAX_VISUAL_IMAGES_PER_REQUEST - 1,
            reference_keep,
        ),
    )
    candidate_keep = _MAX_VISUAL_IMAGES_PER_REQUEST - reference_keep
    if candidate_keep > candidate_count:
        candidate_keep = candidate_count
        reference_keep = _MAX_VISUAL_IMAGES_PER_REQUEST - candidate_keep

    return (
        _uniformly_sample_images(reference_images_b64, reference_keep),
        _uniformly_sample_images(candidate_images_b64, candidate_keep),
    )


def _prepare_visual_image(
    image_b64: str,
) -> tuple[str, str, int, int, bool]:
    try:
        source_bytes = base64.b64decode(image_b64, validate=True)
        with Image.open(BytesIO(source_bytes)) as opened:
            source_mime = Image.MIME.get(opened.format or "", "image/png")
            source = ImageOps.exif_transpose(opened).convert("RGB")
    except Exception:
        estimated_bytes = len(image_b64) * 3 // 4
        return image_b64, "image/png", estimated_bytes, estimated_bytes, False

    if (
        len(source_bytes) <= _MAX_VISUAL_IMAGE_BYTES
        and max(source.size) <= _MAX_VISUAL_IMAGE_DIMENSION
    ):
        return image_b64, source_mime, len(source_bytes), len(source_bytes), False

    smallest: bytes | None = None
    for dimension in (1024, 896, 768, 640, 512, 384):
        resized = source.copy()
        resized.thumbnail((dimension, dimension), Image.Resampling.LANCZOS)
        for quality in (85, 75, 65, 55):
            output = BytesIO()
            resized.save(
                output,
                format="JPEG",
                quality=quality,
                optimize=True,
            )
            payload = output.getvalue()
            if smallest is None or len(payload) < len(smallest):
                smallest = payload
            if len(payload) <= _MAX_VISUAL_IMAGE_BYTES:
                return (
                    base64.b64encode(payload).decode("ascii"),
                    "image/jpeg",
                    len(source_bytes),
                    len(payload),
                    True,
                )
    assert smallest is not None
    return (
        base64.b64encode(smallest).decode("ascii"),
        "image/jpeg",
        len(source_bytes),
        len(smallest),
        True,
    )


def _prepare_visual_images(
    images_b64: list[str],
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    prepared: list[tuple[str, str]] = []
    original_bytes = 0
    sent_bytes = 0
    compressed = 0
    for image_b64 in images_b64:
        data, mime_type, before, after, changed = _prepare_visual_image(
            image_b64
        )
        prepared.append((data, mime_type))
        original_bytes += before
        sent_bytes += after
        compressed += int(changed)
    return prepared, {
        "visual_images_compressed": compressed,
        "visual_image_bytes_original": original_bytes,
        "visual_image_bytes_sent": sent_bytes,
    }


class LLMJudge:
    """Judge rubric items through an OpenAI-compatible model."""

    def __init__(
        self,
        model_id: str = "google/gemini-2.5-flash",
        api_key: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        *,
        max_output_tokens: int = 8192,
        request_timeout_seconds: float = 600.0,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
        reasoning_effort: str | None = None,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client or OpenAI(
            api_key=api_key or "dummy",
            base_url=base_url,
            timeout=request_timeout_seconds,
        )
        self.model_id = model_id
        self.max_output_tokens = max_output_tokens
        self.request_timeout_seconds = request_timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.reasoning_effort = reasoning_effort
        self._clock = clock
        self._sleep = sleep
        self._call_log: list[dict[str, Any]] = []

    @staticmethod
    def _parse_result(raw_response: str) -> JudgeResult:
        raw = re.sub(r"^```(?:json)?\s*", "", raw_response.strip())
        raw = re.sub(r"\s*```$", "", raw.strip())
        match = re.search(r"\{[^{}]*\}", raw)
        if match:
            raw = match.group(0)
        try:
            parsed = json.loads(raw)
            score, reasoning = parsed["score"], parsed["reasoning"]
        except (json.JSONDecodeError, KeyError):
            score_match = re.search(r'"score"\s*:\s*([0-9.]+)', raw)
            reason_match = re.search(
                r'"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"', raw
            )
            if score_match is None:
                raise json.JSONDecodeError("No score found in raw", raw, 0)
            score = float(score_match.group(1))
            reasoning = reason_match.group(1) if reason_match else ""
        return JudgeResult(
            score=max(0.0, min(1.0, float(score))),
            reasoning=str(reasoning),
        )

    def _request(
        self,
        *,
        method: str,
        system_prompt: str,
        user_content: str | list[dict[str, Any]],
        metadata: dict[str, Any],
        parser: Callable[[str], Any] | None = None,
        max_output_tokens_override: int | None = None,
    ) -> Any:
        started = self._clock()
        deadline = started + self.request_timeout_seconds
        remaining = self.request_timeout_seconds
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            if remaining <= 0:
                raise last_error or TimeoutError(
                    "judge request deadline exhausted"
                )
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model_id,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": (
                        max_output_tokens_override
                        if max_output_tokens_override is not None
                        else self.max_output_tokens
                    ),
                    "timeout": remaining,
                }
                if self.reasoning_effort is not None:
                    kwargs["reasoning_effort"] = self.reasoning_effort
                response = self.client.chat.completions.create(**kwargs)
                raw = response.choices[0].message.content or "{}"
                result = (
                    parser(raw)
                    if parser is not None
                    else self._parse_result(raw)
                )
                usage = getattr(response, "usage", None)
                self._call_log.append(
                    {
                        "method": method,
                        "model_id": self.model_id,
                        "raw_response": raw,
                        "score": (
                            result.score
                            if isinstance(result, JudgeResult)
                            else None
                        ),
                        "reasoning": (
                            result.reasoning
                            if isinstance(result, JudgeResult)
                            else ""
                        ),
                        "duration_s": round(self._clock() - started, 6),
                        "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                        "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                        "timestamp": _now(),
                        **metadata,
                    }
                )
                return result
            except Exception as error:
                last_error = error
                if not self._is_retryable(error) or attempt + 1 >= self.retry_attempts:
                    raise
                delay = self.retry_delay_seconds * (2**attempt)
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise last_error
                status = getattr(error, "status_code", None) or getattr(error, "code", None)
                print(
                    f"[judge-retry] ({status or type(error).__name__}), "
                    f"attempt {attempt + 1}/{self.retry_attempts}, waiting {delay:.1f}s ..."
                )
                if delay > 0:
                    self._sleep(min(delay, remaining))
                if delay >= remaining:
                    raise last_error
                remaining = deadline - self._clock()
        raise last_error or RuntimeError("judge request exhausted")

    @staticmethod
    def _parse_json_object(raw_response: str) -> dict[str, Any]:
        raw = re.sub(r"^```(?:json)?\s*", "", raw_response.strip())
        raw = re.sub(r"\s*```$", "", raw.strip())
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end >= start:
            raw = raw[start:end + 1]
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError("judge JSON extraction must return an object")
        return parsed

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        status = getattr(error, "status_code", None) or getattr(error, "code", None)
        text = str(error).lower()
        name = type(error).__name__.lower()
        return (
            status in {429, 500, 502, 503, 504, 529}
            or isinstance(error, (ConnectionError, TimeoutError))
            or isinstance(error, json.JSONDecodeError)
            or "timeout" in text
            or "connection" in text
            or "remoteprotocol" in name
            or "server disconnected" in text
        )

    def evaluate(
        self,
        task_prompt: str,
        conversation: str,
        actions_summary: str,
        rubric: str,
    ) -> JudgeResult:
        user_message = (
            f"## Task Prompt\n{task_prompt}\n\n"
            f"## Conversation\n{conversation}\n\n"
            f"## Actions Taken\n{actions_summary}\n\n"
            f"## Rubric\n{rubric}"
        )
        return self._request(
            method="evaluate",
            system_prompt=_SYSTEM_PROMPT,
            user_content=user_message,
            metadata={"rubric_preview": rubric[:300]},
        )

    def extract_json(
        self,
        prompt: str,
        *,
        method: str = "extract_json",
        max_output_tokens_override: int | None = None,
    ) -> dict[str, Any]:
        """Return a logged JSON object for structured grader extraction."""

        return self._request(
            method=method,
            system_prompt=_JSON_SYSTEM_PROMPT,
            user_content=prompt,
            metadata={"prompt_preview": prompt[:300]},
            parser=self._parse_json_object,
            max_output_tokens_override=max_output_tokens_override,
        )

    def extract_visual_json(
        self,
        prompt: str,
        *,
        images_b64: list[str],
        context: str = "",
        method: str = "extract_visual_json",
        max_output_tokens_override: int | None = None,
    ) -> dict[str, Any]:
        """Return a logged JSON object extracted from visual inputs."""

        original_count = len(images_b64)
        images_b64 = _uniformly_sample_images(
            images_b64,
            min(original_count, _MAX_VISUAL_IMAGES_PER_REQUEST),
        )
        prepared_images, image_stats = _prepare_visual_images(images_b64)
        text = "## Visual Data Extraction\n"
        if context:
            text += f"{context}\n\n"
        text += prompt
        if len(images_b64) != original_count:
            text += (
                "\n\nThe following images are a deterministic uniform sample "
                f"of {len(images_b64)} from {original_count} ordered images."
            )
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{image}"},
            }
            for image, mime_type in prepared_images
        )
        return self._request(
            method=method,
            system_prompt=_JSON_SYSTEM_PROMPT,
            user_content=content,
            metadata={
                "prompt_preview": prompt[:300],
                "n_images": len(images_b64),
                "n_images_original": original_count,
                "context_preview": context[:200],
                **image_stats,
            },
            parser=self._parse_json_object,
            max_output_tokens_override=max_output_tokens_override,
        )

    def evaluate_actions(
        self,
        task_prompt: str,
        artifacts: str,
        rubric: str,
    ) -> JudgeResult:
        user_message = (
            f"## Task Prompt\n{task_prompt}\n\n"
            f"## Agent Actions (from server audit log)\n{artifacts}\n\n"
            f"## Rubric\n{rubric}"
        )
        return self._request(
            method="evaluate_actions",
            system_prompt=_ACTIONS_SYSTEM_PROMPT,
            user_content=user_message,
            metadata={"rubric_preview": rubric[:300]},
        )

    def evaluate_visual(
        self,
        rubric: str,
        reference_images_b64: list[str],
        candidate_images_b64: list[str],
        context: str = "",
    ) -> JudgeResult:
        reference_count_original = len(reference_images_b64)
        candidate_count_original = len(candidate_images_b64)
        reference_images_b64, candidate_images_b64 = _limit_visual_images(
            reference_images_b64,
            candidate_images_b64,
        )
        prepared_reference, reference_stats = _prepare_visual_images(
            reference_images_b64
        )
        prepared_candidate, candidate_stats = _prepare_visual_images(
            candidate_images_b64
        )
        header = "## Visual Evaluation\n"
        if context:
            header += f"{context}\n\n"
        header += f"## Rubric\n{rubric}\n\n"
        header += (
            "## Scoring Calibration\n"
            "- 0.0-0.2: Output is mostly wrong, unrecognizable, or missing most required content\n"
            "- 0.2-0.4: Some elements present but major content errors remain\n"
            "- 0.4-0.6: General structure is right but significant inaccuracies remain\n"
            "- 0.6-0.8: Most content is correct with some minor issues\n"
            "- 0.8-1.0: Content is substantially correct, matching reference closely\n\n"
            'Respond with JSON only: {"score": <float>, "reasoning": "<brief explanation>"}'
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": header}]
        if reference_images_b64:
            reference_label = f"{len(reference_images_b64)} images"
            if len(reference_images_b64) != reference_count_original:
                reference_label = (
                    f"{len(reference_images_b64)} of {reference_count_original} images, "
                    "uniformly sampled"
                )
            content.append(
                {
                    "type": "text",
                    "text": f"### Reference ({reference_label})",
                }
            )
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{image}"},
                }
                for image, mime_type in prepared_reference
            )
        if candidate_images_b64:
            candidate_label = f"{len(candidate_images_b64)} images"
            if len(candidate_images_b64) != candidate_count_original:
                candidate_label = (
                    f"{len(candidate_images_b64)} of {candidate_count_original} images, "
                    "uniformly sampled"
                )
            content.append(
                {
                    "type": "text",
                    "text": f"### Candidate ({candidate_label})",
                }
            )
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{image}"},
                }
                for image, mime_type in prepared_candidate
            )
        result = self._request(
            method="evaluate_visual",
            system_prompt=_VISUAL_SYSTEM_PROMPT,
            user_content=content,
            metadata={
                "rubric_preview": rubric[:300],
                "n_ref_images": len(reference_images_b64),
                "n_cand_images": len(candidate_images_b64),
                "n_ref_images_original": reference_count_original,
                "n_cand_images_original": candidate_count_original,
                "context_preview": context[:200],
                "visual_images_compressed": (
                    reference_stats["visual_images_compressed"]
                    + candidate_stats["visual_images_compressed"]
                ),
                "visual_image_bytes_original": (
                    reference_stats["visual_image_bytes_original"]
                    + candidate_stats["visual_image_bytes_original"]
                ),
                "visual_image_bytes_sent": (
                    reference_stats["visual_image_bytes_sent"]
                    + candidate_stats["visual_image_bytes_sent"]
                ),
            },
        )
        print(
            f"[judge-visual] score={result.score:.2f} "
            f"reasoning={result.reasoning[:200]}"
        )
        return result

    def get_call_log(self) -> list[dict[str, Any]]:
        return list(self._call_log)

    def reset_call_log(self) -> None:
        self._call_log.clear()
