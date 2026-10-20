#!/usr/bin/env python3
"""Shell helper to call WildAWM MCP services reliably from inside the agent
container. Speaks the MCP Streamable-HTTP protocol via the installed `mcp`
SDK (the gateway needs an initialize handshake + session id, so plain curl
does not work).

Usage:
  mcp_call.py <service>                         # list tools for a service
  mcp_call.py <service> <tool> '<json-args>'    # call a tool

Examples:
  mcp_call.py email
  mcp_call.py email send_email '{"to_recipients":["a@b.com"],"subject":"hi","body":"yo"}'
"""
import sys
import json
import asyncio
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

GATEWAY = os.environ.get(
    "AWM_MCP_GATEWAY_URL",
    f"http://mcp:{os.environ.get('AWM_MCP_INTERNAL_PORT', '8080')}",
)


async def _run():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    svc = sys.argv[1]
    tool = sys.argv[2] if len(sys.argv) > 2 else None
    raw_args = sys.argv[3] if len(sys.argv) > 3 else "{}"
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as e:
        print(f"ERROR: args is not valid JSON: {e}", file=sys.stderr)
        return 2

    url = f"{GATEWAY}/{svc}/mcp"
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if not tool or tool in ("list", "--list", "tools"):
                res = await session.list_tools()
                print(json.dumps(
                    [{"name": t.name, "description": (t.description or "")[:200]}
                     for t in res.tools], indent=2))
                return 0
            res = await session.call_tool(tool, args)
            chunks = []
            for c in res.content:
                chunks.append(getattr(c, "text", None) or str(c))
            print("\n".join(chunks))
            if getattr(res, "isError", False):
                return 1
    return 0


def main():
    try:
        sys.exit(asyncio.run(_run()))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
