#!/usr/bin/env python3
"""Restore a backup produced by backup_postgres.py (spec section 45). See README "Recuperación ante desastres".

    python3 scripts/restore_postgres.py backups/permutados_20261008T070000Z.dump \
        --database-url postgresql://permutados:***@localhost:5432/permutados_restore --create

Safety:
* verifies the .sha256 checksum and that ``pg_restore --list`` can read the archive before touching anything;
* refuses to restore into a database that already has tables unless ``--force`` is given (then the existing
  objects are dropped and replaced, ``--clean --if-exists``);
* restores in a single transaction with ``--exit-on-error`` (all or nothing);
* afterwards compares the alembic revision and the row count of every table with the manifest.
``--verify-only`` runs the checks without restoring. Stop the backend and the worker before restoring over the
production database. Prints a JSON summary (no secrets); exit code != 0 on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pgtools import SAFE_IDENT, Target, alembic_revision, sha256_file, table_counts  # noqa: E402


def verify(t: Target, path: str) -> dict:
    if not os.path.isfile(path):
        raise SystemExit(f"backup not found: {path}")
    out: dict = {"file": path}
    sha_file = path + ".sha256"
    if os.path.exists(sha_file):
        expected = open(sha_file).read().split()[0]
        actual = sha256_file(path)
        if expected != actual:
            raise RuntimeError("checksum mismatch: the backup file is corrupt or was modified")
        out["checksum"] = "ok"
    else:
        out["checksum"] = "missing"
    with open(path, "rb") as f:
        listing = t.run("pg_restore", "--list", stdin=f).stdout.decode()
    out["archive_entries"] = sum(1 for line in listing.splitlines() if line and not line.startswith(";"))
    manifest_path = path[: -len(".dump")] + ".json" if path.endswith(".dump") else None
    out["manifest"] = json.load(open(manifest_path)) if manifest_path and os.path.exists(manifest_path) else None
    return out


def restore(t: Target, path: str, *, create: bool, force: bool) -> dict:
    info = verify(t, path)
    admin_db = "postgres"
    if not t.dbname or not SAFE_IDENT.match(t.dbname):
        raise RuntimeError("target database name must match [a-z_][a-z0-9_]*")
    # The name was validated above (no quotes possible).
    exists = t.sql(f"SELECT 1 FROM pg_database WHERE datname = '{t.dbname}'", admin_db) == "1"  # noqa: S608
    if not exists:
        if not create:
            raise RuntimeError(f"database {t.dbname!r} does not exist (use --create)")
        t.run("createdb", t.dbname)
    else:
        n = int(t.sql("SELECT count(*) FROM pg_tables WHERE schemaname='public'"))
        if n and not force:
            raise RuntimeError(f"database {t.dbname!r} already has {n} tables; use --force to replace them")
    args = ["--no-owner", "--no-privileges", "--single-transaction", "--exit-on-error", "-d", t.dbname]
    if exists and force:
        args = ["--clean", "--if-exists", *args]
    with open(path, "rb") as f:
        t.run("pg_restore", *args, stdin=f)
    result = {"ok": True, "restored_into": t.dbname, "checksum": info["checksum"]}
    counts = table_counts(t)
    result["tables"] = len(counts)
    result["alembic_revision"] = alembic_revision(t)
    m = info["manifest"]
    if m:
        diff = {k: {"backup": v, "restored": counts.get(k)} for k, v in m["row_counts"].items() if counts.get(k) != v}
        result["row_counts_match"] = not diff
        result["revision_match"] = m.get("alembic_revision") == result["alembic_revision"]
        if diff:
            result["row_count_differences"] = diff
            result["ok"] = False
        if not result["revision_match"]:
            result["ok"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--database-url", default=os.environ.get("RESTORE_DATABASE_URL") or os.environ.get("DATABASE_URL"))
    ap.add_argument("--docker-service", default=None)
    ap.add_argument("--create", action="store_true", help="create the target database if missing")
    ap.add_argument("--force", action="store_true", help="replace the objects of a non-empty target database")
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args(argv)
    if a.docker_service:
        t = Target(
            user=os.environ.get("POSTGRES_USER", "permutados"),
            dbname=os.environ.get("POSTGRES_DB", "permutados"),
            docker_service=a.docker_service,
        )
    elif a.database_url:
        t = Target.from_url(a.database_url)
    else:
        print(json.dumps({"ok": False, "error": "set --database-url / RESTORE_DATABASE_URL"}))
        return 2
    try:
        if a.verify_only:
            info = verify(t, a.file)
            info.pop("manifest", None)
            print(json.dumps({"ok": True, **info}))
            return 0
        res = restore(t, a.file, create=a.create, force=a.force)
        print(json.dumps(res))
        return 0 if res["ok"] else 1
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode(errors="replace")[-800:]
        print(json.dumps({"ok": False, "error": f"{e.cmd[0] if e.cmd else 'command'} failed", "detail": err}))
        return 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
