#!/bin/bash
set -e

# TEMU Image Factory Docker entrypoint
# 1. Remap PUID/PGID (when root)
# 2. Ensure persistent directories exist
# 3. Seed missing default configs / prompt templates (never overwrite user files)
# 4. Fix ownership (no chmod 777)
# 5. Drop privileges to the non-root tif user via gosu and exec CMD

PUID=${PUID:-1000}
PGID=${PGID:-1000}

CONFIG_DIR=${CONFIG_DIR:-/app/config}
TEMPLATE_DIR=${TEMPLATE_DIR:-/app/templates}
DATA_DIR=${DATA_DIR:-/app/data}
INPUT_DIR=${INPUT_DIR:-/app/input}
OUTPUT_DIR=${OUTPUT_DIR:-/app/output}
CACHE_DIR=${CACHE_DIR:-/app/cache}
LOGS_DIR=${LOGS_DIR:-/app/logs}
INPUT_DEFAULTS_DIR=${INPUT_DEFAULTS_DIR:-/app/input-defaults}

run_init() {
    # Ensure persistent directories exist
    mkdir -p "$CONFIG_DIR" "$TEMPLATE_DIR" "$DATA_DIR" \
        "$INPUT_DIR" "$OUTPUT_DIR" "$CACHE_DIR" "$LOGS_DIR"
    # Seed the bundled sample SKU only on an empty first-run input volume.
    # Existing operator input is never replaced.
    if [ -d "$INPUT_DEFAULTS_DIR" ] && [ -z "$(ls -A "$INPUT_DIR")" ]; then
        cp -a "$INPUT_DEFAULTS_DIR"/. "$INPUT_DIR"/
    fi
    # Seed missing default configs/templates (idempotent, never overwrites)
    python -m src.utils.bootstrap
}

if [ "$(id -u)" = "0" ]; then
    if [ "$PUID" != "1000" ] || [ "$PGID" != "1000" ]; then
        groupmod -g "$PGID" tif 2>/dev/null || true
        usermod -u "$PUID" -g "$PGID" tif 2>/dev/null || true
    fi
    run_init
    # Ensure persistent directories are accessible by app user
    chown -R tif:tif "$CONFIG_DIR" "$TEMPLATE_DIR" "$DATA_DIR" \
        "$INPUT_DIR" "$OUTPUT_DIR" "$CACHE_DIR" "$LOGS_DIR" 2>/dev/null || true
    # Final process runs as non-root tif (PID 1 = gosu -> uvicorn as tif)
    exec gosu tif "$@"
else
    # Already non-root; cannot remap or chown. Just init and run.
    run_init
    exec "$@"
fi
