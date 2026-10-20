#!/bin/bash
# Oracle solution: run golden.py against the live task directories.
set -euo pipefail

python3 /solution/golden.py \
    --workspace /workspace \
    --data-dir /world/data \
    --webharbor-dir /world/webharbor \
    --baseline-dir /baseline

# Sync the golden-written WebHarbor DBs into the served sites.
bash /opt/wildawm-harbor/restore_webharbor.sh
