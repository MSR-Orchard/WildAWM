from __future__ import annotations

import shlex
import subprocess
import time
import uuid
from pathlib import Path

from .base import ExecResult, Sandbox, SandboxError, tar_bytes, untar_bytes


def docker_memory(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value)
    for unit, short in (("Gi", "g"), ("Mi", "m"), ("G", "g"), ("M", "m")):
        if text.endswith(unit):
            return text[: -len(unit)] + short
    return text


class DockerSandbox(Sandbox):
    """A container on the local Docker daemon, kept alive by `sleep infinity` under an orphan-reaping init."""

    backend = "docker"

    def __init__(self, image: str, *, name: str | None = None, cpu: str | int | None = None, memory: str | None = None,
                 network: str | None = None, shm_size: str = "4g", volumes: list[str] = (), env: dict | None = None,
                 privileged: bool = False, extra_args: list[str] = ()):
        self.name = name or f"awmeval-{uuid.uuid4().hex[:12]}"
        self.loopback_host = None if network == "host" else "host.docker.internal"
        command = ["docker", "run", "-d", "--init", "--name", self.name, "--entrypoint", "sleep", "--shm-size", shm_size, "--label", "awmeval=1"]
        command += ["--network", network] if network else ["--add-host", "host.docker.internal:host-gateway"]
        if cpu:
            command += ["--cpus", str(cpu)]
        if memory:
            command += ["--memory", docker_memory(memory)]
        for volume in volumes:
            command += ["-v", volume]
        for key, value in (env or {}).items():
            command += ["-e", f"{key}={value}"]
        if privileged:
            command.append("--privileged")
        command += [*extra_args, image, "infinity"]
        started = subprocess.run(command, capture_output=True, text=True)
        if started.returncode != 0:
            raise SandboxError(f"docker run failed for {image}: {started.stderr.strip()[-2000:]}")

    def run(self, command: str, timeout: int) -> ExecResult:
        try:
            done = subprocess.run(["docker", "exec", self.name, "bash", "-c", command], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            return ExecResult(124, (error.stdout or b"").decode(errors="replace"), "docker exec timed out")
        return ExecResult(done.returncode, done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace"))

    def _pipe(self, command: str, data: bytes, timeout: int = 1800) -> None:
        done = subprocess.run(["docker", "exec", "-i", self.name, "bash", "-c", command], input=data, capture_output=True, timeout=timeout)
        if done.returncode != 0:
            raise SandboxError(f"docker exec failed: {done.stderr.decode(errors='replace')[-2000:]}")

    def write(self, path: str, data: bytes | str, mode: int | None = None) -> None:
        payload = data.encode() if isinstance(data, str) else data
        target = shlex.quote(path)
        chmod = f" && chmod {mode:o} {target}" if mode is not None else ""
        self._pipe(f"mkdir -p \"$(dirname {target})\" && cat > {target}{chmod}", payload)

    def read(self, path: str) -> bytes:
        done = subprocess.run(["docker", "exec", self.name, "cat", path], capture_output=True, timeout=1800)
        if done.returncode != 0:
            raise SandboxError(f"cannot read {path}: {done.stderr.decode(errors='replace')[-500:]}")
        return done.stdout

    def upload(self, local: str | Path, remote: str) -> None:
        local = Path(local)
        if not local.is_dir():
            return super().upload(local, remote)
        self._pipe(f"mkdir -p {shlex.quote(remote)} && tar -xzf - -C {shlex.quote(remote)}", tar_bytes(local))

    def download(self, remote: str, local: str | Path) -> None:
        if not self.exec(["test", "-d", remote], timeout=30).ok:
            return super().download(remote, local)
        done = subprocess.run(["docker", "exec", self.name, "tar", "-czf", "-", "-C", remote, "."], capture_output=True, timeout=1800)
        if done.returncode != 0:
            raise SandboxError(f"cannot download {remote}: {done.stderr.decode(errors='replace')[-500:]}")
        untar_bytes(done.stdout, Path(local))

    def close(self) -> None:
        for _ in range(3):
            if subprocess.run(["docker", "rm", "-f", self.name], capture_output=True).returncode == 0:
                return
            time.sleep(5)
