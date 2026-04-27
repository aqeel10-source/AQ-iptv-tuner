#!/bin/bash
# Daily backup of all AQ-iptv-tuner files.
# Run via DSM Task Scheduler (root, daily).
#
# Creates a timestamped .tar.gz in BACKUP_DIR, keeps the newest KEEP_DAYS.
# Excludes the Backup dir itself, __pycache__, .DS_Store, @eaDir, logos cache
# (logos are re-fetched on startup, ~15MB savings per archive).

set -euo pipefail

PROJECT_DIR="/volume1/docker/AQ-iptv-tuner"
BACKUP_DIR="${PROJECT_DIR}/Backup"
KEEP_DAYS=7
STAMP=$(date +%Y%m%d-%H%M)
ARCHIVE="${BACKUP_DIR}/iptv-backup-${STAMP}.tar.gz"

mkdir -p "${BACKUP_DIR}"

# Create tarball (relative paths, exclude bulky/regenerable dirs)
tar -czf "${ARCHIVE}" \
    -C "$(dirname "${PROJECT_DIR}")" \
    --exclude='AQ-iptv-tuner/Backup' \
    --exclude='AQ-iptv-tuner/app/.env' \
    --exclude='AQ-iptv-tuner/config/logos' \
    --exclude='AQ-iptv-tuner/config/backups' \
    --exclude='AQ-iptv-tuner/config/app.log' \
    --exclude='AQ-iptv-tuner/logs' \
    --exclude='AQ-iptv-tuner/.claude' \
    --exclude='__pycache__' \
    --exclude='.DS_Store' \
    --exclude='@eaDir' \
    "AQ-iptv-tuner"

SIZE=$(du -h "${ARCHIVE}" | cut -f1)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup created: ${ARCHIVE} (${SIZE})"

# Rotate: delete backups older than KEEP_DAYS
DELETED=0
find "${BACKUP_DIR}" -name 'iptv-backup-*.tar.gz' -mtime +${KEEP_DAYS} -print -delete | while read f; do
    DELETED=$((DELETED + 1))
done
REMAINING=$(find "${BACKUP_DIR}" -name 'iptv-backup-*.tar.gz' | wc -l)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Rotation: ${REMAINING} backups kept (max ${KEEP_DAYS} days)"
