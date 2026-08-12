"""
max_browser.py – МАКС: вход с сохранением сессии и РОДНАЯ отложка поста.

Зачем браузер, если есть бот. У бота МАКС отложки нет вовсе (см.
max_social.py: проверено по документации). Бот шлёт «сейчас», а время
держит планировщик Click – значит Click обязан работать в час выхода.
Через веб-версию отложка родная: пост держит и публикует сам МАКС, а
Click в это время может быть выключен. Ради этого и модуль.

Как это делает человек – со слов заказчицы (12.08.2026), её же порядком:
  1. открыть канал: https://web.max.ru/-70916890460398
  2. вписать текст в поле «Пост»
  3. картинку – скрепкой → «Фото или видео» → обычный выбор файла
  4. ПРАВОЙ кнопкой по кружку со стрелкой → «Запланировать пост»
     (левой нельзя: пост уйдёт сразу)
  5. выбрать дату, ниже – время
  6. нажать «Отправить завтра в 13:00»
  7. открывается страница «Запланированные посты», пост там

Селекторы – из разметки, снятой заказчицей с живой страницы. Классы у МАКС
свои у каждой сборки (svelte-1k31az8 и подобные), поэтому держимся за то,
что устойчиво: role, aria-label, data-lexical-editor, contenteditable.

Правило то же, что у ВК и ОК: успех считаем по ответу площадки, а не по
тому, что «клик прошёл».
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

import paths
import yb_playwright as yb

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

BASE = "https://web.max.ru"
TIMEZONE_ID = "Asia/Yekaterinburg"

SEL = {
    # Поле поста. Держимся за role и data-lexical-editor: класс svelte-…
    # меняется у МАКС от сборки к сборке, это их обычная практика.
    "text": ('div[contenteditable][role="textbox"][data-lexical-editor="true"]',
             'div[contenteditable][role="textbox"]',
             '[aria-placeholder="Пост"]', '[placeholder="Пост"]'),
    # Скрепка и пункт «Фото или видео» под ней.
    "attach": ('button[aria-label*="рикреп"]', 'button:has(svg.shape)',
               '[aria-label*="Прикрепить"]'),
    "attach_photo": ('text="Фото или видео"', 'text="Фото"'),
    "file_input": 'input[type="file"]',
    # Кружок со стрелкой. ЛЕВОЙ кнопкой не жмём никогда – пост уйдёт сразу.
    "send": ('button[aria-label="Отправить сообщение"]',
             'button[aria-label*="Отправить сообщени"]'),
    "schedule_item": ('text="Запланировать пост"', 'text="Запланировать"'),
    # Окно «Запланировать пост»: календарь и время.
    "dialog_mark": ('text="Запланировать пост"',),
    "hours": ('[role="spinbutton"][aria-label="Часы"]',
              '.timePicker [role="spinbutton"]:first-child'),
    "mins": ('[role="spinbutton"][aria-label="Минуты"]',
             '.timePicker [role="spinbutton"]:last-child'),
    "confirm": ('button:has-text("Отправить")',),
    # Куда МАКС уводит после успеха.
    "scheduled_page": ("Запланированные посты",),
}

# Куки, которые МАКС отдаёт кому угодно. Признак входа определяем от
# обратного – тем же способом, что и у ОК: угадывать имена бесполезно, их
# меняют без предупреждения, а список гостевых меняется куда реже.
GUEST_COOKIES = frozenset({"_ym_uid", "_ym_d", "_ym_isad", "_ga", "_gid",
                           "tmr_lvid", "tmr_lvidTS", "cookieChoice"})


def session_path(project_id: str) -> Path:
    d = paths.data_root() / project_id / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d / "max-state.json"


def cookie_names(cookies: list) -> list[str]:
    return sorted({str(c.get("name", "")) for c in cookies or [] if c.get("value")})


def looks_logged_in(state: dict) -> bool:
    """
    Похоже ли, что в файле сессия вошедшего.

    У МАКС это ОСОБЫЙ случай: веб-версия – одностраничное приложение, и
    вход хранится не только в куках, но и в localStorage. Playwright
    сохраняет его в разделе origins – туда и смотрим, иначе живая сессия
    выглядела бы пустой.
    """
    cookies = state.get("cookies") or []
    if set(cookie_names(cookies)) - GUEST_COOKIES:
        return True
    for origin in state.get("origins") or []:
        if "max.ru" in str(origin.get("origin", "")) and origin.get("localStorage"):
            return True
    return False


def has_saved_session(project_id: str) -> bool:
    fp = session_path(project_id)
    if not fp.exists():
        return False
    try:
        import json
        return looks_logged_in(json.loads(fp.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return False


def import_session(project_id: str, raw: bytes) -> tuple[bool, str]:
    """Принять готовый файл сессии МАКС (storage_state Playwright)."""
    import json

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"Это не файл сессии: {e}"
    if not isinstance(data, dict) or "cookies" not in data:
        return False, "В файле нет раздела cookies – нужен storage_state Playwright."
    if not looks_logged_in(data):
        found = ", ".join(cookie_names(data.get("cookies") or [])[:12]) or "ни одной"
        return False, ("В файле нет признаков входа в МАКС – похоже, он снят у "
                       f"гостя. Что нашли: {found}. Войдите в МАКС "
                       "(в окне из VHOD-VK-i-OK.py) и сохраните сессию заново.")
    session_path(project_id).write_text(json.dumps(data, ensure_ascii=False),
                                        encoding="utf-8")
    return True, f"Сессия МАКС принята: {len(data.get('cookies') or [])} куки."


def _debug_shot(project_id: str, page, name: str) -> bytes | None:
    """Снимок в момент неудачи – картинкой, чтобы показать её человеку."""
    try:
        blob = page.screenshot(type="png", full_page=False)
    except Exception:  # noqa: BLE001
        return None
    try:
        d = paths.data_root() / project_id / "crosspost"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"max-debug-{name}.png").write_bytes(blob)
    except OSError:
        pass
    return blob


def _first_visible(page, candidates) -> str:
    """Первый кандидат, который ВИДЕН. Пусто – ни одного."""
    if isinstance(candidates, str):
        candidates = (candidates,)
    for sel in candidates:
        try:
            if page.locator(sel).first.is_visible(timeout=900):
                return sel
        except Exception:  # noqa: BLE001
            continue
    return ""


def _click_first(page, candidates, timeout: int = 8_000) -> str:
    if isinstance(candidates, str):
        candidates = (candidates,)
    for sel in candidates:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=timeout)
                return sel
        except Exception:  # noqa: BLE001
            continue
    return ""


def _set_spin(page, selector: str, value: int) -> bool:
    """
    Вписать число в поле-крутилку МАКС (часы или минуты).

    Это не <input>, а contenteditable со role="spinbutton": обычным
    заполнением его не взять. Выделяем содержимое и печатаем, потом
    сверяем, что число встало.
    """
    want = f"{value:02d}"
    try:
        el = page.locator(selector).first
        el.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(want, delay=60)
        page.wait_for_timeout(300)
        shown = (el.inner_text() or "").strip()
        digits = "".join(ch for ch in shown if ch.isdigit())
        if digits and int(digits) == value:
            return True
        # Не поддалось печатью – пробуем стрелками от текущего значения.
        now = int(digits or 0)
        key = "ArrowUp" if value > now else "ArrowDown"
        for _ in range(abs(value - now)):
            page.keyboard.press(key)
            page.wait_for_timeout(40)
        shown = (el.inner_text() or "").strip()
        digits = "".join(ch for ch in shown if ch.isdigit())
        return bool(digits) and int(digits) == value
    except Exception:  # noqa: BLE001
        return False


def _pick_day(page, when: datetime) -> bool:
    """Выбрать число в календаре окна «Запланировать пост»."""
    try:
        return bool(page.evaluate(
            """(day) => {
                const wanted = String(day);
                const nodes = Array.from(document.querySelectorAll(
                    '[role="dialog"] button, [role="dialog"] td, [role="dialog"] div'));
                for (const el of nodes) {
                    if (el.children.length) continue;
                    if ((el.textContent || '').trim() !== wanted) continue;
                    if (el.getAttribute('aria-disabled') === 'true') continue;
                    (el.closest('button, td') || el).click();
                    return true;
                }
                return false;
            }""", when.day))
    except Exception:  # noqa: BLE001
        return False


def schedule_postponed_post(project_id: str, chat_url: str, text: str,
                            image_paths: list[str], when: datetime,
                            log: Callable[[str], None] | None = None,
                            headless: bool = True) -> dict:
    """
    Создать одну отложенную публикацию в канале МАКС.
    {"ok": True} либо {"ok": False, "error": "…", "shot": …}.
    """
    log = log or (lambda m: None)
    if not has_saved_session(project_id):
        return {"ok": False, "error": "Нет сессии МАКС – войдите в «Настройках»"}
    if not chat_url:
        return {"ok": False, "error": "Не указана ссылка на канал МАКС (web.max.ru/…)"}

    from playwright.sync_api import sync_playwright

    engine = yb.resolve_engine()
    with sync_playwright() as pw:
        import vk_social as _vk
        browser = yb._launch(pw, engine, headless=headless, extra_args=_vk.ANTIBOT_ARGS)
        page = None
        try:
            context = browser.new_context(
                storage_state=str(session_path(project_id)),
                viewport={"width": 1280, "height": 900}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            context.add_init_script(_vk.ANTIBOT_INIT)
            page = context.new_page()

            log(f"Открываю канал: {chat_url}")
            page.goto(chat_url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(4_000)          # SPA рисуется не мгновенно

            text_sel = _first_visible(page, SEL["text"])
            if not text_sel:
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "no-editor"),
                        "error": "Не нашли поле «Пост» – либо сессия МАКС не "
                                 "действует, либо ссылка ведёт не в канал"}

            log(f"Ввожу текст ({len(text)} знаков)")
            page.click(text_sel)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.type(text_sel, text, delay=8)
            page.wait_for_timeout(1_000)

            if image_paths:
                log(f"Прикрепляю фото: {len(image_paths)}")
                if not _click_first(page, SEL["attach"], timeout=6_000):
                    return {"ok": False,
                            "shot": _debug_shot(project_id, page, "no-attach"),
                            "error": "Не нашли скрепку для прикрепления фото"}
                page.wait_for_timeout(700)
                try:
                    with page.expect_file_chooser(timeout=6_000) as picked:
                        _click_first(page, SEL["attach_photo"], timeout=5_000)
                    picked.value.set_files(image_paths)
                except Exception:  # noqa: BLE001 – пробуем скрытое поле
                    inp = page.locator(SEL["file_input"])
                    if not inp.count():
                        return {"ok": False,
                                "shot": _debug_shot(project_id, page, "no-file-input"),
                                "error": "Не нашли, куда отдать файлы фото в МАКС"}
                    inp.first.set_input_files(image_paths)
                page.wait_for_timeout(3_500)

            # ПРАВОЙ кнопкой: левая отправит пост сейчас же. Это главное
            # место всей механики, и ошибиться тут нельзя.
            log("Открываю «Запланировать пост» (правой кнопкой по отправке)")
            send_sel = _first_visible(page, SEL["send"])
            if not send_sel:
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "no-send"),
                        "error": "Не нашли кнопку отправки в МАКС"}
            page.locator(send_sel).first.click(button="right")
            page.wait_for_timeout(900)
            if not _click_first(page, SEL["schedule_item"], timeout=6_000):
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "no-schedule-item"),
                        "error": "Меню «Запланировать пост» не появилось. Пост НЕ "
                                 "отправлен – левой кнопкой мы не жали"}
            page.wait_for_timeout(1_200)

            log(f"Ставлю {when.strftime('%d.%m.%Y %H:%M')} (Екатеринбург)")
            if not _pick_day(page, when):
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "no-day"),
                        "error": f"В календаре МАКС не нашли число {when.day}"}
            page.wait_for_timeout(600)

            for what, key, value in (("часы", "hours", when.hour),
                                     ("минуты", "mins", when.minute)):
                sel = _first_visible(page, SEL[key])
                if not sel:
                    return {"ok": False,
                            "shot": _debug_shot(project_id, page, "no-time"),
                            "error": f"Не нашли поле «{what}» в окне отложки МАКС"}
                if not _set_spin(page, sel, value):
                    return {"ok": False,
                            "shot": _debug_shot(project_id, page, "bad-time"),
                            "error": f"МАКС не принял {what} «{value:02d}»"}

            log("Подтверждаю отложку")
            if not _click_first(page, SEL["confirm"], timeout=8_000):
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "no-confirm"),
                        "error": "Не нашли кнопку «Отправить …» в окне отложки"}

            # Ответ площадки: МАКС уводит на «Запланированные посты».
            # Ждём не торопясь – тот же урок, что и на ОК.
            for _ in range(30):
                try:
                    body = page.evaluate(
                        "() => document.body ? (document.body.innerText || '') : ''") or ""
                except Exception:  # noqa: BLE001
                    body = ""
                if any(mark in body for mark in SEL["scheduled_page"]):
                    log("МАКС подтвердил: открылись «Запланированные посты»")
                    yb._save_storage_state(context, session_path(project_id))
                    return {"ok": True}
                page.wait_for_timeout(500)

            return {"ok": False,
                    "shot": _debug_shot(project_id, page, "no-confirmation"),
                    "error": "МАКС не подтвердил отложку: страница «Запланированные "
                             "посты» не открылась. Загляните туда сами – если пост "
                             "там есть, формировать заново не нужно"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e),
                    "shot": _debug_shot(project_id, page, "error") if page else None}
        finally:
            browser.close()
