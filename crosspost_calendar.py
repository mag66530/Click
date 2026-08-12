"""
crosspost_calendar.py — план кросспостинга как календарь недель.

Зачем. Контент-план — это расписание, а расписание человек читает календарём:
видно ритм (где пусто, где два поста подряд), а не просто список одинаковых
строк. Здесь собирается «что рисовать»: недели, дни, плитки постов и знаки
площадок. Ни Streamlit, ни HTML — только данные, поэтому всё покрывается
обычными тестами (tests_crosspost.py).

Выходные. Суббота и воскресенье в сетку не входят: рабочая неделя из пяти
дней, а выходные показываются отдельной узкой колонкой и только тогда, когда
на них действительно стоит пост — обычно перед праздниками. Если за весь
показанный период выходных постов нет, колонки нет вовсе.

Знаки площадок (по ним же легенда в разделе):
    ✓ зелёный   — отложка стоит в соцсети (ВК и ОК держат сами)
    ⏱ синий     — отправит Click в час выхода (Телеграм, МАКС)
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

# Площадки, где отложку держит сама соцсеть. Для них «запланировано» значит
# «запись уже лежит в сообществе» — Click в час выхода не нужен. Для ТГ и МАКС
# то же состояние значит обратное: отправит Click, и он должен быть открыт.
SELF_HOSTED = ("vk", "ok")

TITLE_LIMIT = 76           # длина превью текста в плитке дня
DEFAULT_WEEKS = 2          # сколько недель показываем разом


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
        if net in SELF_HOSTED:
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


def _cell(d: date, today: date, posts_by_day: dict[date, list[dict]]) -> dict:
    return {
        "date": d,
        "num": str(d.day),
        "dow": WEEKDAYS_RU[d.weekday()],
        "is_today": d == today,
        "is_past": d < today,
        "tag": relative_day(d, today),
        "posts": posts_by_day.get(d, []),
    }


def build(posts: list[dict], state: dict, today: date,
          weeks: int = DEFAULT_WEEKS) -> dict:
    """
    Календарь на `weeks` недель начиная с понедельника текущей недели.

    Возвращает {weeks: [...], has_weekend: bool, first: date, last: date,
    title: «17 – 30 августа»}. Неделя — {days: [5 будних ячеек],
    weekend: [ячейки сб/вс с постами]}.
    """
    by_day: dict[date, list[dict]] = {}
    for p in posts:
        d = content_plan.parse_date(p.get("date", ""))
        if d is None:
            continue
        by_day.setdefault(d, []).append(post_view(p, state))
    for day_posts in by_day.values():
        day_posts.sort(key=lambda v: (v["time"], v["title"]))

    monday = today - timedelta(days=today.weekday())
    out_weeks = []
    has_weekend = False
    for w in range(max(1, weeks)):
        start = monday + timedelta(days=7 * w)
        days = [_cell(start + timedelta(days=i), today, by_day) for i in range(5)]
        weekend = [_cell(start + timedelta(days=i), today, by_day) for i in (5, 6)]
        with_posts = [c for c in weekend if c["posts"]]
        has_weekend = has_weekend or bool(with_posts)
        out_weeks.append({
            "days": days,
            "weekend": with_posts,
            # Подпись-заглушка в колонке выходных: «22 – 23 постов нет».
            "weekend_empty": f'{weekend[0]["num"]} – {weekend[1]["num"]}',
        })

    first = monday
    last = monday + timedelta(days=7 * max(1, weeks) - 1)
    return {
        "weeks": out_weeks,
        "has_weekend": has_weekend,
        "first": first,
        "last": last,
        # В заголовке — рабочий диапазон: если выходных в сетке нет, обещать
        # «по воскресенье» нечестно.
        "title": human_range(first, last if has_weekend else last - timedelta(days=2)),
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
