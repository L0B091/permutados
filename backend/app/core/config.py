"""Application settings (loaded from environment / .env).

Secrets are typed as ``SecretStr`` so they never appear in ``repr()`` or logs.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- runtime -----------------------------------------------------------
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # --- infrastructure ----------------------------------------------------
    database_url: str = "postgresql+psycopg://permutados:permutados_dev@localhost:5432/permutados"
    redis_url: str = "redis://localhost:6379/0"

    # --- Dolphin (OpenAI-compatible server: vLLM / Ollama / llama.cpp) -----
    dolphin_base_url: str = ""
    dolphin_model: str = "dphn/Dolphin-Mistral-24B-Venice-Edition"
    dolphin_api_key: SecretStr = SecretStr("")
    dolphin_timeout_seconds: float = 60.0
    dolphin_max_tool_iterations: int = 4
    dolphin_assets_path: str = ""  # folder with prompts/ policies/ tools/ (defaults to <repo>/dolphin)

    # --- WhatsApp Cloud API ------------------------------------------------
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_api_version: str = "v23.0"
    # Outside the free 24h customer-service window only approved templates may be sent. The fallback template
    # takes ONE body parameter (a short, PII-free summary) and invites the user to reply, which reopens the
    # window; the full queued messages are then delivered.
    whatsapp_fallback_template: str = "permutados_novedad"
    whatsapp_template_language: str = "es_AR"
    whatsapp_session_window_hours: int = 24
    whatsapp_window_safety_minutes: int = 15
    whatsapp_max_retries: int = 3

    # --- Mercado Pago ------------------------------------------------------
    mercadopago_access_token: SecretStr = SecretStr("")
    mercadopago_public_key: str = ""
    mercadopago_webhook_secret: SecretStr = SecretStr("")
    mercadopago_environment: Literal["sandbox", "production"] = "sandbox"
    mercadopago_signature_tolerance_seconds: int = 600
    mercadopago_back_url: str = ""  # optional page shown after paying (auto_return only when set)
    mercadopago_statement_descriptor: str = "PERMUTADOS"
    mercadopago_collector_id: str = ""  # optional: when set, payments credited to another account are rejected
    mercadopago_max_retries: int = 3

    # --- URLs --------------------------------------------------------------
    app_base_url: str = "http://localhost:8000"
    webhook_base_url: str = "http://localhost:8000"
    admin_api_url: str = "http://localhost:8000/admin"

    # --- admin auth --------------------------------------------------------
    admin_auth_secret: SecretStr = SecretStr("")
    admin_access_token_minutes: int = 15
    admin_refresh_token_days: int = 7
    admin_max_failed_logins: int = 5
    admin_lockout_minutes: int = 15
    admin_login_rate_limit_per_minute: int = 10

    # --- storage / crypto --------------------------------------------------
    storage_path: str = "./storage"
    # Field-level encryption (AES-256-GCM). urlsafe-base64 of 32 random bytes. Old keys (comma separated) are
    # only used to decrypt during a rotation (see scripts/rotate_encryption_key.py).
    encryption_key: SecretStr = SecretStr("")
    encryption_old_keys: SecretStr = SecretStr("")
    cors_allowed_origins: str = ""

    # --- business rules (defaults; live values are in system_config) -------
    contact_release_price: Decimal = Decimal("3000")
    contact_release_currency: str = "ARS"
    offer_expiration_hours: int = 24
    payment_window_hours: int = 24
    publication_expiration_days: int = 30
    publication_expiry_warning_days: int = 3
    # An offer pauses the item for up to 24h, so cap how many items one bidder can pause at once.
    max_active_offers_per_bidder: int = 3

    # --- images ------------------------------------------------------------
    max_image_bytes: int = 10 * 1024 * 1024
    min_image_bytes: int = 2 * 1024
    min_image_side_px: int = 320
    max_image_side_px: int = 8000
    max_images_per_item: int = 1  # MVP: exactly one. Schema supports more.

    # --- channel (manual administration; see README) -----------------------
    channel_enabled: bool = False
    channel_run_interval: int = 3600
    channel_daily_limit: int = 10
    channel_min_score: int = 40
    channel_max_exposure_per_item: int = 3
    channel_min_hours_between_same_item: int = 48

    # --- misc --------------------------------------------------------------
    rate_limit_user_messages_per_minute: int = 30

    # --- worker: inbound queue + outbox --------------------------------------
    inbound_poll_seconds: int = 2
    inbound_batch_size: int = 20
    inbound_max_attempts: int = 5
    outbox_poll_seconds: int = 2
    outbox_batch_size: int = 50
    outbox_max_attempts: int = 8
    # A notification left in SENDING (worker crashed mid-send) with no status webhook after this many seconds is
    # considered never accepted by Meta and is retried (templates are never resent).
    outbox_sending_grace_seconds: int = 900

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        # Accept plain postgres URLs and force the psycopg3 driver.
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://") :]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://") :]
        return v

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def secret_values(self) -> list[str]:
        """All configured secret values (used by the log redactor and tests)."""
        out: list[str] = []
        for _name, value in self:
            if isinstance(value, SecretStr) and value.get_secret_value():
                out.append(value.get_secret_value())
        # DB password inside the URL is also a secret.
        from sqlalchemy.engine import make_url

        try:
            pw = make_url(self.database_url).password
            if pw:
                out.append(pw)
        except Exception:  # noqa: S110  # pragma: no cover - malformed URL
            pass
        return out

    def validate_for_production(self) -> list[str]:
        """Return a list of problems that must be fixed before running in production."""
        problems: list[str] = []
        if len(self.admin_auth_secret.get_secret_value()) < 32:
            problems.append("ADMIN_AUTH_SECRET must be at least 32 characters")
        if not self.whatsapp_app_secret.get_secret_value():
            problems.append("WHATSAPP_APP_SECRET is required to verify webhooks")
        if not self.mercadopago_webhook_secret.get_secret_value():
            problems.append("MERCADOPAGO_WEBHOOK_SECRET is required to verify webhooks")
        if "*" in self.cors_origins:
            problems.append("CORS_ALLOWED_ORIGINS must not contain '*'")
        from app.core import crypto

        try:
            crypto.parse_key(self.encryption_key.get_secret_value())
        except ValueError as e:
            problems.append(f"ENCRYPTION_KEY: {e}")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
