"""Configuration models used by the vendored task-selection and sandbox-dispatch modules."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskFilterConfig(StrictConfigModel):
    skip_requires_serp: bool = False
    require_all_assets: bool = False


class VisionToolConfig(StrictConfigModel):
    """Optional text-only image description sidecar."""

    enabled: bool = False
    model_id: str | None = None
    base_url: str = "http://127.0.0.1:18003/v1"
    api_key: str | None = None
    reasoning_effort: str | None = None
    request_timeout_seconds: PositiveFloat = 60.0
    retry_attempts: PositiveInt = 3
    max_output_tokens: PositiveInt = 4096
    max_image_bytes: PositiveInt = 20 * 1024 * 1024
