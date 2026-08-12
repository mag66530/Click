"""
crosspost_plan.py — план кросспостинга: строки постов и знаки площадок.

Зачем. Реестр говорит, что и когда выйдет, а раздел должен показывать это так,
чтобы читалось сверху вниз одним взглядом: когда, что за пост, куда уйдёт и что
с ним не так. Здесь собирается «что рисовать» — строки, колонки площадок,
подписи. Ни Streamlit, ни HTML: только данные, поэтому всё проверяется обычными
тестами (tests_crosspost.py).

Календарём это уже было. На живом реестре (посты два-три раза в неделю) сетка
недель оказалась почти пустой: две трети экрана — клетки «постов нет», а сами
посты ужаты в узкие плитки, где текст рвётся по слогам. Строки плотнее и
читаются быстрее, поэтому вернулись к ним — но с колонками площадок, чтобы
статус ВК, ОК, Телеграма и МАКС сравнивался по вертикали.

Знаки площадок (по ним же легенда в разделе):
    ✓ зелёный   — отложка стоит в соцсети (ВК, ОК и МАКС держат сами)
    ⏱ синий     — отправит Click в час выхода (Телеграм)
    ✓ залитый   — уже вышло
    · серый     — ещё не сформировано
    ✕ красный   — ошибка, пост туда не ушёл
"""

from __future__ import annotations

from datetime import date, timedelta

import content_plan
import crosspost_state as cps
import post_text

# Метка сборки — одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

WEEKDAYS_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря")

# Площадки, где отложку держит сама соцсеть ВСЕГДА. Для них «запланировано»
# значит «запись уже лежит в сообществе» — Click в час выхода не нужен. Для ТГ
# то же состояние значит обратное: отправит Click, и он должен быть открыт.
#
# МАКС не в списке нарочно: у него бывает и так, и так. Сформировали родной
# отложкой через веб — держит сам МАКС; заданием боту (запасной путь, если
# нет ссылки на канал) — держит Click. Что именно вышло, помечает само
# формирование признаком «native» в памяти поста.
SELF_HOSTED = ("vk", "ok")

TITLE_LIMIT = 120          # длина превью текста в строке плана
HEAD_LIMIT = 58            # сколько букв уходит в жирное начало строки

# Порядок колонок площадок. Реестр может назвать любую — колонки строятся по
# тому, что в нём реально есть, но в этом порядке: он же в легенде и в справке.
NETWORK_ORDER = ("tg-client", "tg-staff", "tg", "vk", "ok", "max", "zen", "yb")

# Короткая подпись для шапки колонки: «ТГ клиенты» в 64 пикселя не влезает и
# обрезается на «ТГ клиен». Полное имя остаётся подсказкой при наведении.
NETWORK_SHORT = {"tg-client": "ТГ кл.", "tg-staff": "ТГ сотр.", "tg": "ТГ",
                 "vk": "ВК", "ok": "ОК", "max": "МАКС", "zen": "Дзен", "yb": "ЯБ"}


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 пост», «4 поста», «11 постов» — без этого подписи звучат по-роботски."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def human_day(d: date) -> str:
    """«17 августа» — для заголовков дня и панели поста."""
    return f"{d.day} {MONTHS_RU[d.month - 1]}"


def human_range(first: date, last: date) -> str:
    """«17 – 30 августа»; если месяцы разные — «25 августа – 7 сентября»."""
    if first.month == last.month:
        return f"{first.day} – {last.day} {MONTHS_RU[last.month - 1]}"
    return f"{human_day(first)} – {human_day(last)}"


def relative_day(d: date, today: date) -> str:
    """Подпись у числа: «сегодня», «завтра», «через 3 дня», «был вчера»."""
    delta = (d - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == -1:
        return "вчера"
    if 2 <= delta <= 6:
        # «через 2 дня», «через 5 дней» — по-русски, без «дн.»
        tail = "дня" if delta in (2, 3, 4) else "дней"
        return f"через {delta} {tail}"
    return ""


def net_view(post: dict, target: dict, state: dict) -> dict:
    """
    Одна площадка поста: {code, name, mark, cls, note, link}.

    `note` — человеческая подпись для панели поста: не значок, а слова. Значок
    без подписи люди читают как шифр, поэтому подпись есть всегда.
    """
    net = target.get("network") or ""
    view = {"code": net, "name": cps.network_ru(net), "link": ""}

    if net not in content_plan.SUPPORTED:
        # Дзен и прочее Click не формирует. Честно говорим «вручную», а не
        # рисуем пустой кружок, будто просто ещё не дошли руки.
        return {**view, "mark": "⏸", "cls": "off", "note": "публикуется вручную"}

    link = (target.get("published_link") or "").strip()
    if link:
        # Реестр — тоже источник правды: стоит ссылка, значит пост вышел,
        # даже если публиковали руками мимо Click.
        return {**view, "mark": "✓", "cls": "live", "note": "вышло", "link": link}

    status = cps.status_of(state, post, net)
    saved = cps.target(state, post, net)
    view["link"] = (saved.get("link") or "").strip()

    if status in (cps.SENT, cps.SENT_LATE):
        note = "вышло с опозданием" if status == cps.SENT_LATE else "вышло"
        return {**view, "mark": "✓", "cls": "live", "note": note}
    if status == cps.SCHEDULED:
        # «native» проставляет формирование родной отложкой. У МАКС одно и то
        # же состояние значит разное: сформировали через веб – держит сам
        # МАКС; через бота – держит Click, и он должен быть открыт.
        if net in SELF_HOSTED or saved.get("native"):
            return {**view, "mark": "✓", "cls": "set", "note": "отложка стоит в соцсети"}
        return {**view, "mark": "⏱", "cls": "wait", "note": "отправит Click в час выхода"}
    if status == cps.FAILED:
        return {**view, "mark": "✕", "cls": "err",
                "note": saved.get("error") or "ошибка — пост туда не ушёл"}
    if status == cps.MISSED:
        return {**view, "mark": "⏭", "cls": "err", "note": "пропущено — время вышло"}
    return {**view, "mark": "·", "cls": "off", "note": "ещё не сформировано"}


def post_view(post: dict, state: dict) -> dict:
    """
    Плитка поста для дня: время, тип, превью текста, площадки и общее
    состояние строки (по нему красится левая полоска плитки).
    """
    nets = [net_view(post, t, state) for t in post.get("targets", [])]
    classes = [n["cls"] for n in nets]
    text = " ".join(post_text.strip_markup(post.get("text") or "").split())
    title = text[:TITLE_LIMIT] + "…" if len(text) > TITLE_LIMIT else text

    fmt = (post.get("format") or "Пост").strip()
    if fmt.lower() != "пост":
        # Видео и статьи Click не формирует — это не поломка, а другой вид
        # контента. Плитка так и говорит, вместо жёлтой тревоги.
        state_cls, trouble = "manual", f"{fmt.lower()} — вручную"
    elif not text:
        state_cls, trouble = "warn", "нет текста — пост не выйдет"
    elif "err" in classes:
        state_cls, trouble = "err", "ошибка на площадке"
    elif classes and all(c == "live" for c in classes):
        state_cls, trouble = "live", "вышло"
    elif classes and all(c in ("live", "set") for c in classes):
        state_cls, trouble = "set", "отложки стоят"
    elif "off" in classes and not any(c in ("set", "wait") for c in classes):
        state_cls, trouble = "todo", "ещё не сформировано"
    else:
        state_cls, trouble = "wait", "выйдет по расписанию"

    return {
        "post": post,
        "key": cps.post_key(post),
        "date": post.get("date", ""),
        "time": post.get("time", ""),
        "kind": (post.get("post_type") or "").strip(),
        "title": title,
        "full_text": text,
        "photos": len(post.get("images") or []),
        "nets": nets,
        "state": state_cls,
        "note": trouble,
        "row": post.get("row", 0),
    }


def _split_text(text: str) -> tuple[str, str]:
    """
    Разбить текст поста на жирное начало и продолжение.

    В реестре текст — сплошной кусок, заголовка отдельно нет. Первое
    предложение (или первые слова) работает заголовком: по нему пост узнают,
    а хвост поясняет. Если текст — ссылка, вся строка уходит в «заголовок»:
    рвать URL по слову бессмысленно.
    """
    text = " ".join((text or "").split())
    if not text:
        return "", ""
    if text.startswith("http"):
        return text[:TITLE_LIMIT] + ("…" if len(text) > TITLE_LIMIT else ""), ""
    # Естественный разрыв: конец предложения, двоеточие или тире. Если его нет,
    # строку не режем посреди мысли – пусть будет одна, CSS обрежет многоточием.
    cut = -1
    for mark in (". ", "! ", "? ", ": ", " – ", " — "):
        pos = text.find(mark, 8, HEAD_LIMIT + 24)
        if pos > 0 and (cut < 0 or pos < cut):
            cut = pos + (1 if mark in (". ", "! ", "? ", ": ") else 0)
    if cut < 0:
        return (text[:TITLE_LIMIT] + "…" if len(text) > TITLE_LIMIT else text), ""
    head, tail = text[:cut].strip(" –—"), text[cut:].strip(" –—")
    if len(tail) > TITLE_LIMIT:
        tail = tail[:TITLE_LIMIT].rstrip() + "…"
    return head, tail


def columns(views: list[dict]) -> list[dict]:
    """
    Колонки площадок — по тому, что реально стоит в реестре у показанных постов.

    Жёсткий набор «ТГ/ВК/ОК/МАКС» врал в обе стороны: у бренда с Дзеном
    колонки для него не было вовсе, а у бренда без ОК висел пустой столбец.
    """
    seen: dict[str, str] = {}
    for view in views:
        for net in view["nets"]:
            seen.setdefault(net["code"], net["name"])
    order = {code: i for i, code in enumerate(NETWORK_ORDER)}
    return [{"code": code, "name": name, "short": NETWORK_SHORT.get(code, name)}
            for code, name in sorted(seen.items(), key=lambda kv: (order.get(kv[0], 99), kv[0]))]


def todo_of(view: dict) -> dict:
    """
    Колонка «что сделать»: одна короткая фраза на строку.

    Правило простое: если делать нечего — так и говорим тихим серым «всё
    готово». Кнопки и ссылки появляются только там, где человеку правда
    нужно вмешаться, поэтому взгляд идёт по этой колонке и находит работу.
    """
    if view["state"] == "manual":
        return {"text": view["note"], "kind": "quiet"}
    if view["state"] == "warn":
        where = f'строка {view["row"]} листа' if view["row"] else "строка листа"
        return {"text": f"Нет текста — {where} пустая", "kind": "fix", "sheet": True}
    if view["state"] == "err":
        bad = next((n for n in view["nets"] if n["cls"] == "err"), None)
        return {"text": f'{bad["name"]}: {bad["note"]}' if bad else "Ошибка",
                "kind": "err"}
    if view["state"] == "live":
        return {"text": "вышло", "kind": "quiet"}
    if view["state"] == "todo":
        # Не кричим в каждой строке «нажмите «Сформировать план»»: кнопка одна
        # и она наверху. Строка просто говорит, в каком она состоянии.
        return {"text": "ещё не сформировано", "kind": "quiet"}
    off = [n["name"] for n in view["nets"] if n["cls"] == "off"]
    if off:
        return {"text": f'{", ".join(off)} — не выбрано в таблице', "kind": "quiet"}
    return {"text": "всё готово", "kind": "quiet"}


def rows(posts: list[dict], state: dict, today: date,
         horizon: date | None = None) -> dict:
    """
    Строки плана: {rows, columns, first, last, title}.

    Берём посты от сегодня до горизонта включительно, сортируем по времени —
    вперемешку они и раньше сбивали с толку («26.08 стоит выше 18.08»).
    """
    views = []
    for post in posts:
        d = content_plan.parse_date(post.get("date", ""))
        if d is None or d < today or (horizon and d > horizon):
            continue
        view = post_view(post, state)
        head, tail = _split_text(view["full_text"])
        view.update({
            "day": d,
            "head": head,
            "tail": tail,
            "when_day": f"{d.strftime('%d.%m')}",
            "when_note": ", ".join(x for x in (relative_day(d, today),
                                               WEEKDAYS_RU[d.weekday()]) if x),
            "todo": None,
        })
        views.append(view)

    views.sort(key=lambda v: (v["day"], v["time"], v["head"]))
    for view in views:
        view["todo"] = todo_of(view)

    first = views[0]["day"] if views else today
    last = views[-1]["day"] if views else (horizon or today)
    return {
        "rows": views,
        "columns": columns(views),
        "first": first,
        "last": last,
        "title": human_range(first, last) if views else "постов впереди нет",
    }


def next_out(posts: list[dict], state: dict, now_iso: str) -> dict | None:
    """
    Ближайший пост, которому ещё предстоит выйти: для строки состояния
    («ближайший пост — завтра в 09:00, Телеграм и МАКС»). Уже вышедшие
    и целиком сломанные не считаются: ждать от них нечего.
    """
    best = None
    for p in posts:
        when = (p.get("when") or "").strip()
        if not when or when <= now_iso:
            continue
        view = post_view(p, state)
        pending = [n for n in view["nets"] if n["cls"] in ("set", "wait")]
        if not pending:
            continue
        if best is None or when < best["when"]:
            best = {"when": when, "view": view, "pending": pending}
    return best
