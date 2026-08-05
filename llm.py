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
BUILD = "2026-08-06-send-report"

# По убыванию свежести. Свежая может быть недоступна на бесплатном
# тарифе – тогда молча берём следующую.
MODELS = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash")

TIMEOUT = 45

# Пауза между запросами. Бесплатный тариф Gemini считает запросы в минуту, и
# первый боевой прогон упёрся в лимит: 13 черновиков подряд без пауз.
#
# Фиксированные 6 секунд эту беду вылечили, но заказчик справедливо сказала,
# что генерация стала долгой: два десятка черновиков – это две минуты одного
# ожидания. Поэтому пауза адаптивная: начинаем с малой и растим ТОЛЬКО когда
# Google действительно ответил «слишком часто». Пока лимит не мешает, работаем
# быстро; упёрлись – притормаживаем сами.
MIN_GAP_S = 1.5             # с чего начинаем
MAX_GAP_S = 20.0            # куда упираемся после отказов
GAP_DECAY = 0.8             # после удачного ответа пауза потихоньку тает

# Сколько всего готовы ждать один черновик. Раньше ограничения не было:
# на отказ «слишком часто» шло четыре повтора с паузами 5+20+45+60 секунд,
# и так по каждой из трёх моделей – один отзыв мог занять минуты. Заказчик
# это и увидела: «генерирует 4 минуты».
#
# Теперь иначе: упёрлись в лимит – СРАЗУ пробуем следующую модель, у неё
# своя квота. Только если отказали все, ждём один раз и делаем второй круг.
TOTAL_BUDGET_S = 75         # дольше не мучаем – отдаём отзыв на ручной ввод
ROUNDS = 2                  # кругов по всем моделям
ROUND_PAUSE_S = 12          # пауза между кругами, если отказали все
MAX_RETRY_WAIT = 30         # дольше по подсказке Google тоже не ждём

_working_model: str | None = None
_last_call_at: float = 0.0
_gap: float = MIN_GAP_S

# Последний замер – чтобы в логе было видно, где именно уходит время.
last_stats: dict = {}
_no_thinking: set = set()


class LlmError(RuntimeError):
    """Понятная человеку причина, почему черновика нет."""


# ─── Ключи ──────────────────────────────────────────────────────────
#
# Ключей может быть несколько. Бесплатный тариф Gemini считает запросы в
# минуту НА КЛЮЧ, поэтому два-три ключа – это просто во столько же раз
# больше запросов до упора в лимит. Заказчик предложила сама, и это самый
# дешёвый способ ускорить генерацию.
#
# Берём gemini_api_key, gemini_api_key_2 … gemini_api_key_5, а также
# gemini_api_keys через запятую – кому как удобнее записать в секреты.

MAX_KEYS = 5
KEY_COOLDOWN_S = 45          # столько не трогаем ключ после отказа по лимиту


def _secret(name: str) -> str:
    v = (os.environ.get(name.upper()) or os.environ.get(name) or "").strip()
    if v:
        return v
    try:
        import streamlit as st
        return str(st.secrets.get(name) or "").strip()
    except Exception:  # noqa: BLE001 – вне Streamlit секретов просто нет
        return ""


def api_keys() -> list[str]:
    """Все заданные ключи, по порядку и без повторов."""
    found = [_secret("gemini_api_key")]
    found += [_secret(f"gemini_api_key_{n}") for n in range(2, MAX_KEYS + 1)]
    found += [p.strip() for p in _secret("gemini_api_keys").split(",")]
    out: list[str] = []
    for k in found:
        if k and k not in out:
            out.append(k)
    return out


def api_key() -> str:
    keys = api_keys()
    return keys[0] if keys else ""


def is_configured() -> bool:
    return bool(api_keys())


def where() -> str:
    """Что показать в «Настройках», чтобы было понятно, готова ли генерация."""
    n = len(api_keys())
    if not n:
        return "ключ Gemini не задан – черновики ответов формироваться не будут"
    if n == 1:
        return ("задан один ключ Gemini. Второй и третий (gemini_api_key_2, "
                "gemini_api_key_3) ускорят генерацию: лимит считается на каждый ключ отдельно")
    return f"ключей Gemini: {n} – запросы распределяются между ними"


# Когда ключ был занят в последний раз и до какого времени его лучше не
# трогать после отказа по лимиту.
_key_last: dict[str, float] = {}
_key_cool: dict[str, float] = {}


def _pick_key(keys: list[str]) -> tuple[str, float]:
    """
    Ключ, которым можно воспользоваться раньше всех, и сколько его ждать.

    Свободный ключ находится сразу – тогда ждать нечего. Если все в
    остывании, берём тот, что освободится первым.
    """
    now = time.time()
    best, best_wait = keys[0], None
    for k in keys:
        wait = max(_key_cool.get(k, 0.0) - now,
                   _gap - (now - _key_last.get(k, 0.0)), 0.0)
        if best_wait is None or wait < best_wait:
            best, best_wait = k, wait
        if wait <= 0:
            break
    return best, max(0.0, best_wait or 0.0)


def _cool(key: str) -> None:
    _key_cool[key] = time.time() + KEY_COOLDOWN_S


# ─── Запрос ─────────────────────────────────────────────────────────
def _throttle(key: str, wait: float = 0.0) -> None:
    """Выдержать паузу перед запросом этим ключом."""
    if wait > 0:
        time.sleep(wait)
    _key_last[key] = time.time()


def _slower() -> None:
    """Google сказал «слишком часто» – увеличиваем паузу."""
    global _gap
    _gap = min(MAX_GAP_S, max(MIN_GAP_S, _gap * 2))


def _faster() -> None:
    """Ответ пришёл спокойно – можно понемногу разгоняться обратно."""
    global _gap
    _gap = max(MIN_GAP_S, _gap * GAP_DECAY)


def current_gap() -> float:
    return _gap


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


def _post(model: str, prompt: str, key: str, thinking: bool = True, wait: float = 0.0):
    import requests
    _throttle(key, wait)
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

    Перебираем пары «ключ + модель»: у каждого ключа своя квота, у каждой
    модели тоже. Отказ по лимиту – берём следующую пару, а не ждём. Ключ,
    который отказал, ненадолго откладываем в сторону.

    Общее время ограничено TOTAL_BUDGET_S: лучше отдать отзыв на ручной
    ввод, чем держать человека в ожидании минутами.
    """
    global _working_model
    keys = api_keys()
    if not keys:
        raise LlmError("Ключ Gemini не задан – добавьте секрет gemini_api_key.")

    started = time.time()
    order = ([_working_model] + [m for m in MODELS if m != _working_model]
             if _working_model else list(MODELS))
    dead: set[str] = set()          # моделей нет вовсе – больше не трогаем
    last = "не удалось связаться с Gemini"
    calls = 0
    limited_all = False

    for rnd in range(ROUNDS):
        limited = False
        for model in order:
            if model in dead:
                continue
            key, wait = _pick_key(keys)
            spent = time.time() - started
            if spent + wait > TOTAL_BUDGET_S:
                limited_all = True
                break
            calls += 1
            try:
                r = _post(model, prompt, key, thinking=(model not in _no_thinking), wait=wait)
            except Exception as e:  # noqa: BLE001 – сеть
                last = f"Сеть не пустила запрос к Gemini: {e}"
                continue

            if r.status_code == 200:
                text, finish = _text_from(r.json() or {})
                if text and finish != "MAX_TOKENS":
                    _working_model = model
                    _faster()
                    last_stats.update(model=model, seconds=round(time.time() - started, 1),
                                      calls=calls, keys=len(keys), gap=round(_gap, 1))
                    return text
                last = ("Gemini не уложился в ответ и оборвал его на середине."
                        if finish == "MAX_TOKENS" else "Gemini вернул пустой ответ.")
                continue

            if r.status_code == 404:
                dead.add(model)
                last = f"Модель {model} недоступна."
                continue

            # Модель не знает про thinkingConfig – запомним и повторим без него.
            if r.status_code == 400 and model not in _no_thinking and "thinking" in r.text.lower():
                _no_thinking.add(model)
                continue

            last = _explain(r.status_code, r.text)
            if r.status_code == 429:
                _cool(key)          # этот ключ ненадолго в сторону
                _slower()
                limited = True

        if limited and rnd < ROUNDS - 1 and time.time() - started < TOTAL_BUDGET_S:
            time.sleep(min(ROUND_PAUSE_S, max(0.0, TOTAL_BUDGET_S - (time.time() - started))))
            continue
        break

    if limited_all or "лимит" in last.lower():
        tail = (" Добавьте второй ключ Gemini (секрет gemini_api_key_2) – лимит "
                "считается на каждый ключ отдельно." if len(keys) < 2 else
                " Подождите минуту и нажмите «Переписать» ещё раз.")
        last = "Gemini придерживает запросы по лимиту." + tail

    _working_model = None
    last_stats.update(model=None, seconds=round(time.time() - started, 1),
                      calls=calls, keys=len(keys), gap=round(_gap, 1), error=last)
    raise LlmError(last)


def model_in_use() -> str | None:
    return _working_model
