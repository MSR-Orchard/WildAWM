from __future__ import annotations

import io
import shlex
import tarfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

LOOPBACK = {"127.0.0.1", "localhost", "0.0.0.0"}
RUN_DIR = "/tmp/awmeval"
LIVE: set = set()
STOP = threading.Event()


class SandboxError(RuntimeError):
    pass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def check(self, what: str = "command") -> "ExecResult":
        if self.exit_code != 0:
            raise SandboxError(f"{what} failed with exit code {self.exit_code}: {(self.stderr or self.stdout)[-2000:]}")
        return self


def shell_command(cmd: str | list[str], *, env: dict | None = None, cwd: str | None = None, user: str | None = None) -> str:
    """Render one bash command with cwd, env and an optional target user."""
    script = cmd if isinstance(cmd, str) else shlex.join(cmd)
    if cwd:
        script = f"cd {shlex.quote(cwd)} && {script}"
    exports = " ".join(f"{key}={shlex.quote(str(value))}" for key, value in (env or {}).items())
    inner = f"env {exports} bash -c {shlex.quote(script)}" if exports else f"bash -c {shlex.quote(script)}"
    return f"/usr/sbin/runuser -u {shlex.quote(user)} -- {inner}" if user else inner


def write_tar(local: Path, stream, compresslevel: int = 9) -> None:
    with tarfile.open(fileobj=stream, mode="w:gz", compresslevel=compresslevel) as archive:
        for child in sorted(local.iterdir()):
            archive.add(child, arcname=child.name, filter=lambda info: None if "__pycache__" in info.name else info)


def tar_bytes(local: Path) -> bytes:
    buffer = io.BytesIO()
    write_tar(local, buffer)
    return buffer.getvalue()


def untar_bytes(data: bytes, local: Path) -> None:
    local.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        archive.extractall(local, filter="tar")


class Sandbox:
    """One isolated container driven by exec and file transfer."""

    backend = ""
    loopback_host: str | None = None

    def run(self, command: str, timeout: int) -> ExecResult:
        raise NotImplementedError

    def write(self, path: str, data: bytes | str, mode: int | None = None) -> None:
        raise NotImplementedError

    def read(self, path: str) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def exec(self, cmd: str | list[str], timeout: int = 600, env: dict | None = None, cwd: str | None = None, user: str | None = None) -> ExecResult:
        command = shell_command(cmd, env=env, cwd=cwd, user=user)
        return self.run(f"timeout -k 10 {int(timeout)} {command}", timeout + 30)

    def spawn(self, cmd: str | list[str], name: str, env: dict | None = None, cwd: str | None = None, user: str | None = None) -> None:
        """Start a background process with its log under /tmp/awmeval/logs."""
        command = shell_command(cmd, env=env, cwd=cwd, user=user)
        log = f"{RUN_DIR}/logs/{name}.log"
        script = f"mkdir -p {RUN_DIR}/logs && {{ nohup setsid {command} > {log} 2>&1 < /dev/null & echo $! > {RUN_DIR}/logs/{name}.pid; }}"
        self.run(f"bash -c {shlex.quote(script)}", 60).check(f"spawn {name}")

    def alive(self, name: str) -> bool:
        return self.exec(f"kill -0 \"$(cat {RUN_DIR}/logs/{name}.pid)\"", timeout=30).ok

    def log(self, name: str, tail: int = 4000) -> str:
        return self.exec(f"tail -c {tail} {RUN_DIR}/logs/{name}.log 2>/dev/null", timeout=30).stdout

    def exists(self, path: str) -> bool:
        return self.exec(["test", "-e", path], timeout=30).ok

    def upload(self, local: str | Path, remote: str) -> None:
        local = Path(local)
        if not local.is_dir():
            self.write(remote, local.read_bytes())
            return
        staged = f"{RUN_DIR}/upload-{uuid.uuid4().hex}.tgz"
        self.write(staged, tar_bytes(local))
        self.exec(f"mkdir -p {shlex.quote(remote)} && tar -xzf {staged} -C {shlex.quote(remote)} && rm -f {staged}", timeout=1800).check(f"upload {local}")

    def download(self, remote: str, local: str | Path) -> None:
        local = Path(local)
        if not self.exec(["test", "-d", remote], timeout=30).ok:
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(self.read(remote))
            return
        staged = f"{RUN_DIR}/download-{uuid.uuid4().hex}.tgz"
        self.exec(f"mkdir -p {RUN_DIR} && tar -czf {staged} -C {shlex.quote(remote)} .", timeout=1800).check(f"download {remote}")
        try:
            untar_bytes(self.read(staged), local)
        finally:
            self.exec(["rm", "-f", staged], timeout=60)

    def url(self, url: str) -> str:
        """Rewrite a host-loopback URL to the address this sandbox can reach."""
        parts = urlsplit(url)
        if not self.loopback_host or parts.hostname not in LOOPBACK:
            return url
        netloc = self.loopback_host + (f":{parts.port}" if parts.port else "")
        return urlunsplit(parts._replace(netloc=netloc))

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *_exc) -> None:
        try:
            self.close()
        finally:
            LIVE.discard(self)
