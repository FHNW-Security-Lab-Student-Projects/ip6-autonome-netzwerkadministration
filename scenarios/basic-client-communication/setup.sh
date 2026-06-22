#!/usr/bin/env bash
# BASELINE (no fault): healthy-topology sanity check.
# This injects NO fault and has NO teardown. It only clears the snapshot history so the run
# starts from a fresh state, exactly like the fault scenarios do. There is deliberately no
# before/after `--capture-once` here: with no fault there is no good->bad diff to record.
set -euo pipefail

rm -f "$(cd "$(dirname "$0")" && pwd)/../../state_snapshots.json"
echo "[basic-client-communication] cleared snapshot history (no fault injected)"
