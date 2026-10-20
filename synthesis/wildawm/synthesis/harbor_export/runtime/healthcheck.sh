#!/bin/bash
# Healthcheck: WebHarbor sites, MCP gateway, baseline snapshot and browser MCP must all be up.
set -uo pipefail

CONTROL_URL=http://127.0.0.1:8101
MCP_PORT=18080
BROWSER_MCP_PORT=8931

curl -sf --max-time 10 "$CONTROL_URL/health" > /tmp/hc-webharbor.json || exit 1
python3 - <<'PY' || exit 1
import json, sys
row = json.load(open("/tmp/hc-webharbor.json"))
sites = row.get("sites") or {}
ok = (row.get("ok") is True and len(sites) == 15
      and all(s.get("alive") for s in sites.values()))
sys.exit(0 if ok else 1)
PY

curl -sf --max-time 10 "http://127.0.0.1:$MCP_PORT/health" > /tmp/hc-mcp.json || exit 1
python3 - <<'PY' || exit 1
import json, sys
row = json.load(open("/tmp/hc-mcp.json"))
apps = row.get("loaded_apps") or []
ok = (row.get("status") == "ok" and len(apps) >= 8 and not row.get("errors"))
sys.exit(0 if ok else 1)
PY

[ -d /baseline/data ] && [ -d /baseline/workspace ] && [ -d /baseline/webharbor ] || exit 1

# Use the localhost hostname: the server rejects other Host headers.
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -X POST "http://localhost:$BROWSER_MCP_PORT/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"harbor-healthcheck","version":"1.0"}}}')" || exit 1
[ "$code" = "200" ] || exit 1

exit 0
