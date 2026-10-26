#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"

# 1. Ensure required /data subdirectories exist on the mounted host volume
mkdir -p "${DATA_DIR}/chrome-profile" "${DATA_DIR}/screenshots"
chmod 700 "${DATA_DIR}/chrome-profile"

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
    chmod 600 "${secret_file}"
  fi
done

# 3. Purge stale Chromium SingletonLock symlinks left from previous container recreations
rm -f "${DATA_DIR}/chrome-profile/SingletonLock" \
      "${DATA_DIR}/chrome-profile/SingletonCookie" \
      "${DATA_DIR}/chrome-profile/SingletonSocket"

exec "$@"
