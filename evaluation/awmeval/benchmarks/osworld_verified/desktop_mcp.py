#!/opt/awmeval-agent/venv/bin/python
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

from mcp.server.fastmcp import FastMCP

os.environ.update({"DISPLAY": ":1", "HOME": "/home/user", "XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"})
WORKSPACE = Path(os.environ.get("AWMEVAL_WORKSPACE", "/workspace"))
LOG = Path(os.environ.get("AWMEVAL_ACTION_LOG", "/tmp/lite-osworld-actions.jsonl"))
XDO = "/opt/env/bin/xdotool"
VALID_BUTTONS = {"left": "1", "middle": "2", "right": "3"}
KEY_ALIASES = {
    "page up": "Page_Up", "pageup": "Page_Up", "pgup": "Page_Up",
    "page down": "Page_Down", "pagedown": "Page_Down", "pgdn": "Page_Down",
    "arrow up": "Up", "arrow down": "Down", "arrow left": "Left", "arrow right": "Right",
    "return": "Return", "enter": "Return", "escape": "Escape", "esc": "Escape",
    "backspace": "BackSpace", "delete": "Delete", "insert": "Insert",
    "home": "Home", "end": "End", "tab": "Tab", "space": "space",
}
SCREENSHOT_SLOTS = 32  # captures recycle through a fixed ring instead of accumulating one file per action
mcp = FastMCP("osworld")
_action_index = 0
_capture_index = 0


def _run(argv: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=os.environ, check=False)


def _xdo(*args: object, timeout: float = 60.0) -> str:
    result = _run([XDO, *[str(value) for value in args]], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"xdotool exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def _record(kind: str, **values: object) -> int:
    global _action_index
    _action_index += 1
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"index": _action_index, "at": time.time(), "kind": kind, **values}, ensure_ascii=False) + "\n")
    return _action_index


def _screenshot() -> str:
    global _capture_index
    _capture_index += 1
    target = WORKSPACE / "screenshots" / f"screen_{_capture_index % SCREENSHOT_SLOTS:04d}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("lite_stdio_server", "/opt/lite/stdio_server.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("lite.osworld screenshot backend is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = module.op_screenshot({})
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("lite.osworld screenshot backend returned invalid PNG data")
    target.write_bytes(payload)
    return str(target)


def _after(kind: str, **values: object) -> str:
    index = _record(kind, **values)
    time.sleep(0.8)
    return json.dumps({"action_index": index, "screenshot_path": _screenshot()})


def _button(value: str) -> str:
    if value not in VALID_BUTTONS:
        raise ValueError("button must be left, middle, or right")
    return VALID_BUTTONS[value]


def _key(value: str) -> str:
    normalized = " ".join(str(value).strip().split())
    if not normalized:
        raise ValueError("key must not be empty")
    return KEY_ALIASES.get(normalized.casefold(), normalized.replace(" ", "_"))


@mcp.tool(name="screenshot")
def screenshot() -> dict[str, object]:
    """Capture the current 1920x1080 Lite.OSWorld desktop. Use vision_describe_image on the returned PNG before choosing coordinates."""
    return {"screenshot_path": _screenshot()}


@mcp.tool(name="screen_size")
def screen_size() -> dict[str, int]:
    """Return the Lite.OSWorld desktop dimensions."""
    width, height = _xdo("getdisplaygeometry").split()[:2]
    return {"width": int(width), "height": int(height)}


@mcp.tool(name="accessibility_tree")
def accessibility_tree(query: str = "", limit: int = 100) -> dict[str, object]:
    """Return named controls from the current GNOME accessibility tree. Supply a focused query to reduce the result."""
    with urllib.request.urlopen("http://127.0.0.1:5000/accessibility", timeout=20) as response:
        payload = json.loads(response.read())
    raw = str(payload.get("AT") or "")
    if not raw:
        return {"query": query, "matches": [], "error": "accessibility tree unavailable"}
    needle = query.casefold().strip()
    bound = max(1, min(int(limit), 300))
    matches: list[dict[str, object]] = []
    truncated = False
    try:
        root = ElementTree.fromstring(raw)
        for element in root.iter():
            name = element.attrib.get("name", "")
            text = (element.text or "").strip()
            haystack = " ".join((element.tag, name, text, json.dumps(element.attrib, ensure_ascii=False))).casefold()
            if needle and needle not in haystack:
                continue
            if not needle and not name and not text:
                continue
            if len(matches) >= bound:
                truncated = True
                break
            matches.append({"role": element.tag.rsplit("}", 1)[-1], "name": name, "text": text, "attributes": element.attrib})
    except ElementTree.ParseError as error:
        return {"query": query, "matches": [], "error": f"invalid accessibility XML: {error}"}
    return {"query": query, "matches": matches, "truncated": truncated}


@mcp.tool(name="mouse_position")
def mouse_position() -> dict[str, int]:
    """Return the current mouse pointer position."""
    values = {}
    for line in _xdo("getmouselocation", "--shell").splitlines():
        key, _, value = line.partition("=")
        if key in {"X", "Y"} and value.lstrip("-").isdigit():
            values[key.lower()] = int(value)
    return {"x": values.get("x", 0), "y": values.get("y", 0)}


@mcp.tool(name="move")
def move(x: int, y: int) -> str:
    """Move the pointer to an absolute desktop coordinate."""
    _xdo("mousemove", int(x), int(y))
    return _after("move", x=int(x), y=int(y))


@mcp.tool(name="click")
def click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """Click an absolute desktop coordinate."""
    _xdo("mousemove", int(x), int(y))
    _xdo("click", "--repeat", max(1, min(int(clicks), 10)), "--delay", 120, _button(button))
    return _after("click", x=int(x), y=int(y), button=button, clicks=int(clicks))


@mcp.tool(name="double_click")
def double_click(x: int, y: int, button: str = "left") -> str:
    """Double-click an absolute desktop coordinate."""
    return click(x, y, button, 2)


@mcp.tool(name="right_click")
def right_click(x: int, y: int) -> str:
    """Right-click an absolute desktop coordinate."""
    return click(x, y, "right", 1)


@mcp.tool(name="mouse_down")
def mouse_down(button: str = "left", x: int | None = None, y: int | None = None) -> str:
    """Hold a mouse button, optionally after moving to a coordinate."""
    if x is not None and y is not None:
        _xdo("mousemove", int(x), int(y))
    _xdo("mousedown", _button(button))
    return _after("mouse_down", button=button, x=x, y=y)


@mcp.tool(name="mouse_up")
def mouse_up(button: str = "left", x: int | None = None, y: int | None = None) -> str:
    """Release a mouse button, optionally after moving to a coordinate."""
    if x is not None and y is not None:
        _xdo("mousemove", int(x), int(y))
    _xdo("mouseup", _button(button))
    return _after("mouse_up", button=button, x=x, y=y)


@mcp.tool(name="drag")
def drag(start_x: int, start_y: int, end_x: int, end_y: int, button: str = "left", duration: float = 1.0) -> str:
    """Drag between absolute desktop coordinates."""
    _xdo("mousemove", int(start_x), int(start_y))
    _xdo("mousedown", _button(button))
    try:
        time.sleep(0.25)
        steps = max(2, min(100, int(max(abs(end_x - start_x), abs(end_y - start_y)) / 15)))
        delay = max(0.015, min(float(duration), 5.0) / steps)
        for index in range(1, steps + 1):
            x = round(start_x + (end_x - start_x) * index / steps)
            y = round(start_y + (end_y - start_y) * index / steps)
            _xdo("mousemove", x, y)
            time.sleep(delay)
        time.sleep(0.25)
    finally:
        _xdo("mouseup", _button(button))
    return _after("drag", start=[start_x, start_y], end=[end_x, end_y], button=button)


@mcp.tool(name="scroll")
def scroll(dy: int = 0, dx: int = 0, x: int | None = None, y: int | None = None) -> str:
    """Scroll the desktop. Positive dy scrolls up; negative dy scrolls down. Positive dx scrolls right."""
    if x is not None and y is not None:
        _xdo("mousemove", int(x), int(y))
    if dy:
        _xdo("click", "--repeat", abs(int(dy)), "--delay", 30, 4 if dy > 0 else 5)
    if dx:
        _xdo("click", "--repeat", abs(int(dx)), "--delay", 30, 7 if dx > 0 else 6)
    return _after("scroll", dy=int(dy), dx=int(dx), x=x, y=y)


@mcp.tool(name="type_text")
def type_text(text: str, interval: float = 0.012) -> str:
    """Type text into the focused desktop control."""
    delay_ms = max(0, min(500, round(float(interval) * 1000)))
    for index, row in enumerate(text.split("\n")):
        if index:
            _xdo("key", "Return")
        if row:
            _xdo("type", "--clearmodifiers", "--delay", delay_ms, "--", row, timeout=max(60, len(row) * interval * 3 + 30))
    return _after("type_text", characters=len(text))


@mcp.tool(name="press")
def press(key: str, presses: int = 1) -> str:
    """Press one keyboard key one or more times."""
    projected = _key(key)
    for _ in range(max(1, min(int(presses), 20))):
        _xdo("key", "--clearmodifiers", projected)
    return _after("press", key=key, projected_key=projected, presses=int(presses))


@mcp.tool(name="hotkey")
def hotkey(keys: list[str]) -> str:
    """Press a keyboard shortcut such as ['ctrl','s']."""
    if not keys:
        raise ValueError("keys must not be empty")
    projected = [_key(key) for key in keys]
    _xdo("key", "--clearmodifiers", "+".join(projected))
    return _after("hotkey", keys=keys, projected_keys=projected)


@mcp.tool(name="key_down")
def key_down(key: str) -> str:
    """Hold one keyboard key."""
    projected = _key(key)
    _xdo("keydown", projected)
    return _after("key_down", key=key, projected_key=projected)


@mcp.tool(name="key_up")
def key_up(key: str) -> str:
    """Release one keyboard key."""
    projected = _key(key)
    _xdo("keyup", projected)
    return _after("key_up", key=key, projected_key=projected)


@mcp.tool(name="wait")
def wait(seconds: float = 2.0) -> dict[str, object]:
    """Wait for applications to settle and capture a fresh screenshot."""
    time.sleep(max(0.0, min(float(seconds), 30.0)))
    return {"screenshot_path": _screenshot()}


@mcp.tool(name="done")
def done(summary: str = "Task complete") -> str:
    """Mark the task complete after verifying every requested final state. The Lite.OSWorld evaluator remains authoritative."""
    marker = WORKSPACE / ".awmeval-complete.md"
    marker.write_text(summary.strip() or "Task complete", encoding="utf-8")
    _record("done", summary=summary)
    return "Task marked complete; Lite.OSWorld evaluation will judge the desktop state."


@mcp.tool(name="fail")
def fail(reason: str) -> str:
    """Mark the task infeasible. The 330-task scored set excludes tasks whose canonical evaluator expects infeasibility."""
    marker = WORKSPACE / ".awmeval-complete.md"
    marker.write_text("Infeasible: " + reason.strip(), encoding="utf-8")
    _record("fail", reason=reason)
    return "Task marked infeasible."


if __name__ == "__main__":
    mcp.run()
