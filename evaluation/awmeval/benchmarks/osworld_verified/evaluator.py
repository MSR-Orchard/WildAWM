#!/opt/awmeval-ground-truth/venv/bin/python
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

GROUND_ROOT = Path("/opt/awmeval-ground-truth")
CATALOG = GROUND_ROOT / "cua-lite/lite/gym/envs/lite/osworld/data/eval.jsonl"
SERVER = "/opt/lite/stdio_server.py"
PYTHON = "/opt/env/venv/bin/python"


class LocalExecStdioInterface:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.request_id = 0
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            "runuser", "-u", "user", "--", "env",
            "HOME=/home/user", "USER=user", "DISPLAY=:1",
            "XDG_RUNTIME_DIR=/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
            "PATH=/opt/env/venv/bin:/opt/env/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            PYTHON, SERVER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=64 * 1024 * 1024,
        )
        result = await self._call("ping", wait_timeout=30)
        if result.get("version") != 4:
            raise RuntimeError(f"unexpected lite exec-stdio protocol: {result}")

    async def close(self) -> None:
        if self.process is None:
            return
        if self.process.stdin is not None:
            self.process.stdin.close()
            try:
                await self.process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except TimeoutError:
            try:
                os.killpg(self.process.pid, 15)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                try:
                    os.killpg(self.process.pid, 9)
                except ProcessLookupError:
                    pass
                await self.process.wait()
        self.process = None

    async def _call(self, op: str, *, wait_timeout: float = 120, **parameters: Any) -> Any:
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("exec-stdio session is not running")
        async with self.lock:
            self.request_id += 1
            request_id = self.request_id
            request = {"id": request_id, "op": op, **parameters}
            self.process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
            await self.process.stdin.drain()
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=wait_timeout + 15)
            if not line:
                raise RuntimeError(f"exec-stdio exited during {op}")
            response = json.loads(line)
            if response.get("id") != request_id:
                raise RuntimeError(f"exec-stdio response id mismatch during {op}")
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error") or f"exec-stdio {op} failed"))
            if "shot_n" in response:
                length = int(response["shot_n"])
                return await asyncio.wait_for(self.process.stdout.readexactly(length), timeout=wait_timeout + 15) if length else b""
            return response.get("result")

    async def screenshot(self) -> bytes:
        return await self._call("screenshot", wait_timeout=120)

    async def run_command(self, command: str, timeout: float | None = None) -> Any:
        budget = float(timeout if timeout is not None else 300)
        result = await self._call("run_command", wait_timeout=budget, timeout=budget, cmd=command)
        return SimpleNamespace(stdout=result["stdout"], stderr=result["stderr"], returncode=result["returncode"])

    async def read_bytes(self, path: str) -> bytes:
        result = await self._call("read_bytes", path=path)
        return base64.b64decode(result["b64"])

    async def write_bytes(self, path: str, data: bytes) -> None:
        await self._call("write_bytes", path=path, b64=base64.b64encode(data).decode("ascii"))

    async def get_screen_size(self) -> dict[str, int]:
        result = await self.run_command("xdotool getdisplaygeometry")
        width, height = result.stdout.split()[:2]
        return {"width": int(width), "height": int(height)}

    async def get_cursor_position(self) -> dict[str, int]:
        result = await self.run_command("xdotool getmouselocation --shell")
        values = {}
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            if key in {"X", "Y"} and value.lstrip("-").isdigit():
                values[key.lower()] = int(value)
        return {"x": values.get("x", 0), "y": values.get("y", 0)}

    async def type_text(self, text: str) -> None:
        await self._call("input", wait_timeout=max(120, len(text) * 0.03 + 60), action="type", text=text)

    async def hotkey(self, *keys: str) -> None:
        await self._call("input", action="key", keys=list(keys))

    async def press_key(self, key: str) -> None:
        await self.hotkey(key)

    async def key_down(self, key: str) -> None:
        await self._call("input", action="keydown", key=key)

    async def key_up(self, key: str) -> None:
        await self._call("input", action="keyup", key=key)

    async def left_click(self, x: int | None = None, y: int | None = None) -> None:
        await self._call("input", action="click", x=x, y=y, button=1)

    async def right_click(self, x: int | None = None, y: int | None = None) -> None:
        await self._call("input", action="click", x=x, y=y, button=3)

    async def multi_click(self, x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 2) -> None:
        buttons = {"left": 1, "middle": 2, "right": 3}
        await self._call("input", action="click", x=x, y=y, button=buttons[button], repeat=clicks)

    async def double_click(self, x: int | None = None, y: int | None = None, button: str = "left") -> None:
        await self.multi_click(x, y, button, 2)

    async def move_cursor(self, x: int, y: int) -> None:
        await self._call("input", action="move", x=x, y=y)

    async def mouse_down(self, x: int | None = None, y: int | None = None, button: str = "left") -> None:
        buttons = {"left": 1, "middle": 2, "right": 3}
        await self._call("input", action="mouse_down", x=x, y=y, button=buttons[button])

    async def mouse_up(self, x: int | None = None, y: int | None = None, button: str = "left") -> None:
        buttons = {"left": 1, "middle": 2, "right": 3}
        await self._call("input", action="mouse_up", x=x, y=y, button=buttons[button])

    async def drag(self, path: list, button: str = "left", duration: float = 0.5) -> None:
        if not path:
            return
        start_x, start_y = path[0]
        end_x, end_y = path[-1]
        await self.mouse_down(int(start_x), int(start_y), button)
        try:
            steps = max(2, min(100, int(max(abs(end_x - start_x), abs(end_y - start_y)) / 15)))
            for index in range(1, steps + 1):
                await self.move_cursor(round(start_x + (end_x - start_x) * index / steps), round(start_y + (end_y - start_y) * index / steps))
                await asyncio.sleep(max(0.015, duration / steps))
        finally:
            await self.mouse_up(int(end_x), int(end_y), button)

    async def scroll(self, x: int, y: int) -> None:
        if x and not y:
            await self._call("input", action="scroll", axis="x", direction="right" if x > 0 else "left", amount=abs(int(x)) or 1)
        else:
            await self._call("input", action="scroll", direction="up" if y > 0 else "down", amount=abs(int(y)) or 1)

    async def scroll_down(self, clicks: int = 1) -> None:
        await self._call("input", action="scroll", direction="down", amount=clicks)

    async def scroll_up(self, clicks: int = 1) -> None:
        await self._call("input", action="scroll", direction="up", amount=clicks)


def _load_task(path: Path):
    from lite.gym.sandbox.types import SandboxTaskConfig

    uploaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(uploaded, dict):
        raise ValueError("task payload must be an object")
    canonical = None
    with CATALOG.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("metadata", {}).get("osworld_id") == uploaded.get("metadata", {}).get("osworld_id"):
                canonical = row
                break
    if canonical is None:
        raise ValueError("task is absent from the baked Lite.OSWorld catalog")
    if uploaded != canonical:
        raise ValueError("uploaded task does not match the baked Lite.OSWorld catalog")
    return SandboxTaskConfig(task_id=canonical["task_id"], instruction=canonical["instruction"], computer={}, max_steps=canonical.get("max_steps"), metadata=canonical["metadata"])


async def _execute(command: str, task_path: Path) -> dict[str, Any]:
    from lite.gym.envs.lite.osworld.src.utils.setup import setup_fn
    from lite.gym.envs.lite.osworld.src.utils.verify import evaluate_final_fn

    task = _load_task(task_path)
    interface = LocalExecStdioInterface()
    await interface.start()
    computer = SimpleNamespace(interface=interface)
    try:
        if command == "setup":
            await setup_fn(task, computer)
            return {"status": "ok", "task_id": task.task_id}
        value = await evaluate_final_fn(task, computer, actions=None)
        if isinstance(value, tuple):
            score, details = value
        else:
            score, details = value, None
        return {"status": "ok", "task_id": task.task_id, "score": float(score), "details": details}
    finally:
        await interface.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("setup", "evaluate"))
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    result = asyncio.run(_execute(args.command, Path(args.task)))
    print("AWMEVAL_LITE_OSWORLD_RESULT=" + json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
