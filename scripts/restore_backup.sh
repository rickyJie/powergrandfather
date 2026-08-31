#!/usr/bin/env bash
# scripts/restore_backup.sh — restore a CSM backup tar.gz into the
# current project root. See backend/csm/api/backup.py for the shape.
#
# This script is *destructive*: it overwrites csm.db, csm.db-wal,
# csm.db-shm, tasks/ (rsync-style), and alembic/versions/ (add-only).
# The CSM service MUST be stopped first (scripts/stop.sh) — SQLite
# will corrupt if two processes touch the WAL simultaneously.
#
# Usage:
#   ./scripts/restore_backup.sh backups/csm-backup-YYYYMMDD-HHMMSS.tar.gz
#
# Optional env:
#   CSM_RESTORE_YES=1   skip the interactive confirmation
#   CSM_RESTORE_KEEP_TASKS=1  don't touch tasks/ (DB only)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ $# -ne 1 ]; then
  echo "usage: $0 <backup.tar.gz>" >&2
  exit 2
fi

TAR_PATH="$1"
if [ ! -f "$TAR_PATH" ]; then
  echo "backup archive not found: $TAR_PATH" >&2
  exit 2
fi

# Refuse to run against a live process. csm.pid is the canonical
# liveness marker; scripts/stop.sh removes it after a clean shutdown.
if [ -f "$PROJECT_ROOT/csm.pid" ]; then
  PID=$(cat "$PROJECT_ROOT/csm.pid" 2>/dev/null || true)
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "csm.pid points to running process $PID — stop the service first with scripts/stop.sh" >&2
    exit 3
  fi
fi

echo "About to restore into: $PROJECT_ROOT"
echo "  from: $TAR_PATH"
echo "This overwrites csm.db*, tasks/, and adds alembic/versions/*.py."
if [ "${CSM_RESTORE_YES:-}" != "1" ]; then
  printf "Continue? [y/N] "
  read -r ANSWER
  case "$ANSWER" in
    y|Y|yes|YES) ;;
    *) echo "aborted"; exit 1 ;;
  esac
fi

STAGE=$(mktemp -d -t csm-restore-XXXXXXXX)
trap 'rm -rf "$STAGE"' EXIT

echo "-> unpacking to $STAGE"
tar -xzf "$TAR_PATH" -C "$STAGE"

BUNDLE="$STAGE/csm-backup"
if [ ! -d "$BUNDLE" ]; then
  echo "archive layout unexpected — expected top-level csm-backup/ dir" >&2
  exit 4
fi

if [ -f "$BUNDLE/metadata.json" ]; then
  echo "-> metadata:"
  cat "$BUNDLE/metadata.json" | sed 's/^/     /'
fi

# 1. Database. Wipe existing WAL/SHM so the restored db is loaded
#    cleanly (leftover WAL from the old DB would confuse SQLite).
if [ -f "$BUNDLE/csm.db" ]; then
  echo "-> restoring csm.db"
  rm -f "$PROJECT_ROOT/csm.db" "$PROJECT_ROOT/csm.db-wal" "$PROJECT_ROOT/csm.db-shm"
  cp "$BUNDLE/csm.db" "$PROJECT_ROOT/csm.db"
else
  echo "!! no csm.db in bundle — skipping DB restore" >&2
fi

# 2. Workflow YAMLs.
if [ "${CSM_RESTORE_KEEP_TASKS:-}" = "1" ]; then
  echo "-> CSM_RESTORE_KEEP_TASKS=1 — leaving tasks/ untouched"
elif [ -d "$BUNDLE/tasks" ]; then
  echo "-> restoring tasks/"
  mkdir -p "$PROJECT_ROOT/tasks"
  # cp -R overwrites individual files; we don't delete tasks that
  # exist locally but not in the backup, to protect in-flight work.
  cp -R "$BUNDLE/tasks/." "$PROJECT_ROOT/tasks/"
fi

# 3. Alembic version scripts — add-only. If the backup was taken on a
#    newer HEAD than the target box, this pulls in the migration files
#    the target needs to `alembic upgrade head` up to that version.
if [ -d "$BUNDLE/alembic/versions" ]; then
  echo "-> adding alembic version scripts"
  mkdir -p "$PROJECT_ROOT/alembic/versions"
  cp -n "$BUNDLE/alembic/versions/"*.py "$PROJECT_ROOT/alembic/versions/" 2>/dev/null || true
fi

echo
echo "restore complete."
echo "next steps:"
echo "  1. conda activate csm"
echo "  2. alembic upgrade head    # aligns schema with the restored DB"
echo "  3. ./scripts/start.sh      # bring the service back up"
