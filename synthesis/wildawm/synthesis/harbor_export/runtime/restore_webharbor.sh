#!/bin/bash
# Restore the world's WebHarbor DBs into the served sites and verify the served sha256.
set -euo pipefail

WORLD=/world
WEBHARBOR_ROOT=/opt/WebSyn
CONTROL_URL=http://127.0.0.1:8101
LOG_DIR=/tmp/wildawm-logs
mkdir -p "$LOG_DIR"

# Mirrors DB_FILE_BY_SITE in wildawm/webharbor/lib.py.
python3 - > /tmp/wildawm-harbor-sites.txt <<'PY'
import json
DB_FILE_BY_SITE = {
    "allrecipes": "allrecipes.db",
    "amazon": "amazon_store.db",
    "apple": "apple_store.db",
    "arxiv": "arxiv.db",
    "bbc_news": "bbc_news.db",
    "booking": "booking.db",
    "github": "github_mirror.db",
    "google_flights": "google_flights.db",
    "google_map": "gmaps.db",
    "google_search": "google_search.db",
    "huggingface": "hf.db",
    "wolfram_alpha": "wolfram_alpha.db",
    "cambridge_dictionary": "cambridge.db",
    "coursera": "coursera.db",
    "espn": "espn.db",
}
config = json.loads(open("/opt/webharbor-config/sites.json").read())
for site in config["sites"]:
    print(f"{site['id']} {DB_FILE_BY_SITE[site['id']]}")
PY

while read -r SITE DBFILE; do
    SOURCE="$WORLD/webharbor/$DBFILE"
    if [ ! -f "$SOURCE" ]; then
        echo "[restore_webharbor] FATAL: world is missing $SOURCE" >&2
        exit 1
    fi
    for SUBDIR in instance_seed instance; do
        TARGET="$WEBHARBOR_ROOT/$SITE/$SUBDIR/$DBFILE"
        mkdir -p "$(dirname "$TARGET")"
        cp -f "$SOURCE" "$TARGET"
    done
    curl -s -X POST "$CONTROL_URL/restart/$SITE" -o /tmp/restart-$SITE.json || true
    EXPECTED=$(sha256sum "$SOURCE" | cut -d' ' -f1)
    for ATTEMPT in $(seq 1 8); do
        sleep 2
        SERVED="$WEBHARBOR_ROOT/$SITE/instance/$DBFILE"
        if [ -f "$SERVED" ]; then
            ACTUAL=$(sha256sum "$SERVED" | cut -d' ' -f1)
            if [ "$ACTUAL" = "$EXPECTED" ]; then
                echo "[restore_webharbor] $SITE: served sha256 matches world seed (attempt $ATTEMPT)"
                continue 2
            fi
        fi
        echo "[restore_webharbor] $SITE: served bytes mismatch (attempt $ATTEMPT); restarting" >&2
        cp -f "$SOURCE" "$WEBHARBOR_ROOT/$SITE/instance/$DBFILE"
        curl -s -X POST "$CONTROL_URL/restart/$SITE" -o /tmp/restart-$SITE.json || true
    done
    echo "[restore_webharbor] FATAL: $SITE never served the world seed bytes" >&2
    exit 1
done < /tmp/wildawm-harbor-sites.txt
echo "[restore_webharbor] all sites restored and verified" | tee -a "$LOG_DIR/restore.log"
