"""Settings from .env in the current folder. Every mistake is reported at once."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Wrong or missing settings; the message lists every problem."""


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    secret_key: str = field(repr=False)
    operators_chat_id: int | None = None
    host: str = "127.0.0.1"
    port: int = 8096
    panel_url: str = "http://127.0.0.1:8096"
    cookie_secure: bool = False
    database_path: Path = Path("data/support.db")
    company_name: str = "Assistenza clienti"
    sla_first_reply_minutes: int = 30
    auto_close_hours: int = 48
    max_message_chars: int = 3500


def _int(name: str, default: int, errors: list[str], minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name}: нужно целое число, получено {raw!r}")
        return default
    if value < minimum:
        errors.append(f"{name}: не меньше {minimum}, получено {value}")
    return value


def load_settings(env_file: Path | None = Path(".env"), *, need_bot: bool = True) -> Settings:
    """Read settings. ``need_bot=False`` for the panel alone (no Telegram token needed)."""
    if env_file is not None:
        load_dotenv(env_file)
    errors: list[str] = []

    token = os.getenv("BOT_TOKEN", "").strip()
    if need_bot and not token:
        errors.append("BOT_TOKEN: не задан (токен бота от @BotFather)")
    secret = os.getenv("SECRET_KEY", "").strip()
    if len(secret) < 32:
        errors.append("SECRET_KEY: нужна случайная строка не короче 32 символов "
                      "(python3 -c \"import secrets; print(secrets.token_hex(32))\")")

    chat_raw = os.getenv("OPERATORS_CHAT_ID", "").strip()
    chat_id = None
    if chat_raw:
        try:
            chat_id = int(chat_raw)
        except ValueError:
            errors.append(f"OPERATORS_CHAT_ID: нужен числовой id чата, получено {chat_raw!r}")

    settings = Settings(
        bot_token=token,
        secret_key=secret,
        operators_chat_id=chat_id,
        host=os.getenv("HOST", "").strip() or "127.0.0.1",
        port=_int("PORT", 8096, errors),
        panel_url=(os.getenv("PANEL_URL", "").strip() or "http://127.0.0.1:8096").rstrip("/"),
        cookie_secure=os.getenv("COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"},
        database_path=Path(os.getenv("DATABASE_PATH", "").strip() or "data/support.db"),
        company_name=os.getenv("COMPANY_NAME", "").strip() or "Assistenza clienti",
        sla_first_reply_minutes=_int("SLA_FIRST_REPLY_MINUTES", 30, errors),
        auto_close_hours=_int("AUTO_CLOSE_HOURS", 48, errors),
        max_message_chars=_int("MAX_MESSAGE_CHARS", 3500, errors, minimum=100),
    )
    if settings.max_message_chars > 4096:
        errors.append("MAX_MESSAGE_CHARS: Telegram принимает не больше 4096 символов")
    if errors:
        raise ConfigError("Ошибки в .env:\n  - " + "\n  - ".join(errors))
    return settings
