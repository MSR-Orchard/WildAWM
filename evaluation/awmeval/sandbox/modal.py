from __future__ import annotations

import base64
import io
import json
import os
import shlex
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .base import RUN_DIR, ExecResult, Sandbox, SandboxError, shell_command, untar_bytes, write_tar

CHUNK = 8 * 1024 * 1024
RETRY_STATUS = {429, 500, 502, 503, 504}


class _ServiceError(SandboxError):
    """A service call that failed with an HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ModalSandbox(Sandbox):
    """Remote Modal-compatible sandbox: commands run as polled jobs (one at a time), files via REST."""

    backend = "modal"

    def __init__(self, image: str, *, base_url: str | None = None, api_key: str | None = None, name: str | None = None,
                 cpu: str | int | None = None, memory: str | None = None, create_timeout: int = 1800, heartbeat: float = 30,
                 request_timeout: float = 1200, loopback_host: str | None = None, retries: int = 3):
        self.base_url = (base_url or os.environ.get("SANDBOX_BASE_URL") or "").rstrip("/")
        if not self.base_url:
            raise SandboxError("the modal backend needs a service URL: set sandbox.modal.base_url or SANDBOX_BASE_URL")
        self.api_key = api_key or os.environ.get("SANDBOX_API_KEY") or ""
        self.request_timeout = float(request_timeout)
        self.create_timeout = int(create_timeout)
        self.heartbeat = float(heartbeat)
        self.loopback_host = loopback_host
        self.sandbox_id = ""
        self.image = image
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        try:
            created = self._create(image, name, cpu, memory, retries)
            self.sandbox_id = str(created["sandbox_id"])
            self.image = str(created.get("image") or image)
            self._wait_ready(created)
        except BaseException:
            self._delete()
            raise
        self._start_heartbeat()

    def _create(self, image: str, name: str | None, cpu, memory, retries: int) -> dict:
        """Create the sandbox, retrying transient failures; the requested id is used on the first attempt only."""
        error: Exception | None = None
        for attempt in range(retries):
            body: dict = {"image": image, "block_network": False, "timeout": self.create_timeout}
            if cpu:
                body["cpu"] = str(cpu)
            if memory:
                body["memory"] = str(memory)
            if name and attempt == 0:
                body["sandbox_id"] = name
            try:
                return self._request("POST", "/sandboxes", body=body, retry=False)
            except SandboxError as failure:
                error = failure
                if attempt < retries - 1:
                    time.sleep(10 * (attempt + 1))
        raise SandboxError(f"modal create failed for {image}: {error}") from error

    def _wait_ready(self, created: dict) -> None:
        """Block until the sandbox is ready, through the service wait with polling as the fallback."""
        try:
            response = self._request("GET", self._sandbox_path("wait"), params={"timeout": self.create_timeout},
                                     timeout=self.create_timeout + 60)
        except _ServiceError as failure:
            if failure.status == 408:
                raise SandboxError(f"sandbox {self.sandbox_id} did not become ready within {self.create_timeout}s") from failure
            if failure.status not in (404, 405):
                raise
        else:
            if response.get("ready") or response.get("status") == "ready":
                self._require_image(response)
                return
            if response.get("status") == "failed":
                raise SandboxError(f"sandbox {self.sandbox_id} failed: {response.get('status_message', '')}")
            raise SandboxError(f"sandbox {self.sandbox_id} wait returned {response.get('status')!r}")
        deadline = time.monotonic() + self.create_timeout
        while time.monotonic() < deadline:
            status = self._request("GET", self._sandbox_path(""))
            if status.get("ready") or status.get("status") == "ready":
                self._require_image(status)
                return
            if status.get("status") == "failed":
                raise SandboxError(f"sandbox {self.sandbox_id} failed: {status.get('status_message', '')}")
            time.sleep(2)
        raise SandboxError(f"sandbox {self.sandbox_id} did not become ready within {self.create_timeout}s (created: {created.get('status')!r})")

    def _require_image(self, response: dict) -> None:
        image = response.get("image")
        if image:
            self.image = str(image)

    def _start_heartbeat(self) -> None:
        """Renew the sandbox TTL in the background so long evaluations outlive the create timeout."""
        def loop() -> None:
            while not self._heartbeat_stop.wait(self.heartbeat):
                try:
                    self._request("POST", self._sandbox_path("heartbeat"), retry=False, timeout=min(60.0, self.request_timeout))
                except Exception:
                    pass
        self._heartbeat_thread = threading.Thread(target=loop, daemon=True, name=f"modal-heartbeat-{self.sandbox_id[-24:]}")
        self._heartbeat_thread.start()

    def _sandbox_path(self, suffix: str) -> str:
        prefix = f"/sandboxes/{urllib.parse.quote(self.sandbox_id, safe='')}"
        return f"{prefix}/{suffix}" if suffix else prefix

    def _request(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None,
                 timeout: float | None = None, retry: bool = True) -> dict:
        """Call the service REST API with JSON, retrying connection failures and transient statuses."""
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempts = 3 if retry else 1
        for attempt in range(attempts):
            try:
                request = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(request, timeout=timeout or self.request_timeout) as response:
                    payload = response.read()
            except urllib.error.HTTPError as failure:
                detail = failure.read()[:2048].decode("utf-8", "replace").strip()
                error = _ServiceError(failure.code, f"{method} {path} failed with HTTP {failure.code}: {detail}")
                if failure.code not in RETRY_STATUS or attempt == attempts - 1:
                    raise error from failure
            except (urllib.error.URLError, TimeoutError, ConnectionError) as failure:
                if attempt == attempts - 1:
                    raise SandboxError(f"{method} {path} failed: {failure}") from failure
            else:
                return self._json(payload, f"{method} {path}")
            time.sleep(1.0 * (2 ** attempt))
        raise SandboxError(f"{method} {path} did not complete")

    @staticmethod
    def _json(payload: bytes, what: str) -> dict:
        if not payload:
            return {}
        try:
            value = json.loads(payload)
        except ValueError as error:
            raise SandboxError(f"{what} returned non-JSON: {payload[:200]!r}") from error
        if not isinstance(value, dict):
            raise SandboxError(f"{what} returned non-object JSON")
        return value

    def run(self, command: str, timeout: int) -> ExecResult:
        """Submit a job once, then poll it (a server-side wait can return while the job is still running)."""
        job = self._submit(command, timeout)
        deadline, delay = time.monotonic() + timeout + 300, 0.2
        while True:
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)
            try:
                result = self._request("GET", f"/jobs/{urllib.parse.quote(job, safe='')}")
            except SandboxError:
                if time.monotonic() > deadline:
                    raise
                continue
            if result.get("status") in ("succeeded", "failed"):
                exit_code = result.get("exit_code")
                return ExecResult(-1 if exit_code is None else int(exit_code), result.get("stdout") or "", result.get("stderr") or result.get("error") or "")
            if time.monotonic() > deadline:
                raise SandboxError(f"job {job} did not finish within {timeout}s")

    def _submit(self, command: str, timeout: int | None) -> str:
        """Submit one exec job without a server-side wait and return its id."""
        body = {"command": command, "timeout_seconds": timeout, "wait": False}
        response = self._request("POST", self._sandbox_path("exec"), body=body, retry=False)
        job = str(response.get("job_id") or "")
        if not job:
            raise SandboxError("modal exec returned no job id")
        return job

    def spawn(self, cmd: str | list[str], name: str, env: dict | None = None, cwd: str | None = None, user: str | None = None) -> None:
        """Start a detached process from a short exec; a long-running job would block every later call."""
        command = shell_command(cmd, env=env, cwd=cwd, user=user)
        log = f"{RUN_DIR}/logs/{name}.log"
        script = f"mkdir -p {RUN_DIR}/logs && {{ nohup setsid {command} > {log} 2>&1 < /dev/null & echo $! > {RUN_DIR}/logs/{name}.pid; }}"
        self.run(f"bash -c {shlex.quote(script)}", 60).check(f"spawn {name}")

    def alive(self, name: str) -> bool:
        """Pid file plus /proc via the files API; an exec probe would queue behind a running job."""
        try:
            pid = self._download_content(f"{RUN_DIR}/logs/{name}.pid").decode(errors="replace").strip()
        except SandboxError:
            return False
        if not pid.isdigit():
            return False
        try:
            self._download_content(f"/proc/{pid}/stat")
            return True
        except SandboxError:
            return False

    def log(self, name: str, tail: int = 4000) -> str:
        """Tail of a spawned process's log file, read whole through the files API so no job queue is involved."""
        try:
            return self._download_content(f"{RUN_DIR}/logs/{name}.log").decode(errors="replace")[-tail:]
        except SandboxError:
            return ""

    def send(self, stream) -> str:
        """Upload a stream as numbered 8 MiB parts; returns their common path stem."""
        self.exec(["mkdir", "-p", RUN_DIR], timeout=60).check("create run dir")
        stem = f"{RUN_DIR}/chunk-{uuid.uuid4().hex}"
        index = 0
        while (chunk := stream.read(CHUNK)) or index == 0:
            self._write_content(f"{stem}.{index:05d}", chunk)
            index += 1
        return stem

    def _write_content(self, path: str, payload: bytes) -> None:
        self._request("POST", self._sandbox_path("files"), body={"path": path, "content": base64.b64encode(payload).decode("ascii")})

    def write(self, path: str, data: bytes | str, mode: int | None = None) -> None:
        """Upload one file, creating its parent directory first."""
        payload = data.encode() if isinstance(data, str) else data
        self.exec(["mkdir", "-p", str(Path(path).parent)], timeout=60).check(f"create {Path(path).parent}")
        if len(payload) <= CHUNK:
            self._write_content(path, payload)
        else:
            stem, target = self.send(io.BytesIO(payload)), shlex.quote(path)
            self.exec(f"cat {stem}.* > {target} && rm -f {stem}.*", timeout=1800).check(f"assemble {path}")
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

    def _download_content(self, path: str) -> bytes:
        response = self._request("GET", self._sandbox_path("files"), params={"path": path})
        content = response.get("content")
        if not isinstance(content, str):
            raise SandboxError(f"file response for {path} carried no content")
        return base64.b64decode(content)

    def read(self, path: str) -> bytes:
        size = self.exec(["stat", "-c", "%s", path], timeout=60).check(f"stat {path}").stdout.strip()
        if int(size) <= CHUNK:
            return self._download_content(path)
        stem = f"{RUN_DIR}/split-{uuid.uuid4().hex}."
        self.exec(f"mkdir -p {RUN_DIR} && split -b {CHUNK} -d -a 5 {shlex.quote(path)} {stem}", timeout=1800).check(f"split {path}")
        parts = sorted(self.exec(f"ls {stem}*", timeout=60).stdout.split())
        try:
            return b"".join(self._download_content(part) for part in parts)
        finally:
            self.exec(f"rm -f {stem}*", timeout=60)

    def download(self, remote: str, local: str | Path) -> None:
        if not self.exec(["test", "-d", remote], timeout=30).ok:
            return super().download(remote, local)
        staged = f"{RUN_DIR}/download-{uuid.uuid4().hex}.tgz"
        self.exec(f"mkdir -p {RUN_DIR} && tar -czf {staged} -C {shlex.quote(remote)} .", timeout=1800).check(f"download {remote}")
        try:
            untar_bytes(self.read(staged), Path(local))
        finally:
            self.exec(["rm", "-f", staged], timeout=60)

    def _delete(self) -> None:
        """Best-effort deletion; the service TTL removes the sandbox when this fails."""
        if not self.sandbox_id:
            return
        for attempt in range(3):
            try:
                self._request("DELETE", self._sandbox_path(""), retry=False)
                return
            except _ServiceError as failure:
                if failure.status == 404:
                    return
            except SandboxError:
                pass
            if attempt < 2:
                time.sleep(5)

    def close(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)
            self._heartbeat_thread = None
        self._delete()
