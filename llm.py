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

# Метка сборки – одна на всё приложение, лежит в build.py. Держать её
# в каждом модуле своей строкой уже ломалось: у одного файла осталось
# старое значение, и Click принялся перезагружать все модули на каждое
# нажатие, пока не выбило по памяти.
from build import BUILD  # noqa: F401

# По убыванию свежести. Свежая может быть недоступна на бесплатном
# тарифе – тогда молча берём следующую.
MODELS = ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash")

TIMEOUT = 45

# ─── Темп ───────────────────────────────────────────────────────────
#
# Google меряет лимит в ЗАПРОСАХ ЗА МИНУТУ. Значит и держать себя надо в тех
# же единицах: следить, сколько запросов ушло за последние 60 секунд, и не
# переступать порог. Тогда отказов не будет вовсе.
#
# Раньше здесь была «адаптивная пауза»: начинаем с 1.5 секунды, а на каждый
# отказ удваиваем. Идея простая, но на живом прогоне она вела себя плохо, и
# заказчик это увидела. Разгон с малой паузы успевал за первые же секунды
# выбрать минутную квоту; дальше сыпались отказы, пауза улетала в потолок
# (20 сек), а на успехах таяла медленно – и на каждый черновик уходило по
# 40 секунд и по четыре запроса, из которых три отвергнуты. Хуже того, на
# отвергнутые запросы время тратилось впустую: черновиков от них не было.
#
# Считаем иначе. Порог Google не сообщает, поэтому узнаём его на практике:
# получили отказ – значит темп, на котором мы шли, уже велик; опускаем планку
# чуть ниже и дальше держим её сами, не дожидаясь новых отказов. Долгая
# спокойная работа планку потихоньку поднимает обратно.
WINDOW_S = 60.0             # окно, которым Google считает запросы
START_RPM = 12.0            # с какого темпа начинаем (запросов в минуту)
MIN_RPM = 3.0               # ниже не опускаемся – иначе прогон встанет
MAX_RPM = 15.0             # выше не разгоняемся даже после долгого затишья
CALM_STREAK = 8             # столько ответов подряд без отказа – можно быстрее

# Сколько всего готовы ждать один черновик. Раньше ограничения не было:
# на отказ «слишком часто» шло четыре повтора с паузами 5+20+45+60 секунд,
# и так по каждой из трёх моделей – один отзыв мог занять минуты. Заказчик
# это и увидела: «генерирует 4 минуты».
#
# Теперь иначе: упёрлись в лимит – СРАЗУ пробуем следующую модель, у неё
# своя квота. Только если отказали все, ждём один раз и делаем второй круг.
#
# Потолок подняли со 75 секунд до 100 вот почему. Раньше ожидание было
# гаданием: подождём и вдруг повезёт. Теперь ожидание осмысленное – мы знаем,
# когда именно освободится место в минутном окне, и дождавшись получаем
# черновик наверняка. Полное окно – 60 секунд, плюс сам запрос: в 75 такой
# случай не укладывался, и Click сдавался ровно перед успехом.
TOTAL_BUDGET_S = 100        # дольше не мучаем – отдаём отзыв на ручной ввод
ROUNDS = 2                  # кругов по всем моделям
MAX_RETRY_WAIT = 30         # дольше по подсказке Google тоже не ждём

_working_model: str | None = None
_rpm_cap: float = START_RPM
_calm: int = 0              # ответов подряд без отказа по лимиту

# Последний замер – чтобы в логе было видно, где именно уходит время.
last_stats: dict = {}
_no_thinking: set = set()


class LlmError(RuntimeError):
    """
    Понятная человеку причина, почему черновика нет.

    is_limit говорит, что дело именно в лимите Gemini, а не в сети или в
    самом ответе. Прогону это важно: упёршись в лимит, долбить его дальше
    смысла нет – каждая попытка съедает до полутора минут и всё равно
    возвращает то же самое.
    """

    def __init__(self, message: str, is_limit: bool = False):
        super().__init__(message)
        self.is_limit = is_limit


# ─── Ключи ──────────────────────────────────────────────────────────
#
# Ключей может быть несколько. Заказчик предложила сама – и мысль верная, но
# с оговоркой, которая всё решает: Google считает лимит НА ПРОЕКТ, а не на
# ключ. Два ключа ускоряют работу вдвое, только если они из РАЗНЫХ проектов;
# из одного проекта – это один и тот же лимит, записанный дважды.
# Подробности и как мы это распознаём – ниже, в разделе «Общая квота».
#
# Берём gemini_api_key, gemini_api_key_2 … gemini_api_key_5, а также
# gemini_api_keys через запятую – кому как удобнее записать в секреты.

# Ключи идут ПО КРУГУ, по одному запросу на каждый. Раньше брался первый
# ключ, которым можно слать прямо сейчас, – и первый в списке отдувался за
# всех, пока не упирался в лимит. Чередование даёт каждому ключу одинаковый
# отдых, и до лимита дело доходит позже.
MAX_KEYS = 5


def _secret(name: str) -> str:
    v = (os.environ.get(name.upper()) or os.environ.get(name) or "").strip()
    if v:
        return v
    try:
        import streamlit as st
        v = str(st.secrets.get(name) or "").strip()
        if v:
            return v
    except Exception:  # noqa: BLE001 – вне Streamlit секретов просто нет
        pass
    # Последняя очередь – ключи, вписанные в самом приложении. Локально
    # секретов Streamlit нет, и без этого черновики отзывов не работали.
    try:
        import secrets_local
        return secrets_local.get(name)
    except Exception:  # noqa: BLE001
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
                "gemini_api_key_3) ускорят генерацию – но только если создать каждый "
                "в ОТДЕЛЬНОМ проекте Google: лимит считается на проект, а не на ключ")
    if _shared_quota:
        return (f"ключей Gemini: {n}, но лимит у них ОБЩИЙ – похоже, они созданы в одном "
                "проекте Google. Скорости это не добавляет: лимит считается на проект, а "
                "не на ключ. Чтобы ключи работали независимо, каждый нужно создать в НОВОМ "
                "проекте – в AI Studio при создании ключа выбрать новый проект, а не тот же")
    return f"ключей Gemini: {n} – запросы распределяются между ними"


# ─── Общая квота ────────────────────────────────────────────────────
#
# Лимит Gemini считается НА ПРОЕКТ Google, а не на ключ. Дословно из их
# документации: «Rate limits are applied per project, not per API key»
# (ai.google.dev/gemini-api/docs/rate-limits).
#
# Отсюда неприятность: два ключа, созданные в одном проекте AI Studio, делят
# одну квоту и скорости не добавляют ВОВСЕ. И это не просто «не помогло» –
# становится хуже, чем с одним ключом: считая квоты разными, мы шлём такими
# ключами запросы вплотную, без пауз, и общую квоту выбираем втрое быстрее.
# Заказчик это и увидела: ключей несколько, а лог жёлтый от «придерживает
# по лимиту», и пауза выросла до предела.
#
# Отличить один случай от другого можно по факту, ничего лишнего не тратя.
# Лимит меряется в запросах ЗА МИНУТУ. Значит если ключ, которым за последнюю
# минуту почти не пользовались, всё равно получает отказ «слишком часто», –
# квоту у него выбрал не он сам, а соседний ключ. Это и есть общий проект.
SHARED_QUIET_HITS = 2        # столько запросов ключа за минуту – «почти не трогали»
SHARED_BUSY_HITS = 5         # а столько запросов всеми ключами – «работа шла»

_shared_quota: bool = False

# Когда мы слали запросы. Ключ учёта – пара «ключ + модель»: Google считает
# лимит и на проект, И на модель отдельно. Поэтому упёрлись в лимит на
# gemini-3.6-flash – у gemini-3.5-flash своя квота, она ещё цела.
_hits_by: dict[tuple[str, str], list[float]] = {}

# Куда встал круг чередования ключей.
_next_key: int = 0


def _note_use(key: str, model: str) -> None:
    now = time.time()
    box = [t for t in _hits_by.get((key, model), []) if now - t < WINDOW_S]
    _hits_by[(key, model)] = box + [now]


def _stamps(key: str, model: str) -> list[float]:
    """Когда за последнюю минуту слали в ту же квоту, что и этой парой."""
    now = time.time()
    if _shared_quota:
        # Проект общий – квота у всех ключей одна, считаем по модели.
        flat = [t for (_k, m), v in _hits_by.items() if m == model for t in v]
    else:
        flat = list(_hits_by.get((key, model), []))
    return sorted(t for t in flat if now - t < WINDOW_S)


def _hits(key: str) -> int:
    """Сколько раз за минуту трогали именно этот ключ – любой моделью."""
    now = time.time()
    return sum(1 for (k, _m), v in _hits_by.items() if k == key
               for t in v if now - t < WINDOW_S)


def _hits_all() -> int:
    now = time.time()
    return sum(1 for v in _hits_by.values() for t in v if now - t < WINDOW_S)


def _note_limit(key: str, keys: list[str]) -> None:
    """Отказ по лимиту – заодно смотрим, своя у ключа квота или общая."""
    global _shared_quota
    if _shared_quota or len(keys) < 2:
        return
    if _hits(key) <= SHARED_QUIET_HITS and _hits_all() >= SHARED_BUSY_HITS:
        _shared_quota = True


def shared_quota() -> bool:
    """Выяснилось ли, что ключи сидят на одной квоте."""
    return _shared_quota


# ─── Темп и очередь ключей ──────────────────────────────────────────
def _wait_for(key: str, model: str) -> float:
    """
    Сколько ждать, чтобы этот запрос не выбил нас за выбранный темп.

    Считаем ровно как Google: сколько запросов ушло в эту квоту за последние
    60 секунд. Не добрали до планки – шлём сразу. Добрали – ждём, пока самый
    старый запрос выпадет из минутного окна, и место освободится само.
    """
    cap = max(1, int(_rpm_cap))
    seen = _stamps(key, model)
    if len(seen) < cap:
        return 0.0
    return max(0.0, seen[len(seen) - cap] + WINDOW_S - time.time())


def _pick_key(keys: list[str], model: str) -> tuple[str, float]:
    """
    Следующий ключ по кругу и сколько его ждать.

    По кругу – принципиально. Раньше брался первый ключ, которым можно слать
    прямо сейчас, а первым в списке всегда идёт первый ключ: пока пауза мала,
    он успевал освободиться к следующему отзыву, и до второго очередь просто
    не доходила. Получалось, что один ключ выжимается досуха, а остальные
    лежат нетронутыми – ровно то, что заметила заказчик.

    Теперь отсчёт начинается с того ключа, чья очередь подошла. Нагрузка
    делится поровну, и каждому ключу достаётся одинаковый отдых.
    """
    global _next_key
    n = len(keys)
    start = _next_key % n
    best_i, best_wait = start, None
    for off in range(n):
        i = (start + off) % n
        wait = _wait_for(keys[i], model)
        if best_wait is None or wait < best_wait:
            best_i, best_wait = i, wait
        if wait <= 0:
            break
    _next_key = best_i + 1
    return keys[best_i], max(0.0, best_wait or 0.0)


def _slower(key: str, model: str) -> None:
    """
    Google сказал «слишком часто» – значит наша планка выше настоящей.

    Опускаем её чуть ниже того темпа, на котором нас остановили, и дальше
    держим сами. Это и есть отличие от прошлой схемы: не «удвоим паузу и
    посмотрим», а «теперь знаем предел и в него не лезем».
    """
    global _rpm_cap, _calm
    _calm = 0
    seen = len(_stamps(key, model))
    _rpm_cap = max(MIN_RPM, min(_rpm_cap, seen - 1 if seen > 1 else MIN_RPM))


def _faster() -> None:
    """Долго идём спокойно – можно осторожно прибавить."""
    global _rpm_cap, _calm
    _calm += 1
    if _calm >= CALM_STREAK:
        _calm = 0
        _rpm_cap = min(MAX_RPM, _rpm_cap + 1)


def current_pace() -> float:
    """Текущая планка темпа, запросов в минуту."""
    return round(_rpm_cap, 1)


# ─── Запрос ─────────────────────────────────────────────────────────
def _throttle(key: str, model: str, wait: float = 0.0) -> None:
    """Выдержать паузу перед запросом и записать сам запрос."""
    if wait > 0:
        time.sleep(wait)
    _note_use(key, model)


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
    _throttle(key, model, wait)
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

    Ключи идут по кругу, модели – по свежести. Квота у Google считается на
    проект и на модель, поэтому отказ на одной модели не значит, что нет
    места на другой: пробуем следующую, а не ждём.

    Главное же – до отказов стараемся не доводить вовсе: перед запросом
    смотрим, сколько их ушло за последнюю минуту, и при необходимости ждём.
    Отвергнутый запрос не даёт черновика, но время на него тратится; лучше
    подождать заранее, чем получить отказ и подождать всё равно.

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
            key, wait = _pick_key(keys, model)
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
                                      calls=calls, keys=len(keys), shared=_shared_quota,
                                      pace=current_pace(), waited=round(wait, 1))
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
                _note_limit(key, keys)
                # Планку опускаем один раз за круг, а не на каждый отказ:
                # иначе тремя ключами она за секунды падает до самого низа.
                if not limited:
                    _slower(key, model)
                limited = True

        # Отдельной паузы между кругами больше нет. Раньше тут спалось 12
        # секунд вслепую; теперь нужное ожидание посчитает _pick_key – ровно
        # столько, сколько до освобождения минутного окна, и ни секундой
        # больше. Лишний сон только съедал бюджет черновика.
        if limited and rnd < ROUNDS - 1 and time.time() - started < TOTAL_BUDGET_S:
            continue
        break

    if limited_all or "лимит" in last.lower():
        if len(keys) < 2:
            tail = (" Добавьте второй ключ Gemini (секрет gemini_api_key_2), создав его "
                    "в отдельном проекте Google – лимит считается на проект.")
        elif _shared_quota:
            tail = (f" Ключей {len(keys)}, но лимит у них общий: они из одного проекта "
                    "Google, а лимит считается на проект, а не на ключ. Новый ключ нужно "
                    "создавать в НОВОМ проекте, иначе квота остаётся той же самой.")
        else:
            tail = " Подождите минуту и нажмите «Переписать» ещё раз."
        last = "Gemini придерживает запросы по лимиту." + tail

    _working_model = None
    last_stats.update(model=None, seconds=round(time.time() - started, 1),
                      calls=calls, keys=len(keys), shared=_shared_quota,
                      pace=current_pace(), error=last)
    raise LlmError(last, is_limit=bool(limited_all or limited))


def model_in_use() -> str | None:
    return _working_model
