#!/usr/bin/env python3
"""PostgreSQL backup with verification, checksum, manifest and rotation (spec section 45).

    python3 scripts/backup_postgres.py                     # uses DATABASE_URL, BACKUP_DIR, BACKUP_RETENTION_DAYS
    python3 scripts/backup_postgres.py --docker-service postgres   # pg_dump inside the compose container

Produces in BACKUP_DIR:
    permutados_<UTC timestamp>.dump        pg_dump custom format (compressed), mode 0600
    permutados_<UTC timestamp>.dump.sha256 checksum (verified by the restore script)
    permutados_<UTC timestamp>.json        manifest: alembic revision, row counts per table, size, sha256

Steps: dump to a .partial file -> ``pg_restore --list`` (the archive is readable) -> checksum + manifest ->
atomic rename -> rotation (delete backups older than the retention, but always keep the newest
``--keep-min``) -> optional off-site copy (BACKUP_COPY_CMD, e.g. ``rclone copy {file} remote:permutados``).

Important: phone numbers / names are encrypted with ENCRYPTION_KEY, which is NOT in the database. Back the key
up separately (password manager / offline): a backup without the key cannot reveal contacts, and the key without
the backup is useless.
Exit code != 0 on any failure; prints a JSON summary (no secrets).
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pgtools import SnapshotSession, Target, alembic_revision, sha256_file, table_counts  # noqa: E402

PREFIX = "permutados_"


def rotate(directory: str, retention_days: int, keep_min: int, now: float | None = None) -> list[str]:
    """Delete backups older than ``retention_days`` but always keep the newest ``keep_min``."""
    now = now or time.time()
    dumps = sorted(glob.glob(os.path.join(directory, f"{PREFIX}*.dump")), key=os.path.getmtime, reverse=True)
    removed = []
    for path in dumps[keep_min:]:
        if now - os.path.getmtime(path) > retention_days * 86400:
            for p in (path, path + ".sha256", path[: -len(".dump")] + ".json"):
                if os.path.exists(p):
                    os.remove(p)
            removed.append(os.path.basename(path))
    for partial in glob.glob(os.path.join(directory, f"{PREFIX}*.partial")):
        if now - os.path.getmtime(partial) > 86400:  # leftovers of a crashed run
            os.remove(partial)
    return removed


def backup(t: Target, directory: str, *, retention_days: int, keep_min: int, copy_cmd: str | None) -> dict:
    os.makedirs(directory, exist_ok=True)
    os.chmod(directory, 0o700)
    old_umask = os.umask(0o077)
    try:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        final = os.path.join(directory, f"{PREFIX}{stamp}.dump")
        n = 1
        while os.path.exists(final):  # two runs in the same second
            final = os.path.join(directory, f"{PREFIX}{stamp}-{n}.dump")
            n += 1
        partial = final + ".partial"
        started = time.monotonic()
        snap = SnapshotSession(t)  # dump and manifest counts from the same consistent snapshot
        try:
            args = ["-Fc", "-Z", "6", "--no-owner", "--no-privileges", f"--snapshot={snap.snapshot_id}"]
            if t.dbname:
                args += ["-d", t.dbname]
            with open(partial, "wb") as out:
                t.run("pg_dump", *args, stdout=out)
            revision = alembic_revision(t, session=snap)
            counts = table_counts(t, session=snap)
        finally:
            snap.close()
        # The archive must be readable by pg_restore (catches truncated / corrupt dumps).
        with open(partial, "rb") as f:
            listing = t.run("pg_restore", "--list", stdin=f).stdout.decode()
        if "TABLE DATA" not in listing:
            raise RuntimeError("backup verification failed: no table data in archive")
        digest = sha256_file(partial)
        manifest = {
            "file": os.path.basename(final),
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
            "database": t.dbname,
            "alembic_revision": revision,
            "row_counts": counts,
            "size_bytes": os.path.getsize(partial),
            "sha256": digest,
            "format": "pg_dump custom (-Fc)",
            "encryption_note": "PII columns are AES-256-GCM encrypted; ENCRYPTION_KEY is NOT included",
        }
        with open(final + ".sha256", "w") as f:
            f.write(f"{digest}  {os.path.basename(final)}\n")
        with open(final[: -len(".dump")] + ".json", "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        os.replace(partial, final)
        removed = rotate(directory, retention_days, keep_min)
        copied = None
        if copy_cmd:
            cmd = [part.replace("{file}", final) for part in shlex.split(copy_cmd)]
            subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603 - admin-configured command
            copied = True
        return {
            "ok": True,
            "file": final,
            "sha256": digest,
            "size_bytes": manifest["size_bytes"],
            "tables": len(manifest["row_counts"]),
            "alembic_revision": manifest["alembic_revision"],
            "seconds": round(time.monotonic() - started, 2),
            "rotated_out": removed,
            "offsite_copy": copied,
        }
    except Exception:
        for p in glob.glob(os.path.join(directory, "*.partial")):
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    finally:
        os.umask(old_umask)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--docker-service", default=os.environ.get("BACKUP_DOCKER_SERVICE") or None)
    ap.add_argument("--dir", default=os.environ.get("BACKUP_DIR", "./backups"))
    ap.add_argument("--retention-days", type=int, default=int(os.environ.get("BACKUP_RETENTION_DAYS", "14")))
    ap.add_argument("--keep-min", type=int, default=int(os.environ.get("BACKUP_KEEP_MIN", "7")))
    ap.add_argument("--copy-cmd", default=os.environ.get("BACKUP_COPY_CMD") or None)
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
        print(json.dumps({"ok": False, "error": "set DATABASE_URL or --docker-service"}))
        return 2
    try:
        print(json.dumps(backup(t, a.dir, retention_days=a.retention_days, keep_min=a.keep_min, copy_cmd=a.copy_cmd)))
        return 0
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode(errors="replace")[-500:]
        print(json.dumps({"ok": False, "error": f"{e.cmd[0] if e.cmd else 'command'} failed", "detail": err}))
        return 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
