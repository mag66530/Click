"""
tg_social.py – Телеграм: публикация в каналы через Bot API.

У ботов отложки нет – этот модуль шлёт «сейчас», а «в 11:00» обеспечивает
планировщик Click (позже – опция родной отложки через сессию, Q-9).
Зато бот честно поддерживает форматирование: жирное и ссылки уезжают
как HTML (post_text.render(..., "html")).

Правило укладки – лимиты Телеграма, зашитые в plan_delivery():
    подпись к фото ≤ 1024 видимых знаков, текст ≤ 4096, альбом 2–10 фото.
Ключи: tg_bot_token – в «Настройках»; канал бренда – в projects-config.
Один бот на все каналы, бот – админ каждого канала с правом публикации.
"""

from __future__ import annotations

from typing import Callable

import post_text
import secrets_local

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

API = "https://api.telegram.org"
TIMEOUT = 60
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
ALBUM_MAX = 10


def bot_token() -> str:
    return secrets_local.get("tg_bot_token")


def is_configured() -> bool:
    return bool(bot_token())


# ─── Правило укладки (чистая логика, накрыта тестами) ───────────────
def plan_delivery(visible_len: int, n_images: int) -> str:
    """
    Как везти пост:
      'text'          – фото нет: одним сообщением (≤4096; длиннее – частями)
      'photo+caption' – 1 фото, текст влезает подписью
      'album+caption' – 2..10 фото, текст влезает подписью первого
      'media+text'    – фото есть, текст длиннее подписи: альбом, следом текст
    """
    if n_images <= 0:
        return "text"
    if visible_len <= CAPTION_LIMIT:
        return "photo+caption" if n_images == 1 else "album+caption"
    return "media+text"


def split_text(html: str, limit: int = TEXT_LIMIT) -> list[str]:
    """
    Длинный текст → части по границам абзацев (не резать слово и не рвать
    HTML-тег посередине – режем только по \n). Посты реестра сюда почти
    не попадают, но «почти» – не гарантия.
    """
    if len(html) <= limit:
        return [html]
    parts: list[str] = []
    rest = html
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip("\n"))
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    return parts


# ─── Отправка ───────────────────────────────────────────────────────
def _api(method: str, data: dict, files: dict | None = None) -> dict:
    import requests

    r = requests.post(f"{API}/bot{bot_token()}/{method}", data=data, files=files,
                      timeout=TIMEOUT)
    body = r.json() if r.content else {}
    if not body.get("ok"):
        raise RuntimeError(f"Телеграм отказал в {method}: "
                           f"{body.get('description') or f'HTTP {r.status_code}'}")
    return body.get("result") or {}


def _message_link(chat_id: str, result: dict) -> str:
    """Ссылка на пост в публичном канале (@имя). У приватных ссылки нет."""
    mid = (result or {}).get("message_id")
    if not mid or not str(chat_id).startswith("@"):
        return ""
    return f"https://t.me/{str(chat_id).lstrip('@')}/{mid}"


def publish(chat_id: str, markup: str, image_paths: list[str],
            log: Callable[[str], None] | None = None) -> dict:
    """
    Опубликовать пост в канал СЕЙЧАС. Возвращает {"ok": True, "link": …}
    либо {"ok": False, "error": "…"}. markup – внутренняя разметка Click,
    HTML собирается здесь.
    """
    log = log or (lambda m: None)
    if not is_configured():
        return {"ok": False, "error": "Не заполнен tg_bot_token в «Настройках»"}
    if not chat_id:
        return {"ok": False, "error": "Не указан канал Телеграма для бренда"}

    html = post_text.render(markup, "html")
    visible = len(post_text.strip_markup(markup))
    images = list(image_paths or [])[:ALBUM_MAX]
    mode = plan_delivery(visible, len(images))
    log(f"Телеграм: способ доставки – {mode}")

    try:
        first: dict = {}
        if mode == "text":
            for n, chunk in enumerate(split_text(html)):
                res = _api("sendMessage", {"chat_id": chat_id, "text": chunk,
                                           "parse_mode": "HTML",
                                           "disable_web_page_preview": "false"})
                first = first or res
        elif mode == "photo+caption":
            with open(images[0], "rb") as f:
                first = _api("sendPhoto", {"chat_id": chat_id, "caption": html,
                                           "parse_mode": "HTML"}, files={"photo": f})
        else:
            import json
            media = [{"type": "photo", "media": f"attach://p{n}"} for n in range(len(images))]
            if mode == "album+caption":
                media[0]["caption"] = html
                media[0]["parse_mode"] = "HTML"
            files = {f"p{n}": open(p, "rb") for n, p in enumerate(images)}
            try:
                res = _api("sendMediaGroup",
                           {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)},
                           files=files)
            finally:
                for f in files.values():
                    f.close()
            first = (res or [{}])[0] if isinstance(res, list) else res
            if mode == "media+text":
                for chunk in split_text(html):
                    _api("sendMessage", {"chat_id": chat_id, "text": chunk,
                                         "parse_mode": "HTML"})
        return {"ok": True, "link": _message_link(chat_id, first)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def send_alert(text: str) -> bool:
    """
    Тревога в личный чат заказчика (tg_alert_chat_id): сессия умерла, пост
    пропущен. Тихо возвращает False, если не настроено, – тревоги не должны
    ронять то, о чём тревожатся.
    """
    chat = secrets_local.get("tg_alert_chat_id")
    if not (chat and is_configured()):
        return False
    try:
        _api("sendMessage", {"chat_id": chat, "text": text})
        return True
    except Exception:  # noqa: BLE001
        return False


def check_access(chat_id: str = "") -> str:
    """Пусто – всё хорошо, иначе причина словами."""
    if not is_configured():
        return "не заполнен tg_bot_token"
    try:
        _api("getMe", {})
        if chat_id:
            _api("getChat", {"chat_id": chat_id})
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)


# Что отвечает Телеграм, когда бот не готов публиковать, – и что с этим
# делать человеку. Английские фразы приходят от самого Телеграма.
_ACCESS_ADVICE = (
    ("chat not found",
     "канал не найден. Проверьте имя: оно пишется через @ и должно совпадать "
     "с адресом канала (t.me/stalmetural → @stalmetural). Если канал закрытый, "
     "по @-имени бот его не увидит"),
    ("bot is not a member",
     "бот не состоит в канале. Добавьте его в администраторы канала"),
    ("not enough rights",
     "бот в канале есть, но публиковать ему нельзя. В правах администратора "
     "включите «Отправка сообщений» (Post Messages)"),
    ("bot was kicked",
     "бота удалили из канала. Добавьте заново администратором"),
    ("unauthorized",
     "токен бота не подходит. Возьмите его заново у @BotFather"),
)


def access_advice(chat_id: str = "") -> str:
    """
    То же, что check_access, но человеческими словами.

    Телеграм отвечает по-английски и коротко («chat not found»), а сделать
    по этому нужно вполне определённое действие. Без перевода это выясняется
    в час выхода поста – то есть слишком поздно.
    """
    why = check_access(chat_id)
    if not why:
        return ""
    low = why.lower()
    for mark, advice in _ACCESS_ADVICE:
        if mark in low:
            return advice
    return why
