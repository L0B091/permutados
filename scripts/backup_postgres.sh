#!/usr/bin/env bash
# Cron wrapper for scripts/backup_postgres.py (no secrets are echoed). Example crontab on the server:
#   15 4 * * * /opt/permutados/scripts/backup_postgres.sh >> /var/log/permutados-backup.log 2>&1
# BACKUP_MODE=docker  -> pg_dump runs inside the compose "postgres" container (default when docker is present)
# BACKUP_MODE=native  -> uses DATABASE_URL with the pg_dump on PATH (must be >= the server major version)
set -euo pipefail
cd "$(dirname "$0")/.."
# Load .env without overriding variables already set in the environment (cron/systemd may set them).
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[[:space:]]*([A-Z_][A-Z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
    if [[ "$val" == \"*\" || "$val" == \'*\' ]]; then
      val="${val:1:${#val}-2}"
    else
      val="${val%%[[:space:]]#*}"; val="${val%"${val##*[![:space:]]}"}"  # inline comment, trailing spaces
    fi
    [ -n "${!key+x}" ] || export "$key=$val"
  done < .env
fi
mode="${BACKUP_MODE:-}"
if [ -z "$mode" ]; then
  if command -v docker >/dev/null 2>&1; then mode=docker; else mode=native; fi
fi
if [ "$mode" = docker ]; then
  exec python3 scripts/backup_postgres.py --docker-service "${BACKUP_DOCKER_SERVICE:-postgres}"
else
  exec python3 scripts/backup_postgres.py
fi
