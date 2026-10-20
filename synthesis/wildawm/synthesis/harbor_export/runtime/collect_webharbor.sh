#!/bin/bash
# Copy the served WebHarbor site DBs into a flat directory for the verifier.
set -euo pipefail

TARGET=${1:-/tmp/verify-webharbor}
mkdir -p "$TARGET"

python3 - "$TARGET" <<'PY'
import json, shutil, sys
from pathlib import Path

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
target = Path(sys.argv[1])
config = json.loads(Path("/opt/webharbor-config/sites.json").read_text())
copied = 0
for site in config["sites"]:
    dbfile = DB_FILE_BY_SITE[site["id"]]
    source = Path("/opt/WebSyn") / site["id"] / "instance" / dbfile
    if not source.is_file():
        raise SystemExit(f"live WebHarbor DB is missing: {source}")
    shutil.copy2(source, target / dbfile)
    copied += 1
print(f"collected {copied} served WebHarbor DBs into {target}")
PY
