"""
vk_social.py – ВКонтакте: вход с сохранением сессии и отложенная запись.

Почему браузер, а не API. ВК примерно с августа 2025 не регистрирует новые
приложения с доступом к постингу (типа «Standalone» больше нет), поэтому ВК
ведём как Яндекс/2ГИС: один раз входим, сессия сохраняется, дальше Click
ставит РОДНУЮ отложку через «Запланировать» в форме поста – пост держит и
публикует сам ВК, Click в момент выхода может быть выключен.

Откуда селекторы. Форма поста и календарь – из проверенных боем наработок
(разбор в ПОСТАНОВКА-Кросспостинг.md, Приложение Б): data-testid формы
подтверждены вживую, включая грабли – черновик, наслаивающийся при повторе;
минуту, не принимающую ввод с клавиатуры; кнопку подтверждения, которая
бывает неактивной. Классы календаря (vkui…) – React-классы и могут меняться;
все селекторы собраны в SEL, чинить в одном месте.

Правило модуля – «успех только доказанный»: после каждого шага проверяем,
что интерфейс реально изменился, и падаем с понятной ошибкой, если нет.
Однажды «лог писал готово, а поста не было» – сюда это не завозим.
"""

from __future__ import annotations

import time as _time
from datetime import datetime
from pathlib import Path
from typing import Callable

import paths
import yb_playwright as yb

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

BASE = "https://vk.com"

# Часовой пояс браузера. Календарь ВК показывает время по часам браузера,
# поэтому жёстко ставим Екатеринбург – тот же пояс, в котором живёт весь Click
# (apptime). Иначе на сервере с UTC отложка уехала бы на 5 часов.
TIMEZONE_ID = "Asia/Yekaterinburg"

# ─── Все селекторы в одном месте ────────────────────────────────────
SEL = {
    # форма поста (data-testid подтверждены вживую)
    "dialog": '[role="dialog"]',
    "text": '[data-testid="posting_base_screen_input_message"]',
    "file_input": 'input[data-testid="posting_base_screen_download_from_device"]',
    "file_input_any": 'input[type="file"]',
    "photo_remove": '[data-testid="posting_attachment_photo_item_remove"]',
    "submit_now": '[data-testid="posting_submit_button"]',
    # планирование
    "postponed_open": '[data-testid="posting_postponed_button"]',
    "postponed_confirm": '[data-testid="posting_postponed_publish_button"]',
    "calendar": ".vkuiCalendar__host",
    "header_picker": ".vkuiCalendarHeader__picker",
    "day_number": '.vkuiCalendarDay__dayNumber span[aria-hidden="true"]',
    "time_picker": ".vkuiCalendarTime__picker",
    # вход
    "login_phone": 'input[name="login"]',
    "login_password": 'input[name="password"]',
    "login_code_single": 'input[inputmode="numeric"], input[name="code"], input[autocomplete="one-time-code"]',
    "login_code_boxes": 'input[maxlength="1"]',
}

MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь",
             "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def session_path(project_id: str) -> Path:
    d = paths.data_root() / project_id / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d / "vk-state.json"


def is_logged_in(page) -> bool:
    """
    Вошли ли мы на самом деле.

    ПО АДРЕСУ ЭТО НЕ ОПРЕДЕЛИТЬ. ВК не перекидывает гостя на /login: он
    открывает тот же vk.com/feed, только с кнопками «Войти» и «Регистрация»
    вместо ленты. Проверка по адресу говорила «вошли» гостю – и Click
    сохранял пустую сессию, а потом искал в сообществе кнопку «Создать»,
    которой у гостя нет, и винил права аккаунта.

    Смотрим на страницу двумя способами: сначала спрашиваем сам ВК, кто мы
    (window.vk.id – ноль у гостя), потом, если объекта нет, ищем гостевые
    кнопки входа. Достаточно любого достоверного признака.
    """
    try:
        uid = page.evaluate(
            "() => (window.vk && (window.vk.id || window.vk.userId)) || 0")
        if isinstance(uid, (int, float)) and int(uid) > 0:
            return True
    except Exception:  # noqa: BLE001 – объекта нет, решаем по вёрстке
        uid = 0

    try:
        # Гостевая шапка: «Войти» + «Регистрация» рядом. У вошедшего их нет.
        guest = page.locator(
            'button:has-text("Регистрация"), a:has-text("Регистрация"), '
            '#index_login_button, [data-testid="index_login_button"]').count()
        if guest:
            return False
        # Признак вошедшего: слева меню профиля / «Моя страница».
        mine = page.locator(
            '#l_pr, [href="/feed"], [data-testid="left_menu"], '
            'a:has-text("Моя страница")').count()
        return bool(mine)
    except Exception:  # noqa: BLE001
        return False


def _debug_shot(project_id: str, page, name: str) -> bytes | None:
    """
    Снимок страницы при неудаче. Без него «не получилось» – это тупик:
    непонятно, слетела сессия, показалась капча или поменялась вёрстка.
    Снимок возвращаем В ПАМЯТИ, чтобы показать его прямо в разделе.
    """
    try:
        return page.screenshot(type="png", full_page=False)
    except Exception:  # noqa: BLE001
        return None


def check_session(project_id: str, group_url: str = "",
                  headless: bool = True) -> dict:
    """
    Жива ли сессия и виден ли нам кабинет сообщества. Отвечает на вопрос
    «дело во входе или в чём-то другом» до того, как человек будет гадать.

    {"ok": True, "who": "…"} – вошли; иначе причина словами и снимок.
    """
    if not has_saved_session(project_id):
        return {"ok": False, "error": "Сессии нет – Click в ВК ещё не входил"}

    from playwright.sync_api import sync_playwright

    engine = yb.resolve_engine()
    with sync_playwright() as pw:
        browser = yb._launch(pw, engine, headless=headless)
        page = None
        try:
            context = browser.new_context(
                storage_state=str(session_path(project_id)),
                viewport={"width": 1280, "height": 900}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            page = context.new_page()
            page.goto(f"{BASE}/feed", wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2500)
            if not is_logged_in(page):
                return {"ok": False,
                        "error": "ВК нас не узнаёт – показывает страницу для гостя "
                                 "с кнопками «Войти» и «Регистрация». Сессия не "
                                 "сохранилась или истекла: войдите заново.",
                        "shot": _debug_shot(project_id, page, "check")}
            if group_url:
                page.goto(group_url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(2000)
                # Кнопка «Создать» есть только у того, кто может публиковать.
                can_post = page.locator('text="Создать"').count() > 0
                if not can_post:
                    return {"ok": False,
                            "error": "Вошли, но кнопки «Создать» в сообществе нет – "
                                     "у этого аккаунта нет прав публикации, либо "
                                     "ссылка ведёт не в то сообщество",
                            "shot": _debug_shot(project_id, page, "no-create")}
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e),
                    "shot": _debug_shot(project_id, page, "check-error") if page else None}
        finally:
            browser.close()


# Кука, по которой ВК узнаёт вошедшего. Гостевой заход тоже ставит куки
# (язык, счётчики), поэтому «файл сессии есть» ещё не значит «мы вошли» –
# ровно на этом Click и говорил «Сессия сохранена» после неудачного входа.
AUTH_COOKIES = ("remixsid", "remixsid6", "remixnsid")


def has_saved_session(project_id: str) -> bool:
    """Есть ли сохранённая сессия С ПРИЗНАКОМ ВХОДА (не просто куки гостя)."""
    fp = session_path(project_id)
    if not fp.exists():
        return False
    try:
        import json
        cookies = json.loads(fp.read_text(encoding="utf-8")).get("cookies") or []
        return any(str(c.get("name", "")).startswith(AUTH_COOKIES) and c.get("value")
                   for c in cookies)
    except Exception:  # noqa: BLE001
        return False


# ════════════════════════════════════════════════════════════════════
#  Вход – по образцу 2ГИС: скриншот вместо окна, шаги распознаются
# ════════════════════════════════════════════════════════════════════
class VkLoginFlow:
    """
    Пошаговый вход в ВК: телефон → пароль (или код из SMS). Каждый метод –
    один шаг, между вызовами объект живёт в st.session_state. ВК ведёт вход
    через id.vk.com – это нормально, шаг распознаётся по полям на странице.
    """

    def __init__(self, project_id: str, headless: bool = True):
        self.project_id = project_id
        self.headless = headless
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self) -> dict:
        from playwright.sync_api import sync_playwright

        engine = yb.resolve_engine()
        try:
            self._pw = sync_playwright().start()
            self.browser = yb._launch(self._pw, engine, headless=self.headless)
            state = session_path(self.project_id)
            self.context = self.browser.new_context(
                storage_state=str(state) if state.exists() else None,
                viewport={"width": 1000, "height": 760}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            self.page = self.context.new_page()
            self.page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=40_000)
            self.page.wait_for_timeout(2000)
            # По умолчанию ВК может показать вход по QR – переключаемся на телефон.
            try:
                self.page.click("text=Войти другим способом", timeout=4000)
                self.page.wait_for_timeout(1200)
            except Exception:  # noqa: BLE001 – кнопки нет, значит уже форма телефона
                pass
            return self.state()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self.browser:
                self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.browser = self.context = self.page = None
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    def save_session(self) -> None:
        yb._save_storage_state(self.context, session_path(self.project_id))

    # ─── шаги ───────────────────────────────────────────────────────
    def submit_phone(self, phone: str) -> dict:
        self.page.fill(SEL["login_phone"], phone.strip())
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(3000)
        return self.state()

    def submit_password(self, password: str) -> dict:
        self.page.fill(SEL["login_password"], password)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(3000)
        return self.state()

    def submit_code(self, code: str) -> dict:
        code = code.strip()
        single = self.page.locator(SEL["login_code_single"])
        if single.count() >= 1:
            single.first.fill(code)
        else:
            boxes = self.page.locator(SEL["login_code_boxes"])
            if boxes.count() >= len(code):
                for i, digit in enumerate(code):
                    boxes.nth(i).fill(digit)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(3000)
        return self.state()

    # ─── что на экране ──────────────────────────────────────────────
    def page_state(self) -> dict:
        """
        Какой шаг входа сейчас на экране. Порядок важен: сначала ищем поля
        формы (они говорят точно), «вошли» ставим ТОЛЬКО по настоящей
        проверке содержимым – раньше это решалось по адресу страницы, и
        гость на vk.com/feed засчитывался как вошедший.
        """
        try:
            if self.page.locator(SEL["login_password"]).count():
                return {"step": "password"}
            if (self.page.locator(SEL["login_code_boxes"]).count() >= 4
                    or self.page.locator(SEL["login_code_single"]).count()):
                return {"step": "code"}
            if self.page.locator(SEL["login_phone"]).count():
                return {"step": "phone"}
            if is_logged_in(self.page):
                return {"step": "done"}
            # Форм нет, но и не вошли – смотрим ленту: там видно наверняка.
            self.page.goto(f"{BASE}/feed", wait_until="domcontentloaded", timeout=30_000)
            self.page.wait_for_timeout(1500)
            if is_logged_in(self.page):
                return {"step": "done"}
            if self.page.locator(SEL["login_phone"]).count():
                return {"step": "phone"}
            return {"step": "unknown"}
        except Exception:  # noqa: BLE001
            return {"step": "unknown"}

    def state(self) -> dict:
        st = self.page_state()
        st["url"] = self.page.url if self.page else ""
        try:
            st["screenshot"] = self.page.screenshot(type="png", full_page=False)
        except Exception:  # noqa: BLE001
            st["screenshot"] = None
        return st


# ════════════════════════════════════════════════════════════════════
#  Отложенная запись
# ════════════════════════════════════════════════════════════════════
def _read_picker_title(page, picker) -> str:
    """Видимое значение кастомного дропдауна vkui (месяц/год/час/минута)."""
    try:
        return (picker.inner_text() or "").strip().split("\n")[0]
    except Exception:  # noqa: BLE001
        return ""


def _click_dropdown_option(page, picker, option_text: str) -> None:
    """Открыть дропдаун и кликнуть пункт с нужным текстом."""
    picker.click()
    page.wait_for_timeout(400)
    opt = page.locator(f'[role="option"]:has-text("{option_text}")').first
    opt.click()
    page.wait_for_timeout(300)


def _type_picker_value(page, picker, value: int) -> None:
    """
    Впечатать значение в поле-комбобокс (час). Тройной клик выделяет текущее,
    Enter подтверждает. Escape НЕ нажимать: он пересобирает блок времени, и
    ссылки на соседние поля отваливаются («Node is detached») – проверено болью.
    """
    inp = picker.locator("input").first
    inp.click(click_count=3)
    page.wait_for_timeout(150)
    inp.type(str(value), delay=30)
    page.wait_for_timeout(150)
    page.keyboard.press("Enter")
    page.wait_for_timeout(300)


def _set_schedule(page, when: datetime, log: Callable[[str], None]) -> None:
    """
    Открыть «Запланировать» и выставить дату и время в календаре ВК.
    После КАЖДОГО шага сверяем видимое значение: React мог не принять ввод,
    и тогда честная ошибка сейчас лучше «успешной» неактивной кнопки потом.
    """
    page.wait_for_selector(SEL["postponed_open"], timeout=10_000)
    page.click(SEL["postponed_open"])
    page.wait_for_selector(SEL["calendar"], timeout=10_000)
    page.wait_for_timeout(400)

    month_name = MONTHS_RU[when.month - 1]
    pickers = page.locator(SEL["header_picker"])

    if pickers.count() >= 1:
        shown = _read_picker_title(page, pickers.nth(0)).lower()
        if shown != month_name:
            _click_dropdown_option(page, pickers.nth(0), month_name.capitalize())
            now_shown = _read_picker_title(page, pickers.nth(0)).lower()
            if now_shown != month_name:
                raise RuntimeError(f"Месяц не переключился: ждали «{month_name}», в календаре «{now_shown}»")
    if pickers.count() >= 2:
        shown = _read_picker_title(page, pickers.nth(1))
        if shown != str(when.year):
            _click_dropdown_option(page, pickers.nth(1), str(when.year))
            now_shown = _read_picker_title(page, pickers.nth(1))
            if now_shown != str(when.year):
                raise RuntimeError(f"Год не переключился: ждали {when.year}, в календаре «{now_shown}»")

    # День: клик по ячейке с числом, потом проверка aria-selected.
    day_ok = page.evaluate(
        """(args) => {
            const numbers = Array.from(document.querySelectorAll(args.sel));
            const target = numbers.find(el => el.textContent.trim() === String(args.day));
            const cell = target ? target.closest('[role="gridcell"]') : null;
            if (!cell) return false;
            cell.click();
            return true;
        }""",
        {"sel": SEL["day_number"], "day": when.day})
    if not day_ok:
        raise RuntimeError(f"Не нашли день {when.day} в календаре ВК")
    page.wait_for_timeout(300)
    day_selected = page.evaluate(
        """(args) => {
            const numbers = Array.from(document.querySelectorAll(args.sel));
            const target = numbers.find(el => el.textContent.trim() === String(args.day));
            const cell = target ? target.closest('[role="gridcell"]') : null;
            return cell ? cell.getAttribute('aria-selected') === 'true' : false;
        }""",
        {"sel": SEL["day_number"], "day": when.day})
    if not day_selected:
        raise RuntimeError(f"День {when.day} кликнут, но календарь его не выбрал")

    # Час – печатью; ссылку на поле минут после этого берём ЗАНОВО: ввод часа
    # пересобирает DOM блока времени, старая ссылка отваливается.
    hour_picker = page.locator(SEL["time_picker"]).first
    _type_picker_value(page, hour_picker, when.hour)
    shown_hour = _read_picker_title(page, page.locator(SEL["time_picker"]).first)
    if shown_hour and int("0" + "".join(ch for ch in shown_hour if ch.isdigit()) or "0") != when.hour:
        raise RuntimeError(f"Час не принялся: ждали {when.hour}, в поле «{shown_hour}»")

    page.wait_for_timeout(400)
    minute_picker = page.locator(SEL["time_picker"]).last
    # Минуту – только выбором из списка: печать иногда откатывается к прежней.
    _click_dropdown_option(page, minute_picker, f"{when.minute:02d}")
    shown_min = _read_picker_title(page, page.locator(SEL["time_picker"]).last)
    digits = "".join(ch for ch in shown_min if ch.isdigit())
    if digits and int(digits) != when.minute:
        raise RuntimeError(f"Минута не принялась: ждали {when.minute:02d}, в поле «{shown_min}»")

    # Подтверждение. Неактивная кнопка = дата не принята, кликать бесполезно.
    page.wait_for_selector(SEL["postponed_confirm"], timeout=10_000)
    disabled = page.eval_on_selector(
        SEL["postponed_confirm"],
        "el => el.disabled || el.getAttribute('aria-disabled') === 'true'")
    if disabled:
        raise RuntimeError("Кнопка «Добавить в очередь» неактивна – календарь не принял дату/время")
    page.click(SEL["postponed_confirm"])
    page.wait_for_timeout(800)
    if page.locator(SEL["postponed_confirm"]).count():
        raise RuntimeError("Попап планирования не закрылся после подтверждения – похоже, не сработало")
    log("Отложка подтверждена: попап закрылся")


def schedule_postponed_post(project_id: str, group_url: str, text: str,
                            image_paths: list[str], when: datetime,
                            log: Callable[[str], None] | None = None,
                            headless: bool = True) -> dict:
    """
    Создать ОДНУ отложенную запись в сообществе под сохранённой сессией.

    Возвращает {"ok": True} либо {"ok": False, "error": "…словами…"}.
    Браузер одноразовый: открыли, поставили, закрыли – формирование идёт
    пачкой раз в день, постоянный браузер ему не нужен.
    """
    log = log or (lambda m: None)
    if not has_saved_session(project_id):
        return {"ok": False, "error": "Нет сессии ВК – войдите в «Настройках» («Вход в ВК»)"}
    if not group_url:
        return {"ok": False, "error": "Не указана ссылка на сообщество ВК"}

    from playwright.sync_api import sync_playwright

    engine = yb.resolve_engine()
    page = None
    with sync_playwright() as pw:
        browser = yb._launch(pw, engine, headless=headless)
        try:
            context = browser.new_context(
                storage_state=str(session_path(project_id)),
                viewport={"width": 1280, "height": 900}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            page = context.new_page()

            log(f"Открываю сообщество: {group_url}")
            page.goto(group_url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2500)
            if not is_logged_in(page):
                return {"ok": False, "shot": _debug_shot(project_id, page, "session"),
                        "error": "ВК открыл страницу как гостю – сессия не действует. "
                                 "Войдите заново в «Настройках» → «Вход в ВК»."}

            # «Создать» → «Пост». Меню – hover-флайаут: между кликами пауза
            # короткая, иначе меню закроется раньше, чем найдём пункт.
            log("Открываю форму поста")
            page.click('text="Создать"', timeout=15_000)
            page.wait_for_timeout(400)
            page.click('text="Пост"', timeout=10_000)
            page.wait_for_selector(SEL["dialog"], timeout=10_000)
            page.wait_for_timeout(1200)
            dlg = SEL["dialog"]

            # Чистим черновик: ВК сам восстанавливает прошлую незаконченную
            # форму, и без чистки текст и фото наслаиваются (дубли – проверено).
            for _ in range(10):
                btn = page.locator(f'{dlg} {SEL["photo_remove"]}')
                if not btn.count():
                    break
                btn.first.click()
                page.wait_for_timeout(300)
            if page.locator(f'{dlg} {SEL["text"]}').count():
                page.click(f'{dlg} {SEL["text"]}')
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.wait_for_timeout(200)

            if image_paths:
                log(f"Прикрепляю фото: {len(image_paths)}")
                inp = page.locator(f'{dlg} {SEL["file_input"]}')
                if not inp.count():
                    inp = page.locator(f'{dlg} {SEL["file_input_any"]}')
                inp.first.set_input_files(image_paths[:10])
                page.wait_for_timeout(2500)

            log(f"Ввожу текст ({len(text)} знаков)")
            page.click(f'{dlg} {SEL["text"]}')
            page.type(f'{dlg} {SEL["text"]}', text, delay=8)
            page.wait_for_timeout(600)
            typed = page.eval_on_selector(f'{dlg} {SEL["text"]}', "el => el.textContent || ''")
            if text.strip() and not (typed or "").strip():
                return {"ok": False, "error": "Текст не попал в поле поста – вёрстка ВК изменилась?"}

            # «Далее» – к экрану, где живёт «Запланировать».
            page.click(f'{dlg} >> text="Далее"', timeout=15_000)
            page.wait_for_timeout(1000)

            log(f"Ставлю таймер на {when.strftime('%d.%m.%Y %H:%M')} (Екатеринбург)")
            _set_schedule(page, when, log)

            yb._save_storage_state(context, session_path(project_id))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 – наружу словами, решает вызывающий
            return {"ok": False, "error": str(e),
                    "shot": _debug_shot(project_id, page, "error") if page else None}
        finally:
            browser.close()
