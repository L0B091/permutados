#!/usr/bin/env bash
# Daily logical backup with retention. Run from cron on the server, e.g.
#   15 4 * * * /opt/permutados/scripts/backup_postgres.sh >> /var/log/permutados-backup.log 2>&1
# Uses the compose postgres service; credentials come from .env (never echoed).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION="${BACKUP_RETENTION_DAYS:-14}"
mkdir -p "$BACKUP_DIR"
umask 077
file="$BACKUP_DIR/permutados_$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$file.partial"
mv "$file.partial" "$file"
find "$BACKUP_DIR" -name 'permutados_*.dump' -type f -mtime +"$RETENTION" -delete
echo "backup ok: $file"
# Restore: docker compose exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean < file.dump
