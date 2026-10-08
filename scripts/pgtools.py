"""Shared helpers for the backup/restore scripts. Standard library only (runs on the host or in a container).

Connection: either a DATABASE_URL (native pg_dump/pg_restore/psql on PATH) or ``docker_service`` (run the tools
inside the docker compose PostgreSQL container, connecting through its local socket). Passwords are passed via
the PGPASSWORD environment variable, never on the command line, and never printed.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass
class Target:
    host: str | None = None
    port: str | None = None
    user: str | None = None
    password: str | None = None
    dbname: str | None = None
    docker_service: str | None = None

    @classmethod
    def from_url(cls, url: str) -> Target:
        u = urlparse(url.replace("postgresql+psycopg://", "postgresql://", 1))
        if u.scheme not in ("postgresql", "postgres"):
            raise SystemExit("DATABASE_URL must be a postgresql:// URL")
        return cls(
            host=u.hostname,
            port=str(u.port) if u.port else None,
            user=unquote(u.username) if u.username else None,
            password=unquote(u.password) if u.password else None,
            dbname=u.path.lstrip("/") or None,
        )

    def with_db(self, dbname: str) -> Target:
        return Target(self.host, self.port, self.user, self.password, dbname, self.docker_service)

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        for k, v in (("PGHOST", self.host), ("PGPORT", self.port), ("PGUSER", self.user), ("PGDATABASE", self.dbname)):
            if v:
                env[k] = v
        if self.password:
            env["PGPASSWORD"] = self.password
        return env

    def cmd(self, tool: str, *args: str) -> list[str]:
        if self.docker_service:
            base = ["docker", "compose", "exec", "-T", self.docker_service, tool]
            if self.user:
                base += ["-U", self.user]
            return base + list(args)
        return [tool, *args]

    def run(self, tool: str, *args: str, stdin=None, stdout=None, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603 - fixed tool names, no shell
            self.cmd(tool, *args),
            env=self.env(),
            stdin=stdin,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def sql(self, query: str, dbname: str | None = None) -> str:
        args = ["-At", "-v", "ON_ERROR_STOP=1", "-c", query]
        db = dbname or self.dbname
        if db:
            args = ["-d", db, *args]
        return self.run("psql", *args).stdout.decode().strip()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _lines(t: Target, query: str, dbname: str | None, session: SnapshotSession | None) -> list[str]:
    if session is not None:
        return session.query(query)
    return t.sql(query, dbname).splitlines()


def table_counts(t: Target, dbname: str | None = None, session: SnapshotSession | None = None) -> dict[str, int]:
    tables = _lines(t, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename", dbname, session)
    if not tables:
        return {}
    unsafe = [n for n in tables if not SAFE_IDENT.match(n)]
    if unsafe:
        raise RuntimeError(f"unexpected table names: {unsafe!r}")
    # Identifiers come from pg_tables and were validated above.
    query = " UNION ALL ".join(f"SELECT '{n}', count(*) FROM public.\"{n}\"" for n in tables)  # noqa: S608
    out = {}
    for line in _lines(t, query, dbname, session):
        name, n = line.split("|")
        out[name] = int(n)
    return out


def alembic_revision(t: Target, dbname: str | None = None, session: SnapshotSession | None = None) -> str | None:
    try:
        rows = _lines(t, "SELECT version_num FROM alembic_version", dbname, session)
        return rows[0] if rows else None
    except (subprocess.CalledProcessError, RuntimeError):
        return None


class SnapshotSession:
    """A psql session holding one REPEATABLE READ transaction whose snapshot is exported, so ``pg_dump
    --snapshot`` and the manifest row counts see exactly the same data even while the app keeps writing."""

    def __init__(self, t: Target) -> None:
        args = ["-At", "-q", "-X", "-v", "ON_ERROR_STOP=1"]
        if t.dbname:
            args += ["-d", t.dbname]
        self._p = subprocess.Popen(  # noqa: S603 - fixed tool name, no shell
            t.cmd("psql", *args),
            env=t.env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        self.snapshot_id = self.query("SELECT pg_export_snapshot()")[0]

    def query(self, sql: str) -> list[str]:
        marker = f"__permutados_end_{uuid.uuid4().hex}__"
        assert self._p.stdin and self._p.stdout
        self._p.stdin.write(sql.rstrip().rstrip(";") + ";\n\\echo " + marker + "\n")
        self._p.stdin.flush()
        lines = []
        while True:
            line = self._p.stdout.readline()
            if line == "":
                err = self._p.stderr.read() if self._p.stderr else ""
                raise RuntimeError(f"psql session ended unexpectedly: {err[-500:]}")
            line = line.rstrip("\n")
            if line == marker:
                return lines
            lines.append(line)

    def close(self) -> None:
        try:
            if self._p.poll() is None:
                self.query("COMMIT")
                assert self._p.stdin
                self._p.stdin.close()
                self._p.wait(10)
        finally:
            if self._p.poll() is None:
                self._p.kill()
