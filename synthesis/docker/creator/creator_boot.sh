#!/bin/bash
# Load the world's web seeds into instance_seed/ before the upstream start script runs.
set -e

WORLD="${AWM_WORLD_DIR:-/world}"

if [ -d "$WORLD/webharbor" ]; then
    echo "[creator] loading first-class web seeds from $WORLD/webharbor"
    python3 /opt/load_webharbor_seeds.py "$WORLD/webharbor" || true
else
    echo "[creator] no $WORLD/webharbor mounted — serving pristine image seeds"
fi

echo "[creator] handing off to /opt/websyn_start.sh"
exec /opt/websyn_start.sh
