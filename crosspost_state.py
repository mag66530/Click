"""
crosspost_state.py – что уже сформировано и что вышло по постам реестра.

Зачем. Реестр говорит, ЧТО должно выйти. Этот модуль помнит, что с каждым
постом уже сделано: отложка создана, пост вышел, была ошибка. На нём держится
защита от дублей: повторное формирование (кнопкой, авто-циклом или после
перезапуска) видит запись и второй отложки не создаёт.

Ключ поста – бренд + дата + отпечаток текста. Отпечаток нужен, чтобы поймать
правку реестра задним числом: текст поменяли после формирования – ключ
разошёлся, и в разделе видно «в реестре текст изменился».

Где лежит. `users-data/<PROJECT>/crosspost/state.json` – рядом с остальными
данными проекта, вне папки программы (обновление Click их не трогает).
В облаке файловая система временная, поэтому со временем сюда добавится
зеркало в repo_store – как у настроек проекта; пока состояние локальное,
и это честно: до появления планировщика терять нечего.

Состояния цели (площадки у поста):
    waiting    – ещё не сформировано
    scheduled  – отложка стоит на площадке (ВК/ОК держат сами)
    sent       – вышло
    sent-late  – вышло с опозданием (Click был выключен в час Х)
    missed     – пропущено: окно опоздания вышло, публиковать поздно
    failed     – ошибка, причина словами в `error`
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import apptime
import paths

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

WAITING, SCHEDULED, SENT, SENT_LATE, MISSED, FAILED = (
    "waiting", "scheduled", "sent", "sent-late", "missed", "failed")

# Как показываем состояние человеку. Значок + подпись + класс строки –
# те же классы, что у строк отчёта, чтобы разделы выглядели одинаково.
HUMAN = {
    WAITING:   ("–",  "не сформировано", ""),
    # «Запланировано» покрывает оба случая: отложка на площадке (ВК, ОК и
    # МАКС через веб – держит сама сеть) и задание планировщику (ТГ и МАКС
    # через бота – отправит Click).
    SCHEDULED: ("🕓", "запланировано",   "skip"),
    SENT:      ("✅", "вышло",           "ok"),
    SENT_LATE: ("✅", "вышло с опозданием", "warn"),
    MISSED:    ("⏭", "пропущено",       "warn"),
    FAILED:    ("❌", "ошибка",          "err"),
}

# Как называется площадка в интерфейсе.
NETWORK_RU = {
    "vk": "ВК",
    "ok": "ОК",
    "tg": "Телеграм",
    "tg-staff": "ТГ сотрудники",
    "tg-client": "ТГ клиенты",
    "max": "МАКС",
    "zen": "Дзен",
    "yb": "Яндекс.Бизнес",
}


def network_ru(code: str) -> str:
    return NETWORK_RU.get(code, code or "–")


# ─── Ключи ──────────────────────────────────────────────────────────
def text_fingerprint(text: str) -> str:
    """Короткий отпечаток текста – по нему ловим правку реестра после формирования."""
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:12]


def post_key(post: dict) -> str:
    """
    Устойчивый ключ поста: бренд + дата + отпечаток текста.
    Номер строки в ключ НЕ входит: строки в таблице двигаются, а пост тот же.
    """
    return f'{post.get("brand", "")}|{post.get("date", "")}|{text_fingerprint(post.get("text", ""))}'


# ─── Хранилище ──────────────────────────────────────────────────────
def _path(project_id: str) -> Path:
    d = paths.data_root() / project_id / "crosspost"
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def load(project_id: str) -> dict:
    """Всё состояние проекта. Пустое – значит ещё ничего не формировали."""
    fp = _path(project_id)
    if not fp.exists():
        return {}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # Испорченный файл не должен ронять раздел: считаем, что записей нет.
        return {}


def save(project_id: str, state: dict) -> None:
    """Пишем через временный файл: обрыв на середине не оставит битый state.json."""
    fp = _path(project_id)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(fp)


# ─── Чтение и запись статуса одной цели ─────────────────────────────
def target(state: dict, post: dict, network: str) -> dict:
    """Что известно про эту площадку у этого поста. Пусто – ещё не трогали."""
    return (state.get(post_key(post), {}).get("targets", {}) or {}).get(network, {})


def status_of(state: dict, post: dict, network: str) -> str:
    return target(state, post, network).get("state") or WAITING


def is_done(state: dict, post: dict, network: str) -> bool:
    """Формировать больше не нужно: отложка стоит или пост уже вышел."""
    return status_of(state, post, network) in (SCHEDULED, SENT, SENT_LATE)


def set_status(project_id: str, post: dict, network: str, status: str,
               link: str = "", error: str = "", extra: dict | None = None) -> dict:
    """Записать состояние площадки и сразу сохранить. Возвращает всё состояние."""
    state = load(project_id)
    key = post_key(post)
    entry = state.setdefault(key, {
        "brand": post.get("brand", ""),
        "date": post.get("date", ""),
        "when": post.get("when", ""),
        "targets": {},
    })
    entry["targets"][network] = {
        "state": status,
        "link": link,
        "error": error,
        "at": apptime.now().isoformat(timespec="seconds"),
        **(extra or {}),
    }
    save(project_id, state)
    return state


def forget_post(project_id: str, post: dict) -> None:
    """Забыть пост целиком – «сформировать заново» после осознанной правки."""
    state = load(project_id)
    if state.pop(post_key(post), None) is not None:
        save(project_id, state)


def forget_target(project_id: str, post: dict, network: str) -> None:
    """
    Забыть ОДНУ площадку поста: ошибка сбрасывается, пост по ней снова
    «ещё не сформирован». Остальные площадки не трогаем – их отложки стоят.
    Нужно после ошибки формирования: раньше красная строка висела вечно,
    пока пост целиком не переформируют.
    """
    state = load(project_id)
    targets = state.get(post_key(post), {}).get("targets", {})
    if targets.pop(network, None) is not None:
        save(project_id, state)


# ─── Сводка для раздела ─────────────────────────────────────────────
def summarize(state: dict, posts: list[dict]) -> dict:
    """
    Цифры для шапки раздела: сколько целей ждёт формирования, стоит отложками,
    вышло, сломалось. Считаем по целям, а не по постам: один пост живёт на
    нескольких площадках, и «пост вышел» без уточнения где – полуправда.
    """
    out = {WAITING: 0, SCHEDULED: 0, SENT: 0, SENT_LATE: 0, MISSED: 0, FAILED: 0}
    for post in posts:
        for t in post.get("targets", []):
            # Реестр – тоже источник правды: стоит ссылка на пост, значит вышел,
            # даже если формировали не мы (человек опубликовал руками).
            if (t.get("published_link") or "").strip():
                out[SENT] += 1
                continue
            out[status_of(state, post, t["network"])] += 1
    return out


def problems(state: dict, posts: list[dict]) -> list[dict]:
    """
    «Что требует внимания»: ошибки, пропуски и посты без картинки/текста.
    Отдельным списком наверху – иначе беда теряется среди десятков строк плана.
    """
    out: list[dict] = []
    for post in posts:
        # Видео и статьи Click не формирует (вне scope) – и жаловаться на
        # отсутствие у них текста и картинки нечестно: это не поломка, а
        # другой вид контента. Иначе список «требует внимания» забивается шумом.
        if (post.get("format") or "Пост").strip().lower() != "пост":
            continue
        if not (post.get("text") or "").strip():
            out.append({"post": post, "what": "нет текста поста", "network": ""})
        if not post.get("images"):
            out.append({"post": post, "what": "нет картинки", "network": ""})
        for t in post.get("targets", []):
            if (t.get("published_link") or "").strip():
                continue
            st_ = status_of(state, post, t["network"])
            if st_ in (FAILED, MISSED):
                info = target(state, post, t["network"])
                out.append({
                    "post": post,
                    "network": t["network"],
                    "what": info.get("error") or HUMAN[st_][1],
                })
    return out
