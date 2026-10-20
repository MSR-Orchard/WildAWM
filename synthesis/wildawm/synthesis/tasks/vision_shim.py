"""Shared describe_image MCP wiring for text-only synthesis models."""

import json
import sys
from pathlib import Path

from wildawm_common import DESCRIBE_IMAGE_MCP

from wildawm import const_config as cfg

CONTAINER_DESCRIBE_IMAGE_MCP = "/tmp/describe_image_mcp.py"

JUDGE_NOTE = """

## Reading files
Your model cannot see images directly. The `Read` tool is disabled in this
session. Read text files with Bash tools such as `cat`, `sqlite3`, `grep`, and
`diff`. For image files (.png/.jpg/.jpeg/.gif/.webp/.bmp), call the
`describe_image` tool with the image's absolute path and an optional focused
question. Treat the tool's text response as the image contents. If it returns a
`[describe_image error]`, cite that and fall back to DB/text evidence.
"""

JUDGE_IMAGE_NOTE = """
## Reading images with the describe_image tool
A `describe_image` MCP tool is available in this session. It describes any image
file (.png/.jpg/.jpeg/.gif/.webp/.bmp) given its ABSOLUTE path and an optional
focused question; treat its text reply as the image contents. Use it whenever
you need visual evidence from an image — for example to confirm a visual
attribute of a served image that page text alone does not reveal. If it
returns a `[describe_image error]`, cite that and fall back to DB/text evidence.
"""

JUDGE_IMAGE_NOTE_OC = """
## Reading images with the vision_describe_image tool
A `vision_describe_image` MCP tool is available in this session. It describes
any image file (.png/.jpg/.jpeg/.gif/.webp/.bmp) given its ABSOLUTE path and an
optional focused question; treat its text reply as the image contents. Use it
whenever you need visual evidence from an image — for example to confirm a
visual attribute of a served image that page text alone does not
reveal. Use `bash` (cat, sqlite3, grep, diff, curl) for all text/DB evidence.
If `vision_describe_image` returns a `[describe_image error]`, cite it and fall
back to text/DB evidence instead of trying to read the image bytes directly.
"""

GENERATOR_NOTE = """

## Text-only image grounding
Your model cannot see images directly. For this text-only model, the `Read`
tool and browser screenshot tool are disabled because they can return image
payloads. Use text-first browser tools, `curl`, `sqlite3`, `grep`, and
catalog/page text for most evidence. When a task needs a visual fact, save the
relevant image to a local file such as `/tmp/xai_image.jpg`, then call
`describe_image` with that absolute path and a focused question. Treat the
tool's text response as the image contents, record the visual evidence in
`reason.md` and `meta.json`, and then write the deliverables. If
`describe_image` returns a
`[describe_image error]`, fall back to another grounded anchor instead of
sending image files or screenshots to the model.
"""

SKETCH_NOTE = """

## Text-only image grounding
Your model cannot see images directly. For this text-only model, the `Read`
tool and browser screenshot tool are disabled because they can return image
payloads. Use Bash tools such as `cat`, `sqlite3`, `grep`, and `curl` for text
evidence. If a proposed sketch must hinge on a visual fact, save the relevant
image to a local file and call `describe_image` with that absolute path and a
focused question. Use the returned text as the visual evidence for the sketch.
"""


def mcp_config(script_path: str | Path = DESCRIBE_IMAGE_MCP, *,
               command: str | None = None) -> str:
    """Return a claude-code --mcp-config payload for the describe_image server."""
    vision = cfg.opencode_vision_spec()
    env = {
        "AWM_VISION_BASE_URL": cfg.container_host_url(vision["base_url"]),
        "AWM_VISION_API_KEY": vision.get("api_key", ""),
        "AWM_VISION_MODEL": vision["api_model"],
    }
    env.update(cfg.vision_reasoning_env())
    return json.dumps({
        "mcpServers": {
            "vision": {
                "type": "stdio",
                "command": command or sys.executable,
                "args": [str(script_path)],
                "env": env,
            }
        }
    })


def generator_mcp_config() -> str:
    """MCP config evaluated inside the generator container."""
    return mcp_config(CONTAINER_DESCRIBE_IMAGE_MCP, command="python3")


def generator_disallowed_tools() -> str:
    """Tools that can hand text-only generator models raw image payloads."""
    return "Read,mcp__browser__browser_take_screenshot"
