"""
crosspost_form.py – формирование: посты реестра → отложки и задания.

Здесь то, что не зависит от конкретной площадки: какие посты пора
формировать, каким текстом, с какими картинками, и как записать результат
в память (crosspost_state). Сами площадки – в своих модулях (vk_social и
далее); этот слой зовёт их по одному посту и честно записывает исход.

Важное открытие по текстам (реестр СМУ, 2026-08-10): в реестре лежат
ГОТОВЫЕ посты – с контактами, сайтом и хэштегами внутри. Никакого
«соцокончания» поверх не нужно: тело поста и есть весь пост. Добавляется
только автоссылка «нашем сайте» → сайт бренда (post_text.autolink) и
рендер под площадку.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

import apptime
import content_plan
import crosspost_state as cps
import post_text

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

# За сколько минут до выхода отложку ставить ещё имеет смысл. ВК и ОК не дают
# планировать «на сейчас», да и толку в отложке за минуту до срока никакого.
MIN_LEAD_MINUTES = 10


# ─── Что пора формировать ───────────────────────────────────────────
def pending_for(posts: list[dict], state: dict, network: str,
                today=None) -> list[dict]:
    """
    Посты, у которых цель `network` ещё не сформирована: дата впереди,
    «Ссылка» в реестре пуста И в памяти нет «отложка стоит/вышло».
    posts_to_form уже отфильтровал реестровую часть – здесь добавляем память.
    """
    out = []
    for p in content_plan.posts_to_form(posts, today=today):
        nets = {t["network"] for t in p["targets"]}
        if network in nets and not cps.is_done(state, p, network):
            out.append(p)
    return out


# ─── Текст и время ──────────────────────────────────────────────────
def social_markup(post: dict, site: str) -> str:
    """Разметка поста для соцсетей: готовый текст реестра + автоссылка на сайт."""
    return post_text.autolink(post.get("text") or "", site or "")


def when_local(post: dict) -> datetime:
    """Время выхода поста – datetime с поясом Екатеринбурга."""
    dt = datetime.fromisoformat(post["when"])
    return dt if dt.tzinfo else dt.replace(tzinfo=apptime.TZ)


def form_messengers(project_id: str, posts: list[dict], site: str,
                    channels: dict[str, str],
                    progress: Callable[[str], None] | None = None) -> list[dict]:
    """
    Поставить задания планировщику для Телеграма и МАКС.

    channels: {"tg-client": "@канал", "tg-staff": "@канал", "max": "id чата"} –
    что не заполнено, то молча пропускается (эта площадка ещё не подключена).
    Задание идемпотентно по id (ключ поста + сеть): переформирование обновляет
    текст, но выполненное задание вторым разом не поедет.
    """
    import scheduler

    progress = progress or (lambda m: None)
    state = cps.load(project_id)
    results: list[dict] = []
    for network in ("tg-client", "tg-staff", "max"):
        chat = (channels.get(network) or "").strip()
        if not chat:
            continue
        for post in pending_for(posts, state, network):
            task = {
                "id": f"{cps.post_key(post)}|{network}",
                "project": project_id,
                "brand": post.get("brand", project_id),
                "network": network,
                "chatId": chat,
                "when": post["when"],
                "date": post["date"],
                "markup": social_markup(post, site),
                "sourceText": post.get("text", ""),
                "images": list(post.get("images") or []),
            }
            scheduler.queue_task(project_id, task)
            cps.set_status(project_id, post, network, cps.SCHEDULED,
                           extra={"textHash": cps.text_fingerprint(post.get("text", ""))})
            progress(f"{post['date']} → {cps.network_ru(network)}: задание поставлено")
            results.append({"post": post, "network": network, "ok": True})
    return results


# ─── Формирование браузерных площадок (ВК и ОК) на весь план ────────
def _form_browser_all(project_id: str, network: str, schedule_fn,
                      group_url: str, posts: list[dict], site: str,
                      progress: Callable[[str], None] | None = None,
                      headless: bool = True) -> list[dict]:
    """
    Общий цикл для площадок «вход + родная отложка» (ВК, ОК): по каждому
    несформированному посту – отложка через schedule_fn.

    Возвращает список исходов: {"post": …, "ok": bool, "error": "…"}.
    Каждый исход СРАЗУ пишется в память (crosspost_state) – оборвали на
    середине, повтор доформирует только оставшееся, без дублей.
    Картинка не скачалась – пост помечается ошибкой и НЕ публикуется без
    фото молча: в реестре фото стоит, значит пост без него неполный.
    """
    import yb_playwright as yb
    import paths

    progress = progress or (lambda m: None)
    ru = cps.network_ru(network)
    state = cps.load(project_id)
    todo = pending_for(posts, state, network)
    results: list[dict] = []
    temp = paths.data_root() / project_id / "temp"
    temp.mkdir(parents=True, exist_ok=True)

    for n, post in enumerate(todo, 1):
        label = f"{post['date']} ({n}/{len(todo)})"

        # Отложка в ПРОШЛОЕ невозможна: соцсеть такое время просто не примет.
        # Click этого не проверял и честно пытался – ВК подставлял вместо
        # 9:00 своё ближайшее время, и прогон падал с «ВК показывает 20:00».
        # Ловится это в первый же день: пост стоит на сегодня на 9 утра, а
        # формируют его вечером.
        late_by = apptime.now() - when_local(post)
        if late_by > timedelta(minutes=-MIN_LEAD_MINUTES):
            hours = late_by.total_seconds() / 3600
            err = (f"время выхода уже прошло ({post['date']} {post.get('time', '')}"
                   + (f", {hours:.0f} ч назад" if hours >= 1 else "") +
                   f") – {ru} отложку в прошлое не примет. Перенесите дату в "
                   "реестре или опубликуйте вручную")
            cps.set_status(project_id, post, network, cps.MISSED, error=err)
            progress(f"{label}: пропускаю – {err}")
            results.append({"post": post, "ok": False, "error": err})
            continue

        progress(f"{label}: готовлю текст и фото")
        markup = social_markup(post, site)
        # ОК умеет жирный в теме группы и набирает его сам (Ctrl+B), поэтому
        # ему отдаём разметку. ВК и МАКС жирного в посте не умеют – им
        # плоский текст, как и было.
        text = markup if network == "ok" else post_text.render(markup, "plain")

        local: list[str] = []
        if post.get("images"):
            local, errors = yb.fetch_photos(list(post["images"]), temp)
            if errors:
                err = f"картинка не скачалась: {'; '.join(errors)} – прикрепите файл в разделе"
                cps.set_status(project_id, post, network, cps.FAILED, error=err)
                results.append({"post": post, "ok": False, "error": err})
                continue

        progress(f"{label}: ставлю отложку в {ru}")
        res = schedule_fn(project_id, group_url, text, local, when_local(post),
                          log=progress, headless=headless)
        if res.get("ok"):
            # native – отложку держит сама площадка, Click в час выхода не
            # нужен. У МАКС это зависит от того, как формировали (родная
            # отложка или задание боту), поэтому помечаем сам исход, а не
            # площадку списком.
            cps.set_status(project_id, post, network, cps.SCHEDULED,
                           extra={"textHash": cps.text_fingerprint(post.get("text", "")),
                                  "native": True})
            results.append({"post": post, "ok": True})
        else:
            cps.set_status(project_id, post, network, cps.FAILED, error=res.get("error", ""))
            results.append({"post": post, "ok": False, "error": res.get("error", "")})
    return results


def form_vk_all(project_id: str, group_url: str, posts: list[dict], site: str,
                progress: Callable[[str], None] | None = None,
                headless: bool = True) -> list[dict]:
    import vk_social
    return _form_browser_all(project_id, "vk", vk_social.schedule_postponed_post,
                             group_url, posts, site, progress, headless)


def form_ok_all(project_id: str, group_url: str, posts: list[dict], site: str,
                progress: Callable[[str], None] | None = None,
                headless: bool = True) -> list[dict]:
    import ok_browser
    return _form_browser_all(project_id, "ok", ok_browser.schedule_postponed_post,
                             group_url, posts, site, progress, headless)


def form_zen_all(project_id: str, studio_url: str, posts: list[dict], site: str,
                 progress: Callable[[str], None] | None = None,
                 headless: bool = True, email: str = "") -> list[dict]:
    """
    Дзен – статьи с родной отложкой «Опубликовать позже».

    Почему не через общий `_form_browser_all`. У Дзена другой материал: не
    пост из ячейки, а СТАТЬЯ, текст которой лежит по ссылке на документ.
    Картинки реестра ему тоже не нужны – в статье свои иллюстрации, а
    таблицы Click рисует сам. Общий цикл пришлось бы обвешивать
    исключениями на каждом шаге, поэтому у Дзена свой – короткий и честный.

    Заголовок и разбор статьи делает zen_doc; ошибку разбора (документ не
    расшарен, ссылка битая) записываем в память поста словами, чтобы она
    попала в «Что требует внимания», а не потерялась в логе.
    """
    import zen_browser
    import zen_doc

    progress = progress or (lambda m: None)
    ru = cps.network_ru("zen")
    state = cps.load(project_id)
    todo = pending_for(posts, state, "zen")
    results: list[dict] = []

    for n, post in enumerate(todo, 1):
        label = f"{post['date']} ({n}/{len(todo)})"

        late_by = apptime.now() - when_local(post)
        if late_by > timedelta(minutes=-MIN_LEAD_MINUTES):
            err = (f"время выхода уже прошло ({post['date']} {post.get('time', '')}) – "
                   f"{ru} отложку в прошлое не примет. Перенесите дату в реестре "
                   "или опубликуйте статью вручную")
            cps.set_status(project_id, post, "zen", cps.MISSED, error=err)
            progress(f"{label}: пропускаю – {err}")
            results.append({"post": post, "ok": False, "error": err})
            continue

        progress(f"{label}: беру текст статьи")
        try:
            article = zen_doc.article_for(post)
        except Exception as e:  # noqa: BLE001 – один битый документ не валит прогон
            err = f"текст статьи не прочитался: {e}"
            cps.set_status(project_id, post, "zen", cps.FAILED, error=err)
            progress(f"{label}: {err}")
            results.append({"post": post, "ok": False, "error": err})
            continue

        info = zen_doc.counts(article)
        progress(f"{label}: «{article['title'][:60]}» – {info['chars']} знаков, "
                 f"{info['head']} подзаголовков, {info['table']} таблиц")
        progress(f"{label}: ставлю отложку в {ru}")
        res = zen_browser.schedule_article(project_id, studio_url, article,
                                           when_local(post), email=email,
                                           log=progress, headless=headless)
        if res.get("ok"):
            cps.set_status(project_id, post, "zen", cps.SCHEDULED,
                           extra={"textHash": cps.text_fingerprint(post.get("text", "")),
                                  "native": True,
                                  "title": article.get("title", ""),
                                  "warnings": res.get("warnings") or []})
            for w in res.get("warnings") or []:
                progress(f"{label}: ⚠️ {w}")
            results.append({"post": post, "ok": True, "warnings": res.get("warnings") or []})
        else:
            cps.set_status(project_id, post, "zen", cps.FAILED, error=res.get("error", ""))
            results.append({"post": post, "ok": False, "error": res.get("error", "")})
    return results


def form_max_all(project_id: str, chat_url: str, posts: list[dict], site: str,
                 progress: Callable[[str], None] | None = None,
                 headless: bool = True) -> list[dict]:
    """
    МАКС – теперь тоже родной отложкой, как ВК и ОК.

    До этого МАКС шёл заданием планировщику: бот МАКС отложки не умеет
    вовсе, и время держал Click – значит Click обязан был работать в час
    выхода. Через веб-версию пост держит сам МАКС, и Click в это время может
    быть выключен. Ради этого всё и затевалось.
    """
    import max_browser
    return _form_browser_all(project_id, "max", max_browser.schedule_postponed_post,
                             chat_url, posts, site, progress, headless)
