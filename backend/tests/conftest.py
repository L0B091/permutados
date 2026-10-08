"""Test setup: real PostgreSQL (migrated with Alembic) and real Redis (db 15).

Defaults target the local services; override with TEST_DATABASE_URL / TEST_REDIS_URL.
"""

from __future__ import annotations

import io
import os
import random
import tempfile

import pytest

TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://permutados:permutados_dev@localhost:5432/permutados_test"
)
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": TEST_DB,
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"),
        "ADMIN_AUTH_SECRET": "test-admin-secret-0123456789-abcdefghijklmnop",
        "WHATSAPP_APP_SECRET": "test-wa-app-secret",
        "WHATSAPP_VERIFY_TOKEN": "test-verify-token",
        "WHATSAPP_ACCESS_TOKEN": "fake-wa-access-token-for-tests-only",
        "MERCADOPAGO_WEBHOOK_SECRET": "test-mp-webhook-secret",
        "MERCADOPAGO_ACCESS_TOKEN": "fake-mp-access-token-for-tests-only",
        "DOLPHIN_API_KEY": "dolphin-test-key-secret",
        # Fixed, test-only key (urlsafe base64 of 32 bytes). Never used outside the test suite.
        "ENCRYPTION_KEY": "dGVzdC1vbmx5LWVuY3J5cHRpb24ta2V5LTMyYnl0ZXM",
        "STORAGE_PATH": tempfile.mkdtemp(prefix="permutados-test-"),
        "LOG_LEVEL": "WARNING",
        "CONTACT_RELEASE_PRICE": "3000",
        "CONTACT_RELEASE_CURRENCY": "ARS",
    }
)

from PIL import Image  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.core import db as dbmod  # noqa: E402
from app.core.ratelimit import get_rate_limiter  # noqa: E402

KEEP_TABLES = {"zones", "alembic_version"}


def make_alembic_config():
    from alembic.config import Config

    here = os.path.dirname(os.path.dirname(__file__))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    cfg.attributes["database_url"] = TEST_DB
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    """Fresh schema via Alembic (downgrade to base, then upgrade head) — also verifies the migration."""
    from alembic import command

    cfg = make_alembic_config()
    engine = dbmod.get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    dbmod.reset_engine()


@pytest.fixture(autouse=True)
def clean_db(migrated_db):
    engine = dbmod.get_engine()
    with engine.begin() as conn:
        tables = [r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))]
        victims = [t for t in tables if t not in KEEP_TABLES]
        conn.execute(text("TRUNCATE " + ", ".join(victims) + " RESTART IDENTITY CASCADE"))
    get_rate_limiter().reset()
    from app.services.system import config_service as cfg

    with dbmod.session_scope() as s:
        cfg.ensure_defaults(s)
    yield


@pytest.fixture
def db():
    session = dbmod.get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---- factories --------------------------------------------------------------------------------
def make_image(fmt: str = "JPEG", size: tuple[int, int] = (800, 600), seed: int = 1, noise: bool = True) -> bytes:
    rnd = random.Random(seed)
    if noise:
        img = Image.effect_noise(size, 64).convert("RGB")
        # add some structure (edges) so the quality heuristic sees a real photo-like image
        px = img.load()
        for _ in range(40):
            x0, y0 = rnd.randrange(size[0] - 50), rnd.randrange(size[1] - 50)
            color = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            for x in range(x0, x0 + 50):
                for y in range(y0, y0 + 50):
                    px[x, y] = color
    else:
        img = Image.new("RGB", size, (128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=90) if fmt == "JPEG" else img.save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def image_bytes() -> bytes:
    return make_image()


_wa_counter = iter(range(5491100000000, 5491199999999))


@pytest.fixture
def make_user(db):
    from app.services.users.service import get_or_create_by_whatsapp

    def _make(name: str | None = None):
        wa = str(next(_wa_counter))
        u = get_or_create_by_whatsapp(db, wa, name)
        db.commit()
        u.test_wa_id = wa  # plaintext kept only on the test object (the DB stores it encrypted)
        return u

    return _make


@pytest.fixture
def publish_item(db):
    """Create a PUBLISHED item through the real service flow."""
    from app.services.publications import service as pubs

    counter = iter(range(1, 10_000))

    def _publish(
        owner,
        title: str = "Bicicleta rodado 29",
        price="150000",
        zone: str = "Palermo",
        accepts_swap: bool = True,
        category: str = "bicicletas",
        description: str | None = None,
    ):
        item = pubs.start_draft(db, owner, [make_image(seed=next(counter))])
        pubs.set_ai_identification(db, owner, item.id, {"title": title, "category": category})
        pubs.fill_draft(
            db,
            owner,
            item.id,
            identification_confirmed=True,
            zone=zone,
            base_price=price,
            accepts_swap=accepts_swap,
            has_damage=False,
            works=True,
            description=description,
        )
        pubs.publish(db, owner, item.id)
        db.commit()
        return item

    return _publish


@pytest.fixture
def gateway():
    from app.services.mercadopago.fake import FakeMercadoPagoGateway

    return FakeMercadoPagoGateway()


@pytest.fixture
def client(gateway):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(payment_gateway=gateway)) as c:
        yield c


@pytest.fixture
def notifications(db):
    from sqlalchemy import select

    from app.models.conversation import Notification

    def _get(user_id: int | None = None, kind: str | None = None):
        db.expire_all()
        stmt = select(Notification).order_by(Notification.id)
        if user_id is not None:
            stmt = stmt.where(Notification.user_id == user_id)
        if kind is not None:
            stmt = stmt.where(Notification.kind == kind)
        return list(db.execute(stmt).scalars())

    return _get


@pytest.fixture
def wa_client():
    from app.services.whatsapp.client import RecordingWhatsAppClient

    return RecordingWhatsAppClient()


@pytest.fixture
def wa_post(client):
    """POST a correctly signed WhatsApp webhook payload to the real endpoint."""
    import json as _json

    from app.services.whatsapp.signature import compute_signature

    def _post(payload: dict):
        body = _json.dumps(payload).encode()
        r = client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={
                "x-hub-signature-256": compute_signature("test-wa-app-secret", body),
                "content-type": "application/json",
            },
        )
        assert r.status_code == 200, r.text
        return r.json()

    return _post
