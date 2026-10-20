#!/usr/bin/env python3
"""Build-time patch: make nanobot persist turn messages incrementally; fails on anchor drift."""

import py_compile
import sys
from pathlib import Path

PKG = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/local/lib/python3.12/dist-packages/nanobot")

PATCHES = {
    # agent/runner.py: include the live turn message list in checkpoints
    PKG / "agent" / "runner.py": [
        # phase=awaiting_tools
        (
            """                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },""",
            """                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                        "_messages": list(messages),
                    },""",
        ),
        # phase=tools_completed
        (
            """                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                    },""",
            """                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                        "_messages": list(messages),
                    },""",
        ),
        # phase=final_response (main loop)
        (
            """                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },""",
            """                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                    "_messages": list(messages),
                },""",
        ),
        # phase=final_response (injection-drain path)
        (
            """                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },""",
            """                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                        "_messages": list(messages),
                    },""",
        ),
    ],
    # agent/loop.py: incremental flush, skipped on the final save
    PKG / "agent" / "loop.py": [
        (
            """    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn\"""",
            """    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
    _PENDING_USER_TURN_KEY = "pending_user_turn"
    _TURN_FLUSH_MARK_KEY = "_turn_flush_mark\"""",
        ),
        (
            """        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)""",
            """        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            turn_messages = payload.pop("_messages", None)
            if turn_messages is not None:
                self._flush_turn_progress(
                    session, turn_messages, len(initial_messages))
            self._set_runtime_checkpoint(session, payload)""",
        ),
        (
            """    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        \"\"\"Persist the latest in-flight turn state into session metadata.\"\"\"""",
            """    def _flush_turn_progress(
        self, session: Session, messages: list[dict[str, Any]], skip: int
    ) -> None:
        \"\"\"Incrementally persist completed turn messages (WildAWM patch).

        Appends the not-yet-flushed suffix of the runner's live message list
        to the session so a cancelled/killed turn still leaves its full
        trajectory on disk. The caller (_set_runtime_checkpoint) saves.
        \"\"\"
        try:
            mark = int(session.metadata.get(self._TURN_FLUSH_MARK_KEY) or 0)
        except (TypeError, ValueError):
            mark = 0
        start = max(mark, skip)
        if len(messages) <= start:
            return
        self._save_turn(session, messages, start)
        session.metadata[self._TURN_FLUSH_MARK_KEY] = len(messages)

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        \"\"\"Persist the latest in-flight turn state into session metadata.\"\"\"""",
        ),
        (
            """        self._save_turn(
            ctx.session, ctx.all_messages, ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
        )""",
            """        try:
            _flushed = int(
                ctx.session.metadata.pop(self._TURN_FLUSH_MARK_KEY, 0) or 0)
        except (TypeError, ValueError):
            _flushed = 0
        self._save_turn(
            ctx.session, ctx.all_messages, max(ctx.save_skip, _flushed),
            turn_latency_ms=ctx.turn_latency_ms,
        )""",
        ),
        # reset a stale mark left by a killed turn
        (
            """        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session
        )""",
            """        ctx.session.metadata.pop(self._TURN_FLUSH_MARK_KEY, None)
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session
        )""",
        ),
        (
            """        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):""",
            """        session = self.sessions.get_or_create(key)
        session.metadata.pop(self._TURN_FLUSH_MARK_KEY, None)
        if self._restore_runtime_checkpoint(session):""",
        ),
        (
            """        self._save_turn(session, all_msgs, 1 + len(history), turn_latency_ms=latency_ms)""",
            """        try:
            _flushed = int(
                session.metadata.pop(self._TURN_FLUSH_MARK_KEY, 0) or 0)
        except (TypeError, ValueError):
            _flushed = 0
        self._save_turn(session, all_msgs, max(1 + len(history), _flushed),
                        turn_latency_ms=latency_ms)""",
        ),
    ],
}


def main() -> int:
    failures = []
    for path, repls in PATCHES.items():
        text = path.read_text()
        for old, new in repls:
            if new in text:  # already applied
                continue
            n = text.count(old)
            if n != 1:
                failures.append(f"{path}: anchor matched {n} times (want 1):\n{old}")
                continue
            text = text.replace(old, new)
        path.write_text(text)
    if failures:
        print("patch_nanobot.py FAILED — nanobot-ai source drifted:", file=sys.stderr)
        for f in failures:
            print(" * " + f, file=sys.stderr)
        return 1
    for path in PATCHES:
        py_compile.compile(str(path), doraise=True)
    print(f"patch_nanobot.py: patched {', '.join(str(p) for p in PATCHES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
