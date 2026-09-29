#!/usr/bin/env bash
set -euo pipefail

DB_PATH="${1:-/data/db/app.sqlite}"
BACKUP_DIR="${2:-/data/library/backups/sqlite}"

mkdir -p "${BACKUP_DIR}"
TS="$(date +%Y%m%d-%H%M%S)"
TARGET="${BACKUP_DIR}/app-${TS}.sqlite"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required for backup" >&2
  exit 1
fi

sqlite3 "${DB_PATH}" "PRAGMA wal_checkpoint(FULL);"
sqlite3 "${DB_PATH}" ".backup '${TARGET}'"

echo "backup_complete target=${TARGET}"
