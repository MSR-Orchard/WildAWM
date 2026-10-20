from __future__ import annotations

import io
import shlex
import tempfile
import time
import uuid
from pathlib import Path

from .base import RUN_DIR, ExecResult, Sandbox, SandboxError, write_tar

CHUNK = 8 * 1024 * 1024


class OrchardSandbox(Sandbox):
    """A pod on an Orchard Env cluster (https://github.com/microsoft/Orchard-Agentic)."""

    backend = "orchard"

    def __init__(self, image: str, *, base_url: str, api_key: str, name: str | None = None, cpu: str | int | None = None,
                 memory: str | None = None, create_timeout: int = 1800, loopback_host: str | None = None, retries: int = 3):
        try:
            from orchard_env import SandboxClient
        except ImportError as error:
            raise SandboxError("the orchard backend needs orchard-env: uv sync --extra orchard") from error
        self.loopback_host = loopback_host
        self.client = SandboxClient(base_url=base_url, api_key=api_key)
        for attempt in range(retries):
            try:
                sandbox_id = (name or f"awmeval-{uuid.uuid4().hex[:8]}")[:48] if attempt == 0 else None
                self.sandbox = self.client.create_sandbox(image, block_network=False, sandbox_id=sandbox_id, cpu=str(cpu) if cpu else None, memory=memory, timeout=create_timeout)
                break
            except Exception as error:
                if attempt == retries - 1:
                    raise SandboxError(f"orchard create failed for {image}: {error}") from error
                time.sleep(10 * (attempt + 1))
        self.sandbox.start_heartbeat(30)

    def run(self, command: str, timeout: int) -> ExecResult:
        """Submit once, then poll the job (the SDK's blocking exec re-submits the command when its wait request fails)."""
        job = self.client._request("POST", f"/sandboxes/{self.sandbox.sandbox_id}/exec", json={"command": command, "timeout_seconds": timeout, "wait": False})
        deadline, delay = time.monotonic() + timeout + 300, 0.2
        while True:
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)
            try:
                result = self.sandbox.get_job(job["job_id"])
            except Exception:
                if time.monotonic() > deadline:
                    raise
                continue
            if result.is_complete:
                return ExecResult(result.exit_code if result.exit_code is not None else -1, result.stdout or "", result.stderr or result.error or "")
            if time.monotonic() > deadline:
                raise SandboxError(f"job {job['job_id']} did not finish within {timeout}s")

    def send(self, stream) -> str:
        """Upload a stream as numbered 8 MiB parts; returns their common path stem."""
        stem = f"{RUN_DIR}/chunk-{uuid.uuid4().hex}"
        index = 0
        while (chunk := stream.read(CHUNK)) or index == 0:
            self.sandbox.upload_content(chunk, f"{stem}.{index:05d}")
            index += 1
        return stem

    def write(self, path: str, data: bytes | str, mode: int | None = None) -> None:
        payload = data.encode() if isinstance(data, str) else data
        if len(payload) <= CHUNK:
            self.sandbox.upload_content(payload, path)
        else:
            stem, target = self.send(io.BytesIO(payload)), shlex.quote(path)
            self.exec(f"mkdir -p \"$(dirname {target})\" && cat {stem}.* > {target} && rm -f {stem}.*", timeout=1800).check(f"assemble {path}")
        if mode is not None:
            self.exec(["chmod", f"{mode:o}", path], timeout=60).check(f"chmod {path}")

    def upload(self, local: str | Path, remote: str) -> None:
        """Stream large files and directories from disk in parts instead of holding them in memory."""
        local, target = Path(local), shlex.quote(remote)
        if not local.is_dir():
            if local.stat().st_size <= CHUNK:
                return self.write(remote, local.read_bytes())
            with local.open("rb") as stream:
                stem = self.send(stream)
            self.exec(f"mkdir -p \"$(dirname {target})\" && cat {stem}.* > {target} && rm -f {stem}.*", timeout=7200).check(f"upload {local}")
            return
        with tempfile.TemporaryFile() as staged:
            write_tar(local, staged, compresslevel=1)
            staged.seek(0)
            stem = self.send(staged)
        self.exec(f"mkdir -p {target} && cat {stem}.* | tar -xzf - -C {target} && rm -f {stem}.*", timeout=7200).check(f"upload {local}")

    def read(self, path: str) -> bytes:
        size = self.exec(["stat", "-c", "%s", path], timeout=60).check(f"stat {path}").stdout.strip()
        if int(size) <= CHUNK:
            return self.sandbox.download_content(path)
        stem = f"{RUN_DIR}/split-{uuid.uuid4().hex}."
        self.exec(f"mkdir -p {RUN_DIR} && split -b {CHUNK} -d -a 5 {shlex.quote(path)} {stem}", timeout=1800).check(f"split {path}")
        parts = sorted(self.exec(f"ls {stem}*", timeout=60).stdout.split())
        try:
            return b"".join(self.sandbox.download_content(part) for part in parts)
        finally:
            self.exec(f"rm -f {stem}*", timeout=60)

    def close(self) -> None:
        try:
            self.sandbox.stop_heartbeat()
            self.sandbox.delete()
        except Exception:
            pass
        self.client.close()
