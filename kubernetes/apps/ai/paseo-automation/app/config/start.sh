#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${PASEO_PASSWORD:?PASEO_PASSWORD must be non-empty}"
mkdir -p "$CODEX_HOME" "$PASEO_HOME"
install -m 600 /etc/paseo/codex.toml "$CODEX_HOME/config.toml"
install -m 600 /etc/paseo/paseo.json "$PASEO_HOME/config.json"
exec /usr/local/bin/paseo-docker-entrypoint
