#!/bin/bash
# WebSyn startup with WildAWM's process-group supervisor and stale-port cleanup.
set -e

SITES=(allrecipes amazon apple arxiv bbc_news booking github
       google_flights google_map google_search huggingface wolfram_alpha
       cambridge_dictionary coursera espn)
BASE_PORT=40000
PID_DIR=/tmp/websyn_pids
mkdir -p "$PID_DIR"
rm -f "$PID_DIR"/*.pid

kill_port() {
    python3 - "$1" <<'PY'
import os
import signal
import sys
import time
from pathlib import Path

port = int(sys.argv[1])
want = f"{port:04X}"


def listening_inodes():
    out = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            local, state, inode = parts[1], parts[3], parts[9]
            if state == "0A" and local.rpartition(":")[2].upper() == want:
                host_hex = local.rpartition(":")[0].upper()
                if table.endswith("tcp") and host_hex not in {"00000000", "0100007F"}:
                    continue
                out.add(inode)
    return out


def pids_for_inodes(inodes):
    pids = set()
    if not inodes:
        return pids
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fds = list((proc / "fd").iterdir())
        except (FileNotFoundError, PermissionError):
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                pids.add(int(proc.name))
                break
    return pids


for _ in range(2):
    pids = pids_for_inodes(listening_inodes())
    if not pids:
        break
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline:
        if not pids_for_inodes(listening_inodes()):
            break
        time.sleep(0.05)
PY
}

site_ready() {
    python3 - "$1" <<'PY'
import sys
import urllib.request

port = int(sys.argv[1])
try:
    r = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
    raise SystemExit(0 if r.status < 500 else 1)
except Exception:
    raise SystemExit(1)
PY
}

start_site() {
    local site="$1"
    local port="$2"
    kill_port "$port"
    python3 /opt/site_runner.py "$site" "$port" \
        > "/tmp/websyn_${site}.log" 2>&1 &
    echo "$!" > "$PID_DIR/${site}.pid"
    echo "  $site -> port $port (PID $!)"
}

echo "[WebSyn] Resetting all databases to seed state..."
for d in "${SITES[@]}"; do
    rm -rf "/opt/WebSyn/$d/instance"
    cp -a "/opt/WebSyn/$d/instance_seed" "/opt/WebSyn/$d/instance"
done

echo "[WebSyn] Starting 15 sites on ports ${BASE_PORT}-$((BASE_PORT + 14))..."
for i in "${!SITES[@]}"; do
    site="${SITES[$i]}"
    port=$((BASE_PORT + i))
    start_site "$site" "$port"
done

echo "[WebSyn] Waiting for sites to become ready..."
max_wait=60
interval=2
elapsed=0
while [ $elapsed -lt $max_wait ]; do
    sleep $interval
    elapsed=$((elapsed + interval))
    ready=0
    for i in "${!SITES[@]}"; do
        port=$((BASE_PORT + i))
        if site_ready "$port"; then
            ready=$((ready + 1))
        fi
    done
    echo "  [${elapsed}/${max_wait}s] ${ready}/15 sites ready"
    if [ $ready -eq 15 ]; then
        break
    fi
done

echo "[WebSyn] Repairing failed initial site launches if needed..."
for i in "${!SITES[@]}"; do
    site="${SITES[$i]}"
    port=$((BASE_PORT + i))
    if ! site_ready "$port"; then
        echo "  [repair] $site :$port not ready; restarting with stale-port cleanup"
        start_site "$site" "$port"
        sleep 1
    fi
done

echo "[WebSyn] Site status:"
for i in "${!SITES[@]}"; do
    site="${SITES[$i]}"
    port=$((BASE_PORT + i))
    if site_ready "$port"; then
        echo "  [OK] $site :$port"
    else
        echo "  [!!] $site :$port FAILED -- check /tmp/websyn_${site}.log"
    fi
done

echo "[WebSyn] Starting control server on :8101 (PID 1)..."
exec python3 /opt/control_server.py --port 8101
