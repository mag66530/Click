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
    "send": ("button.send", ".Composer button.send", ".send-button",
             'button[aria-label*="Отправить"]', 'button[aria-label*="Send"]'),
    # Пункт меню под правым кликом по «Отправить».
    "schedule_item": ('.MenuItem:has-text("Отправить позже")',
                      '[role="menuitem"]:has-text("Отправить позже")',
                      'text="Отправить позже"',
                      '.MenuItem:has-text("Schedule")',
                      'text="Schedule Message"'),
    # Скрепка вложений.
    "attach": ('.Composer button[aria-label*="Attach"]',
               'button[aria-label*="Прикрепить"]',
               '.Composer .icon-attach', 'button:has(.icon-attach)'),
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
    return {nodes, full};
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
  let done = 0, missed = 0;
  for (const span of args.spans) {
    const map = build();
    const idx = map.full.indexOf(span.text);
    if (idx < 0) { missed++; continue; }
    const r = rangeFor(map, idx, idx + span.text.length);
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
          bold: el.querySelectorAll('b, strong').length,
          links: el.querySelectorAll('a[href]').length,
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

    chunks = post_text.plain_chunks(markup)
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

    # 2. Разметка поверх готового текста.
    spans = [{"kind": "bold", "text": t.strip()} for t, bold in chunks
             if bold and len(t.strip()) > 1]
    spans += [{"kind": "link", "text": t, "url": u}
              for t, u in post_text.anchor_spans(markup)]
    if spans:
        try:
            res = page.evaluate(_MARK_JS, {"sel": sel, "spans": spans}) or {}
        except Exception as e:  # noqa: BLE001 – разметка не должна ронять прогон
            res = {"error": str(e)}
        if res.get("error"):
            log(f"  разметку наложить не вышло ({res['error']}) – текст обычный")
        else:
            want_bold = sum(1 for s in spans if s["kind"] == "bold")
            want_links = sum(1 for s in spans if s["kind"] == "link")
            log(f"  разметка: жирных {res.get('bold', 0)}/{want_bold}, "
                f"ссылок {res.get('links', 0)}/{want_links}")
            # Текст не должен измениться ни на букву – иначе убираем формат.
            if _letters(res.get("text", "")) != _letters(plain):
                log("  разметка изменила текст – возвращаю обычный")
                _clear_editor(page, sel)
                page.keyboard.insert_text(plain)
    log(f"  текст вписан ({len([l for l in got.split(chr(10)) if l.strip()])} строк)")
    return ""


# ─── Вложения ───────────────────────────────────────────────────────
def _attach_photos(page, image_paths: list[str], project_id: str,
                   log: Callable[[str], None]) -> str:
    """
    Прикрепить фото. '' при успехе, иначе причина.

    В Web A самый надёжный путь – отдать файлы скрытому input[type=file],
    а не гонять меню «Фото или видео»: меню у сборок разное, а скрытый input
    один. После выбора Телеграм открывает окно предпросмотра с полем подписи
    и своей кнопкой отправки – её и будем искать дальше.
    """
    try:
        with page.expect_file_chooser(timeout=6_000) as picked:
            hit = _click_first(page, SEL["attach"], timeout=5_000)
            if not hit:
                raise RuntimeError("нет скрепки")
            log(f"  нажал скрепку вложений ({hit})")
            # Часть сборок сразу открывает выбор файла, часть – меню с пунктом.
            _click_first(page, ('text="Photo or Video"', 'text="Фото или видео"',
                                'text="Фото или Видео"'), timeout=2_500)
        picked.value.set_files(image_paths)
        log("  файлы отданы через окно выбора файла")
    except Exception as e:  # noqa: BLE001 – пробуем скрытое поле напрямую
        log(f"  окно выбора файла не сработало ({e}) – пробую скрытое поле input")
        inp = page.locator(SEL["file_input"])
        if not inp.count():
            _save_diag(project_id, page, "no-file-input", log)
            return "Не нашли, куда отдать файлы фото в Телеграме"
        try:
            inp.first.set_input_files(image_paths)
            log("  файлы отданы через скрытое поле input")
        except Exception as e2:  # noqa: BLE001
            return f"Файл фото не принялся: {e2}"
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


# ─── Открыть меню отложки правой кнопкой ─────────────────────────────
def _open_schedule_menu(page, log: Callable[[str], None]) -> tuple[bool, str]:
    """
    Правой кнопкой по «Отправить» → «Отправить позже»/«Schedule Message».

    ВАЖНО: НИКАКОГО Escape. В Телеграме Escape закрывает открытый чат – после
    него пропадает и кнопка отправки, и введённый текст. Из-за этого правый
    клик уходил в 30-секундный таймаут (живой прогон 16:22).
    """
    for attempt in range(1, 4):
        send_sel = _first_visible(page, SEL["send"])
        if not send_sel:
            log(f"  кнопку отправки не видно (попытка {attempt}/3) – жду…")
            page.wait_for_timeout(1_500)
            continue
        log(f"  правый клик по отправке ({send_sel}), попытка {attempt}/3")
        clicked = False
        try:
            page.locator(send_sel).first.click(button="right", timeout=5_000)
            clicked = True
        except Exception as e:  # noqa: BLE001
            log(f"    обычный правый клик не прошёл: {str(e).splitlines()[0][:100]}")
        if not clicked:
            # Запасной путь: сами шлём событие contextmenu по центру кнопки.
            try:
                page.eval_on_selector(send_sel, """el => {
                    const r = el.getBoundingClientRect();
                    el.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true,
                        cancelable: true, clientX: r.left + r.width/2,
                        clientY: r.top + r.height/2}));
                }""")
                log("    отправил contextmenu вручную")
            except Exception as e:  # noqa: BLE001
                log(f"    contextmenu вручную не прошёл: {e}")
        page.wait_for_timeout(1_200)
        hit = _click_first(page, SEL["schedule_item"], timeout=5_000)
        if hit:
            log(f"  нашёл и нажал «Отправить позже»/«Schedule» ({hit})")
            return True, ""
        page.wait_for_timeout(1_000)
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

            log(f"── ШАГ 2/6: ввожу текст ({len(text)} знаков) ──")
            why = _fill_post_text(page, text_sel, text, log)
            if why:
                return {"ok": False,
                        "shot": _dump(project_id, page, "bad-text", log),
                        "error": why}
            page.wait_for_timeout(800)
            # Карточка сайта по ссылке из текста – убираем крестиком, как руками.
            yb.drop_link_card(page, yb.text_domains(text), log,
                              diag_dir=_diag_dir(project_id))

            if image_paths:
                log(f"── ШАГ 3/6: прикрепляю фото ({len(image_paths)}) ──")
                why = _attach_photos(page, image_paths, project_id, log)
                if why:
                    return {"ok": False,
                            "shot": _dump(project_id, page, "no-photo", log),
                            "error": why}
            else:
                log("── ШАГ 3/6: фото нет, пропускаю ──")

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

            # Ответ площадки: окно отложки закрылось (подтверждение принято).
            # Мы жали именно кнопку подтверждения, поэтому закрытие окна – это
            # успех, а не отмена. Если рядом видно «Отложенные» – скажем и это.
            for _ in range(30):
                if _modal_gone(page):
                    where = ("«Отложенные сообщения»"
                             if any(m in _body_text(page) for m in SEL["scheduled_page"])
                             else "окно отложки")
                    log(f"✅ Телеграм принял отложку: {where}")
                    yb._save_storage_state(context, session_path(project_id))
                    return {"ok": True}
                page.wait_for_timeout(500)

            return {"ok": False,
                    "shot": _dump(project_id, page, "no-confirmation", log),
                    "error": ("Телеграм не подтвердил отложку: окно календаря не "
                              "закрылось. Загляните в «Отложенные сообщения» – если "
                              "пост там есть, формировать заново не нужно")}
        except Exception as e:  # noqa: BLE001
            log(f"‼️ исключение на прогоне: {e}")
            return {"ok": False, "error": str(e),
                    "shot": _dump(project_id, page, "error", log) if page else None}
        finally:
            browser.close()
