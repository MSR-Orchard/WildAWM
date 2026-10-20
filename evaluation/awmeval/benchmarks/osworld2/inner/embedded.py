"""DesktopEnv provider that boots the pinned OSWorld v2 qcow2 with QEMU TCG inside the sandbox."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

DISK = Path("/System.qcow2")
LOG = Path("/tmp/awmeval-embedded-qemu.log")


class EmbeddedQEMUManager:
    """VM registry stub: every sandbox owns one fresh guest."""

    def initialize_registry(self, **kwargs: Any) -> None:
        return None

    def add_vm(self, vm_path: str, **kwargs: Any) -> None:
        return None

    def delete_vm(self, vm_path: str, **kwargs: Any) -> None:
        return None

    def occupy_vm(self, vm_path: str, pid: int, **kwargs: Any) -> None:
        return None

    def list_free_vms(self, **kwargs: Any) -> list[str]:
        return []

    def check_and_clean(self, **kwargs: Any) -> None:
        return None

    def get_vm_path(self, **kwargs: Any) -> str:
        return str(DISK)


class EmbeddedQEMUProvider:
    """QEMU TCG with slirp port forwards, so neither /dev/kvm nor CAP_NET_ADMIN is needed."""

    def __init__(self, *, ram_size: str, cpu_cores: int, startup_timeout_seconds: int, expose_task_service: bool) -> None:
        self.ram_size = ram_size
        self.cpu_cores = cpu_cores
        self.startup_timeout_seconds = startup_timeout_seconds
        self.expose_task_service = expose_task_service
        self.software_virgl = shutil.which("Xvfb") is not None
        self.volume_size: int | None = None
        self.server_port = 15000
        self.chromium_port = 19222
        self.vnc_port = 8006
        self.vlc_port = 18080
        self.host_service_ports = {3000: 13000} if expose_task_service else {}
        self.process: subprocess.Popen[bytes] | None = None
        self.xvfb_process: subprocess.Popen[bytes] | None = None
        self.log_stream: Any = None
        self.log_path = LOG

    def prepare_volume(self, path_to_vm: str, volume_size: int | None, os_type: str) -> None:
        self.volume_size = int(volume_size) if volume_size is not None else None

    def finalize_volume(self, path_to_vm: str, volume_size: int | None, os_type: str, controller: Any, setup_controller: Any, client_password: str) -> None:
        if volume_size is None:
            return
        from desktop_env.providers.volume import expand_guest_volume

        expand_guest_volume(os_type=os_type, controller=controller, setup_controller=setup_controller, client_password=client_password)

    @staticmethod
    def _clean_runtime_disks() -> None:
        for path in (Path("/boot.qcow2"), Path("/boot.img")):
            path.unlink(missing_ok=True)
        storage = Path("/storage")
        storage.mkdir(parents=True, exist_ok=True)
        for name in ("boot.qcow2", "boot.img"):
            (storage / name).unlink(missing_ok=True)

    @staticmethod
    def _stop_runtime_nginx() -> None:
        subprocess.run(["nginx", "-s", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        subprocess.run(["pkill", "-TERM", "-x", "nginx"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            probe = subprocess.run(["pgrep", "-x", "nginx"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            if probe.returncode != 0:
                return
            time.sleep(0.2)
        subprocess.run(["pkill", "-KILL", "-x", "nginx"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)

    @staticmethod
    def _patch_software_virgl_display() -> None:
        path = Path("/run/display.sh")
        source = path.read_text(encoding="utf-8")
        needle = 'if [[ "$GPU" != [Yy1]* ]] || [[ "$ARCH" != "amd64" ]]; then\n  return 0\nfi\n'
        replacement = 'if [[ "${AWMEVAL_SOFTWARE_VIRGL:-}" == [Yy1]* ]]; then\n  DISPLAY_OPTS="-display gtk,gl=on -device virtio-vga-gl,max_hostmem=1G"\n  export DISPLAY=:99\n  return 0\nfi\n\n' + needle
        if replacement in source:
            return
        if needle not in source:
            raise RuntimeError("qemu display source does not match the pinned runtime")
        path.write_text(source.replace(needle, replacement), encoding="utf-8")

    @staticmethod
    def _patch_user_port_mapping() -> None:
        path = Path("/run/network.sh")
        source = path.read_text(encoding="utf-8")
        old = '  for port in $list; do\n    args+="hostfwd=tcp::$port-$VM_NET_IP:$port,"\n  done\n'
        new = '  for port in $list; do\n    host_port="${port%%:*}"\n    guest_port="${port##*:}"\n    args+="hostfwd=tcp::${host_port}-$VM_NET_IP:${guest_port},"\n  done\n'
        if old in source:
            path.write_text(source.replace(old, new), encoding="utf-8")
        elif new not in source:
            raise RuntimeError("qemu user-port mapping source does not match the pinned runtime")

    def _environment(self, path_to_vm: str) -> dict[str, str]:
        disk = Path(path_to_vm)
        if disk.resolve() != DISK:
            raise ValueError(f"embedded QEMU disk must be {DISK}, got {disk}")
        if not disk.is_file():
            raise FileNotFoundError(disk)
        user_ports = ["15000:5000", "19222:9222", "18080:8080"]
        if self.expose_task_service:
            user_ports.append("13000:3000")
        values = dict(os.environ)
        values.update({
            "KVM": "N",
            # TCG's default `max` CPU looks like 32-bit athlon-xp to LLVM 15, so llvmpipe apps abort.
            "CPU_MODEL": "EPYC-v4",
            "NETWORK": "user",
            "USER_PORTS": ",".join(user_ports),
            "RAM_SIZE": self.ram_size,
            "CPU_CORES": str(self.cpu_cores),
            "RAM_CHECK": "N",
            "ALLOCATE": "N",
            "DISK_SIZE": "16G",
            "DISPLAY": "web",
        })
        if self.software_virgl:
            values["AWMEVAL_SOFTWARE_VIRGL"] = "Y"
            values["LIBGL_ALWAYS_SOFTWARE"] = "1"
        if self.volume_size is not None and self.volume_size > 50:
            values["ROOT_DISK_SIZE"] = f"{self.volume_size}G"
        return values

    def start_emulator(self, path_to_vm: str, headless: bool, os_type: str = "Ubuntu") -> None:
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("embedded QEMU is already running")
        entrypoint = Path("/run/entry.sh")
        if not entrypoint.is_file():
            raise FileNotFoundError(entrypoint)
        self._stop_runtime_nginx()
        self._clean_runtime_disks()
        self._patch_user_port_mapping()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_stream = self.log_path.open("ab", buffering=0)
        if self.software_virgl:
            self._patch_software_virgl_display()
            Path("/tmp/.X11-unix/X99").unlink(missing_ok=True)
            Path("/tmp/.X99-lock").unlink(missing_ok=True)
            self.xvfb_process = subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"], stdin=subprocess.DEVNULL, stdout=self.log_stream, stderr=subprocess.STDOUT, start_new_session=True)
            xvfb_deadline = time.monotonic() + 30
            while not Path("/tmp/.X11-unix/X99").exists():
                if self.xvfb_process.poll() is not None:
                    raise RuntimeError(f"embedded Xvfb exited during startup with code {self.xvfb_process.returncode}; log={self.log_path}")
                if time.monotonic() >= xvfb_deadline:
                    raise TimeoutError(f"embedded Xvfb was not ready within 30s; log={self.log_path}")
                time.sleep(0.1)
        self.process = subprocess.Popen([str(entrypoint)], cwd="/run", env=self._environment(path_to_vm), stdin=subprocess.DEVNULL, stdout=self.log_stream, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + self.startup_timeout_seconds
        last_error = "control endpoint has not responded"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"embedded QEMU exited during startup with code {self.process.returncode}; log={self.log_path}")
            try:
                with urlopen(f"http://127.0.0.1:{self.server_port}/screenshot", timeout=30) as response:
                    prefix = response.read(8)
                    status = response.status
                if status == 200 and prefix == b"\x89PNG\r\n\x1a\n":
                    return
                last_error = f"HTTP {status} invalid PNG prefix"
            except (OSError, URLError) as error:
                last_error = f"{type(error).__name__}: {error}"
            time.sleep(2)
        raise TimeoutError(f"embedded QEMU was not ready within {self.startup_timeout_seconds}s: {last_error}; log={self.log_path}")

    def get_ip_address(self, path_to_vm: str) -> str:
        return f"localhost:{self.server_port}:{self.chromium_port}:{self.vnc_port}:{self.vlc_port}"

    def save_state(self, path_to_vm: str, snapshot_name: str) -> None:
        raise NotImplementedError("embedded QEMU snapshots are not used; every retry starts from the immutable backing image")

    def revert_to_snapshot(self, path_to_vm: str, snapshot_name: str) -> str:
        self.stop_emulator(path_to_vm)
        return path_to_vm

    @staticmethod
    def _stop_group(process: subprocess.Popen[bytes] | None, grace: int, kill_wait: int) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=kill_wait)

    def stop_emulator(self, path_to_vm: str, region: str | None = None, *args: Any, **kwargs: Any) -> None:
        process, self.process = self.process, None
        self._stop_group(process, 30, 10)
        self._stop_runtime_nginx()
        xvfb_process, self.xvfb_process = self.xvfb_process, None
        self._stop_group(xvfb_process, 10, 5)
        Path("/tmp/.X11-unix/X99").unlink(missing_ok=True)
        Path("/tmp/.X99-lock").unlink(missing_ok=True)
        if self.log_stream is not None:
            self.log_stream.close()
            self.log_stream = None
        self._clean_runtime_disks()

    def get_host_service_ports(self) -> dict[int, int]:
        return dict(self.host_service_ports)


def patch_embedded_qemu_provider(*, ram_size: str, cpu_cores: int, startup_timeout_seconds: int, expose_task_service: bool) -> None:
    """Route DesktopEnv(provider_name="docker") to the embedded QEMU provider."""
    from desktop_env import desktop_env as desktop_env_module

    def factory(provider_name: str, region: str | None, use_proxy: bool = False, use_public_ip: bool = False):
        if provider_name != "docker":
            raise ValueError(f"embedded QEMU patch only accepts DesktopEnv provider_name=docker, got {provider_name}")
        provider = EmbeddedQEMUProvider(ram_size=ram_size, cpu_cores=cpu_cores, startup_timeout_seconds=startup_timeout_seconds, expose_task_service=expose_task_service)
        return EmbeddedQEMUManager(), provider

    desktop_env_module.create_vm_manager_and_provider = factory
