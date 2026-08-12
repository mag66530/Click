"""
max_social.py – МАКС: публикация в каналы через Bot API.

У бота МАКС отложки нет (родная отложка приложения ботам недоступна –
проверено по документации POST /messages), поэтому модуль шлёт «сейчас»,
а время обеспечивает планировщик Click. Форматирование бот принимает:
format=html – жирное и ссылки доезжают.

Площадка молодая, API дорабатывается: базовый адрес и форма вложений
вынесены в константы, при расхождении с dev.max.ru чинить здесь.
Ключи: max_bot_token – в «Настройках»; канал бренда – в projects-config.
Бот – администратор канала.
"""

from __future__ import annotations

from typing import Callable

import post_text
import secrets_local

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

BASE = "https://platform-api.max.ru"     # сверить с dev.max.ru при первом живом прогоне
TIMEOUT = 60
TEXT_LIMIT = 4000                        # лимит текста сообщения по документации


def bot_token() -> str:
    return secrets_local.get("max_bot_token")


def is_configured() -> bool:
    return bool(bot_token())


# ─── Сборка тела сообщения (чистая логика, накрыта тестом) ──────────
def build_body(markup: str, photo_tokens: list[str]) -> dict:
    """Тело NewMessageBody: HTML-текст + вложения-фото."""
    body: dict = {"text": post_text.render(markup, "html"), "format": "html"}
    if photo_tokens:
        body["attachments"] = [{"type": "image", "payload": {"token": t}}
                               for t in photo_tokens]
    return body


# ─── Вызовы API ─────────────────────────────────────────────────────
def _upload_image(path: str) -> str:
    """Фото → токен вложения: спросить адрес загрузки, залить файл."""
    import requests

    r = requests.post(f"{BASE}/uploads", params={"access_token": bot_token(),
                                                 "type": "image"}, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"МАКС не дал адрес загрузки: HTTP {r.status_code}")
    url = (r.json() or {}).get("url")
    if not url:
        raise RuntimeError("МАКС не дал адрес загрузки (пустой url)")
    with open(path, "rb") as f:
        up = requests.post(url, files={"data": f}, timeout=120)
    if up.status_code != 200:
        raise RuntimeError(f"МАКС не принял файл: HTTP {up.status_code}")
    photos = (up.json() or {}).get("photos") or {}
    for v in photos.values():
        if v.get("token"):
            return v["token"]
    raise RuntimeError("МАКС принял файл, но токена вложения не вернул")


def publish(chat_id: str, markup: str, image_paths: list[str],
            log: Callable[[str], None] | None = None) -> dict:
    """
    Опубликовать пост в канал СЕЙЧАС. {"ok": True} либо {"ok": False,
    "error": …}. Ссылку на сообщение МАКС не отдаёт – в сводке будет
    просто «вышло».
    """
    import requests

    log = log or (lambda m: None)
    if not is_configured():
        return {"ok": False, "error": "Не заполнен max_bot_token в «Настройках»"}
    if not chat_id:
        return {"ok": False, "error": "Не указан канал МАКС для бренда"}
    if len(post_text.strip_markup(markup)) > TEXT_LIMIT:
        return {"ok": False, "error": f"Текст длиннее {TEXT_LIMIT} знаков – "
                                      "МАКС такой не примет, сократите пост"}
    try:
        tokens = []
        for p in (image_paths or []):
            log("МАКС: гружу фото")
            tokens.append(_upload_image(p))
        r = requests.post(f"{BASE}/messages",
                          params={"access_token": bot_token(), "chat_id": chat_id},
                          json=build_body(markup, tokens), timeout=TIMEOUT)
        if r.status_code != 200:
            detail = ""
            try:
                detail = (r.json() or {}).get("message") or ""
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"МАКС отказал: HTTP {r.status_code} {detail}".strip())
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def check_access(chat_id: str = "") -> str:
    """Пусто – всё хорошо, иначе причина словами."""
    import requests

    if not is_configured():
        return "не заполнен max_bot_token"
    try:
        r = requests.get(f"{BASE}/me", params={"access_token": bot_token()}, timeout=30)
        if r.status_code != 200:
            return f"МАКС не признал токен: HTTP {r.status_code}"
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)
