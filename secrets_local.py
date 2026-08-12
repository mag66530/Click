"""
secrets_local.py – ключи к веб-сервисам, введённые прямо в приложении.

Зачем. В облаке ключи лежат в секретах Streamlit, и это удобно: вписал один
раз в панели – работает. Локально того же самого нет: надо руками создать
`.streamlit/secrets.toml`, знать его формат и не ошибиться в отступах. Из-за
этого на своём компьютере не работало ничего, что ходит в интернет:
черновики отзывов (Gemini), таблица КП (Google), хранение данных в
репозитории (GitHub).

Теперь ключи можно вписать в самом приложении, в «Настройках». Лежат они
в JSON рядом с остальными данными проекта – то есть ВНЕ папки Click:

    Windows  %LOCALAPPDATA%\\Click\\users-data\\secrets.json

Так они переживают обновление программы и не попадают в репозиторий даже
по случайности: в папке проекта их просто нет.

Порядок поиска везде одинаковый и менять его нельзя:

    переменная окружения → секреты Streamlit → этот файл

Облако как работало, так и работает: там первые два источника заполнены, и
до файла дело не доходит. Файл – это последняя подстраховка для локального
запуска.

ВАЖНО: ключи хранятся как есть, без шифрования – шифровать их всё равно
нечем, пароль пришлось бы держать рядом. Это обычный файл с доступом того
же уровня, что и сохранённая сессия Яндекса, которая лежит там же.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import paths

# Метка сборки – одна на всё приложение, лежит в build.py. Держать её
# в каждом модуле своей строкой уже ломалось: у одного файла осталось
# старое значение, и Click принялся перезагружать все модули на каждое
# нажатие, пока не выбило по памяти.
from build import BUILD  # noqa: F401

FILE_NAME = "secrets.json"

# Что вообще умеет храниться здесь. Порядок – он же порядок полей на экране.
KNOWN = (
    ("gemini_api_key", "Ключ Gemini", "Черновики ответов на отзывы"),
    ("gemini_api_key_2", "Ключ Gemini № 2", "Второй ключ – вдвое быстрее"),
    ("gemini_api_key_3", "Ключ Gemini № 3", "Третий ключ – втрое быстрее"),
    ("gcp_service_account_b64", "Ключ Google (JSON или base64)", "Таблица КП"),
    ("github_token", "Токен GitHub", "Хранение данных между перезапусками"),
    ("github_repo", "Репозиторий GitHub", "owner/name, если не тот, что по умолчанию"),
    # Кросспостинг. ВК ключей не требует – там вход и сессия, как у Яндекса.
    ("ok_app_key", "ОК: ключ приложения", "Кросспостинг – application_key из письма ОК"),
    ("ok_app_secret", "ОК: секрет приложения", "Кросспостинг – secret key из письма ОК"),
    ("ok_access_token", "ОК: вечный access_token", "Генерируется ПОСЛЕ выдачи прав (Приложение А.2)"),
    ("ok_session_secret_key", "ОК: session_secret_key", "Выдаётся вместе с токеном; пусто – посчитается сам"),
    ("tg_bot_token", "Телеграм: токен бота", "Кросспостинг – бот-админ каналов, от BotFather"),
    ("tg_alert_chat_id", "Телеграм: чат для тревог", "Личный chat_id – сюда прилетят сбои кросспостинга"),
    ("max_bot_token", "МАКС: токен бота", "Кросспостинг – бот-админ каналов, от МастерБота"),
)
NAMES = tuple(n for n, _, _ in KNOWN)


def path() -> Path:
    return paths.data_root() / FILE_NAME


def load() -> dict[str, str]:
    """Всё, что вписано в приложении. Пусто – ничего не вписывали."""
    fp = path()
    if not fp.exists():
        return {}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(data, dict) and v}


def save(values: dict[str, str]) -> Path:
    """
    Записать ключи. Пустое значение стирает ключ, а не пишет пустую строку:
    иначе стёртый в поле ключ продолжал бы перекрывать переменную окружения.
    """
    fp = path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    current = load()
    for k, v in values.items():
        v = (v or "").strip()
        if v:
            current[k] = v
        else:
            current.pop(k, None)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, fp)
    try:                                   # читать может только владелец
        os.chmod(fp, 0o600)
    except OSError:
        pass
    return fp


def get(name: str) -> str:
    """Значение из этого файла. Пусто – не вписано."""
    return (load().get(name) or "").strip()


def source_of(name: str) -> str:
    """
    Откуда возьмётся ключ: «окружение», «секреты Streamlit», «настройки»
    или пусто, если его нет нигде. Нужно, чтобы на экране было видно, почему
    поле пустое, а всё работает (или наоборот).
    """
    if (os.environ.get(name.upper()) or os.environ.get(name) or "").strip():
        return "переменная окружения"
    try:
        import streamlit as st
        if str(st.secrets.get(name) or "").strip():
            return "секреты Streamlit"
    except Exception:  # noqa: BLE001 – вне Streamlit секретов нет
        pass
    return "настройки приложения" if get(name) else ""


def masked(value: str) -> str:
    """«AIza…9f2c» – показать, что ключ есть, не показывая сам ключ."""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 12:
        return v[:2] + "…"
    return f"{v[:6]}…{v[-4:]} ({len(v)} знаков)"
