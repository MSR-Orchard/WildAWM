"""Simulated user agent — LLM-driven user for multi-turn dialogue evaluation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from openai import OpenAI

from ..models.message import Message


_SYSTEM_PROMPT = """\
你是一个模拟用户。你的任务是根据以下人设与AI助手进行对话。

## 你的人设
{persona}

## 规则
1. 始终保持人设角色，用自然口语回复，不要暴露你是AI
2. 根据助手的提问如实回答（基于你的人设信息）
3. 如果助手问了你人设中没有的信息，说"不太清楚具体数字"或类似自然回复
4. 如果助手已经给出了完整的计算结果和建议，且你没有更多问题，输出 [DONE]
5. 如果你对回答满意或助手已充分回答了你的问题，输出 [DONE]
6. 回复要简短自然，像真实用户一样（1-3句话）
"""


def _format_transcript(messages: list[Message]) -> str:
    """Format conversation messages into a readable transcript."""
    lines = []
    for msg in messages:
        if msg.role == "system":
            continue
        text = msg.text
        if not text:
            continue
        if msg.role == "user":
            if text.startswith("[user_agent]"):
                text = text[len("[user_agent]"):].strip()
            lines.append(f"[用户]: {text}")
        elif msg.role == "assistant":
            lines.append(f"[助手]: {text}")
    return "\n".join(lines)


class UserAgent:
    """Simulated user that generates responses via an LLM."""

    def __init__(
        self,
        model_id: str,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        *,
        max_output_tokens: int = 65536,
        request_timeout_seconds: float = 600.0,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=request_timeout_seconds,
        )
        self.model_id = model_id
        self.max_output_tokens = max_output_tokens
        self.request_timeout_seconds = request_timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self._clock = clock
        self._sleep = sleep
        self._call_log: list[dict[str, Any]] = []

    def generate_response(
        self,
        persona: str,
        conversation_messages: list[Message],
    ) -> str | None:
        """Generate a simulated user reply.

        Returns the reply text, or None if the user is satisfied ([DONE]).
        """
        system = _SYSTEM_PROMPT.format(persona=persona)
        transcript = _format_transcript(conversation_messages)
        user_msg = (
            f"以下是到目前为止的对话：\n\n{transcript}\n\n"
            "请根据你的人设回复助手的最新消息。如果你满意了就输出 [DONE]。"
        )

        last_error: Exception | None = None
        started = self._clock()
        deadline = started + self.request_timeout_seconds
        remaining = self.request_timeout_seconds
        for attempt in range(self.retry_attempts):
            if remaining <= 0:
                raise last_error or TimeoutError(
                    "user-agent request deadline exhausted"
                )
            try:
                resp = self.client.chat.completions.create(
                    model=self.model_id,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_msg},
                    ],
                    max_tokens=self.max_output_tokens,
                    timeout=remaining,
                )
                text = (resp.choices[0].message.content or "").strip()
                usage = getattr(resp, "usage", None)
                self._call_log.append(
                    {
                        "model_id": self.model_id,
                        "response": text,
                        "duration_s": round(self._clock() - started, 6),
                        "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                        "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                    }
                )
                if "[DONE]" in text:
                    return None
                if text:
                    return text
                return None
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= self.retry_attempts:
                    raise
                delay = self.retry_delay_seconds * (2**attempt)
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise last_error
                print(
                    f"[user-agent-retry] {type(exc).__name__}, "
                    f"attempt {attempt + 1}/{self.retry_attempts}, waiting {delay:.1f}s ..."
                )
                if delay > 0:
                    self._sleep(min(delay, remaining))
                if delay >= remaining:
                    raise last_error
                remaining = deadline - self._clock()
        raise last_error or RuntimeError("user-agent request exhausted")

    def get_call_log(self) -> list[dict[str, Any]]:
        return list(self._call_log)

    def reset_call_log(self) -> None:
        self._call_log.clear()
