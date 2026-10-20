"""Code shared by the WildAWM synthesis and evaluation components."""

from pathlib import Path

#: Single-file describe_image MCP asset: copy or upload it, never import it (starts a stdio server).
DESCRIBE_IMAGE_MCP = Path(__file__).resolve().parent / "assets" / "describe_image_mcp.py"


def describe_image_mcp_path() -> Path:
    """Path of the canonical describe_image MCP asset."""
    return DESCRIBE_IMAGE_MCP
