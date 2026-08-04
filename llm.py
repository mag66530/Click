"""
llm.py – черновик ответа на отзыв через Google Gemini.

Почему обычный HTTPS, а не SDK. `requirements.txt` в Click закреплён по
версиям намеренно, и каждая новая строка там заставляет Streamlit Cloud
пересобирать окружение. `requests` уже стоит – одного POST достаточно,
ставить ради него отдельную библиотеку незачем.

Ключ берётся из секретов приложения (`gemini_api_key`) или из переменной
окружения `GEMINI_API_KEY`. В коде и в репозитории его нет.

Про модель. Имена моделей у Google меняются, а старые со временем
отключают. Поэтому здесь список кандидатов: первая, которая ответила,
запоминается на время жизни процесса. Если Google завтра переименует
модель, приложение переживёт это само, без правки кода.
"""

from __future__ import annotations

import os
import time

API = "https://generativelanguage.googleapis.com/v1beta/models"

# По убыванию свежести. Свежая может быть недоступна на бесплатном
# тарифе – тогда молча берём следующую.
MODELS = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash")

TIMEOUT = 45
RETRIES = 3                 # на лимит запросов и сетевые сбои
RETRY_PAUSE = (2, 6, 15)    # секунды между попытками

_working_model: str | None = None


class LlmError(RuntimeError):
    """Понятная человеку причина, почему черновика нет."""


# ─── Ключ ───────────────────────────────────────────────────────────
def api_key() -> str:
    v = (os.environ.get("GEMINI_API_KEY") or os.environ.get("gemini_api_key") or "").strip()
    if v:
        return v
    try:
        import streamlit as st
        return str(st.secrets.get("gemini_api_key") or "").strip()
    except Exception:  # noqa: BLE001 – вне Streamlit секретов просто нет
        return ""


def is_configured() -> bool:
    return bool(api_key())


def where() -> str:
    """Что показать в «Настройках», чтобы было понятно, готова ли генерация."""
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("gemini_api_key"):
        return "ключ Gemini взят из переменной окружения"
    if is_configured():
        return "ключ Gemini взят из секретов приложения"
    return "ключ Gemini не задан – черновики ответов формироваться не будут"


# ─── Запрос ─────────────────────────────────────────────────────────
def _post(model: str, prompt: str, key: str):
    import requests
    return requests.post(
        f"{API}/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            # Температура повыше: заказчик жмёт «Переписать» именно затем,
            # чтобы получить ДРУГОЙ вариант, а не тот же текст ещё раз.
            "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1024},
        },
        timeout=TIMEOUT,
    )


def _text_from(payload: dict) -> str:
    for cand in (payload.get("candidates") or []):
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text") or "" for p in parts).strip()
        if text:
            return text
    return ""


def _explain(code: int, body: str) -> str:
    if code in (400, 403):
        return ("Google не принял ключ Gemini. Проверьте секрет gemini_api_key "
                "и что для проекта включён Generative Language API.")
    if code == 429:
        return "Упёрлись в лимит запросов Gemini. Попробуйте позже или реже."
    if code >= 500:
        return f"Gemini временно недоступен (HTTP {code})."
    return f"Gemini вернул HTTP {code}: {body[:200]}"


def generate(prompt: str) -> str:
    """
    Текст черновика по готовому промпту. Бросает LlmError с причиной,
    понятной человеку – её показываем прямо в очереди на подтверждение.
    """
    key = api_key()
    if not key:
        raise LlmError("Ключ Gemini не задан – добавьте секрет gemini_api_key.")

    global _working_model
    models = [_working_model] if _working_model else list(MODELS)
    last = "не удалось связаться с Gemini"

    for model in models:
        for attempt in range(RETRIES):
            try:
                r = _post(model, prompt, key)
            except Exception as e:  # noqa: BLE001 – сеть
                last = f"Сеть не пустила запрос к Gemini: {e}"
                if attempt < RETRIES - 1:
                    time.sleep(RETRY_PAUSE[attempt])
                    continue
                break

            if r.status_code == 200:
                text = _text_from(r.json() or {})
                if text:
                    _working_model = model
                    return text
                last = "Gemini вернул пустой ответ."
                break

            # 404 – такой модели нет: пробуем следующую из списка.
            if r.status_code == 404 and not _working_model:
                last = f"Модель {model} недоступна."
                break

            last = _explain(r.status_code, r.text)
            # Лимит и серверные сбои имеет смысл переждать, остальное – нет.
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < RETRIES - 1:
                    time.sleep(RETRY_PAUSE[attempt])
                    continue
            break

    # Закреплённая модель отвалилась – в следующий раз перебираем заново.
    _working_model = None
    raise LlmError(last)


def model_in_use() -> str | None:
    return _working_model
