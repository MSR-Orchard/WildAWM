#!/bin/bash
# Runs as root to lock down egress and files, then starts the harness as AWM_HOST_UID.
set -e

TARGET_UID="${AWM_HOST_UID:-1000}"
TARGET_GID="${AWM_HOST_GID:-1000}"
if [ -z "${AWM_OPENAI_BASE_URL:-}" ] || [ -z "${AWM_OPENAI_MODEL:-}" ]; then
    echo "[entrypoint] AWM_OPENAI_BASE_URL and AWM_OPENAI_MODEL are required" >&2
    exit 1
fi
# Disable the per-request attribution hash so prompts stay prefix-cacheable.
CLAUDE_CODE_ATTRIBUTION_HEADER=0
CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS="${CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS:-1}"
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"
DISABLE_INTERLEAVED_THINKING="${DISABLE_INTERLEAVED_THINKING:-1}"
AWM_CLAUDE_CODE_MODEL="${AWM_OPENAI_MODEL:-${OPENAI_MODEL:-}}"
AWM_MODEL_CONTEXT_WINDOW="${AWM_MODEL_CONTEXT_WINDOW:-131072}"
if [ -n "$AWM_CLAUDE_CODE_MODEL" ]; then
    ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-$AWM_CLAUDE_CODE_MODEL}"
    ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-$AWM_CLAUDE_CODE_MODEL}"
    ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-$AWM_CLAUDE_CODE_MODEL}"
    ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-$AWM_CLAUDE_CODE_MODEL}"
    ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-$AWM_CLAUDE_CODE_MODEL}"
    CLAUDE_CODE_SUBAGENT_MODEL="${CLAUDE_CODE_SUBAGENT_MODEL:-$AWM_CLAUDE_CODE_MODEL}"
fi
if [ -z "${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-}" ]; then
    CLAUDE_CODE_AUTO_COMPACT_WINDOW="$AWM_MODEL_CONTEXT_WINDOW"
fi
if [ -z "${AWM_OPENCLAW_MODEL_CONTEXT_WINDOW:-}" ]; then
    AWM_OPENCLAW_MODEL_CONTEXT_WINDOW="$AWM_MODEL_CONTEXT_WINDOW"
fi
export AWM_MCP_BASE_URL AWM_LLM_PROXY_PORT AWM_LLM_PROXY_BASE_URL AWM_BROWSER_MCP_PORT AWM_BROWSER_MCP_URL AWM_MODEL_CONTEXT_WINDOW CLAUDE_CODE_ATTRIBUTION_HEADER CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC DISABLE_INTERLEAVED_THINKING ANTHROPIC_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_SMALL_FAST_MODEL CLAUDE_CODE_SUBAGENT_MODEL CLAUDE_CODE_AUTO_COMPACT_WINDOW AWM_OPENCLAW_MODEL_CONTEXT_WINDOW

eval "$(python3 - <<'PY'
from wildawm import const_config as cfg

for key, value in {
    "AWM_AGENT_TURN_TIMEOUT": cfg.DEFAULT_AGENT_TURN_TIMEOUT_S,
    "AWM_HTTP_PROBE_TIMEOUT": cfg.HTTP_PROBE_TIMEOUT_S,
    "AWM_OPENCLAW_PROVIDER_TIMEOUT": cfg.OPENCLAW_PROVIDER_TIMEOUT_S,
}.items():
    print(f'export {key}="${{{key}:-{value}}}"')
PY
)"

active_mcp_services() {
    python3 - "$AWM_MCP_BASE_URL/apps" <<'PY'
import json, sys, time, urllib.request
url = sys.argv[1]
for attempt in range(30):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read())
        print(" ".join(str(app) for app in payload.get("loaded", []) if str(app).strip()))
        raise SystemExit(0)
    except Exception:
        if attempt == 29:
            raise
        time.sleep(1)
PY
}
AWM_ACTIVE_MCP_SERVICES="$(active_mcp_services)"
export AWM_ACTIVE_MCP_SERVICES
echo "[entrypoint] AWM_AGENT_KIND=$AWM_AGENT_KIND uid=$TARGET_UID active_mcp_services=${AWM_ACTIVE_MCP_SERVICES:-none}"

CA_DIR=/webharbor-ca
CA=$CA_DIR/root.crt
MERGED=/tmp/wildawm-ca-bundle.crt

if [ -d "$CA_DIR" ]; then
    for i in $(seq 1 30); do [ -r "$CA" ] && break; sleep 1; done
    if [ -r "$CA" ]; then
        cat /etc/ssl/certs/ca-certificates.crt "$CA" > "$MERGED" 2>/dev/null
        chmod 644 "$MERGED"
        cp "$CA" /usr/local/share/ca-certificates/wildawm-webharbor.crt
        # A full system CA rebuild is slow on overlayfs; harnesses get the merged bundle via env.
        if [ "$AWM_UPDATE_SYSTEM_CA" = "1" ]; then
            timeout "${AWM_UPDATE_SYSTEM_CA_TIMEOUT:-20}" update-ca-certificates -f 2>/dev/null || true
        else
            echo "[entrypoint] skipping system CA rebuild (AWM_UPDATE_SYSTEM_CA=0)"
        fi

        export CURL_CA_BUNDLE="$MERGED"
        export REQUESTS_CA_BUNDLE="$MERGED"
        export SSL_CERT_FILE="$MERGED"
        export NODE_EXTRA_CA_CERTS="$CA"
    fi
fi

# Default-deny egress: only loopback, private docker networks and the LLM API (HTTPS).
if command -v iptables >/dev/null 2>&1; then
    LLM_IPS=""
    LLM_CIDRS=""
    if [ -n "$OPENAI_BASE_URL" ]; then
        LLM_HOST=$(python3 -c "from urllib.parse import urlparse; print(urlparse('$OPENAI_BASE_URL').hostname)" 2>/dev/null)
        if [ -n "$LLM_HOST" ]; then
            LLM_IPS=$(getent ahosts "$LLM_HOST" 2>/dev/null | awk '{print $1}' | sort -u)
        fi
        # Google APIs use rotating anycast IPs, so allowlist the published Google ranges.
        case "$LLM_HOST" in
            *.googleapis.com|googleapis.com)
                # Prefer the baked goog.json, then a live fetch, then a hardcoded list.
                if [ -r /var/cache/wildawm/goog.json ]; then
                    LLM_CIDRS=$(python3 -c "import json;[print(p['ipv4Prefix']) for p in json.load(open('/var/cache/wildawm/goog.json')).get('prefixes',[]) if 'ipv4Prefix' in p]" 2>/dev/null || true)
                    if [ -n "$LLM_CIDRS" ]; then
                        echo "[entrypoint] using cached goog.json (/var/cache/wildawm/goog.json)"
                    fi
                fi
                if [ -z "$LLM_CIDRS" ]; then
                    # -k: the frozen clock makes certs look not-yet-valid; never fail under set -e.
                    LLM_CIDRS=$(curl -sk --max-time "$AWM_HTTP_PROBE_TIMEOUT" https://www.gstatic.com/ipranges/goog.json 2>/dev/null \
                        | python3 -c "import sys,json;[print(p['ipv4Prefix']) for p in json.load(sys.stdin).get('prefixes',[]) if 'ipv4Prefix' in p]" 2>/dev/null || true)
                fi
                if [ -z "$LLM_CIDRS" ]; then
                    LLM_CIDRS="142.250.0.0/15 142.251.0.0/16 172.217.0.0/16 172.253.0.0/16 173.194.0.0/16 192.178.0.0/15 64.233.160.0/19 57.140.0.0/14 216.58.192.0/19 74.125.0.0/16 108.177.0.0/17 209.85.128.0/17 34.0.0.0/9 35.192.0.0/11"
                    echo "[entrypoint] goog.json unavailable; using hardcoded Google netblock fallback"
                fi
                ;;
        esac
    fi

    iptables -F OUTPUT 2>/dev/null || true
    iptables -A OUTPUT -o lo -j ACCEPT 2>/dev/null
    iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null
    # Docker-internal networks only (embedded DNS, WebHarbor, MCP).
    iptables -A OUTPUT -d 127.0.0.0/8 -j ACCEPT 2>/dev/null
    iptables -A OUTPUT -d 10.0.0.0/8 -j ACCEPT 2>/dev/null
    iptables -A OUTPUT -d 172.16.0.0/12 -j ACCEPT 2>/dev/null
    iptables -A OUTPUT -d 192.168.0.0/16 -j ACCEPT 2>/dev/null
    # The LLM API, HTTPS only.
    for ip in $LLM_IPS; do
        iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT 2>/dev/null
    done
    for cidr in $LLM_CIDRS; do
        iptables -A OUTPUT -d "$cidr" -p tcp --dport 443 -j ACCEPT 2>/dev/null
    done
    # REJECT so stray egress fails fast instead of hanging; the DROP policy is a backstop.
    iptables -A OUTPUT -j REJECT 2>/dev/null || true
    iptables -P OUTPUT DROP 2>/dev/null || \
        echo "[entrypoint] WARNING: could not set OUTPUT policy DROP (missing NET_ADMIN?)"

    echo "[entrypoint] Egress lockdown active — default DROP (LLM: ${LLM_HOST:-none}, $(echo $LLM_IPS | wc -w) ip(s) + $(echo $LLM_CIDRS | wc -w) cidr(s))"
fi

# IPv6: allow only loopback, link-local and ULA.
if command -v ip6tables >/dev/null 2>&1; then
    ip6tables -F OUTPUT 2>/dev/null || true
    ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null
    ip6tables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null
    ip6tables -A OUTPUT -d ::1/128 -j ACCEPT 2>/dev/null
    ip6tables -A OUTPUT -d fe80::/10 -j ACCEPT 2>/dev/null
    ip6tables -A OUTPUT -d fc00::/7 -j ACCEPT 2>/dev/null
    ip6tables -A OUTPUT -j REJECT 2>/dev/null || true
    ip6tables -P OUTPUT DROP 2>/dev/null || true
fi

for f in /workspace/WORLD.md /workspace/USER.md; do
    if [ -f "$f" ]; then
        chown root:root "$f"
        chmod 444 "$f"
    fi
done

# Runtime user with the host uid/gid.
sed -i "/^wildawm-agent:/d" /etc/passwd 2>/dev/null || true
echo "wildawm-agent:x:${TARGET_UID}:${TARGET_GID}:WildAWM Agent:/home/agent:/bin/bash" >> /etc/passwd
chown -R "$TARGET_UID:$TARGET_GID" /workspace 2>/dev/null || true
chown -R "$TARGET_UID:$TARGET_GID" /home/agent 2>/dev/null || true

# Link dashboard-written configs from /agent-config to where each harness expects them.
CONFIG_DIR=/agent-config
if [ -d "$CONFIG_DIR" ]; then
    [ -f "$CONFIG_DIR/nanobot-config.json" ] && \
        ln -sf "$CONFIG_DIR/nanobot-config.json" /home/agent/.nanobot/config.json

    [ -f "$CONFIG_DIR/opencode.json" ] && \
        ln -sf "$CONFIG_DIR/opencode.json" /home/agent/.config/opencode/opencode.json

    # Copy, not symlink: codex writes to its config when adding MCP servers.
    if [ -f "$CONFIG_DIR/codex-config.toml" ]; then
        cp "$CONFIG_DIR/codex-config.toml" /home/agent/.codex/config.toml
        chown "$TARGET_UID:$TARGET_GID" /home/agent/.codex/config.toml
    fi
fi

# Run cli.js directly; npx fails to bind in this container.
PWMCP_CLI=/usr/lib/node_modules/@playwright/mcp/cli.js
# Playwright's bundled Chromium (Chrome is not installed).
PW_CHROME=$(find /opt/pw-browsers -name "chrome" -type f 2>/dev/null | head -1)
if [ -f "$PWMCP_CLI" ]; then
    runuser -p -u wildawm-agent -- node "$PWMCP_CLI" \
        --port "$AWM_BROWSER_MCP_PORT" --host 0.0.0.0 \
        --executable-path "$PW_CHROME" \
        --headless \
        --no-sandbox \
        --ignore-https-errors \
        > /tmp/playwright-mcp.log 2>&1 &
    echo "[entrypoint] Playwright MCP on :$AWM_BROWSER_MCP_PORT (PID $!, chrome=$PW_CHROME)"
fi

# codex compacts below this window to avoid context-length errors.
AWM_CODEX_MODEL_CONTEXT_WINDOW="$AWM_MODEL_CONTEXT_WINDOW"
ADAPTER=/usr/local/bin/agent_adapter.py
if [ -f "$ADAPTER" ]; then
    ADAPTER_PORT="${AWM_AGENT_ADAPTER_PORT:-8999}"
    runuser -p -u wildawm-agent -- \
        env AWM_AGENT_KIND="$AWM_AGENT_KIND" \
            AWM_AGENT_ADAPTER_PORT="$ADAPTER_PORT" \
            AWM_CODEX_MODEL_CONTEXT_WINDOW="$AWM_CODEX_MODEL_CONTEXT_WINDOW" \
            HOME=/home/agent \
        python3 "$ADAPTER" > /tmp/agent-adapter.log 2>&1 &
    echo "[entrypoint] agent adapter on :${ADAPTER_PORT} (PID $!)"
fi

echo "[entrypoint] Starting agent: $AWM_AGENT_KIND"

# Not exec: the adapter may kill the harness, so the shell stays PID 1 and relaunches it.
run_harness_keepalive() {
    pid=""
    trap 'kill -TERM ${pid:-0} 2>/dev/null; exit 0' TERM INT
    # `|| true`: wait returns the kill status, which must not exit PID 1 under set -e.
    while :; do
        "$@" &
        pid=$!
        wait $pid || true
        echo "[entrypoint] harness exited — relaunching in 2s so the next turn can attach"
        (sleep 2 || true) & wait $! || true
    done
}

start_llm_proxy() {
    local log_path="$1"
    runuser -p -u wildawm-agent -- \
        python3 /usr/local/bin/anthropic-to-openai-proxy.py \
        > "$log_path" 2>&1 &
}

case "$AWM_AGENT_KIND" in
    nanobot)
        # nanobot reaches the model through the local proxy via its `custom` provider.
        echo "[entrypoint] Starting anthropic→openai proxy on :$AWM_LLM_PROXY_PORT for nanobot..."
        start_llm_proxy /tmp/proxy.log
        sleep 2
        # nanobot's model-call timeouts default far below the turn budget; align them with it.
        if [ -z "${NANOBOT_LLM_TIMEOUT_S:-}" ]; then
            if [ "${AWM_AGENT_TURN_TIMEOUT:-1800}" -gt 120 ]; then
                export NANOBOT_LLM_TIMEOUT_S="$((AWM_AGENT_TURN_TIMEOUT - 60))"
            else
                export NANOBOT_LLM_TIMEOUT_S="${AWM_AGENT_TURN_TIMEOUT:-1800}"
            fi
        fi
        export NANOBOT_OPENAI_COMPAT_TIMEOUT_S="${NANOBOT_OPENAI_COMPAT_TIMEOUT_S:-$NANOBOT_LLM_TIMEOUT_S}"
        export NANOBOT_STREAM_IDLE_TIMEOUT_S="${NANOBOT_STREAM_IDLE_TIMEOUT_S:-$NANOBOT_LLM_TIMEOUT_S}"
        echo "[entrypoint] nanobot model timeouts: llm=${NANOBOT_LLM_TIMEOUT_S}s openai_compat=${NANOBOT_OPENAI_COMPAT_TIMEOUT_S}s stream_idle=${NANOBOT_STREAM_IDLE_TIMEOUT_S}s"
        # -v keeps runtime logs on; otherwise nanobot disables logging.
        run_harness_keepalive runuser -p -u wildawm-agent -- \
            nanobot serve --host 0.0.0.0 -w /workspace -v \
            --timeout "${AWM_AGENT_TURN_TIMEOUT}"
        ;;
    opencode)
        # opencode routes LLM calls through the sanitizing proxy.
        echo "[entrypoint] Starting sanitizing proxy on :$AWM_LLM_PROXY_PORT for opencode..."
        start_llm_proxy /tmp/proxy.log
        sleep 2

        # opencode drops image input unless the model declares image modalities.
        AWM_OC_CFG=/home/agent/.config/opencode/opencode.json
        if [ -e "$AWM_OC_CFG" ]; then
            runuser -p -u wildawm-agent -- env AWM_OC_CFG="$AWM_OC_CFG" python3 - <<'PYEOF' \
                || echo "[entrypoint] WARNING: opencode modalities injection had non-fatal errors"
import json, os
p = os.environ["AWM_OC_CFG"]
real = os.path.realpath(p)          # follow the /agent-config symlink
with open(real) as f:
    cfg = json.load(f)
changed = 0
for prov in (cfg.get("provider") or {}).values():
    if not isinstance(prov, dict):
        continue
    for m in (prov.get("models") or {}).values():
        if isinstance(m, dict) and "modalities" not in m:
            m["modalities"] = {"input": ["text", "image"], "output": ["text"]}
            changed += 1
if changed:
    with open(real, "w") as f:
        json.dump(cfg, f, indent=2)
print(f"[entrypoint] opencode: enabled vision on {changed} model entr(y/ies)")
PYEOF
        fi

        run_harness_keepalive runuser -p -u wildawm-agent -- \
            env \
            OPENCODE_CONFIG="$AWM_OC_CFG" \
            OPENCODE_DISABLE_AUTOUPDATE=1 \
            OPENCODE_DISABLE_MODELS_FETCH=true \
            opencode serve --hostname 0.0.0.0 --port "$OPENCODE_INTERNAL_PORT"
        ;;
    claude-code)
        mkdir -p /home/agent/.claude
        python3 - <<'PYEOF'
import json
from pathlib import Path

path = Path("/home/agent/.claude/settings.json")
try:
    settings = json.loads(path.read_text())
except (FileNotFoundError, json.JSONDecodeError):
    settings = {}
settings["cleanupPeriodDays"] = max(
    int(settings.get("cleanupPeriodDays") or 0), 3650)
path.write_text(json.dumps(settings, indent=2) + "\n")
PYEOF
        date -Iseconds > /home/agent/.claude/.last-cleanup
        touch -d "$(date -Iseconds)" /home/agent/.claude/.last-cleanup
        chown -R "$TARGET_UID:$TARGET_GID" /home/agent/.claude

        echo "[entrypoint] Starting Anthropic→OpenAI proxy on :$AWM_LLM_PROXY_PORT..."
        start_llm_proxy /tmp/anthropic-proxy.log
        sleep 2

        # Best-effort: a failed registration must not trip set -e.
        echo "[entrypoint] Registering MCP servers with claude-code..."
        runuser -p -u wildawm-agent -- bash -c '
            for svc in $AWM_ACTIVE_MCP_SERVICES; do
                claude mcp add --transport http $svc "$AWM_MCP_BASE_URL/$svc/mcp" 2>/dev/null
            done
            claude mcp add --transport http wildawm "$AWM_MCP_BASE_URL/mcp" 2>/dev/null
            claude mcp add --transport http browser "$AWM_BROWSER_MCP_URL" 2>/dev/null
            if [ -f /agent-config/describe_image_mcp.py ]; then
                claude mcp add vision python3 /agent-config/describe_image_mcp.py 2>/dev/null
            fi
        ' || echo "[entrypoint] WARNING: claude-code mcp registration had non-fatal errors"
        # /health reports ready only after MCP tools are registered.
        touch /tmp/mcp-registered.flag

        echo "[entrypoint] claude-code: container stays alive for docker exec"
        exec runuser -p -u wildawm-agent -- \
            tail -f /dev/null
        ;;
    codex)
        # codex uses the proxy for Responses-to-Chat translation.
        echo "[entrypoint] Starting Responses→Chat proxy on :$AWM_LLM_PROXY_PORT..."
        start_llm_proxy /tmp/proxy.log
        sleep 2

        # Best-effort: a failed registration must not trip set -e.
        echo "[entrypoint] Registering MCP servers with codex..."
        runuser -p -u wildawm-agent -- bash -c '
            for svc in $AWM_ACTIVE_MCP_SERVICES; do
                codex mcp add $svc --url "$AWM_MCP_BASE_URL/$svc/mcp" 2>/dev/null
            done
            codex mcp add wildawm --url "$AWM_MCP_BASE_URL/mcp" 2>/dev/null
                codex mcp add browser --url "$AWM_BROWSER_MCP_URL" 2>/dev/null
            if [ -f /agent-config/describe_image_mcp.py ]; then
                codex mcp add vision -- python3 /agent-config/describe_image_mcp.py 2>/dev/null || true
            fi
        ' || echo "[entrypoint] WARNING: codex mcp registration had non-fatal errors"
        touch /tmp/mcp-registered.flag

        echo "[entrypoint] codex: container stays alive for docker exec"
        exec runuser -p -u wildawm-agent -- \
            tail -f /dev/null
        ;;
    hermes)
        # hermes speaks OpenAI chat/completions; point it at the local proxy.
        echo "[entrypoint] Starting OpenAI-compat proxy on :$AWM_LLM_PROXY_PORT for hermes..."
        start_llm_proxy /tmp/proxy.log
        sleep 2

        echo "[entrypoint] Configuring hermes model provider + MCP servers..."
        # Lazy installs would hang (PyPI is blocked); on failure the container stays not-ready.
        HERMES_CONFIG_OK=0
        if runuser -p -u wildawm-agent -- env HOME=/home/agent OPENAI_MODEL="$OPENAI_MODEL" \
            AWM_MODEL_CONTEXT_WINDOW="$AWM_MODEL_CONTEXT_WINDOW" \
            HERMES_DISABLE_LAZY_INSTALLS=1 \
            bash -lc '
            # A fixed model.model skips the /v1/models auto-detect, which the proxy does not serve.
            hermes config set model.provider custom            </dev/null 2>>/tmp/hermes-cfg.log &&
            hermes config set model.base_url "$AWM_LLM_PROXY_BASE_URL" </dev/null 2>>/tmp/hermes-cfg.log &&
            hermes config set model.api_key not-used         </dev/null 2>>/tmp/hermes-cfg.log &&
            hermes config set model.model "$OPENAI_MODEL" </dev/null 2>>/tmp/hermes-cfg.log &&
            if [ -n "${AWM_MODEL_CONTEXT_WINDOW:-}" ]; then
                hermes config set model.context_length "$AWM_MODEL_CONTEXT_WINDOW" </dev/null 2>>/tmp/hermes-cfg.log || exit 1
            fi &&
            if [ -n "${AWM_REASONING_EFFORT:-}" ]; then
                hermes config set agent.reasoning_effort "$AWM_REASONING_EFFORT" </dev/null 2>>/tmp/hermes-cfg.log
            fi
        ' </dev/null; then
            HERMES_CONFIG_OK=1
        else
            echo "[entrypoint] ERROR: hermes provider/model config FAILED — no turn can succeed without it; NOT touching /tmp/mcp-registered.flag so the adapter reports not-ready (see /tmp/hermes-cfg.log)"
        fi

        # hermes mcp add is interactive-only, so write mcp_servers through its config API.
        runuser -p -u wildawm-agent -- env HOME=/home/agent \
            HERMES_DISABLE_LAZY_INSTALLS=1 \
            bash -c '/opt/hermes-agent/.venv/bin/python - 2>>/tmp/hermes-cfg.log' <<'PYEOF' \
            || echo "[entrypoint] WARNING: hermes mcp registration had non-fatal errors (see /tmp/hermes-cfg.log)"
from hermes_cli.config import load_config, save_config
import os

SERVICES = tuple(filter(None, os.environ.get("AWM_ACTIVE_MCP_SERVICES", "").split()))
base = os.environ.get(
    "AWM_MCP_BASE_URL",
    f"http://mcp:{os.environ.get('AWM_MCP_INTERNAL_PORT', '8080')}",
).rstrip("/")
servers = {svc: {"url": f"{base}/{svc}/mcp", "enabled": True}
           for svc in SERVICES}
servers["wildawm"] = {"url": f"{base}/mcp", "enabled": True}
browser_url = os.environ.get(
    "AWM_BROWSER_MCP_URL",
    f"http://localhost:{os.environ.get('AWM_BROWSER_MCP_PORT', '8931')}/mcp",
)
servers["browser"] = {"url": browser_url, "enabled": True}

cfg = load_config()
cfg.setdefault("mcp_servers", {}).update(servers)
save_config(cfg)
print(f"[entrypoint] hermes: wrote {len(servers)} MCP servers to config.yaml")
PYEOF

        if [ "$HERMES_CONFIG_OK" = 1 ]; then
            touch /tmp/mcp-registered.flag
        fi

        echo "[entrypoint] hermes: container stays alive for the in-container adapter"
        exec runuser -p -u wildawm-agent -- \
            tail -f /dev/null
        ;;
    openclaw)
        # openclaw runs headless (agent --local) against the local proxy as a custom provider.
        echo "[entrypoint] Starting OpenAI-compat proxy on :$AWM_LLM_PROXY_PORT for openclaw..."
        start_llm_proxy /tmp/proxy.log
        sleep 2

        echo "[entrypoint] Configuring openclaw provider + MCP servers..."
        # Critical config: on failure the readiness flag is not set.
        OPENCLAW_CONFIG_OK=0
        if runuser -p -u wildawm-agent -- env HOME=/home/agent OPENAI_MODEL="$OPENAI_MODEL" \
            AWM_VISION_BASE_URL="$AWM_VISION_BASE_URL" \
            AWM_VISION_API_KEY="$AWM_VISION_API_KEY" \
            AWM_VISION_MODEL="$AWM_VISION_MODEL" \
            AWM_OPENCLAW_MODEL_CONTEXT_WINDOW="$AWM_OPENCLAW_MODEL_CONTEXT_WINDOW" \
            OPENCLAW_ACCEPT_RISK=1 bash -lc '
            M="$OPENAI_MODEL"
            INPUT_FLAG="--custom-image-input"
            if [ -f /agent-config/describe_image_mcp.py ]; then
                INPUT_FLAG="--custom-text-input"
            fi
            # onboard --skip-health avoids the gateway probe that fails setup. Keep apostrophe-free.
            openclaw onboard --non-interactive --accept-risk --flow manual \
                --auth-choice custom-api-key \
                --custom-provider-id wildawm \
                --custom-base-url "$AWM_LLM_PROXY_BASE_URL" \
                --custom-api-key not-used \
                --custom-model-id "$M" \
                --custom-compatibility openai \
                "$INPUT_FLAG" \
                --skip-health \
                --json > /tmp/openclaw-onboard.log 2>&1 &&
            openclaw models set "wildawm/$M" > /tmp/openclaw-models.log 2>&1 &&
            python3 - "$M" "${AWM_OPENCLAW_MODEL_CONTEXT_WINDOW:-131072}" <<PY &&
import json, os, sys
from pathlib import Path

model = sys.argv[1]
try:
    context = int(sys.argv[2])
except Exception:
    context = 131072
p = Path("/home/agent/.openclaw/openclaw.json")
cfg = json.loads(p.read_text())
provider = cfg.setdefault("models", {}).setdefault("providers", {}).setdefault("wildawm", {})
models = provider.setdefault("models", [])
if not models:
    models.append({"id": model, "name": model + " (Custom Provider)"})
reasoning = bool(os.environ.get("AWM_REASONING_EFFORT", "").strip())
for entry in models:
    if entry.get("id") == model:
        entry["contextWindow"] = context
        entry.setdefault("maxTokens", 4096)
        if reasoning:
            entry["reasoning"] = True
        break
else:
    entry = {"id": model, "name": model + " (Custom Provider)", "contextWindow": context, "maxTokens": 4096}
    if reasoning:
        entry["reasoning"] = True
    models.append(entry)
p.write_text(json.dumps(cfg, indent=2))
PY
            # Raise per-request and per-run timeouts so slow endpoints are not cut off.
            openclaw config set models.providers.wildawm.timeoutSeconds "${AWM_OPENCLAW_PROVIDER_TIMEOUT}" \
                > /tmp/openclaw-timeout.log 2>&1 &&
            openclaw config set agents.defaults.timeoutSeconds "${AWM_AGENT_TURN_TIMEOUT}" \
                >> /tmp/openclaw-timeout.log 2>&1
        ' </dev/null; then
            OPENCLAW_CONFIG_OK=1
        else
            echo "[entrypoint] ERROR: openclaw provider/model config FAILED (onboard --skip-health / models set) — no turn can succeed without it; NOT touching /tmp/mcp-registered.flag so the adapter reports not-ready (see /tmp/openclaw-{onboard,models}.log)"
        fi

        # Best-effort: vision wiring, exec policy and MCP registrations.
        runuser -p -u wildawm-agent -- env HOME=/home/agent OPENAI_MODEL="$OPENAI_MODEL" \
            AWM_VISION_BASE_URL="$AWM_VISION_BASE_URL" \
            AWM_VISION_API_KEY="$AWM_VISION_API_KEY" \
            AWM_VISION_MODEL="$AWM_VISION_MODEL" \
            AWM_OPENCLAW_MODEL_CONTEXT_WINDOW="$AWM_OPENCLAW_MODEL_CONTEXT_WINDOW" \
            OPENCLAW_ACCEPT_RISK=1 bash -lc '
            set +e
            M="$OPENAI_MODEL"
            # Vision wiring needs AWM_VISION_BASE_URL and AWM_VISION_MODEL; warn and skip otherwise.
            if [ -f /agent-config/describe_image_mcp.py ] \
               && [ -n "${AWM_VISION_BASE_URL:-}" ] \
               && [ -n "${AWM_VISION_MODEL:-}" ]; then
                python3 - "$AWM_VISION_BASE_URL" "$AWM_VISION_API_KEY" "$AWM_VISION_MODEL" <<PY
import json, sys
from pathlib import Path
p = Path("/home/agent/.openclaw/openclaw.json")
cfg = json.loads(p.read_text())
base = (sys.argv[1] or "").strip().rstrip("/")
key = (sys.argv[2] or "").strip()
model = (sys.argv[3] or "").strip()
providers = cfg.setdefault("models", {}).setdefault("providers", {})
providers["wildawm-vision"] = {
    "baseUrl": base,
    "api": "openai-completions",
    "apiKey": key,
    "models": [{
        "id": model,
        "name": model + " (WildAWM vision)",
        "contextWindow": 128000,
        "maxTokens": 4096,
        "input": ["text", "image"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "reasoning": False,
    }],
}
defaults = cfg.setdefault("agents", {}).setdefault("defaults", {})
defaults["imageModel"] = {"primary": "wildawm-vision/" + model}
defaults.setdefault("models", {})["wildawm-vision/" + model] = {}
p.write_text(json.dumps(cfg, indent=2))
PY
                openclaw mcp add vision \
                    --command python3 \
                    --arg /agent-config/describe_image_mcp.py \
                    --env "AWM_VISION_BASE_URL=$AWM_VISION_BASE_URL" \
                    --env "AWM_VISION_API_KEY=$AWM_VISION_API_KEY" \
                    --env "AWM_VISION_MODEL=$AWM_VISION_MODEL" \
                    --env "AWM_VISION_REASONING_EFFORT=${AWM_VISION_REASONING_EFFORT:-}" \
                    --env "AWM_VISION_REASONING_CHAT_PARAM=${AWM_VISION_REASONING_CHAT_PARAM:-}" \
                    --env "AWM_VISION_REASONING_RESPONSES_PARAM=${AWM_VISION_REASONING_RESPONSES_PARAM:-}" \
                    --timeout 300 \
                    --no-probe > /tmp/openclaw-vision-mcp.log 2>&1
            elif [ -f /agent-config/describe_image_mcp.py ]; then
                echo "[entrypoint] WARNING: AWM_VISION_BASE_URL / AWM_VISION_MODEL not set — skipping openclaw vision provider wiring; image tools will fail until the vision endpoint is passed through the agent env"
            else
                # Pin native image-tool calls to the primary multimodal model.
                openclaw config set agents.defaults.imageModel.primary "wildawm/$M" \
                    > /tmp/openclaw-imagemodel.log 2>&1
            fi
            # Headless: auto-approve every exec/tool call.
            openclaw exec-policy preset yolo > /tmp/openclaw-execpolicy.log 2>&1
            # Save MCP servers without a connect probe for fast startup.
            for svc in $AWM_ACTIVE_MCP_SERVICES; do
                openclaw mcp add $svc --url "$AWM_MCP_BASE_URL/$svc/mcp" --transport streamable-http --no-probe 2>>/tmp/openclaw-cfg.log
            done
            openclaw mcp add wildawm --url "$AWM_MCP_BASE_URL/mcp" --transport streamable-http --no-probe 2>>/tmp/openclaw-cfg.log
            openclaw mcp add browser --url "$AWM_BROWSER_MCP_URL" --transport streamable-http --no-probe 2>>/tmp/openclaw-cfg.log
        ' </dev/null || echo "[entrypoint] WARNING: openclaw config had non-fatal errors (see /tmp/openclaw-*.log)"

        if [ "$OPENCLAW_CONFIG_OK" = 1 ]; then
            touch /tmp/mcp-registered.flag
        fi

        echo "[entrypoint] openclaw: container stays alive for the in-container adapter"
        exec runuser -p -u wildawm-agent -- \
            tail -f /dev/null
        ;;
    *)
        echo "[entrypoint] ERROR: unknown AWM_AGENT_KIND=$AWM_AGENT_KIND"
        echo "  supported: nanobot, opencode, claude-code, codex, hermes, openclaw"
        exit 1
        ;;
esac
