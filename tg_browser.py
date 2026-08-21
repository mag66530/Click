"""
tg_browser.py – Телеграм: вход с сохранением сессии и РОДНАЯ отложка поста
через веб-версию (web.telegram.org/a/, «Web A»).

Зачем браузер, если есть бот (tg_social.py). У бота Телеграма отложки нет
вовсе: бот шлёт «сейчас», а время держит планировщик Click – значит Click
обязан работать в час выхода, и пост уходил не в том виде (13.08.2026,
из-за этого публикацию ботом и выключили). Через веб-версию под аккаунтом
отложка РОДНАЯ: пост держит и публикует сам Телеграм, а Click в это время
может быть выключен. Ровно как у ВК, ОК, МАКС и Дзена – их родные отложки
Click ставит браузером и в час выхода не притрагивается.

Как это делает человек (веб-версия /a/, со слов заказчицы 19.08.2026):
  1. открыть web.telegram.org/a/, папка «Соц сети» → нужный канал
  2. вписать текст в поле поста, выделить жирным, вставить анкор
  3. картинку – скрепкой, лишнюю карточку ссылки убрать крестиком
  4. ПРАВОЙ кнопкой по кружку «Отправить» → «Отправить позже»
     (левой нельзя: пост уйдёт сразу)
  5. в календаре выбрать день, затем часы и минуты
  6. нажать «Отправить <дата> в ЧЧ:ММ» / «Отправить сегодня в ЧЧ:ММ»
  7. Телеграм уводит в «Отложенные сообщения» – пост там

Селекторы – из исходников Web A (проект telegram-tt): там классы читаемые
и устойчивые (`.CalendarModal`, `.calendar-grid .day-button`, `.timepicker`,
`#editable-message-text`), не хешированные. Всё равно держимся за role,
id и текст, а не за оформление: у сборки может смениться что угодно.

Правило то же, что у ВК/ОК/МАКС: успех считаем по ответу площадки (окно
отложки закрылось, открылись «Отложенные»), а не по тому, что «клик прошёл».
И главное правило поля чата: НИ ОДНОЙ клавиши, которая может отправить, –
текст вставляем одним insert_text, переносы строк не печатаем.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urlparse

import paths
import yb_playwright as yb

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

BASE = "https://web.telegram.org"
APP = f"{BASE}/a/"                       # «Web A»: заказчица входит именно сюда
TIMEZONE_ID = "Asia/Yekaterinburg"

SEL = {
    # Поле поста – contenteditable. У Web A устойчивый id, за него и держимся.
    "text": ("#editable-message-text",
             'div[contenteditable="true"][role="textbox"]',
             '.message-input-wrapper [contenteditable="true"]',
             '.Composer [contenteditable="true"]'),
    # Кружок «Отправить». ЛЕВОЙ кнопкой не жмём никогда – пост уйдёт сразу.
    # ПЕРВЫМ идёт кнопка отправки из окна предпросмотра фото («Send Photo»):
    # после вложения картинки обычная button.send перекрыта этим окном, а
    # отложку ставим правым кликом именно по кнопке окна.
    "send": (".simple-message-input-confirm",
             ".AttachmentModal button.send", ".AttachmentModal .Button.send",
             'button[aria-label="Send Photo"]', 'button[aria-label*="Отправить фото"]',
             "button.send", ".Composer button.send", ".send-button",
             'button[aria-label*="Отправить"]', 'button[aria-label*="Send"]'),
    # Пункт меню под правым кликом по «Отправить». .MenuItem – Web A,
    # .btn-menu-item – окно предпросмотра фото (Web K-разметка).
    "schedule_item": ('.MenuItem:has-text("Отправить позже")',
                      '[role="menuitem"]:has-text("Отправить позже")',
                      '.btn-menu-item:has-text("Отправить позже")',
                      '.btn-menu-item:has-text("Запланировать")',
                      'text="Отправить позже"',
                      '.MenuItem:has-text("Schedule")',
                      '.btn-menu-item:has-text("Schedule")',
                      'text="Schedule Message"',
                      'text="Schedule"'),
    # Скрепка вложений Web A. Первым — СТАБИЛЬНЫЙ id (#attach-menu-button,
    # подтверждён дампом 21.08.2026): по нему кнопка берётся всегда, а прежний
    # отбор по классу иногда не срабатывал и клик уходил на соседнюю кнопку
    # (прогон 20:22: взял button:has(.icon-attach) — и окно выбора не открылось).
    "attach": ('#attach-menu-button', 'button#attach-menu-button',
               'button[aria-label="Add an attachment"]',
               'button.AttachMenu--button', '.AttachMenu--button',
               '.Composer button[aria-label*="Attach"]',
               'button[aria-label*="Attach"]', 'button[aria-label*="Прикрепить"]',
               'button:has(.icon-attach)',
               'button:has(use[href="#icon-attach"])'),
    "file_input": 'input[type="file"]',
    # Окно календаря отложки Web A.
    "modal": (".CalendarModal",),
    "modal_header": (".CalendarModal h4", ".CalendarModal .modal-header"),
    "month_prev": (".CalendarModal button:has(.icon-previous)",),
    "month_next": (".CalendarModal button:has(.icon-next)",),
    "time_inputs": (".CalendarModal .timepicker input",
                    ".CalendarModal input.form-control"),
    # Куда Телеграм уводит после успеха.
    "scheduled_page": ("Отложенные сообщения", "Отложенные", "Scheduled"),
}

# Экран входа/QR – по нему видно, что сессия не действует.
LOGIN_MARKS = ("Log in by QR", "Log in by phone", "Войти по номеру",
               "Отсканируйте", "Scan the QR", "QR-код", "Открыть Telegram",
               "Please scan")
# Аккаунт не в канале (для админа канала не появляется, но пусть будет).
JOIN_MARKS = ("Вступить", "Join Channel", "Подписаться", "Join Group")

# Куки, которые web.telegram.org отдаёт кому угодно. Признак входа определяем
# от обратного – как у ОК и МАКС: угадывать имена бесполезно, их меняют, а
# вход у Web A всё равно живёт в localStorage, а не в куках.
GUEST_COOKIES = frozenset({"stel_ln", "stel_dt"})


def parse_proxy(raw: str) -> dict | None:
    """
    Строка прокси из «Настроек» → настройки Playwright ({"server", "username",
    "password"}) или None, если пусто/непонятно.

    Телеграм у части провайдеров заблокирован (web.telegram.org не
    открывается вовсе – ERR_CONNECTION_TIMED_OUT), и до него нужно ходить
    через прокси. Принимаем привычные форматы:
      socks5://user:pass@1.2.3.4:1080
      http://1.2.3.4:8080
      1.2.3.4:1080            (схему достроим до socks5)
    Логин/пароль Playwright требует отдельно от адреса, поэтому вытаскиваем их.
    """
    s = (raw or "").strip()
    if not s:
        return None
    if "://" not in s:
        s = "socks5://" + s
    try:
        u = urlparse(s)
    except Exception:  # noqa: BLE001
        return None
    if not u.hostname or not u.port:
        return None
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = unquote(u.username)
    if u.password:
        cfg["password"] = unquote(u.password)
    return cfg


def shared_proxy_raw() -> str:
    """
    Прокси Телеграма из ОБЩЕГО хранилища команды (ветка click-data через
    repo_store, файл app-data/tg-proxy.json). Так значение подхватывается у
    всех, но НЕ лежит в исходниках и в рассылаемых zip-архивах.
    """
    try:
        import repo_store
        data = repo_store.load("tg-proxy") or {}
        return (data.get("tg_proxy") or "").strip()
    except Exception:  # noqa: BLE001 – нет токена/сети: просто не будет прокси
        return ""


def proxy_config() -> dict | None:
    """
    Прокси для Телеграма. Приоритет: своё значение на этой машине
    («Настройки» → «Телеграм: прокси»), иначе – общий для команды из
    хранилища click-data.
    """
    import secrets_local
    raw = secrets_local.get("tg_proxy") or shared_proxy_raw()
    return parse_proxy(raw)


def session_path(project_id: str) -> Path:
    d = paths.data_root() / project_id / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d / "tg-state.json"


def cookie_names(cookies: list) -> list[str]:
    return sorted({str(c.get("name", "")) for c in cookies or [] if c.get("value")})


def looks_logged_in(state: dict) -> bool:
    """
    Похоже ли, что в файле сессия вошедшего.

    У Web A вход, как и у МАКС, живёт в localStorage (ключи dcN_auth_key,
    user_auth и подобные), а не в куках. Playwright сохраняет localStorage в
    разделе origins – туда и смотрим, иначе живая сессия выглядела бы пустой.
    """
    cookies = state.get("cookies") or []
    if set(cookie_names(cookies)) - GUEST_COOKIES:
        return True
    for origin in state.get("origins") or []:
        if "web.telegram.org" in str(origin.get("origin", "")) and origin.get("localStorage"):
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
    """Принять готовый файл сессии Телеграма (storage_state Playwright)."""
    import json

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"Это не файл сессии: {e}"
    if not isinstance(data, dict) or "cookies" not in data:
        return False, "В файле нет раздела cookies – нужен storage_state Playwright."
    if not looks_logged_in(data):
        found = ", ".join(cookie_names(data.get("cookies") or [])[:12]) or "ни одной"
        return False, ("В файле нет признаков входа в Телеграм – похоже, он снят у "
                       f"гостя. Что нашли: {found}. Войдите в Телеграм по QR "
                       "(web.telegram.org/a/) в окне из VHOD-VK-OK-MAX-TG.py и "
                       "сохраните сессию заново.")
    session_path(project_id).write_text(json.dumps(data, ensure_ascii=False),
                                        encoding="utf-8")
    return True, f"Сессия Телеграма принята: {len(data.get('cookies') or [])} куки."


# ─── Адрес канала → чем его открыть в Web A ──────────────────────────
def canonical_tme(chat: str) -> str:
    """
    Значение из «Настроек» → ссылка t.me, которую понимает deep-link Web A.

    Принимаем всё, чем человек мог записать канал:
      @stalmetural            → https://t.me/stalmetural   (публичный)
      t.me/stalmetural        → https://t.me/stalmetural
      https://t.me/+HASH      → https://t.me/+HASH         (приватный, по инвайту)
      +HASH / stalmetural     → достраиваем t.me сами
    Прямой адрес web.telegram.org возвращаем пустым: его открываем как есть.
    """
    c = (chat or "").strip()
    if not c:
        return ""
    if "web.telegram.org" in c:
        return ""
    if c.startswith(("http://", "https://")):
        return c
    if c.startswith("t.me/"):
        return "https://" + c
    if c.startswith("@"):
        return "https://t.me/" + c[1:]
    # «+HASH», «joinchat/HASH» или голое имя – в любом случае t.me/<как есть>.
    return "https://t.me/" + c.lstrip("/")


def search_token(chat: str) -> str:
    """
    Чем искать канал в поиске Web A, если deep-link не открыл его.

    Для @имени и t.me/имени – само имя. Для приватного инвайта (t.me/+HASH,
    joinchat) поиском не найти – возвращаем пусто. Иначе считаем, что это
    название канала, и ищем по нему.
    """
    tme = canonical_tme(chat)
    if tme:
        tail = tme.split("t.me/")[-1] if "t.me/" in tme else ""
        if tail and not tail.startswith(("+", "joinchat")):
            return tail.strip("/")
        if tail:
            return ""
    c = (chat or "").strip()
    if c.startswith("@"):
        return c[1:]
    if c.startswith(("+", "http", "t.me/")):
        return ""
    return c


def deep_link(chat: str) -> str:
    """Адрес Web A, открывающий нужный канал сразу при загрузке приложения."""
    if "web.telegram.org" in (chat or ""):
        return chat.strip()
    tme = canonical_tme(chat)
    if not tme:
        return APP
    return f"{APP}#?tgaddr=" + quote(tme, safe="")


# ─── Мелкие помощники браузера ──────────────────────────────────────
def _diag_dir(project_id: str):
    d = paths.data_root() / project_id / "crosspost"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _debug_shot(project_id: str, page, name: str,
                log: Callable[[str], None] | None = None) -> bytes | None:
    """Снимок в момент неудачи – картинкой (и файлом рядом с логом)."""
    try:
        blob = page.screenshot(type="png", full_page=False)
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"  снимок экрана сделать не вышло: {e}")
        return None
    try:
        fp = _diag_dir(project_id) / f"tg-debug-{name}.png"
        fp.write_bytes(blob)
        if log:
            log(f"  📸 снимок экрана: {fp}")
    except OSError:
        pass
    return blob


def _save_diag(project_id: str, page, name: str,
               log: Callable[[str], None] | None = None) -> None:
    """Разметку области сохраняем рядом с логом – по ней доводим селекторы."""
    try:
        fp = _diag_dir(project_id) / f"tg-{name}.html"
        fp.write_text(page.evaluate("() => document.body.innerHTML.slice(0, 200000)") or "",
                      encoding="utf-8")
        if log:
            log(f"  🧩 разметка страницы сохранена: {fp} (пришлите её – доведём "
                "селекторы точно)")
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"  разметку сохранить не вышло: {e}")


def _dump(project_id: str, page, name: str, log: Callable[[str], None]):
    """И снимок, и разметку разом – для любого места, где что-то пошло не так."""
    blob = _debug_shot(project_id, page, name, log)
    _save_diag(project_id, page, name, log)
    return blob


def _log_screen(page, log: Callable[[str], None], where: str) -> None:
    """
    Подробно: где сейчас браузер и что на экране. Это главный ориентир «на
    каком шаге упало»: в логе видно и адрес, и первые слова страницы, и есть
    ли уже поле ввода / окно календаря.
    """
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        url = "?"
    body = " ".join(_body_text(page).split())
    has_input = bool(_first_visible(page, SEL["text"]))
    has_modal = bool(_first_visible(page, SEL["modal"]))
    log(f"  [{where}] адрес: {url}")
    log(f"  [{where}] поле ввода: {'есть' if has_input else 'нет'}; "
        f"окно календаря: {'есть' if has_modal else 'нет'}; на экране "
        f"текста {len(body)} знаков")
    if body:
        log(f"  [{where}] начало экрана: «{body[:200]}»")


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


def _body_text(page) -> str:
    try:
        return page.evaluate("() => document.body ? (document.body.innerText || '') : ''") or ""
    except Exception:  # noqa: BLE001
        return ""


# ─── Открыть канал ──────────────────────────────────────────────────
COMPOSER_WAIT_S = 40            # столько ждём поле ввода ПОСЛЕ выбора чата
APP_SHELL_WAIT_S = 30          # столько ждём загрузку приложения (список чатов/поиск)

# Поле поиска на левой панели и кликабельный результат.
SEARCH_SEL = ('input#telegram-search-input', '#telegram-search-input',
              '.LeftMain input[type="text"]', '#LeftColumn input[type="text"]',
              'input[type="text"]')
RESULT_SEL = ('.LeftSearch .ListItem-button', '#LeftColumn .ListItem-button',
              '.chat-list .ListItem-button', '.ListItem-button',
              '.chat-item-clickable', '#LeftColumn [role="button"].ListItem-button')
# Признаки, что приложение прогрузилось (есть список чатов / поиск).
SHELL_SEL = ('input#telegram-search-input', '.chat-list', '.ListItem-button',
             '#LeftColumn')


def _wait_for_composer(page, log: Callable[[str], None] | None = None,
                       timeout_s: int = COMPOSER_WAIT_S) -> str:
    """Дождаться поля ввода поста, а не заглянуть один раз и уйти."""
    log = log or (lambda m: None)
    said = 0
    for waited in range(timeout_s):
        sel = _first_visible(page, SEL["text"])
        if sel:
            if waited:
                log(f"  чат открылся за {waited} с")
            return sel
        if waited and waited - said >= 12:
            said = waited
            log(f"  жду поле ввода… ({waited} с)")
        page.wait_for_timeout(1_000)
    return ""


def _wait_app_shell(page, log: Callable[[str], None]) -> bool:
    """Дождаться, пока приложение прогрузит список чатов/поиск."""
    for waited in range(APP_SHELL_WAIT_S):
        if _first_visible(page, SHELL_SEL):
            if waited:
                log(f"  приложение загрузилось за {waited} с")
            return True
        page.wait_for_timeout(1_000)
    return False


def _why_no_composer(page) -> str:
    """Почему поля ввода так и не появилось – ответ по тому, ЧТО на экране."""
    body = _body_text(page)
    seen = " ".join(body.split())
    if any(m in body for m in LOGIN_MARKS):
        return ("Телеграм показывает экран входа (QR/номер) – сессия не "
                "действует. Соберите файл сессий заново: войдите в "
                "web.telegram.org/a/ по QR в окне из VHOD-VK-OK-MAX-TG.py и загрузите "
                "файл в «Настройках». Пост НЕ отправлен")
    if any(m in body for m in JOIN_MARKS):
        return ("Телеграм предлагает вступить в канал – аккаунт в нём не состоит "
                "или не админ. Добавьте аккаунт администратором канала с правом "
                "публикации. Пост НЕ отправлен")
    if len(seen) < 40:
        return ("Телеграм так и не загрузился – на снимке пустой экран. Из "
                "облака это бывает: попробуйте сформировать на своём компьютере, "
                "там же, где делали файл сессий")
    # Приложение прогрузилось (виден список чатов), но нужный канал не открылся –
    # почти всегда потому, что он задан ССЫЛКОЙ-ПРИГЛАШЕНИЕМ, а по ней имени нет.
    return ("Не удалось открыть нужный канал: Телеграм остался на главной "
            "(список чатов). Впишите в поле канала его @имя (для публичного) "
            "или ТОЧНОЕ НАЗВАНИЕ канала (для закрытого) вместо ссылки-приглашения "
            "t.me/+… — тогда Click найдёт его поиском. Пост НЕ отправлен")


def _try_click_result(page, log: Callable[[str], None], seconds: int) -> str:
    """Подождать результаты поиска и нажать первый. Возвращает селектор или ''."""
    for _ in range(seconds):
        page.wait_for_timeout(1_000)
        opened = _click_first(page, RESULT_SEL, timeout=3_000)
        if opened:
            log(f"  нажал первый результат ({opened})")
            return opened
    return ""


def _open_by_search(page, query: str, project_id: str,
                    log: Callable[[str], None]) -> str:
    """
    Открыть канал ПОИСКОМ, как руками: загрузить приложение, напечатать имя в
    поиск (настоящим набором, чтобы поиск запустился), подождать результаты и
    нажать первый. Если под вкладкой «Chats» пусто – переходим на «Channels».
    """
    log(f"  открываю приложение и ищу канал по имени «{query}»")
    try:
        page.goto(APP, wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:  # noqa: BLE001
        log(f"  приложение не открылось: {e}")
        return ""
    if not _wait_app_shell(page, log):
        log("  приложение так и не прогрузило список чатов")
        _save_diag(project_id, page, "no-shell", log)
        return ""
    search = _first_visible(page, SEARCH_SEL)
    if not search:
        log("  поле поиска на левой панели не нашли")
        _save_diag(project_id, page, "no-search", log)
        return ""
    log(f"  поле поиска: {search} — набираю «{query}»")
    try:
        page.locator(search).first.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        # НАСТОЯЩИЙ набор: fill не всегда запускает поиск Телеграма.
        page.keyboard.type(query, delay=60)
    except Exception as e:  # noqa: BLE001
        log(f"  не смог напечатать в поиск: {e}")
        return ""

    # Результаты через прокси приходят не мгновенно – ждём подольше.
    if _try_click_result(page, log, seconds=10):
        return _wait_for_composer(page, log)

    # Под «Chats» пусто – канал часто под вкладкой «Channels» («Каналы»).
    if _click_first(page, ('button:has-text("Channels")', 'text="Channels"',
                           'button:has-text("Каналы")', 'text="Каналы"',
                           '[role="tab"]:has-text("Channels")'), timeout=3_000):
        log("  переключился на вкладку «Channels», жду результаты")
        if _try_click_result(page, log, seconds=8):
            return _wait_for_composer(page, log)

    log("  среди результатов поиска не нашли, что нажать")
    _save_diag(project_id, page, "no-search-result", log)
    return ""


def _open_channel(page, chat: str, project_id: str,
                  log: Callable[[str], None]) -> str:
    """
    Открыть канал бренда. Возвращает селектор поля ввода или ''.

    Правильный путь зависит от того, чем записан канал:
      • @имя / название канала  → открываем ПОИСКОМ (как руками: печатаем имя,
        жмём первый результат). Это надёжнее всего.
      • прямой адрес web.telegram.org → открываем как есть.
      • ссылка-приглашение t.me/+HASH (закрытый канал) → имени в ней нет,
        поиском не найти; пробуем deep-link, а если не вышло – честно просим
        вписать @имя или НАЗВАНИЕ канала.
    """
    log(f"Открываю канал: {chat}")

    # Прямой веб-адрес.
    if "web.telegram.org" in chat:
        try:
            page.goto(chat, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:  # noqa: BLE001
            log(f"  адрес не открылся: {e}")
            return ""
        return _wait_for_composer(page, log)

    low = chat.strip().lower()
    looks_like_link = ("t.me/" in low) or low.startswith("@")
    is_invite = ("t.me/+" in low) or ("joinchat" in low)
    token = search_token(chat)

    # Публичный @канал/ссылка (не приглашение): надёжнее всего – прямой
    # deep-link. Не открылся – пробуем поиском.
    if token and looks_like_link and not is_invite:
        target = deep_link(chat)
        log(f"  публичный канал – пробую прямой адрес: {target}")
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:  # noqa: BLE001
            log(f"  прямой адрес не открылся: {e}")
        _wait_app_shell(page, log)
        page.wait_for_timeout(2_000)
        sel = _wait_for_composer(page, log, timeout_s=18)
        if sel:
            _log_screen(page, log, "канал открыт (прямой адрес)")
            return sel
        log("  прямой адрес не открыл чат – пробую поиском")
        sel = _open_by_search(page, token, project_id, log)
        _log_screen(page, log, "канал открыт" if sel else "канал не открылся")
        return sel

    # Название канала (без ссылки/@) – только поиск.
    if token and not is_invite:
        sel = _open_by_search(page, token, project_id, log)
        _log_screen(page, log, "канал открыт" if sel else "канал не открылся")
        return sel

    # Ссылка-приглашение: имени нет, только deep-link (без долгого ожидания).
    target = deep_link(chat)
    log("  канал задан ссылкой-приглашением (имени в ней нет).")
    log(f"  пробую deep-link Web A: {target}")
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:  # noqa: BLE001
        log(f"  deep-link не открылся: {e}")
    _wait_app_shell(page, log)
    page.wait_for_timeout(3_000)
    sel = _wait_for_composer(page, log, timeout_s=15)
    if sel:
        _log_screen(page, log, "канал открыт (deep-link)")
        return sel
    log("  по ссылке-приглашению канал открыть не вышло: Телеграм остался на "
        "главной. ВПИШИТЕ в поле канала его @имя (для публичного) или ТОЧНОЕ "
        "НАЗВАНИЕ канала (для закрытого) — тогда открою поиском.")
    _log_screen(page, log, "после deep-link (инвайт)")
    return ""


# ─── Ввод текста и разметки в поле поста ────────────────────────────
def _letters(s: str) -> str:
    return re.sub(r"\W", "", s or "", flags=re.U).lower()


def _probe(line: str) -> str:
    return _letters(line)[:32]


def _editor_text(page, sel: str) -> str:
    for _ in range(2):
        try:
            return page.eval_on_selector(
                sel, "el => el.innerText != null ? el.innerText : (el.value || '')") or ""
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(250)
    return ""


def _clear_editor(page, sel: str) -> bool:
    """Опустошить поле перед вводом. Ни одной клавиши, которая может отправить."""
    for _ in range(3):
        try:
            page.locator(sel).first.click(timeout=5_000)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
        except Exception:  # noqa: BLE001
            pass
        if not _editor_text(page, sel).strip():
            return True
        page.wait_for_timeout(250)
    return not _editor_text(page, sel).strip()


# Разметка накладывается НА ГОТОВЫЙ текст (жирный/ссылки) – так же, как у ОК:
# сначала вставляем обычный текст целым, потом выделяем куски и включаем им
# формат средствами самого редактора. Текст при этом не переписываем, чтобы
# он не пострадал.
_MARK_JS = """
(args) => {
  const el = document.querySelector(args.sel);
  if (!el) return {error: 'нет поля'};
  const build = () => {
    const nodes = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let full = '';
    while (w.nextNode()) { nodes.push([w.currentNode, full.length]); full += w.currentNode.nodeValue; }
    // Нормализованная копия: пробелы/переводы строк схлопнуты в один пробел,
    // ЭМОДЗИ выкинуты. nmap[k] — индекс k-го символа norm в исходном full.
    // Эмодзи в поле Телеграма — это картинки (в тексте их нет), а в реестре
    // жирным часто помечен кусок с эмодзи в начале («❔ Почему…») — из-за
    // этого длинные куски не находились (была причина «наложено 3/7»).
    const isEmoji = ch => /\p{Extended_Pictographic}/u.test(ch)
                       || ch === '️' || ch === '‍';
    let norm = '', nmap = [], i = 0;
    while (i < full.length) {
      const cp = full.codePointAt(i);
      const len = cp > 0xFFFF ? 2 : 1;
      const ch = full.substr(i, len);
      if (isEmoji(ch)) { i += len; continue; }         // эмодзи выкидываем
      if (/\s/.test(ch)) {
        if (!(norm.length && norm[norm.length - 1] === ' ')) { norm += ' '; nmap.push(i); }
      } else {
        for (let k = 0; k < len; k++) { norm += ch[k]; nmap.push(i); }
      }
      i += len;
    }
    return {nodes, full, norm, nmap};
  };
  const rangeFor = (map, from, to) => {
    const at = (pos, end) => {
      for (const [node, start] of map.nodes) {
        const len = node.nodeValue.length;
        if (pos >= start && pos <= start + len) {
          if (pos === start + len && !end) continue;
          return [node, pos - start];
        }
      }
      return null;
    };
    const a = at(from, false), b = at(to, true);
    if (!a || !b) return null;
    const r = document.createRange();
    r.setStart(a[0], a[1]); r.setEnd(b[0], b[1]);
    return r;
  };
  // Куски идут в порядке текста. Ищем каждый ВПЕРЁД от конца прошлого
  // (ncursor), чтобы жирное слово не село на свой же более ранний повтор
  // без выделения. Поиск — по нормализованному тексту (пробелы схлопнуты).
  let done = 0, missed = 0, ncursor = 0;
  for (const span of args.spans) {
    const map = build();
    const needle = (span.text || '')
      .replace(/[\p{Extended_Pictographic}️‍]/gu, ' ')
      .replace(/\s+/g, ' ').trim();
    if (!needle) { missed++; continue; }
    let nidx = map.norm.indexOf(needle, ncursor);
    if (nidx < 0) nidx = map.norm.indexOf(needle);
    if (nidx < 0) { missed++; continue; }
    const from = map.nmap[nidx];
    const to = map.nmap[nidx + needle.length - 1] + 1;
    ncursor = nidx + needle.length;
    const r = rangeFor(map, from, to);
    if (!r) { missed++; continue; }
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(r);
    el.focus();
    try {
      if (span.kind === 'link') document.execCommand('createLink', false, span.url);
      else document.execCommand('bold', false, null);
      done++;
    } catch (e) { missed++; }
  }
  const sel = window.getSelection();
  if (sel) sel.removeAllRanges();
  el.dispatchEvent(new Event('input', {bubbles: true}));
  return {done, missed,
          applied_bold: el.querySelectorAll('b, strong').length,
          applied_links: el.querySelectorAll('a[href]').length,
          text: el.innerText || el.textContent || ''};
}
"""


def _fill_post_text(page, sel: str, markup: str,
                    log: Callable[[str], None]) -> str:
    """
    Вписать текст поста и наложить разметку реестра (жирный, анкор).
    Возвращает '' при успехе, иначе причину отказа словами.

    Порядок – как у человека и как у ОК: сначала обычный текст ОДНИМ куском
    (insert_text, не печать: в поле чата каждый перевод строки – Enter, то
    есть «отправить»), потом поверх – формат. Разметку проверяем по факту:
    легла или нет; не легла – остаётся обычный текст, без звёздочек.
    """
    import post_text

    # visible_chunks (а не plain_chunks): в Телеграм адрес ссылки НЕ пишем
    # словами – он уедет внутрь самой ссылки. plain_chunks дописывал «текст
    # адрес», и в пост уходило «нихромовой проволоки stalmetural.ru/…» разом,
    # да ещё и лишние ~44 знака давали перебор подписи («-40»).
    chunks = post_text.visible_chunks(markup)
    plain = "".join(t for t, _ in chunks)
    filled = [ln for ln in plain.split("\n") if _probe(ln)]
    log(f"  текст к вводу: {len(plain)} знаков, строк {len(plain.splitlines())}")

    if not _clear_editor(page, sel):
        return ("Поле поста в Телеграме не очистилось – в нём остался текст "
                "прошлой попытки. Ничего не вводим: иначе пост уйдёт склеенным")
    log("  поле очищено, вставляю текст одним куском (insert_text)")

    # 1. Обычный текст – всегда, при любом исходе разметки.
    page.keyboard.insert_text(plain)
    page.wait_for_timeout(400)
    got = _editor_text(page, sel)
    log(f"  в поле после вставки: {len(got)} знаков")
    seen = _letters(got)
    missing = [what for what, line in (("начало", filled[0] if filled else ""),
                                       ("конец", filled[-1] if filled else ""))
               if line and _probe(line) not in seen]
    if missing:
        return (f"В поле Телеграма не видно {missing[0]} текста после ввода "
                f"(в поле {len(got)} знаков). Отложку не ставим")

    # 2. Разметка поверх готового текста. Куски идут в порядке текста – JS ищет
    # каждый вперёд от предыдущего, поэтому жирное слово не спутается со своим
    # же более ранним НЕжирным повтором (была причина «жирных 2/6»). Координаты
    # не передаём: в посте эмодзи (🌐 ✉️ 📞) – суррогатные пары, и смещения
    # Python (кодовые точки) разошлись бы с JS (UTF-16).
    # Жирный кусок РЕЖЕМ по строкам: один диапазон через границы абзацев
    # выделить нельзя (однострочные вопросы вставали, а блок контактов из 3
    # строк — нет). Каждую строку выделяем отдельным куском.
    spans: list[dict] = []
    for t, bold in chunks:
        if not bold:
            continue
        for line in t.split("\n"):
            s = line.strip()
            if len(s) > 1:
                spans.append({"kind": "bold", "text": s})
    spans += [{"kind": "link", "text": t, "url": u}
              for t, u in post_text.anchor_spans(markup)]
    want_bold = sum(1 for s in spans if s["kind"] == "bold")
    want_links = sum(1 for s in spans if s["kind"] == "link")
    if spans:
        try:
            res = page.evaluate(_MARK_JS, {"sel": sel, "spans": spans}) or {}
        except Exception as e:  # noqa: BLE001 – разметка не должна ронять прогон
            res = {"error": str(e)}
        if res.get("error"):
            log(f"  разметку наложить не вышло ({res['error']}) – текст обычный")
        else:
            log(f"  разметка: наложено {res.get('done', 0)}/{len(spans)} "
                f"(жирных {want_bold}, ссылок {want_links}; "
                f"промахов {res.get('missed', 0)})")
            # Текст не должен измениться ни на букву – иначе убираем формат.
            if _letters(res.get("text", "")) != _letters(plain):
                log("  разметка изменила текст – возвращаю обычный")
                _clear_editor(page, sel)
                page.keyboard.insert_text(plain)
    log(f"  текст вписан ({len([l for l in got.split(chr(10)) if l.strip()])} строк)")
    return ""


# Карточка-превью сайта в Web A живёт в своём блоке над полем ввода
# (.WebPagePreview) со своим крестиком. Разметка ОК тут не подходит – у
# Телеграма свой DOM, поэтому крестик ищем по его собственным признакам.
_TG_LINK_CARD = (
    ".WebPagePreview button.Button",
    ".WebPagePreview button",
    ".WebPagePreview .icon-close",
    'button[aria-label="Cancel"]:below(.WebPagePreview)',
    ".ComposerEmbeddedMessage button.embedded-cancel",
)


def _drop_link_card_tg(page, log: Callable[[str], None]) -> None:
    """
    Убрать превью-карточку сайта в Телеграме (как крестиком руками).

    Заказчица просила: ссылка в тексте не должна разворачиваться карточкой.
    Ошибок наружу не отдаём – нет карточки, значит и убирать нечего.
    """
    try:
        if not page.locator(".WebPagePreview").count():
            return  # карточки нет – всё хорошо
    except Exception:  # noqa: BLE001
        return
    for css in _TG_LINK_CARD:
        try:
            loc = page.locator(css).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=1500)
                page.wait_for_timeout(300)
                if not page.locator(".WebPagePreview").count():
                    log("  карточку сайта убрал (превью ссылки закрыто)")
                    return
        except Exception:  # noqa: BLE001
            continue
    # Крайняя мера – кликнуть крестик из самой страницы.
    try:
        closed = page.evaluate(
            """() => {
              const card = document.querySelector('.WebPagePreview');
              if (!card) return true;
              const btn = card.querySelector('button, .icon-close, [role="button"]');
              if (btn) { btn.click(); return !document.querySelector('.WebPagePreview'); }
              return false;
            }""")
        if closed:
            log("  карточку сайта убрал (закрыл из страницы)")
            return
    except Exception:  # noqa: BLE001
        pass
    log("  внимание: карточка сайта осталась – закройте её вручную, "
        "если не нужна")


# ─── Вложения ───────────────────────────────────────────────────────
# Вставка фото в поле поста через буфер (Ctrl+V) – ГЛАВНЫЙ путь.
# Кликать «скрепку» на web.telegram нельзя, когда страница прыгает: клик ждёт,
# пока кнопка «устоится», и падает по таймауту (прогон 21.08.2026 20:44 –
# «Не нашли скрепку», хотя меню вложений на экране было). Синтетическое событие
# paste устойчивости не ждёт: шлём картинку прямо в поле, и Телеграм открывает
# окно «Send Photo» с подписью – ровно как при Ctrl+V руками (так и просила
# заказчица). Тот же проверенный приём, что у МАКСа/ОК/ВК.
_PASTE_JS = r"""
(args) => {
  const el = document.querySelector(args.sel);
  if (!el) return false;
  el.focus();
  const bin = atob(args.b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const file = new File([arr], args.name, {type: args.mime});
  const dt = new DataTransfer();
  dt.items.add(file);
  let ev;
  try { ev = new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: dt}); }
  catch (e) { ev = new Event('paste', {bubbles: true, cancelable: true}); }
  try { Object.defineProperty(ev, 'clipboardData', {value: dt}); } catch (e) {}
  el.dispatchEvent(ev);
  return true;
}
"""


def _paste_photo(page, field_sel: str, image_paths: list[str],
                 log: Callable[[str], None]) -> bool:
    """Вставить фото в поле поста (Ctrl+V). True – событие ушло без ошибок."""
    import base64
    import mimetypes
    if not field_sel:
        return False
    sent = False
    for p in image_paths[:10]:
        try:
            b64 = base64.b64encode(Path(p).read_bytes()).decode()
            mime = mimetypes.guess_type(p)[0] or "image/png"
            ok = page.evaluate(_PASTE_JS, {"sel": field_sel, "b64": b64,
                                           "mime": mime, "name": Path(p).name})
            sent = sent or bool(ok)
            page.wait_for_timeout(900)
        except Exception as e:  # noqa: BLE001 – вставка не должна ронять прогон
            log(f"  вставка фото не удалась: {str(e).splitlines()[0][:70]}")
    return sent


def _send_photo_open(page) -> bool:
    """
    Открылось ли окно «Send Photo» (предпросмотр с подписью)?

    После вставки картинки Телеграм показывает окно, экран которого начинается
    со слов «Send Photo» (прогон 19:11: «начало экрана: Send Photo Отгрузка…»).
    По ним и по классу окна вложения и узнаём, что фото принялось.
    """
    try:
        body = _body_text(page).lower()
        if "send photo" in body or "отправить фото" in body:
            return True
    except Exception:  # noqa: BLE001
        pass
    for sel in ('.AttachmentModal', '.AttachmentModal img',
                '.AttachmentModal .Button.send'):
        try:
            if page.locator(sel).first.is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _attach_photos(page, text_sel: str, image_paths: list[str], project_id: str,
                   log: Callable[[str], None]) -> str:
    """
    Прикрепить фото. '' при успехе, иначе причина.

    ГЛАВНЫЙ путь – вставка (Ctrl+V) прямо в поле: не требует кликать
    «прыгающую» скрепку и открывает то же окно «Send Photo». Запас – скрепка
    вложений (#attach-menu-button) → скрытый input / окно выбора файла.
    """
    # 1. Вставка картинки в поле (Ctrl+V) – без клика по скрепке.
    if _paste_photo(page, text_sel, image_paths, log):
        page.wait_for_timeout(1_500)
        if _send_photo_open(page):
            log("  фото вставлено в поле (Ctrl+V) — открылось окно «Send Photo»")
            _log_screen(page, log, "после вложения фото")
            return ""
        log("  вставка не открыла окно отправки — пробую скрепку")

    # 2а. Запас (скрепка): открыть меню вложений.
    hit = _click_first(page, SEL["attach"], timeout=6_000)
    if not hit:
        _save_diag(project_id, page, "no-attach-button", log)
        return "Не нашли скрепку вложений в Телеграме"
    log(f"  нажал скрепку вложений ({hit})")
    page.wait_for_timeout(900)   # дать меню «Photo or Video / File» раскрыться

    # 2б. Отдать файл скрытому input напрямую (сначала тому, что принимает фото).
    for isel in ('input[type="file"][accept*="image"]',
                 'input[type="file"][accept*="video"]',
                 'input[type="file"]'):
        inp = page.locator(isel)
        if not inp.count():
            continue
        try:
            inp.first.set_input_files(image_paths)
            log(f"  файлы отданы напрямую в input вложений ({isel})")
            page.wait_for_timeout(2_000)
            log("  жду окно предпросмотра/подписи Телеграма")
            _log_screen(page, log, "после вложения фото")
            return ""
        except Exception as e:  # noqa: BLE001 – пойдём через окно выбора
            log(f"  input не принял ({str(e).splitlines()[0][:70]}) – пробую окно выбора")
            break

    # 2в. Запас: клик «Photo or Video» и перехват системного окна выбора файла.
    try:
        with page.expect_file_chooser(timeout=6_000) as picked:
            if not _click_first(page, ('text="Photo or Video"', 'text="Фото или видео"',
                                       'text="Фото или Видео"', 'text="Photo"'),
                                timeout=4_000):
                raise RuntimeError("пункт «Photo or Video» не нашёлся")
        picked.value.set_files(image_paths)
        log("  файлы отданы через окно выбора файла")
    except Exception as e:  # noqa: BLE001
        log(f"  окно выбора файла не сработало ({str(e).splitlines()[0][:70]})")
        _save_diag(project_id, page, "no-file-input", log)
        return "Не нашли, куда отдать файлы фото в Телеграме"
    page.wait_for_timeout(2_000)
    log("  жду окно предпросмотра/подписи Телеграма")
    _log_screen(page, log, "после вложения фото")
    return ""


# ─── Календарь и время ──────────────────────────────────────────────
# И русские, и английские названия месяцев: интерфейс Телеграма бывает любым.
_MONTHS = (("январ", 1), ("jan", 1), ("феврал", 2), ("feb", 2),
           ("март", 3), ("mar", 3), ("апрел", 4), ("apr", 4),
           ("мая", 5), ("май", 5), ("may", 5), ("июн", 6), ("jun", 6),
           ("июл", 7), ("jul", 7), ("август", 8), ("aug", 8),
           ("сентябр", 9), ("sep", 9), ("октябр", 10), ("oct", 10),
           ("ноябр", 11), ("nov", 11), ("декабр", 12), ("dec", 12))


def month_year(header: str) -> tuple[int, int]:
    """«Август 2026» / «August 2026» → (8, 2026). Не разобрали – (0, 0)."""
    low = (header or "").lower()
    year = re.search(r"(20\d\d)", low)
    for prefix, num in _MONTHS:
        if prefix in low:
            return num, int(year.group(1)) if year else 0
    return 0, 0


def _modal_header(page) -> str:
    for sel in SEL["modal_header"]:
        try:
            el = page.locator(sel).first
            if el.count():
                t = (el.inner_text() or "").strip()
                if t:
                    return t
        except Exception:  # noqa: BLE001
            continue
    return ""


_CLICK_DAY_JS = r"""
(day) => {
  const modal = document.querySelector('.CalendarModal');
  if (!modal) return {ok: false, why: 'нет окна календаря'};
  const grid = modal.querySelector('.calendar-grid') || modal;
  const want = String(day);
  for (const c of Array.from(grid.querySelectorAll('.day-button'))) {
    if (c.classList.contains('weekday') || c.classList.contains('faded')
        || c.classList.contains('disabled')) continue;
    if (c.hasAttribute('disabled')) continue;
    if ((c.textContent || '').trim() !== want) continue;
    const hit = c.closest('button') || c;
    hit.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    return {ok: true};
  }
  return {ok: false, why: 'нужного числа нет среди кликабельных дней'};
}
"""


def _pick_day(page, when: datetime,
              log: Callable[[str], None]) -> tuple[bool, str]:
    """Долистать календарь до месяца поста и нажать день. (ок, причина)."""
    for _ in range(15):
        header = _modal_header(page)
        month, year = month_year(header)
        log(f"  календарь показывает: «{header}» → месяц {month:02d}.{year}")
        if month and year and (year, month) != (when.year, when.month):
            forward = (year, month) < (when.year, when.month)
            log(f"  листаю {'вперёд' if forward else 'назад'} на {when.month:02d}.{when.year}")
            if not _click_first(page, SEL["month_next" if forward else "month_prev"],
                                timeout=4_000):
                return False, f"не нашли стрелку перелистывания месяца (на экране «{header}»)"
            page.wait_for_timeout(500)
            continue
        try:
            res = page.evaluate(_CLICK_DAY_JS, when.day) or {}
        except Exception as e:  # noqa: BLE001
            return False, f"не смогли нажать число {when.day}: {e}"
        if res.get("ok"):
            log(f"  нажал число {when.day}")
            return True, ""
        return False, f"{res.get('why', 'день не выбран')} (на экране «{header}»)"
    return False, "календарь не долистался до нужного месяца"


def _time_inputs(page):
    for sel in SEL["time_inputs"]:
        loc = page.locator(sel)
        try:
            if loc.count() >= 2:
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _set_time(page, when: datetime, log: Callable[[str], None]) -> str:
    """Вписать часы и минуты в два поля Web A. '' при успехе, иначе причина."""
    inputs = _time_inputs(page)
    if inputs is None:
        return "не нашли поля времени в окне отложки"
    log(f"  полей времени найдено: {inputs.count()}")
    for i, (what, value) in enumerate((("часы", when.hour), ("минуты", when.minute))):
        field = inputs.nth(i)
        want = f"{value:02d}"
        try:
            field.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            field.type(want, delay=80)
            page.wait_for_timeout(200)
        except Exception as e:  # noqa: BLE001
            return f"поле «{what}» не принялось: {e}"
        try:
            got = "".join(ch for ch in (field.input_value() or "") if ch.isdigit())
        except Exception:  # noqa: BLE001
            got = ""
        log(f"  {what}: вписал {want}, в поле стало «{got or '?'}»")
        if got and int(got) != value:
            return f"Телеграм показал в поле «{what}» {got}, а нужно {want}"
    return ""


# ─── Сверка кнопки подтверждения ────────────────────────────────────
def confirm_caption(page) -> str:
    """Надпись кнопки подтверждения в окне отложки – это ответ самой площадки."""
    try:
        return (page.evaluate("""() => {
            const modal = document.querySelector('.CalendarModal') || document;
            for (const b of Array.from(modal.querySelectorAll('button'))) {
                const t = (b.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!/^(Отправить|Заплан|Send|Schedule)\\b/.test(t)) continue;
                const r = b.getBoundingClientRect();
                if (r.width < 4 || r.height < 4) continue;
                return t;
            }
            return '';
        }""") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def caption_time_ok(caption: str, when: datetime) -> bool:
    """
    Сходится ли время на кнопке с задуманным. Это последняя проверка перед
    нажатием: Телеграм пишет на кнопке, на какое время он собрался
    («Отправить сегодня в 20:08»). Нет надписи – не мешаем (нечего сверять).
    Есть время, и оно другое – жать нельзя, пост уйдёт не тогда.
    """
    if not caption:
        return True
    return re.search(rf"\b0?{when.hour}:{when.minute:02d}\b", caption) is not None


def _click_confirm(page, caption: str) -> bool:
    """
    Нажать кнопку подтверждения НАСТОЯЩИМ кликом мыши (Playwright), а не JS:
    программный b.click() Телеграм не принимает – окно оставалось открытым,
    отложка не ставилась (живой прогон 16:30).
    """
    # 1. Кнопка ровно с той надписью, что мы сверили («Send today at 17:09»).
    if caption:
        try:
            btn = page.locator(".CalendarModal button", has_text=caption).first
            if btn.count():
                btn.click(timeout=6_000)
                return True
        except Exception:  # noqa: BLE001
            pass
    # 2. Любая главная кнопка окна: Send / Отправить / Schedule / Заплан.
    for sel in ('.CalendarModal button:has-text("Send")',
                '.CalendarModal button:has-text("Отправить")',
                '.CalendarModal button:has-text("Schedule")',
                '.CalendarModal button:has-text("Заплан")',
                '.CalendarModal .Button.confirm-dialog-button',
                '.CalendarModal button.confirm-dialog-button'):
        try:
            b = page.locator(sel).first
            if b.count() and b.is_visible():
                b.click(timeout=6_000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _modal_gone(page) -> bool:
    try:
        return page.locator(".CalendarModal").count() == 0
    except Exception:  # noqa: BLE001
        return False


# Кнопка отправки в окне «Send Photo». Точный класс дал заказчик из инспектора:
# .simple-message-input-confirm. Таких кнопок на странице ДВЕ – одна в главном
# поле (спрятана, когда открыто окно фото), вторая в самом окне. Берём именно
# ВИДИМУЮ и, по возможности, ту, что внутри всплывающего окна (.popup).
_MEDIA_SEND = (
    ".popup-new-media .simple-message-input-confirm",
    ".popup .simple-message-input-confirm",
    ".simple-message-input-confirm",
    ".AttachmentModal button.Button.send",
    ".AttachmentModal button.send",
)


def _visible_media_send(page):
    """Локатор ВИДИМОЙ кнопки отправки окна фото (или None). Пишем, что нашли."""
    for sel in _MEDIA_SEND:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 4)):
                el = loc.nth(i)
                if el.is_visible():
                    return sel, el
        except Exception:  # noqa: BLE001
            continue
    return "", None


# Помечаем кнопку отправки и меню «More actions» прямо в DOM, чтобы потом
# кликнуть по НИМ элементом Playwright (точно, без промаха в пустоту → без
# меню браузера). По живому дампу окна «Send Photo» (Web A / telegram-tt):
# кнопка отправки – это .Button.primary с иконкой в правом-нижнем углу, класс
# у неё хэшированный и aria пустая; рядом есть кнопка aria='More actions' (⋮),
# в её меню тоже есть «Schedule».
_MARK_SEND_JS = r"""
() => {
  document.querySelectorAll('[data-click-send]').forEach(e => e.removeAttribute('data-click-send'));
  document.querySelectorAll('[data-click-more]').forEach(e => e.removeAttribute('data-click-more'));
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden'
           && s.display !== 'none' && s.opacity !== '0';
  };
  const btns = Array.from(document.querySelectorAll('button, [role="button"]')).filter(vis);
  const info = b => {
    const cls = (b.className || '').toString();
    return {
      b, cls,
      primary: /\bprimary\b/.test(cls),
      icon: !!b.querySelector('[class*="icon"], .tgico, i, svg'),
      aria: b.getAttribute('aria-label') || b.getAttribute('title') || '',
      r: b.getBoundingClientRect()
    };
  };
  const arr = btns.map(info);
  // ГЛАВНОЕ: кнопка отправки в окне предпросмотра фото — она ОДНОЗНАЧНА.
  // Заказчица прислала её точную разметку (21.08.2026):
  //   Web K — button.simple-message-input-confirm (в .popup-new-media);
  //   Web A — .AttachmentModal .Button.send.
  // Берём ИМЕННО ЕЁ и не гадаем по «primary+иконка»: на экране есть ещё
  // микрофон канала (тоже primary+иконка и НИЖЕ по экрану), и сортировка
  // «правее-ниже» цепляла его, а не кнопку окна отправки — правый клик уходил
  // в микрофон, меню отложки не открывалось (заказчица помогла руками).
  const EXPLICIT = '.simple-message-input-confirm, ' +
                   '.AttachmentModal .Button.send, .AttachmentModal button.send';
  const notNew = d => !/new message|новое сообщение/i.test(d.aria);
  let sends = [];
  for (const d of arr) {
    try { if (d.b.matches(EXPLICIT)) { sends = [d]; break; } } catch (e) {}
  }
  // Запас — прежняя эвристика, если точной кнопки на экране не нашлось.
  if (!sends.length) sends = arr.filter(d => /\bmain-button\b/.test(d.cls) && notNew(d));
  if (!sends.length) sends = arr.filter(d => /send message|отправить сообщени/i.test(d.aria) && notNew(d));
  if (!sends.length) sends = arr.filter(d => /\bsend\b/.test(d.cls) && notNew(d));
  if (!sends.length) sends = arr.filter(d => d.primary && d.icon && notNew(d));
  sends.sort((p, q) => (q.r.right + q.r.bottom) - (p.r.right + p.r.bottom));
  const more = arr.find(d => /more actions|ещё|еще|more|schedul/i.test(d.aria));
  if (sends[0]) sends[0].b.setAttribute('data-click-send', '1');
  if (more) more.b.setAttribute('data-click-more', '1');
  const sr = sends[0] ? sends[0].r : null;
  return {
    send: !!sends[0], more: !!more,
    sx: sr ? Math.round(sr.left + sr.width / 2) : 0,
    sy: sr ? Math.round(sr.top + sr.height / 2) : 0,
    all: arr.slice(0, 30).map(d => ({
      cls: d.cls.slice(0, 60), aria: d.aria, primary: d.primary, icon: d.icon,
      x: Math.round(d.r.left + d.r.width / 2), y: Math.round(d.r.top + d.r.height / 2)
    }))
  };
}
"""


def _right_click_at(page, x: int, y: int) -> None:
    """Правый клик по координатам – запасной путь, когда элемент не взять."""
    page.mouse.move(x, y)
    page.wait_for_timeout(120)
    page.mouse.click(x, y, button="right")


# Разослать событие contextmenu прямо в кнопку отправки – когда обычный правый
# клик не проходит из-за «прыгающей» страницы (окно «Send Photo» ещё едет,
# сверху всплыл баннер «новый вход» – Playwright ждёт устойчивости и падает по
# таймауту, живой прогон 21.08.2026). Синтетическое событие ждать анимацию не
# обязано: telegram-tt слушает contextmenu на кнопке и открывает то же меню
# отложки. Ниже реальных кликов, чтобы «как рукой» оставалось первым.
_DISPATCH_CONTEXTMENU_JS = r"""
() => {
  const b = document.querySelector('[data-click-send="1"]');
  if (!b) return false;
  const r = b.getBoundingClientRect();
  const x = Math.round(r.left + r.width / 2);
  const y = Math.round(r.top + r.height / 2);
  const opts = {bubbles: true, cancelable: true, view: window,
                button: 2, buttons: 2, clientX: x, clientY: y};
  b.dispatchEvent(new MouseEvent('mousedown', {...opts, button: 2}));
  b.dispatchEvent(new MouseEvent('mouseup', {...opts, button: 2}));
  b.dispatchEvent(new MouseEvent('contextmenu', opts));
  return true;
}
"""


def _dispatch_contextmenu(page) -> bool:
    """Синтетический contextmenu по помеченной кнопке отправки. True – ушло."""
    try:
        return bool(page.evaluate(_DISPATCH_CONTEXTMENU_JS))
    except Exception:  # noqa: BLE001
        return False


# Унять «прыжки» страницы web.telegram. Web A постоянно доигрывает анимации и
# плавно доскраливает ленту, из-за чего кнопки уезжают из-под курсора и клики
# промахиваются (заказчица: «всё прыгает и скачет, не успеваю»). Вырубаем
# анимации, переходы и плавную прокрутку — страница перестаёт дёргаться, и
# правый клик по отправке попадает точнее.
_CALM_CSS = (
    "*,*::before,*::after{animation-duration:0s !important;"
    "animation-delay:0s !important;transition-duration:0s !important;"
    "transition-delay:0s !important;scroll-behavior:auto !important;}"
)


def _calm_page(page, log: Callable[[str], None]) -> None:
    """Погасить анимации/плавную прокрутку web.telegram, чтобы не «прыгало»."""
    try:
        page.add_style_tag(content=_CALM_CSS)
        log("  погасил анимации страницы (чтобы не прыгала)")
    except Exception:  # noqa: BLE001
        pass


def _dismiss_login_banner(page, log: Callable[[str], None]) -> None:
    """
    Убрать баннер «новый вход», из-за которого страница дёргается.

    Телеграм показывает «Someone just got access to your messages! … Is it
    you? YES, IT'S ME / NO, IT'S NOT ME!» на НАШ же вход — это Click зашёл по
    сохранённой сессии. Пока баннер висит, страница прыгает и скачет (скролл,
    окно «Send Photo» едет), и правый клик по отправке промахивается
    (заказчица 21.08.2026: «всё прыгает и скачет, кнопку отложенного не жмёт»).
    Подтверждаем «YES, IT'S ME» — вход действительно наш, — баннер исчезает,
    страница успокаивается. ЖМЁМ ТОЛЬКО «YES»/«Это я»: «NO/Это не я» — никогда.
    """
    # Баннер всплывает НЕ мгновенно, а через пару секунд после загрузки — с
    # одной проверки его легко проглядеть (прогон 20:22: баннер на экране был,
    # а проверка сразу после открытия канала его ещё не видела). Поэтому
    # заглядываем несколько раз. Сначала убеждаемся, что баннер ЕСТЬ (чтобы не
    # нажать случайное «YES» где-то ещё): ищем его опознавательный текст.
    marks = ("got access to your messages", "new login to your account",
             "доступ к вашим сообщени", "новый вход")
    for _ in range(3):
        try:
            body = (page.inner_text("body", timeout=1_500) or "").lower()
        except Exception:  # noqa: BLE001
            return
        if not any(m in body for m in marks):
            page.wait_for_timeout(1_000)
            continue
        for sel in ('button:has-text("YES")', 'button:has-text("Это я")',
                    'button:has-text("Да, это я")', 'button:has-text("ЭТО Я")'):
            try:
                b = page.locator(sel).first
                if b.count() and b.is_visible():
                    b.click(timeout=2_000)
                    page.wait_for_timeout(600)
                    log("  убрал баннер «новый вход» (это наш вход) — страница успокоилась")
                    return
            except Exception:  # noqa: BLE001
                continue
        return


# Счётчик подписи Телеграма. Когда подпись длиннее лимита (1024 у обычного
# аккаунта, 2048 у Premium), Телеграм рисует ОСТАТОК отрицательным числом
# («-40»). Ловим самое маленькое такое число на экране.
_CAPTION_COUNTER_JS = r"""
() => {
  let val = null;
  for (const e of document.querySelectorAll('div, span, p')) {
    if (e.children.length) continue;
    const t = (e.textContent || '').trim();
    if (/^-\d{1,4}$/.test(t)) {
      const n = parseInt(t, 10);
      if (val === null || n < val) val = n;
    }
  }
  return val;
}
"""


def caption_overflow(page) -> int:
    """Насколько подпись длиннее лимита (0 – в порядке). Ошибки глушим."""
    try:
        val = page.evaluate(_CAPTION_COUNTER_JS)
        return -int(val) if isinstance(val, (int, float)) and val < 0 else 0
    except Exception:  # noqa: BLE001
        return 0


def _schedule_error(page) -> str:
    """
    Текст попапа-ошибки Телеграма, если отложку отклонили (напр. «Слишком
    длинная подпись»). '' – ошибки нет. Проверяем видимые всплывашки/диалоги,
    а не весь body: слова вроде «длинн» могут быть и в самом посте.
    """
    zones = (".Modal", ".confirm-dialog", '[class*="Notification"]',
             '[class*="Toast"]', '[class*="popup"]', '[role="dialog"]')
    needles = ("слишком длинн", "длинная подпись", "укоротите", "too long",
               "caption is too long", "limit")
    for sel in zones:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 6)):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                t = (el.inner_text(timeout=500) or "").strip()
                low = t.lower()
                if any(n in low for n in needles):
                    return t.replace("\n", " ")[:120]
        except Exception:  # noqa: BLE001
            continue
    return ""


# Нажать пункт «Отправить позже»/«Schedule» в ВЫПАВШЕМ меню — жёстко.
# Правый клик по кнопке отправки меню открывает, но обычный клик по пункту
# падал, пока страница ещё «едет» (прогон 21:16: меню не поймали 3 раза). Тут:
#   1) обычным Playwright-кликом с force (без ожидания устойчивости);
#   2) если не вышло — кликаем пункт прямо в DOM (JS), мимо всех «прыжков».
_CLICK_MENU_ITEM_JS = r"""
(needles) => {
  const items = document.querySelectorAll(
    '.MenuItem, [role="menuitem"], .btn-menu-item, .Menu li, .ListItem');
  for (const it of items) {
    const t = (it.textContent || '').trim().toLowerCase();
    if (!t) continue;
    if (needles.some(n => t.includes(n))) {
      const hit = it.closest('.MenuItem, [role="menuitem"], .btn-menu-item, li') || it;
      hit.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
      hit.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
      hit.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
      return t.slice(0, 40);
    }
  }
  return '';
}
"""

# Что за меню (если хоть какое-то) выскочило — для точной диагностики в логе.
_MENU_DUMP_JS = r"""
() => {
  const out = [];
  const items = document.querySelectorAll(
    '.MenuItem, [role="menuitem"], .btn-menu-item, .Menu li');
  for (const it of items) {
    const r = it.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const t = (it.textContent || '').trim();
    if (t) out.push(t.slice(0, 40));
    if (out.length >= 12) break;
  }
  return out;
}
"""

_SCHED_NEEDLES = ("отправить позже", "запланировать", "schedule")


def _click_schedule_item(page) -> str:
    """Нажать пункт «Отправить позже»/«Schedule» в меню. Возвращает как нажали."""
    for sel in SEL["schedule_item"]:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                el.click(force=True, timeout=2_000)
                return sel
        except Exception:  # noqa: BLE001
            continue
    try:
        hit = page.evaluate(_CLICK_MENU_ITEM_JS, list(_SCHED_NEEDLES))
        if hit:
            return f"js:{hit}"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _dump_open_menu(page, log: Callable[[str], None]) -> None:
    """Написать в лог, какое меню (если есть) открылось после правого клика."""
    try:
        items = page.evaluate(_MENU_DUMP_JS) or []
    except Exception:  # noqa: BLE001
        items = []
    if items:
        log("  меню после правого клика содержит: " + " | ".join(items))
    else:
        log("  после правого клика меню не появилось вовсе")


# ─── Открыть меню отложки правой кнопкой ─────────────────────────────
def _open_schedule_menu(page, log: Callable[[str], None]) -> tuple[bool, str]:
    """
    Правой кнопкой по «Отправить» → «Отправить позже»/«Schedule Message».

    Кнопку отправки ищем сами (координатами внутри окна «Send Photo»), потому
    что её класс у Web A свой и снаружи не угадывался. Если не нашли – пишем в
    лог все кнопки окна, чтобы доводить точно, не гоняя человека за файлами.

    ВАЖНО: НИКАКОГО Escape. В Телеграме Escape закрывает открытый чат – после
    него пропадает и кнопка отправки, и введённый текст.
    """
    last_buttons: list[dict] = []
    for attempt in range(1, 4):
        # Помечаем кнопку отправки и «More actions» в DOM и кликаем ПО ЭЛЕМЕНТУ
        # (Playwright сам попадает точно в кнопку → меню Телеграма, не браузера).
        try:
            info = page.evaluate(_MARK_SEND_JS) or {}
        except Exception as e:  # noqa: BLE001
            info = {}
            log(f"  разметка кнопок не прошла: {str(e).splitlines()[0][:90]}")
        last_buttons = info.get("all") or last_buttons

        # 1) Правый клик по кнопке отправки окна. Три способа подряд, каждый
        #    жёстче предыдущего – окно «Send Photo» едет, сверху всплывает
        #    баннер «новый вход», и обычный клик Playwright падает по таймауту
        #    (ждёт устойчивости, живой прогон 21.08.2026):
        #      а) клик по элементу с force=True – без проверки «устойчива ли»;
        #      б) правый клик по координатам кнопки (мышью, мимо проверок);
        #      в) синтетический contextmenu прямо в кнопку (анимацию не ждёт).
        if info.get("send"):
            log(f"  кнопка отправки найдена, правый клик, попытка {attempt}/3")
            sx, sy = int(info.get("sx") or 0), int(info.get("sy") or 0)
            hit = ""
            for way, action in (
                ("force", lambda: page.locator('[data-click-send="1"]').first.click(
                    button="right", force=True, timeout=2_500)),
                ("координаты", lambda: _right_click_at(page, sx, sy) if sx or sy else None),
                ("contextmenu", lambda: _dispatch_contextmenu(page)),
            ):
                try:
                    action()
                except Exception as e:  # noqa: BLE001
                    log(f"    правый клик ({way}) не прошёл: {str(e).splitlines()[0][:70]}")
                    continue
                page.wait_for_timeout(900)
                hit = _click_schedule_item(page)
                if hit:
                    log(f"  нашёл и нажал «Отправить позже»/«Schedule» ({hit}, {way})")
                    return True, ""
                if attempt == 1 and way == "force":
                    _dump_open_menu(page, log)   # в лог: какое меню открылось

        # Запас: меню ⋮ «More actions» в шапке окна «Send Photo». В некоторых
        # сборках Web A пункт «Schedule» лежит именно тут, а не под правым кликом.
        if info.get("more"):
            try:
                page.locator('[data-click-more="1"]').first.click(force=True, timeout=2_000)
                page.wait_for_timeout(700)
                hit = _click_schedule_item(page)
                if hit:
                    log(f"  нашёл «Schedule» в меню ⋮ «More actions» ({hit})")
                    return True, ""
                _dump_open_menu(page, log)
            except Exception as e:  # noqa: BLE001
                log(f"    меню ⋮ не открылось: {str(e).splitlines()[0][:70]}")

        # 2) Запас: берём кнопку окна прямо по точному классу
        #    (.simple-message-input-confirm и т.п.) и правый клик — тоже жёстко:
        #    force, затем по координатам (страница может ещё «ехать»).
        sel, el = _visible_media_send(page)
        if el is not None:
            log(f"  запасная кнопка ({sel}), правый клик, попытка {attempt}/3")
            for way in ("force", "координаты"):
                try:
                    if way == "force":
                        el.click(button="right", force=True, timeout=2_500)
                    else:
                        box = el.bounding_box()
                        if not box:
                            continue
                        _right_click_at(page, int(box["x"] + box["width"] / 2),
                                        int(box["y"] + box["height"] / 2))
                except Exception as e:  # noqa: BLE001
                    log(f"    правый клик ({way}) не прошёл: {str(e).splitlines()[0][:70]}")
                    continue
                page.wait_for_timeout(800)
                hit = _click_schedule_item(page)
                if hit:
                    log(f"  нашёл и нажал «Отправить позже»/«Schedule» ({sel}, {way})")
                    return True, ""

        log(f"  кнопку отправки/меню не поймал (попытка {attempt}/3) – жду…")
        page.wait_for_timeout(1_200)

    # Не вышло – показываем, ЧТО было на экране: это заменяет пересылку файлов.
    if last_buttons:
        log("  кнопки окна отправки (для доводки селектора):")
        for d in last_buttons[:14]:
            log(f"    • класс «{d['cls'][:60]}» aria='{d['aria']}' "
                f"primary={d.get('primary')} иконка={d['icon']} @({d['x']},{d['y']})")
    return False, ("меню «Отправить позже» не появилось. Пост НЕ отправлен – "
                   "левой кнопкой мы не жали")


# ─── Главная функция ────────────────────────────────────────────────
def schedule_postponed_post(project_id: str, chat_url: str, text: str,
                            image_paths: list[str], when: datetime,
                            log: Callable[[str], None] | None = None,
                            headless: bool = True) -> dict:
    """
    Создать одну отложенную публикацию в канале Телеграма через веб-версию.
    {"ok": True} либо {"ok": False, "error": "…", "shot": …}.

    `text` – внутренняя разметка Click (**жирный**, [текст](url)); жирный и
    анкор накладываются в поле редактора, как у ОК.
    """
    log = log or (lambda m: None)
    if not has_saved_session(project_id):
        return {"ok": False, "error": "Нет сессии Телеграма – войдите в «Настройках»"}
    if not chat_url:
        return {"ok": False, "error": "Не указан канал Телеграма для бренда"}

    from playwright.sync_api import sync_playwright

    engine = yb.resolve_engine()
    proxy = proxy_config()
    with sync_playwright() as pw:
        import vk_social as _vk
        # Через прокси-с-паролем Chromium постоянное соединение Телеграма НЕ
        # проводит (QR не рисуется, вход виснет), а Firefox – проводит. Поэтому
        # при прокси сначала Firefox, при его отсутствии – откат на Chromium.
        # Без прокси – как раньше, Chromium (в облаке Телеграм доступен и так).
        browser = None
        if proxy:
            log(f"Телеграм через прокси: {proxy.get('server')}"
                + (" (с логином)" if proxy.get("username") else ""))
            try:
                browser = pw.firefox.launch(headless=headless, proxy=proxy)
                log("  браузер: Firefox (лучший для прокси-с-паролем)")
            except Exception as e:  # noqa: BLE001 – Firefox не установлен
                # Firefox нужен для прокси-с-паролем. Если его нет – ставим САМИ
                # (как это делает resolve_engine для основного движка), один раз,
                # и пробуем снова: заказчице не нужно помнить про ручную команду.
                if yb._is_not_installed(e):
                    log("  Firefox не установлен — ставлю сам (разово, 1–3 минуты)…")
                    import subprocess
                    import sys
                    subprocess.run(
                        [sys.executable, "-m", "playwright", "install", "firefox"],
                        check=False)
                    try:
                        browser = pw.firefox.launch(headless=headless, proxy=proxy)
                        log("  браузер: Firefox (поставлен и запущен)")
                    except Exception as e2:  # noqa: BLE001
                        log(f"  Firefox всё равно не поднялся "
                            f"({str(e2).splitlines()[0][:100]}); откат на Chromium — "
                            "через прокси ТГ может не соединиться")
                else:
                    log(f"  Firefox не запустился ({str(e).splitlines()[0][:120]}); "
                        "откат на Chromium — через прокси ТГ может не соединиться. "
                        "Поставьте Firefox: python -m playwright install firefox")
        if browser is None:
            browser = yb._launch(pw, engine, headless=headless,
                                 extra_args=_vk.ANTIBOT_ARGS, proxy=proxy)
        page = None
        try:
            context = browser.new_context(
                storage_state=str(session_path(project_id)),
                viewport={"width": 1280, "height": 900}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            context.add_init_script(_vk.ANTIBOT_INIT)
            page = context.new_page()

            log(f"Диагностика по шагам сохраняется в: {_diag_dir(project_id)}")
            log("── ШАГ 1/6: открываю канал ──")
            text_sel = _open_channel(page, chat_url, project_id, log)
            if not text_sel:
                return {"ok": False,
                        "shot": _dump(project_id, page, "no-channel", log),
                        "error": _why_no_composer(page)}

            # Гасим «прыжки» страницы (анимации/плавную прокрутку) и убираем
            # баннер «новый вход» — оба трясут страницу и сбивают клики.
            _calm_page(page, log)
            _dismiss_login_banner(page, log)

            log(f"── ШАГ 2/6: ввожу текст ({len(text)} знаков) ──")
            why = _fill_post_text(page, text_sel, text, log)
            if why:
                return {"ok": False,
                        "shot": _dump(project_id, page, "bad-text", log),
                        "error": why}
            page.wait_for_timeout(800)
            # Карточка сайта по ссылке из текста – убираем крестиком, как руками.
            _drop_link_card_tg(page, log)

            if image_paths:
                log(f"── ШАГ 3/6: прикрепляю фото ({len(image_paths)}) ──")
                why = _attach_photos(page, text_sel, image_paths, project_id, log)
                if why:
                    return {"ok": False,
                            "shot": _dump(project_id, page, "no-photo", log),
                            "error": why}
                page.wait_for_timeout(1_500)   # дать окну «Send Photo» устояться
            else:
                log("── ШАГ 3/6: фото нет, пропускаю ──")

            # Подпись длиннее лимита Телеграма? Тогда отложка может «принять» окно,
            # а сообщение не создать. Пишем это в лог явно (счётчик «-40» на экране
            # человек не успевает разглядеть).
            over = caption_overflow(page)
            if over:
                log(f"  ⚠️ подпись длиннее лимита Телеграма на {over} знаков "
                    f"(счётчик показывает -{over}). Обычный аккаунт держит 1024 "
                    "знака в подписи к фото, Premium — 2048. Если отложка не "
                    "встанет — причина здесь: сократите пост или включите Premium")

            # Ещё раз гасим прыжки и баннер «новый вход»: окно «Send Photo»
            # только что открылось и может ещё «ехать», а правый клик по кнопке
            # отправки требует, чтобы она стояла на месте.
            _calm_page(page, log)
            _dismiss_login_banner(page, log)

            # ПРАВОЙ кнопкой: левая отправит пост сейчас же.
            log("── ШАГ 4/6: открываю «Отправить позже» (правой кнопкой по отправке) ──")
            opened, why = _open_schedule_menu(page, log)
            if not opened:
                return {"ok": False,
                        "shot": _dump(project_id, page, "no-schedule-menu", log),
                        "error": why}
            page.wait_for_timeout(1_000)
            if not _first_visible(page, SEL["modal"]):
                return {"ok": False,
                        "shot": _dump(project_id, page, "no-modal", log),
                        "error": "Окно отложки Телеграма не открылось"}
            log("  окно отложки (.CalendarModal) открылось")

            log(f"── ШАГ 5/6: выбираю дату и время {when.strftime('%d.%m.%Y %H:%M')} (Екатеринбург) ──")
            picked, why = _pick_day(page, when, log)
            if not picked:
                return {"ok": False,
                        "shot": _dump(project_id, page, "no-day", log),
                        "error": (f"Не смогли выбрать {when.strftime('%d.%m.%Y')} "
                                  f"в календаре Телеграма: {why}. Пост НЕ отправлен")}
            page.wait_for_timeout(500)

            why = _set_time(page, when, log)
            if why:
                return {"ok": False,
                        "shot": _dump(project_id, page, "bad-time", log),
                        "error": f"Время отложки: {why}. Пост НЕ отправлен"}

            cap = confirm_caption(page)
            if cap:
                log(f"  Телеграм пишет на кнопке: «{cap}»")
            else:
                log("  надписи на кнопке подтверждения не нашли (сверять нечего)")
            if not caption_time_ok(cap, when):
                return {"ok": False,
                        "shot": _dump(project_id, page, "wrong-time", log),
                        "error": (f"Телеграм понял время иначе: на кнопке «{cap}», "
                                  f"а нужно {when.strftime('%d.%m %H:%M')}. "
                                  "Ничего не отправили")}

            log("── ШАГ 6/6: подтверждаю отложку ──")
            if not _click_confirm(page, cap):
                return {"ok": False,
                        "shot": _dump(project_id, page, "no-confirm", log),
                        "error": "Не нашли кнопку подтверждения в окне отложки"}

            # Окно отложки закрылось – подтверждение принято. «Закрылось» = успех,
            # ЕСЛИ рядом нет попапа-ошибки (Телеграм при длинной подписи не даёт
            # поставить и пишет «Слишком длинная подпись»). Держим паузу, чтобы
            # человек успел увидеть экран.
            closed = False
            for _ in range(30):
                if _modal_gone(page):
                    closed = True
                    break
                page.wait_for_timeout(500)
            page.wait_for_timeout(2_000)
            err = _schedule_error(page)
            log("  держу паузу, чтобы было видно экран…")
            page.wait_for_timeout(3_500)          # человек смотрит на результат
            yb._save_storage_state(context, session_path(project_id))

            if err:
                _dump(project_id, page, "schedule-error", log)
                extra = (f" (подпись длиннее лимита на {over} знаков — сократите "
                         "пост или включите Premium)" if over else "")
                return {"ok": False,
                        "error": f"Телеграм отклонил отложку: «{err}»{extra}"}
            if not closed:
                return {"ok": False,
                        "shot": _dump(project_id, page, "no-confirmation", log),
                        "error": ("Окно отложки не закрылось. Загляните в "
                                  "«Отложенные сообщения»: если пост там есть — всё "
                                  "в порядке")}
            log("✅ Телеграм принял отложку")
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            log(f"‼️ исключение на прогоне: {e}")
            return {"ok": False, "error": str(e),
                    "shot": _dump(project_id, page, "error", log) if page else None}
        finally:
            # Когда человек смотрит на браузер (не headless) – держим окно ещё
            # пару секунд на ЛЮБОМ исходе, чтобы ошибку не «схлопнуло» мгновенно.
            if page is not None and not headless:
                try:
                    page.wait_for_timeout(2_500)
                except Exception:  # noqa: BLE001
                    pass
            browser.close()
