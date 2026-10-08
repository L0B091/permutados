"""Backups (section 45): real pg_dump / pg_restore against the local PostgreSQL, restore into a new database,
verify checksums, row counts, alembic revision and that encrypted PII is still readable with the key."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.item import Item
from app.models.user import User
from app.services.users.pii import reveal_whatsapp_for_delivery
from tests.conftest import TEST_DB

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
RESTORE_DB = "permutados_restore_test"
RESTORE_URL = TEST_DB.rsplit("/", 1)[0] + "/" + RESTORE_DB

pytestmark = pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")


def _admin_exec(sql: str) -> None:
    eng = create_engine(TEST_DB.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            c.execute(text(sql))
    finally:
        eng.dispose()


@pytest.fixture
def restore_db():
    _admin_exec(f"DROP DATABASE IF EXISTS {RESTORE_DB} WITH (FORCE)")
    yield RESTORE_URL
    _admin_exec(f"DROP DATABASE IF EXISTS {RESTORE_DB} WITH (FORCE)")


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BACKUP_")}
    env.update(extra)
    return env


def _run(script: str, *args: str, env=None) -> tuple[int, dict]:
    p = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True, env=env or _env(), timeout=120
    )
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    assert lines, p.stdout + p.stderr
    return p.returncode, json.loads(lines[-1])


def _backup(directory, *extra, env=None):
    return _run("backup_postgres.py", "--database-url", TEST_DB, "--dir", str(directory), *extra, env=env)


@pytest.fixture
def seeded(db, make_user, publish_item):
    owner, other = make_user("Owner"), make_user("Other")
    items = [publish_item(owner, title=f"Bicicleta {n}") for n in range(3)]
    db.commit()
    return owner, other, items


def test_backup_restore_roundtrip_with_encrypted_pii(seeded, tmp_path, restore_db, db):
    owner, other, items = seeded
    code, res = _backup(tmp_path / "bk")
    assert code == 0 and res["ok"], res
    dump = Path(res["file"])
    assert dump.exists() and oct(dump.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "bk").stat().st_mode & 0o777) == "0o700"
    manifest = json.loads(dump.with_suffix(".json").read_text())
    assert manifest["alembic_revision"] == res["alembic_revision"] == "0003"
    assert manifest["row_counts"]["users"] == 2 and manifest["row_counts"]["items"] == 3
    assert "ENCRYPTION_KEY is NOT included" in manifest["encryption_note"]
    assert dump.with_name(dump.name + ".sha256").read_text().split()[0] == res["sha256"]
    raw = dump.read_bytes()
    assert owner.test_wa_id.encode() not in raw  # WhatsApp numbers are encrypted at rest, also in backups

    code, out = _run("restore_postgres.py", str(dump), "--database-url", restore_db, "--create")
    assert code == 0 and out["ok"] and out["row_counts_match"] and out["revision_match"], out

    eng = create_engine(restore_db)
    try:
        with Session(eng) as s:
            assert s.execute(select(func.count()).select_from(Item)).scalar_one() == 3
            u = s.execute(select(User).where(User.id == owner.id)).scalar_one()
            assert reveal_whatsapp_for_delivery(u) == owner.test_wa_id  # same ENCRYPTION_KEY -> readable
            assert s.execute(select(func.count()).select_from(AuditLog)).scalar_one() > 0
            # The append-only trigger on audit_logs survived the restore.
            with pytest.raises(Exception):
                s.execute(text("DELETE FROM audit_logs"))
                s.flush()
            s.rollback()
    finally:
        eng.dispose()

    # A non-empty target is refused unless --force; --force replaces it.
    code, out = _run("restore_postgres.py", str(dump), "--database-url", restore_db)
    assert code == 1 and "use --force" in out["error"]
    code, out = _run("restore_postgres.py", str(dump), "--database-url", restore_db, "--force")
    assert code == 0 and out["row_counts_match"], out


def test_backup_is_consistent_while_the_app_keeps_writing(seeded, tmp_path, restore_db):
    stop = threading.Event()
    eng = create_engine(TEST_DB)

    def writer():
        n = 0
        while not stop.is_set():
            with eng.begin() as c:
                c.execute(
                    text(
                        "INSERT INTO domain_events (event_type, aggregate_type, aggregate_id, payload, failed_handlers,"
                        " attempts) VALUES ('ITEM_UPDATED', 'item', :i, '{}', '[]', 1)"
                    ),
                    {"i": str(n)},
                )
            n += 1
            time.sleep(0.002)

    t = threading.Thread(target=writer)
    t.start()
    try:
        time.sleep(0.2)
        code, res = _backup(tmp_path / "bk")
    finally:
        stop.set()
        t.join()
        eng.dispose()
    assert code == 0, res
    code, out = _run("restore_postgres.py", res["file"], "--database-url", restore_db, "--create")
    # Dump and manifest come from the same exported snapshot, so counts match exactly despite the writes.
    assert code == 0 and out["row_counts_match"], out


def test_corrupt_or_truncated_backups_are_rejected(seeded, tmp_path, restore_db):
    code, res = _backup(tmp_path / "bk")
    dump = Path(res["file"])
    code, out = _run("restore_postgres.py", str(dump), "--database-url", restore_db, "--verify-only")
    assert code == 0 and out["checksum"] == "ok" and out["archive_entries"] > 10

    bad = tmp_path / "bad" / dump.name
    bad.parent.mkdir()
    shutil.copy(dump, bad)
    shutil.copy(str(dump) + ".sha256", str(bad) + ".sha256")
    data = bytearray(bad.read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad.write_bytes(bytes(data))
    code, out = _run("restore_postgres.py", str(bad), "--database-url", restore_db, "--create")
    assert code == 1 and "checksum mismatch" in out["error"]

    trunc = tmp_path / "trunc.dump"
    trunc.write_bytes(dump.read_bytes()[:2000])
    code, out = _run("restore_postgres.py", str(trunc), "--database-url", restore_db, "--create")
    assert code == 1 and out["ok"] is False
    # Nothing was created by the failed attempts.
    eng = create_engine(TEST_DB.rsplit("/", 1)[0] + "/postgres")
    try:
        with eng.connect() as c:
            assert c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": RESTORE_DB}).first() is None
    finally:
        eng.dispose()


def test_rotation_keeps_minimum_and_offsite_copy(seeded, tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir(mode=0o700)
    now = time.time()
    for name, age_days in (
        ("20200101T000000Z", 30),
        ("20200102T000000Z", 20),
        ("20200103T000000Z", 10),
        ("20200104T000000Z", 1),
    ):
        base = bk / f"permutados_{name}"
        for p in (Path(f"{base}.dump"), Path(f"{base}.dump.sha256"), Path(f"{base}.json")):
            p.write_text("x")
            os.utime(p, (now - age_days * 86400, now - age_days * 86400))
    stale_partial = bk / "permutados_20200105T000000Z.dump.partial"
    stale_partial.write_text("x")
    os.utime(stale_partial, (now - 2 * 86400, now - 2 * 86400))
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    code, res = _backup(bk, "--retention-days", "14", "--keep-min", "2", "--copy-cmd", f"cp {{file}} {offsite}/")
    assert code == 0, res
    assert sorted(res["rotated_out"]) == ["permutados_20200101T000000Z.dump", "permutados_20200102T000000Z.dump"]
    left = sorted(p.name for p in bk.glob("*.dump"))
    assert left == ["permutados_20200103T000000Z.dump", "permutados_20200104T000000Z.dump", Path(res["file"]).name]
    assert not (bk / "permutados_20200101T000000Z.json").exists()
    assert not stale_partial.exists()
    assert res["offsite_copy"] is True and (offsite / Path(res["file"]).name).exists()

    # keep-min wins over retention: with everything "old", the newest N stay.
    code, res = _backup(bk, "--retention-days", "0", "--keep-min", "3")
    assert code == 0 and len(list(bk.glob("*.dump"))) == 3


def test_failed_backup_leaves_no_partial_and_reports_error(tmp_path):
    bad_url = TEST_DB.rsplit("/", 1)[0] + "/does_not_exist_db"
    code, res = _run("backup_postgres.py", "--database-url", bad_url, "--dir", str(tmp_path / "bk"))
    assert code != 0 and res["ok"] is False
    assert "permutados_dev" not in json.dumps(res)  # the password never appears in the output
    assert list((tmp_path / "bk").glob("*")) == []


def test_cron_wrapper_native_mode(seeded, tmp_path):
    p = subprocess.run(
        ["bash", str(SCRIPTS / "backup_postgres.sh")],
        capture_output=True,
        text=True,
        timeout=120,
        env=_env(BACKUP_MODE="native", DATABASE_URL=TEST_DB, BACKUP_DIR=str(tmp_path / "cron")),
    )
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout.strip().splitlines()[-1])["ok"] is True
    assert len(list((tmp_path / "cron").glob("*.dump"))) == 1


def test_migration_0003_down_and_up_with_real_data(db, make_user, publish_item, gateway, tmp_path, restore_db):
    """Restore a populated head database, downgrade to 0002 and upgrade again: data survives and the phase-3
    columns are backfilled (channel_status from channel_posts, checkout expiry from the payment deadline)."""
    from alembic import command

    from app.core.clock import utcnow
    from app.models.conversation import Notification
    from app.models.enums import AdminRole, NotificationStatus
    from app.services.admin.auth import create_admin
    from app.services.channel import service as channel
    from app.services.notifications.service import enqueue
    from app.services.offers import service as offers
    from app.services.payments import service as payments
    from tests.conftest import make_alembic_config

    admin = create_admin(db, "root", "root-password-123456", AdminRole.ADMIN)
    owner, bidder = make_user(), make_user()
    visible = publish_item(owner, title="Mesa")
    reserved = publish_item(owner, title="Silla")
    channel.register_manual_post(db, admin.id, visible.public_id, external_ref=None)
    o = offers.create_offer(db, bidder, reserved.public_id, offer_type="MONEY", offered_price="1").offer
    tx = offers.accept_offer(db, owner, o.public_id).transaction
    payments.create_payment(db, bidder, tx.public_id, gateway)
    n = enqueue(db, user_id=owner.id, kind="REPLY", body="x", idempotency_key="mig")
    db.flush()
    n.status, n.sending_started_at = NotificationStatus.SENDING, utcnow()
    db.commit()

    code, res = _backup(tmp_path / "bk")
    assert code == 0, res
    code, out = _run("restore_postgres.py", res["file"], "--database-url", restore_db, "--create")
    assert code == 0 and out["ok"], out

    cfg = make_alembic_config()
    cfg.attributes["database_url"] = restore_db
    eng = create_engine(restore_db)
    try:
        command.downgrade(cfg, "0002")
        with eng.connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002"
            assert c.execute(text("SELECT status::text FROM notifications WHERE idempotency_key='mig'")).scalar() == (
                "PENDING"
            )
            assert c.execute(text("SELECT count(*) FROM items")).scalar() == 2
        command.upgrade(cfg, "head")
        with Session(eng) as s:
            rows = dict(s.execute(text("SELECT public_id, channel_status::text FROM items")).all())
            assert rows == {visible.public_id: "VISIBLE", reserved.public_id: "NOT_POSTED"}
            exp, due = s.execute(
                text(
                    "SELECT p.checkout_expires_at, t.payment_due_at FROM payments p "
                    "JOIN transactions t ON t.id = p.transaction_id"
                )
            ).one()
            assert exp == due
            assert s.execute(text("SELECT count(*) FROM domain_events WHERE processed_at IS NULL")).scalar() == 0
            assert s.get(Notification, n.id).status == NotificationStatus.PENDING
    finally:
        eng.dispose()
