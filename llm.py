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

# Метка сборки. Одна на все модули Click: облако умеет обновить главный
# скрипт, оставив этот модуль в памяти прежним, и тогда страница зовёт
# функцию, которой тут ещё нет. streamlit_app сверяет метку и при
# расхождении перезагружает модуль сам.
BUILD = "2026-08-05-kp-xlsx"

# По убыванию свежести. Свежая может быть недоступна на бесплатном
# тарифе – тогда молча берём следующую.
MODELS = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash")

TIMEOUT = 45

# Бесплатный тариф Gemini считает запросы в минуту. Первый же боевой прогон
# упёрся в лимит на восьмом городе: 13 черновиков подряд без пауз – и всё,
# «Упёрлись в лимит». Поэтому держим паузу между вызовами сами: лучше прогон
# на минуту длиннее, чем половина отзывов без черновика.
MIN_GAP_S = 6.0             # ≈10 запросов в минуту

RETRIES = 4                 # на лимит запросов и сетевые сбои
RETRY_PAUSE = (5, 20, 45, 60)
MAX_RETRY_WAIT = 90         # дольше ждать по подсказке Google не станем

_working_model: str | None = None
_last_call_at: float = 0.0


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
def _throttle() -> None:
    """Выдержать паузу между запросами, чтобы не выбить дневной лимит за минуту."""
    global _last_call_at
    wait = MIN_GAP_S - (time.time() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.time()


def _retry_after(payload: dict) -> float:
    """
    Сколько Google просит подождать. Он присылает это в деталях ошибки
    (RetryInfo, поле retryDelay вида "27s") – слушать подсказку надёжнее,
    чем гадать своими паузами.
    """
    err = (payload or {}).get("error") or {}
    for d in err.get("details") or []:
        delay = str(d.get("retryDelay") or "").strip()
        if delay.endswith("s"):
            try:
                return min(float(delay[:-1]), MAX_RETRY_WAIT)
            except ValueError:
                pass
    return 0.0


def _thinking_config(model: str) -> dict:
    """
    Свежие модели Gemini «думают» перед ответом, и эти размышления едят тот же
    лимит выходных токенов. В первом боевом прогоне из-за этого обрезало ВСЕ
    черновики, а в пару ответов просочились куски рассуждений по-английски
    («Can a salutation have 2 sentences?»). Ответ на отзыв – задача короткая,
    размышления тут не нужны.

    У 3.x параметр называется thinkingLevel, у 2.5 – thinkingBudget; вместе их
    слать нельзя, поэтому выбираем по имени модели.
    """
    if model.startswith("gemini-3"):
        return {"thinkingLevel": "minimal"}
    return {"thinkingBudget": 0}


def _post(model: str, prompt: str, key: str, thinking: bool = True):
    import requests
    _throttle()
    cfg = {
        # Температура повыше: заказчик жмёт «Переписать» именно затем,
        # чтобы получить ДРУГОЙ вариант, а не тот же текст ещё раз.
        "temperature": 0.9,
        # С запасом: ответ на отзыв – это абзац-другой, но лимит общий
        # с размышлениями модели, и жадничать тут нечем.
        "maxOutputTokens": 4096,
    }
    if thinking:
        cfg["thinkingConfig"] = _thinking_config(model)
    return requests.post(
        f"{API}/{model}:generateContent",
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
        timeout=TIMEOUT,
    )


def _text_from(payload: dict) -> tuple[str, str]:
    """
    Текст ответа и причина завершения.

    Куски с пометкой thought – это размышления модели, а не ответ; они не
    должны попасть в текст, который человек отправит от имени бренда.
    """
    for cand in (payload.get("candidates") or []):
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text") or "" for p in parts if not p.get("thought")).strip()
        if text:
            return text, str(cand.get("finishReason") or "")
    return "", str(((payload.get("candidates") or [{}])[0]).get("finishReason") or "")


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
    # Даже если модель уже выбрана, держим остальные про запас: у каждой
    # модели своя квота, и упёршись в лимит одной, имеет смысл доработать
    # прогон на другой, а не оставлять половину отзывов без черновика.
    models = ([_working_model] + [m for m in MODELS if m != _working_model]
              if _working_model else list(MODELS))
    last = "не удалось связаться с Gemini"

    for model in models:
        thinking = True
        for attempt in range(RETRIES):
            try:
                r = _post(model, prompt, key, thinking=thinking)
            except Exception as e:  # noqa: BLE001 – сеть
                last = f"Сеть не пустила запрос к Gemini: {e}"
                if attempt < RETRIES - 1:
                    time.sleep(RETRY_PAUSE[attempt])
                    continue
                break

            if r.status_code == 200:
                text, finish = _text_from(r.json() or {})
                if text and finish != "MAX_TOKENS":
                    _working_model = model
                    return text
                if finish == "MAX_TOKENS":
                    # Обрезанный ответ не отдаём: половина фразы под именем
                    # бренда хуже, чем честное «черновика нет».
                    last = "Gemini не уложился в ответ и оборвал его на середине."
                    break
                last = "Gemini вернул пустой ответ."
                break

            # Модель не знает про thinkingConfig – повторяем без него.
            if r.status_code == 400 and thinking and "thinking" in r.text.lower():
                thinking = False
                continue

            # 404 – такой модели нет: пробуем следующую из списка.
            if r.status_code == 404:
                last = f"Модель {model} недоступна."
                break

            last = _explain(r.status_code, r.text)
            # Лимит и серверные сбои имеет смысл переждать, остальное – нет.
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < RETRIES - 1:
                    try:
                        asked = _retry_after(r.json() or {})
                    except Exception:  # noqa: BLE001 – тело может быть не JSON
                        asked = 0.0
                    time.sleep(max(asked, RETRY_PAUSE[attempt]))
                    continue
                # Ждать дальше бессмысленно – пробуем следующую модель,
                # у неё своя квота.
                if model == _working_model:
                    _working_model = None
                break
            break

    # Ни одна модель не ответила – в следующий раз перебираем заново.
    _working_model = None
    raise LlmError(last)


def model_in_use() -> str | None:
    return _working_model
