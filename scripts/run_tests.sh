#!/usr/bin/env bash
# Runs the whole suite against a REAL PostgreSQL and Redis.
#   TEST_DATABASE_URL (default postgresql+psycopg://permutados:permutados_dev@localhost:5432/permutados_test)
#   TEST_REDIS_URL    (default redis://localhost:6379/15)
# WARNING: the test database schema is dropped and recreated.
set -euo pipefail
cd "$(dirname "$0")/../backend"
PY="${PYTHON:-../.venv/bin/python}"
"$PY" -m ruff check app tests migrations ../scripts
"$PY" -m pytest -q "$@"
