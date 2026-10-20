#!/bin/bash
# Single-container world boot for a Harbor task; Harbor execs into it for later phases.
set -euo pipefail

WORLD=/world
BASELINE=/baseline
WORKSPACE=/workspace
RUNTIME_DIR=/opt/wildawm-harbor
# Overridable for runtimes that stage the world blob elsewhere.
WORLD_ARCHIVE="${WILDAWM_WORLD_ARCHIVE:-/opt/world.tar.zst}"
LOG_DIR=/tmp/wildawm-logs
CONTROL_URL=http://127.0.0.1:8101
MCP_PORT=18080
BROWSER_MCP_PORT=8931

# Pinned Node runtime for the Playwright MCP (the base image's Node 18 is too old).
PW_NODE_VERSION=22.23.2
PW_NODE_ARCHIVE=/opt/wildawm-node-archive/node-v22.23.2-linux-x64.tar.xz
PW_NODE_SHA256=d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307
PW_NODE=/opt/wildawm-node/bin/node

mkdir -p "$LOG_DIR" "$WORKSPACE"

log() { echo "[wildawm-harbor] $*" | tee -a "$LOG_DIR/boot.log"; }

if [ ! -d "$WORLD/data" ]; then
    log "extracting $WORLD_ARCHIVE -> $WORLD"
    rm -rf "$WORLD"
    python3 - "$WORLD_ARCHIVE" <<'PY'
import os, sys, tarfile, zstandard
archive = sys.argv[1]
# Members carry a top-level "world/" prefix; reject any that escape /world.
allowed_root = os.path.realpath("/world")
with open(archive, "rb") as raw:
    with zstandard.ZstdDecompressor().stream_reader(raw) as stream:
        with tarfile.open(fileobj=stream, mode="r|") as tf:
            for member in tf:
                dest = os.path.realpath(os.path.join("/", member.name))
                if dest != allowed_root and not dest.startswith(allowed_root + os.path.sep):
                    raise RuntimeError(f"unsafe archive member: {member.name}")
                tf.extractall("/", members=[member], filter="data")
PY
fi
if [ ! -d "$WORLD/data" ] || [ ! -d "$WORLD/workspace" ]; then
    log "FATAL: world extraction incomplete"
    exit 1
fi

if [ -f /opt/world-patch/patch_data.py ]; then
    log "applying task world_patch"
    AWM_WORLD_PATCH_PHASE=data python3 /opt/world-patch/patch_data.py \
        --instance-dir "$WORLD" \
        --workspace "$WORLD/workspace" \
        --data-dir "$WORLD/data" \
        --webharbor-dir "$WORLD/webharbor" \
        >> "$LOG_DIR/world-patch.log" 2>&1
fi

log "staging $WORLD/workspace -> $WORKSPACE"
cp -a "$WORLD/workspace/." "$WORKSPACE/"
# Agent containers run as uid 1000 and own their workspace.
chown -R agent:agent "$WORKSPACE" 2>/dev/null || true

log "mapping WebHarbor domains in /etc/hosts"
python3 - <<'PY'
import json
from pathlib import Path
config = json.loads(Path("/opt/webharbor-config/sites.json").read_text())
domains = sorted({
    domain
    for site in config.get("sites", [])
    for domain in site.get("domains", [])
})
marker = "# wildawm-harbor-webharbor"
hosts = Path("/etc/hosts")
original = hosts.read_text()
kept = [line for line in original.splitlines() if marker not in line]
kept.extend(f"127.0.0.1 {domain} {marker}" for domain in domains)
hosts.write_text("\n".join(kept) + "\n")
print(f"mapped {len(domains)} domains")
PY

# WebHarbor runs on the real clock; only the MCP gateway is frozen.
log "starting WebHarbor (/entrypoint.sh)"
env -u FAKETIME -u LD_PRELOAD /entrypoint.sh >> "$LOG_DIR/webharbor.log" 2>&1 &
WEBHARBOR_PID=$!
log "webharbor pid $WEBHARBOR_PID; waiting for all sites alive"
for i in $(seq 1 180); do
    if curl -sf "$CONTROL_URL/health" > /tmp/wh-health.json 2>/dev/null; then
        if python3 - <<'PY'
import json, sys
row = json.load(open("/tmp/wh-health.json"))
sites = row.get("sites") or {}
sys.exit(0 if row.get("ok") is True and len(sites) == 15
         and all(s.get("alive") for s in sites.values()) else 1)
PY
        then
            log "all 15 WebHarbor sites alive"
            break
        fi
    fi
    if [ "$i" = 180 ]; then
        log "FATAL: WebHarbor sites did not become healthy"
        tail -40 "$LOG_DIR/webharbor.log" || true
        exit 1
    fi
    sleep 1
done

log "restoring served WebHarbor DBs"
bash "$RUNTIME_DIR/restore_webharbor.sh"

read -r FROZEN_DATE FROZEN_TIME <<<"$(python3 -c "
import json
meta = json.load(open('$WORLD/meta.json'))
print(meta['frozen_clock_date'], meta.get('frozen_clock_time', '10:00:00'))
")"
log "frozen clock: $FROZEN_DATE $FROZEN_TIME"
FAKETIME="@$FROZEN_DATE $FROZEN_TIME" \
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1 \
    python3 -m wildawm.cli.gateway \
    --world_dir "$WORLD/data" \
    --extra_apps_dir "$WORLD/overlay/apps" \
    --host 127.0.0.1 --port "$MCP_PORT" \
    >> "$LOG_DIR/mcp.log" 2>&1 &
MCP_PID=$!
log "mcp gateway pid $MCP_PID; waiting for health"
for i in $(seq 1 120); do
    if curl -sf "http://127.0.0.1:$MCP_PORT/health" > /tmp/mcp-health.json 2>/dev/null; then
        if python3 - <<'PY'
import json, sys
row = json.load(open("/tmp/mcp-health.json"))
apps = row.get("loaded_apps") or []
sys.exit(0 if row.get("status") == "ok" and len(apps) >= 8
         and not row.get("errors") else 1)
PY
        then
            log "mcp gateway healthy: $(python3 -c "import json;print(json.load(open('/tmp/mcp-health.json')).get('loaded_apps'))")"
            break
        fi
    fi
    if [ "$i" = 120 ]; then
        log "FATAL: MCP gateway did not become healthy"
        tail -40 "$LOG_DIR/mcp.log" || true
        exit 1
    fi
    sleep 1
done

# Baseline snapshot: post-patch, pre-agent.
if [ ! -d "$BASELINE" ]; then
    log "snapshotting baseline: cp -a $WORLD $BASELINE"
    cp -a "$WORLD" "$BASELINE"
fi

if [ -e /webharbor-ca ] || [ -L /webharbor-ca ]; then rm -rf /webharbor-ca; fi
ln -s /var/lib/caddy/pki/authorities/local /webharbor-ca
if [ -f /webharbor-ca/root.crt ]; then
    cp -f /webharbor-ca/root.crt /usr/local/share/ca-certificates/wildawm-webharbor-root.crt
    update-ca-certificates > /dev/null 2>&1 || true
    log "installed WebHarbor root CA into system trust store"
fi

# Fail closed: the agent's config registers this server as "browser".
log "preparing Playwright MCP runtime (node $PW_NODE_VERSION)"
PWMCP_CLI="$(readlink -f "$(command -v playwright-mcp)")"
PW_CHROME="$(rg --files /opt/pw-browsers 2>/dev/null | rg '/chrome$' | head -1)"
if [ ! -f "$PW_NODE_ARCHIVE" ] || [ ! -f "$PWMCP_CLI" ] || [ ! -x "$PW_CHROME" ]; then
    log "FATAL: Playwright MCP runtime incomplete (archive=$PW_NODE_ARCHIVE cli=$PWMCP_CLI chrome=$PW_CHROME)"
    exit 1
fi
actual_sha="$(sha256sum "$PW_NODE_ARCHIVE" | awk '{print $1}')"
if [ "$actual_sha" != "$PW_NODE_SHA256" ]; then
    log "FATAL: Playwright Node archive sha256 mismatch: $actual_sha"
    exit 1
fi
mkdir -p /opt/wildawm-node/bin
if [ ! -x "$PW_NODE" ]; then
    python3 - "$PW_NODE_ARCHIVE" "$PW_NODE" "$PW_NODE_VERSION" <<'PY'
import shutil, sys, tarfile
from pathlib import Path
archive, dest, version = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
member = f"node-v{version}-linux-x64/bin/node"
with tarfile.open(archive, "r:xz") as stream:
    source = stream.extractfile(member)
    if source is None:
        raise SystemExit(f"cannot read {member} from {archive}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(".node.tmp")
    with temporary.open("wb") as output:
        shutil.copyfileobj(source, output)
    temporary.chmod(0o755)
    temporary.replace(dest)
PY
fi
node_version="$("$PW_NODE" --version)"
if [ "$node_version" != "v$PW_NODE_VERSION" ]; then
    log "FATAL: unexpected Playwright Node version: $node_version"
    exit 1
fi
log "starting Playwright MCP at :$BROWSER_MCP_PORT (node=$PW_NODE chrome=$PW_CHROME)"
# runuser -p keeps HOME=/root; use an agent-owned HOME so Chromium can create its profile.
HOME=/home/agent PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
runuser -p -u agent -- "$PW_NODE" "$PWMCP_CLI" \
    --port "$BROWSER_MCP_PORT" --host 127.0.0.1 \
    --executable-path "$PW_CHROME" \
    --headless --no-sandbox --ignore-https-errors \
    >> "$LOG_DIR/playwright-mcp.log" 2>&1 &
PWMCP_PID=$!
if ! runuser -p -u agent -- python3 "$RUNTIME_DIR/check_playwright_mcp.py" \
        --url "http://localhost:$BROWSER_MCP_PORT/mcp" --timeout-seconds 60; then
    log "FATAL: Playwright MCP did not become ready on :$BROWSER_MCP_PORT"
    tail -40 "$LOG_DIR/playwright-mcp.log" || true
    kill "$PWMCP_PID" 2>/dev/null || true
    exit 1
fi
log "playwright mcp ready on :$BROWSER_MCP_PORT (pid $PWMCP_PID)"

touch "$RUNTIME_DIR/ready"
log "world boot complete"
exec sleep infinity
