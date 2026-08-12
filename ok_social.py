"""
ok_social.py – Одноклассники: родная отложка через REST API.

Единственная соцсеть проекта, где остался нормальный API-путь: ВК закрыл
регистрацию приложений, а у ОК приложение заводится (с модерацией прав,
см. ПОСТАНОВКА-Кросспостинг.md, Приложение А.2). Пост кладётся методом
mediatopic.post с publishAt – дальше его держит и публикует сам ОК.

Ключи (все – из «Настроек», в коде ничего):
    ok_app_key             публичный ключ приложения (application_key)
    ok_app_secret          секретный ключ приложения
    ok_access_token        «вечный» токен, сгенерированный ПОСЛЕ выдачи прав
    ok_session_secret_key  выдаётся вместе с токеном; если пуст – считается
                           MD5(access_token + app_secret), это штатная схема ОК

Часовой пояс: publishAt ОК принимает ТОЛЬКО по Москве. Наружу этот модуль
берёт время Екатеринбурга (как весь Click) и конвертирует сам – человек
про Москву не думает. Ошибка здесь = пост уехал на 2 часа, поэтому
конвертация накрыта тестом.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import secrets_local

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

API = "https://api.ok.ru/fb.do"
TIMEOUT = 60
MSK = timezone(timedelta(hours=3), "Москва")   # круглый год, часы не переводят


# ─── Ключи ──────────────────────────────────────────────────────────
def creds() -> dict:
    return {
        "app_key": secrets_local.get("ok_app_key"),
        "app_secret": secrets_local.get("ok_app_secret"),
        "token": secrets_local.get("ok_access_token"),
        "session_secret": secrets_local.get("ok_session_secret_key"),
    }


def is_configured() -> bool:
    c = creds()
    return bool(c["app_key"] and c["app_secret"] and c["token"])


def session_secret(token: str, app_secret: str, explicit: str = "") -> str:
    """Секрет сессии: выданный вместе с токеном, иначе MD5(token+app_secret)."""
    if explicit:
        return explicit
    return hashlib.md5((token + app_secret).encode()).hexdigest().lower()


def sign(params: dict, session_secret_key: str) -> str:
    """
    Подпись запроса по схеме ОК: параметры сортируются по имени, склеиваются
    в «имя=значение» без разделителей, в хвост – секрет сессии, всё в MD5.
    access_token в подпись НЕ входит – это правило ОК, не наша прихоть.
    """
    line = "".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5((line + session_secret_key).encode("utf-8")).hexdigest().lower()


# ─── Время ──────────────────────────────────────────────────────────
def publish_at_msk(when_local: datetime) -> str:
    """Время Екатеринбурга (или любое со своим поясом) → строка ОК по Москве."""
    if when_local.tzinfo is None:
        raise ValueError("publish_at_msk ждёт время С ПОЯСОМ – иначе конвертация наугад")
    return when_local.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")


# ─── Вложение поста ─────────────────────────────────────────────────
def build_attachment(text: str, photo_tokens: list[str]) -> str:
    """JSON-вложение mediatopic: текст + фото. Порядок блоков = порядок в посте."""
    media: list[dict] = []
    if (text or "").strip():
        media.append({"type": "text", "text": text})
    if photo_tokens:
        media.append({"type": "photo", "list": [{"id": t} for t in photo_tokens]})
    return json.dumps({"media": media}, ensure_ascii=False)


# ─── Вызовы API ─────────────────────────────────────────────────────
def _call(method: str, extra: dict, files: dict | None = None) -> dict:
    """Один подписанный вызов. Ошибку ОК возвращаем словами, не кодом."""
    import requests

    c = creds()
    params = {"application_key": c["app_key"], "method": method,
              "format": "json", **extra}
    sig = sign(params, session_secret(c["token"], c["app_secret"], c["session_secret"]))
    params.update({"access_token": c["token"], "sig": sig})
    r = requests.post(API, data=params, files=files, timeout=TIMEOUT)
    data = r.json() if r.content else {}
    if isinstance(data, dict) and data.get("error_code") is not None:
        raise RuntimeError(f"ОК отказал в {method}: {data.get('error_msg')} "
                           f"(код {data.get('error_code')})")
    return data


def upload_photos(group_id: str, image_paths: list[str]) -> list[str]:
    """Фото в альбом группы → токены для вложения. Пути – уже скачанные файлы."""
    import requests

    if not image_paths:
        return []
    up = _call("photosV2.getUploadUrl", {"gid": group_id, "count": str(len(image_paths))})
    url = up.get("upload_url")
    if not url:
        raise RuntimeError("ОК не дал адрес загрузки фото (photosV2.getUploadUrl)")
    files = {f"pic{n}": open(p, "rb") for n, p in enumerate(image_paths, 1)}
    try:
        resp = requests.post(url, files=files, timeout=120).json()
    finally:
        for f in files.values():
            f.close()
    photos = (resp or {}).get("photos") or {}
    tokens = [v.get("token") for v in photos.values() if v.get("token")]
    if len(tokens) != len(image_paths):
        raise RuntimeError(f"ОК принял {len(tokens)} фото из {len(image_paths)}")
    return tokens


def schedule_topic(group_id: str, text: str, image_paths: list[str],
                   when_local: datetime) -> dict:
    """
    Поставить отложенный пост в группу. Возвращает {"ok": True, "topicId": …}
    либо {"ok": False, "error": "…"}. Время – екатеринбургское с поясом,
    в Москву переводим здесь.
    """
    if not is_configured():
        return {"ok": False, "error": "Не заполнены ключи ОК в «Настройках» "
                                      "(ok_app_key / ok_app_secret / ok_access_token)"}
    try:
        tokens = upload_photos(group_id, image_paths)
        resp = _call("mediatopic.post", {
            "gid": group_id,
            "type": "GROUP_THEME",
            "attachment": build_attachment(text, tokens),
            "publishAt": publish_at_msk(when_local),
            "onBehalfOfGroup": "true",
        })
        topic_id = resp if isinstance(resp, (str, int)) else (resp or {}).get("id", "")
        return {"ok": True, "topicId": str(topic_id)}
    except Exception as e:  # noqa: BLE001 – наружу словами, решает вызывающий
        return {"ok": False, "error": str(e)}


def check_access() -> str:
    """Живая проверка ключей: пусто – всё хорошо, иначе причина словами."""
    if not is_configured():
        return "не заполнены ключи ОК"
    try:
        _call("users.getCurrentUser", {"fields": "uid"})
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)
