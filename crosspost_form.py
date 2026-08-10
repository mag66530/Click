"""
crosspost_form.py — формирование: посты реестра → отложки и задания.

Здесь то, что не зависит от конкретной площадки: какие посты пора
формировать, каким текстом, с какими картинками, и как записать результат
в память (crosspost_state). Сами площадки — в своих модулях (vk_social и
далее); этот слой зовёт их по одному посту и честно записывает исход.

Важное открытие по текстам (реестр СМУ, 2026-08-10): в реестре лежат
ГОТОВЫЕ посты — с контактами, сайтом и хэштегами внутри. Никакого
«соцокончания» поверх не нужно: тело поста и есть весь пост. Добавляется
только автоссылка «нашем сайте» → сайт бренда (post_text.autolink) и
рендер под площадку.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

import apptime
import content_plan
import crosspost_state as cps
import post_text

# Метка сборки — одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401


# ─── Что пора формировать ───────────────────────────────────────────
def pending_for(posts: list[dict], state: dict, network: str,
                today=None) -> list[dict]:
    """
    Посты, у которых цель `network` ещё не сформирована: дата впереди,
    «Ссылка» в реестре пуста И в памяти нет «отложка стоит/вышло».
    posts_to_form уже отфильтровал реестровую часть — здесь добавляем память.
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
    """Время выхода поста — datetime с поясом Екатеринбурга."""
    dt = datetime.fromisoformat(post["when"])
    return dt if dt.tzinfo else dt.replace(tzinfo=apptime.TZ)


# ─── Формирование ВК на весь план ───────────────────────────────────
def form_vk_all(project_id: str, group_url: str, posts: list[dict], site: str,
                progress: Callable[[str], None] | None = None,
                headless: bool = True) -> list[dict]:
    """
    Пройти по всем несформированным ВК-целям и поставить родные отложки.

    Возвращает список исходов: {"post": …, "ok": bool, "error": "…"}.
    Каждый исход СРАЗУ пишется в память (crosspost_state) — оборвали на
    середине, повтор доформирует только оставшееся, без дублей.
    Картинка не скачалась — пост помечается ошибкой и НЕ публикуется без
    фото молча: в реестре фото стоит, значит пост без него неполный.
    """
    import vk_social
    import yb_playwright as yb
    import paths

    progress = progress or (lambda m: None)
    state = cps.load(project_id)
    todo = pending_for(posts, state, "vk")
    results: list[dict] = []
    temp = paths.data_root() / project_id / "temp"
    temp.mkdir(parents=True, exist_ok=True)

    for n, post in enumerate(todo, 1):
        label = f"{post['date']} ({n}/{len(todo)})"
        progress(f"{label}: готовлю текст и фото")
        text = post_text.render(social_markup(post, site), "plain")

        local: list[str] = []
        if post.get("images"):
            local, errors = yb.fetch_photos(list(post["images"]), temp)
            if errors:
                err = f"картинка не скачалась: {'; '.join(errors)} — прикрепите файл в разделе"
                cps.set_status(project_id, post, "vk", cps.FAILED, error=err)
                results.append({"post": post, "ok": False, "error": err})
                continue

        progress(f"{label}: ставлю отложку в ВК")
        res = vk_social.schedule_postponed_post(
            project_id, group_url, text, local, when_local(post),
            log=progress, headless=headless)
        if res.get("ok"):
            cps.set_status(project_id, post, "vk", cps.SCHEDULED,
                           extra={"textHash": cps.text_fingerprint(post.get("text", ""))})
            results.append({"post": post, "ok": True})
        else:
            cps.set_status(project_id, post, "vk", cps.FAILED, error=res.get("error", ""))
            results.append({"post": post, "ok": False, "error": res.get("error", "")})
    return results
