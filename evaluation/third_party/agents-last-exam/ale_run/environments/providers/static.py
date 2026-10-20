"""StaticProvider — wraps an already-running VM by its cua-server endpoint.

No VM lifecycle of its own: ``acquire`` returns a handle pointing at the
configured URL, ``release`` is a no-op (unless ``cleanup_on_release`` runs a
shell snippet to scrub the VM between iterations).

Used for:

- **Image baking**: bring up a VM by hand, iterate on deployer code without
  paying the 3-5 min boot cost on every run.
- **Dev debug**: poke at a long-running staging VM, reproduce stuck cases.
- **Tests against a fixed scratch VM**: stable IP, deterministic state.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from ...base_interface import Provider, ReleaseMode, SandboxHandle, SandboxSpec

logger = logging.getLogger(__name__)


# ======================================================================
# Config
# ======================================================================


@dataclasses.dataclass(frozen=True)
class StaticProviderConfig:
    """Pin to a pre-existing VM."""

    endpoint: str
    """Full cua-server URL, e.g. ``http://1.2.3.4:5000``."""

    image: str = "ale-ubuntu22"
    """Image family the static VM was built from. Determines the
    SandboxHandle's baked paths. Must be registered in
    :mod:`ale_run.environments.images`."""

    vm_id: str = "static"
    """Informational id — shows up in logs / run.json."""

    cleanup_on_release: bool = False
    """If True, run :attr:`cleanup_script` on the VM during ``release``.
    The VM itself is never destroyed."""

    cleanup_script: str | None = None
    """Shell snippet executed via ``sandbox.run_command`` on release.
    Only runs when ``cleanup_on_release`` is True."""

    co_located_filesystem: bool = False
    """Whether the orchestration process shares the sandbox filesystem."""

    desktop_uid: int = 1000
    desktop_gid: int = 1000


def _build_provider_config(raw: dict[str, Any]) -> StaticProviderConfig:
    # back-compat: yaml ``os: linux`` (old) maps to the linux default image.
    image = raw.get("image")
    if image is None:
        os_legacy = raw.get("os") or "linux"
        image = "ale-ubuntu22" if os_legacy == "linux" else "ale-win10"
    return StaticProviderConfig(
        endpoint=str(raw["endpoint"]),
        image=str(image),
        vm_id=str(raw.get("vm_id") or "static"),
        cleanup_on_release=bool(raw.get("cleanup_on_release", False)),
        cleanup_script=raw.get("cleanup_script"),
        co_located_filesystem=bool(raw.get("co_located_filesystem", False)),
        desktop_uid=int(raw.get("desktop_uid", 1000)),
        desktop_gid=int(raw.get("desktop_gid", 1000)),
    )


# ======================================================================
# Provider
# ======================================================================


class StaticProvider(Provider):
    """Provider impl that skips ``gcloud create / delete``."""

    def __init__(self, config: StaticProviderConfig | dict[str, Any]):
        if isinstance(config, dict):
            config = _build_provider_config(config)
        self._cfg = config

    async def acquire(self, spec: SandboxSpec) -> SandboxHandle:
        from ..images import get as get_image

        image = get_image(self._cfg.image)
        return SandboxHandle(
            id=self._cfg.vm_id,
            endpoint=self._cfg.endpoint,
            os=image.os,
            **image.sandbox_paths(),
            metadata={
                "provider": "static",
                "static": True,
                "snapshot": spec.snapshot,
                "image": image.name,
                "co_located_filesystem": self._cfg.co_located_filesystem,
                "desktop_uid": self._cfg.desktop_uid,
                "desktop_gid": self._cfg.desktop_gid,
            },
        )

    async def release(self, sandbox: SandboxHandle, *, mode: ReleaseMode = "keep") -> None:
        if not self._cfg.cleanup_on_release or not self._cfg.cleanup_script:
            return
        try:
            await sandbox.run_command(self._cfg.cleanup_script, timeout=120)
        except Exception as e:
            logger.warning("StaticProvider cleanup failed: %s", e)

    def open_session(self, vm: SandboxHandle) -> Any:
        from cua_bench.computers.remote import RemoteDesktopSession

        from .gcloud import _init_computer_skip_wait

        session = RemoteDesktopSession(api_url=vm.endpoint, os_type=vm.os)
        _init_computer_skip_wait(session)
        return session
