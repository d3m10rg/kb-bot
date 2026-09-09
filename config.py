from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    forbidden_sticker_packs: frozenset[str]
    mute_minutes: int
    captcha_timeout_seconds: int
    warning_texts: tuple[str, str, str]
    database_path: str

    @classmethod
    def from_env(cls, env_file: str = ".env") -> "Settings":
        load_dotenv(env_file)

        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            raise ValueError("BOT_TOKEN не задан в .env")

        mute_minutes = _positive_int("MUTE_MINUTES", 10)
        captcha_timeout = _positive_int("CAPTCHA_TIMEOUT_SECONDS", 60)
        packs = frozenset(
            item.strip().casefold()
            for item in os.getenv("FORBIDDEN_STICKER_PACKS", "").split(",")
            if item.strip()
        )
        warning_texts = (
            _warning_text(1),
            _warning_text(2),
            _warning_text(3),
        )

        return cls(
            bot_token=bot_token,
            forbidden_sticker_packs=packs,
            mute_minutes=mute_minutes,
            captcha_timeout_seconds=captcha_timeout,
            warning_texts=warning_texts,
            database_path=os.getenv("DATABASE_PATH", "bot.sqlite3").strip()
            or "bot.sqlite3",
        )


def _positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} должен быть целым числом") from error
    if value <= 0:
        raise ValueError(f"{name} должен быть больше нуля")
    return value


def _warning_text(attempt: int) -> str:
    return os.getenv(
        f"WARNING_TEXT_{attempt}",
        f"ПЛЕЙСХОЛДЕР: предупреждение {attempt} из 3.",
    )
