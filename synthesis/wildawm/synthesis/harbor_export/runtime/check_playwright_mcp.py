#!/usr/bin/env python3
"""Verify the in-world Playwright MCP endpoint before OpenCode starts."""

from __future__ import annotations

import argparse
import asyncio
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

_REQUIRED_TOOLS = {
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_take_screenshot",
}


async def _list_tools(session) -> set[str]:
    result = await session.list_tools()
    return {tool.name for tool in result.tools}


async def _launch_browser(session) -> str:
    """Navigate to about:blank to force a real Chromium launch, catching launch failures at boot."""
    result = await session.call_tool("browser_navigate", {"url": "about:blank"})
    if result.isError:
        raise RuntimeError(
            "browser_navigate(about:blank) failed: "
            + "".join(getattr(p, "text", "") for p in result.content)[:400])
    return "browser launched"


async def _probe(url: str) -> tuple[int, str]:
    async with streamablehttp_client(url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await _list_tools(session)
            missing = sorted(_REQUIRED_TOOLS - tools)
            if missing:
                raise RuntimeError(
                    "Playwright MCP is missing required tools: "
                    + ", ".join(missing))
            launch = await _launch_browser(session)
    return len(tools), launch


async def _main(url: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            n_tools, launch = await _probe(url)
            print(
                f"Playwright MCP ready: {url} tools={n_tools} ({launch})",
                flush=True,
            )
            return
        except BaseException as error:
            last_error = error
            await asyncio.sleep(0.5)
    raise RuntimeError(
        f"Playwright MCP did not become ready within {timeout_seconds}s: "
        f"{type(last_error).__name__}: {last_error}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8931/mcp")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args()
    asyncio.run(_main(args.url, args.timeout_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
