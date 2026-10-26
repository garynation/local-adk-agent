#!/usr/bin/env bash
set -uo pipefail

DATA_DIR="${DATA_DIR:-/data}"

# 1. Ensure required /data subdirectories exist on the mounted host volume.
# If the host directory was auto-created by Docker daemon as root:root, fall back gracefully
# so uvicorn still starts and serves the Web UI on port 8080 instead of crash-looping.
if ! mkdir -p "${DATA_DIR}/chrome-profile" "${DATA_DIR}/screenshots" 2>/dev/null; then
  echo "[WARN] ${DATA_DIR} is not writable by UID $(id -u). Fix on host with: sudo chown -R \$(id -u):\$(id -g) ~/.config/local-adk-agent/data" >&2
  export DATA_DIR="/tmp/adk-data"
  mkdir -p "${DATA_DIR}/chrome-profile" "${DATA_DIR}/screenshots"
fi
chmod 700 "${DATA_DIR}/chrome-profile" 2>/dev/null || true

# 2. Lock down any secret/token/backup files on /data to owner-only (0600) without ever overwriting them
for secret_file in \
  "${DATA_DIR}/config.json" \
  "${DATA_DIR}/config.json.bak" \
  "${DATA_DIR}/.secret.key" \
  "${DATA_DIR}/sessions.db" \
  "${DATA_DIR}/client_secret.json" \
  "${DATA_DIR}/client_secret.json.bak" \
  "${DATA_DIR}/workspace_client_secret.json" \
  "${DATA_DIR}/workspace_token.json" \
  "${DATA_DIR}/workspace_token.json.bak" \
  "${DATA_DIR}/gchat_service_account.json" \
  "${DATA_DIR}/gchat_service_account.json.bak"; do
  if [ -f "${secret_file}" ]; then
    chmod 600 "${secret_file}" 2>/dev/null || true
  fi
done

# 3. Purge stale Chromium SingletonLock symlinks left from previous container recreations
rm -f "${DATA_DIR}/chrome-profile/SingletonLock" \
      "${DATA_DIR}/chrome-profile/SingletonCookie" \
      "${DATA_DIR}/chrome-profile/SingletonSocket" 2>/dev/null || true

exec "$@"
