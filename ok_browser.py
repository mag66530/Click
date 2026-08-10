"""
ok_browser.py – Одноклассники: вход с сохранением сессии и отложка в группе.

Почему браузер, а не API. Заявку на API-права ОК заказчику отклонили
(2026-08-10), поэтому ОК ведём тем же путём, что ВК: один раз входим,
сессия сохраняется, отложка ставится через родной интерфейс группы –
дальше пост держит и публикует сам ОК. API-клиент (ok_social.py) остаётся
в репозитории на случай, если права всё же дадут.

Откуда селекторы. Вход и форма поста – проверены вживую в разобранных
наработках (см. ПОСТАНОВКА-Кросспостинг.md, Приложение Б): поля
st.email/st.password, кнопка «Войти» строго в форме пароля (на странице
есть похожие поля поиска), кнопка «Создать пост» a.pf-head_itx_a, текст
.js-posting-itx, фото .js-photos-btn, публикация
button.posting_submit.js-publish-btn. НЕ проверена вживую только механика
отложки (значок часов в форме) – селекторы-кандидаты собраны в
SEL["postpone_candidates"], первый живой прогон их уточнит: если ни один
не найдётся, вернём честную ошибку и сохраним снимок формы для разбора.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

import paths
import yb_playwright as yb

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

BASE = "https://ok.ru"
TIMEZONE_ID = "Asia/Yekaterinburg"   # как у ВК: календарь живёт по часам браузера

SEL = {
    # вход (проверено вживую)
    "login": 'input[name="st.email"]',
    "password": 'input[name="st.password"]',
    # «Войти через VK ID» – иконка ВК на форме входа ОК. ОСНОВНОЙ путь:
    # у аккаунтов брендов своего пароля ОК обычно нет, вход идёт через ВК.
    # Клик открывает всплывающее окно id.vk.com (проверено вживую 06.07.2026).
    "vk_id_button": ("a.social-icon-button.__vk_id, a[class*='__vk_id'], "
                     "[data-l*='vk'] .social-icon-button, a[title*='ВКонтакте']"),
    "popup_phone": 'input[name="login"]',
    "popup_password": 'input[name="password"]',
    "popup_next": 'button:has-text("Продолжить")',
    # Проверка «вы не робот» – ВК показывает её и во всплывающем окне.
    "captcha_box": ('text="Проверяем, что вы не робот"', "#captcha", ".vkc__Captcha",
                    'iframe[src*="captcha"]'),
    "captcha_continue": ('button:has-text("Продолжить")', 'button:has-text("Начать")'),
    "code_single": 'input[inputmode="numeric"], input[name="code"], input[autocomplete="one-time-code"]',
    "code_boxes": 'input[maxlength="1"]',
    # форма поста (проверено вживую, 2026-07)
    "create_post": "a.pf-head_itx_a",
    "text": '.js-posting-itx[contenteditable="true"]',
    "photo_btn": ".js-photos-btn",
    "file_input": 'input[type="file"]',
    "submit": "button.posting_submit.js-publish-btn",
    # отложка (кандидаты – уточняются на пилоте)
    "postpone_candidates": (
        '[data-l*="postpone"]',
        ".js-pp-toggler",
        ".posting_settings .ic_clock",
        'button[title*="тлож"]',
        'text="Отложенная публикация"',
    ),
    "date_candidates": ('input[name="date"]', ".js-date-input input", 'input[placeholder*="ата"]'),
    "time_candidates": ('input[name="time"]', ".js-time-input input", 'input[placeholder*="ремя"]'),
}


def session_path(project_id: str) -> Path:
    d = paths.data_root() / project_id / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d / "ok-state.json"


# Куки, по которым ОК узнаёт вошедшего. Как и у ВК, гостевой заход тоже
# ставит куки (язык, счётчики) – без этой проверки «сессия сохранена»
# показывалось бы и после неудачного входа.
AUTH_COOKIES = ("AUTH_ID", "auth_id", "JSESSIONID", "AUTH_SIG", "OK_LOGIN")


def has_saved_session(project_id: str) -> bool:
    """Есть ли сохранённая сессия С ПРИЗНАКОМ ВХОДА (не просто куки гостя)."""
    fp = session_path(project_id)
    if not fp.exists():
        return False
    try:
        import json
        cookies = json.loads(fp.read_text(encoding="utf-8")).get("cookies") or []
        return any(str(c.get("name", "")) in AUTH_COOKIES and c.get("value")
                   for c in cookies)
    except Exception:  # noqa: BLE001
        return False


def is_logged_in(page) -> bool:
    """
    Вошли ли мы в ОК на самом деле – по содержимому, а не по адресу.
    Гостю ОК показывает форму входа прямо на главной; вошедшему – левое
    меню с разделами профиля.
    """
    try:
        if page.locator(SEL["password"]).count():
            return False
        if page.locator('a:has-text("Регистрация"), #field_email').count() \
                and not page.locator(".toolbar_nav, #hook_Block_MainMenu").count():
            return False
        return bool(page.locator(".toolbar_nav, #hook_Block_MainMenu, "
                                 'a[href="/feed"]').count())
    except Exception:  # noqa: BLE001
        return False


def _debug_shot(project_id: str, page, name: str) -> str:
    """Снимок формы для разбора «не нашли элемент». Возвращает путь или пусто."""
    try:
        d = paths.data_root() / project_id / "crosspost"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / f"ok-debug-{name}.png"
        page.screenshot(path=str(fp))
        return str(fp)
    except Exception:  # noqa: BLE001
        return ""


# ════════════════════════════════════════════════════════════════════
#  Вход в ОК ЧЕРЕЗ ВК – основной путь
# ════════════════════════════════════════════════════════════════════
class OkViaVkLoginFlow:
    """
    Вход в ОК кнопкой «Войти через VK ID» – так, как заказчик делает руками.

    Почему это основной путь. У рабочих аккаунтов брендов своего пароля ОК
    нет: и ВК, и ОК открываются одной учёткой ВК. Клик по иконке ВК на форме
    входа ОК открывает всплывающее окно id.vk.com; после успеха окно
    закрывается само, а вкладка ОК оказывается залогинена.

    Связка сессий. В браузер подкладывается УЖЕ СОХРАНЁННАЯ СЕССИЯ ВК –
    тогда ВК узнаёт нас во всплывающем окне и вход в ОК проходит без ввода
    телефона и кода вовсе («вошли в ВК – ОК подтянулся»). Сессии ВК нет –
    просто вводим телефон и код в том же окне.
    """

    def __init__(self, project_id: str, headless: bool = True):
        self.project_id = project_id
        self.headless = headless
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None      # вкладка ОК
        self.popup = None     # всплывающее окно ВК

    # ─── жизненный цикл ─────────────────────────────────────────────
    def start(self) -> dict:
        from playwright.sync_api import sync_playwright

        import vk_social

        engine = yb.resolve_engine()
        try:
            self._pw = sync_playwright().start()
            import vk_social as _vk
            self.browser = yb._launch(self._pw, engine, headless=self.headless,
                                      extra_args=_vk.ANTIBOT_ARGS)
            # Подкладываем сессию ВК, если она есть: ради неё всё и затевалось.
            vk_state = vk_social.session_path(self.project_id)
            ok_state = session_path(self.project_id)
            start_state = (str(ok_state) if ok_state.exists()
                           else str(vk_state) if vk_state.exists() else None)
            self.context = self.browser.new_context(
                storage_state=start_state,
                viewport={"width": 1100, "height": 800}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            self.context.add_init_script(_vk.ANTIBOT_INIT)
            self.page = self.context.new_page()
            self.page.goto(BASE, wait_until="domcontentloaded", timeout=40_000)
            self.page.wait_for_timeout(2000)

            if is_logged_in(self.page):          # сессия ОК уже жива
                return self.state()

            btn = self.page.locator(SEL["vk_id_button"]).first
            if not btn.count():
                return {**self.state(), "step": "no-vk-button",
                        "note": "На форме входа ОК не нашли кнопку «Войти через ВК». "
                                "Можно войти обычным логином и паролем ОК ниже."}
            with self.page.expect_popup(timeout=20_000) as info:
                btn.click()
            self.popup = info.value
            self.popup.wait_for_load_state("domcontentloaded")
            self.popup.wait_for_timeout(4000)
            # В окне ВК тоже сперва предлагают QR – уходим на телефон.
            if _vk.click_in_frames(self.popup, ("Войти другим способом", "Другой способ")):
                self.popup.wait_for_timeout(3000)
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
            self.browser = self.context = self.page = self.popup = None
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    def save_session(self) -> None:
        """
        Сохраняем состояние ЦЕЛИКОМ (куки и ok.ru, и vk.com) в сессию ОК:
        публикации нужны ok.ru-куки, а vk.com-куки внутри не мешают и
        помогают, если ОК позже переспросит вход через ВК.
        """
        yb._save_storage_state(self.context, session_path(self.project_id))

    # ─── шаги во всплывающем окне ───────────────────────────────────
    def submit_phone(self, phone: str) -> dict:
        self.popup.fill(SEL["popup_phone"], phone.strip())
        self.popup.click(SEL["popup_next"])
        self.popup.wait_for_timeout(3000)
        return self.state()

    def submit_password(self, password: str) -> dict:
        self.popup.fill(SEL["popup_password"], password)
        self.popup.click(SEL["popup_next"])
        self.popup.wait_for_timeout(3000)
        return self.state()

    def request_code_instead(self) -> dict:
        """Уйти с пароля на SMS-код – у аккаунтов брендов пароля обычно нет."""
        for label in ("Забыли или не установили пароль?", "Нет, восстановить пароль",
                      "Отправить код", "Получить код"):
            try:
                self.popup.click(f"text={label}", timeout=4000)
                self.popup.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001 – шага может не быть
                continue
        return self.state()

    def press_captcha_continue(self) -> dict:
        """Нажать «Продолжить» в проверке «вы не робот» внутри окна ВК."""
        import vk_social as _vk
        fr = _vk.captcha_frame(self.popup)
        if fr is not None:
            _vk.press_captcha_in(fr)
            self.popup.wait_for_timeout(4000)
        return self.state()

    def submit_code(self, code: str) -> dict:
        code = code.strip()
        single = self.popup.locator(SEL["code_single"])
        if single.count() >= 1:
            single.first.fill(code)
        else:
            boxes = self.popup.locator(SEL["code_boxes"])
            if boxes.count() >= len(code):
                for i, digit in enumerate(code):
                    boxes.nth(i).fill(digit)
        self.popup.keyboard.press("Enter")
        self.popup.wait_for_timeout(3500)
        return self.state()

    # ─── что на экране ──────────────────────────────────────────────
    def page_state(self) -> dict:
        # Окно закрылось – ВК подтвердил вход, смотрим на саму вкладку ОК.
        if self.popup is None or self.popup.is_closed():
            try:
                self.page.wait_for_timeout(1500)
                self.page.reload(wait_until="domcontentloaded", timeout=30_000)
                self.page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001
                pass
            return {"step": "done"} if is_logged_in(self.page) else {"step": "login"}
        try:
            import vk_social as _vk
            if _vk.captcha_frame(self.popup) is not None:
                return {"step": "captcha"}
            if self.popup.locator(SEL["popup_password"]).count():
                return {"step": "password"}
            if (self.popup.locator(SEL["code_boxes"]).count() >= 4
                    or self.popup.locator(SEL["code_single"]).count()):
                return {"step": "code"}
            if self.popup.locator(SEL["popup_phone"]).count():
                return {"step": "phone"}
            return {"step": "unknown"}
        except Exception:  # noqa: BLE001
            return {"step": "unknown"}

    def state(self) -> dict:
        st = self.page_state()
        shot_from = self.page if (self.popup is None or self.popup.is_closed()) else self.popup
        try:
            st["screenshot"] = shot_from.screenshot(type="png", full_page=False)
        except Exception:  # noqa: BLE001
            st["screenshot"] = None
        return st


# ════════════════════════════════════════════════════════════════════
#  Вход по логину и паролю ОК – запасной путь
# ════════════════════════════════════════════════════════════════════
class OkLoginFlow:
    """Пошаговый вход в ОК: логин+пароль, при необходимости код."""

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
            import vk_social as _vk
            self.browser = yb._launch(self._pw, engine, headless=self.headless,
                                      extra_args=_vk.ANTIBOT_ARGS)
            state = session_path(self.project_id)
            self.context = self.browser.new_context(
                storage_state=str(state) if state.exists() else None,
                viewport={"width": 1000, "height": 760}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            self.context.add_init_script(_vk.ANTIBOT_INIT)
            self.page = self.context.new_page()
            self.page.goto(BASE, wait_until="domcontentloaded", timeout=40_000)
            self.page.wait_for_timeout(1800)
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

    def submit_credentials(self, login: str, password: str) -> dict:
        """Логин и пароль разом. Кнопка «Войти» – строго в форме пароля:
        на странице ОК есть похожие поля поиска, промах уже случался."""
        self.page.fill(SEL["login"], login.strip())
        self.page.fill(SEL["password"], password)
        form = self.page.locator(SEL["password"]).locator("xpath=ancestor::form[1]")
        form.locator('button:has-text("Войти")').first.click()
        self.page.wait_for_timeout(3000)
        return self.state()

    def submit_code(self, code: str) -> dict:
        code = code.strip()
        single = self.page.locator(SEL["code_single"])
        if single.count() >= 1:
            single.first.fill(code)
        else:
            boxes = self.page.locator(SEL["code_boxes"])
            if boxes.count() >= len(code):
                for i, digit in enumerate(code):
                    boxes.nth(i).fill(digit)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(3000)
        return self.state()

    def page_state(self) -> dict:
        try:
            url = self.page.url or ""
            if self.page.locator(SEL["password"]).count():
                return {"step": "login"}
            if (self.page.locator(SEL["code_boxes"]).count() >= 4
                    or self.page.locator(SEL["code_single"]).count()):
                return {"step": "code"}
            if "anonym" in url or "/dk?st.cmd=" in url:
                return {"step": "login"}
            # «Вошли» ставим только по содержимому страницы: адрес у ОК
            # у гостя и у вошедшего может совпадать (та же беда, что у ВК).
            return {"step": "done"} if is_logged_in(self.page) else {"step": "login"}
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
#  Отложка в группе
# ════════════════════════════════════════════════════════════════════
def _first_present(page, candidates) -> str:
    for sel in candidates:
        try:
            if page.locator(sel).count():
                return sel
        except Exception:  # noqa: BLE001
            continue
    return ""


def schedule_postponed_post(project_id: str, group_url: str, text: str,
                            image_paths: list[str], when: datetime,
                            log: Callable[[str], None] | None = None,
                            headless: bool = True) -> dict:
    """
    Создать одну отложенную публикацию в группе ОК под сохранённой сессией.
    {"ok": True} либо {"ok": False, "error": "…"}. Если механика отложки
    не нашлась (селекторы-кандидаты не совпали) – пост НЕ публикуется
    сейчас, сохраняется снимок формы, ошибка объясняет, что прислать.
    """
    log = log or (lambda m: None)
    if not has_saved_session(project_id):
        return {"ok": False, "error": "Нет сессии ОК – войдите в «Настройках» («Вход в ОК»)"}
    if not group_url:
        return {"ok": False, "error": "Не указана ссылка на группу ОК"}

    from playwright.sync_api import sync_playwright

    engine = yb.resolve_engine()
    with sync_playwright() as pw:
        import vk_social as _vk
        browser = yb._launch(pw, engine, headless=headless, extra_args=_vk.ANTIBOT_ARGS)
        try:
            context = browser.new_context(
                storage_state=str(session_path(project_id)),
                viewport={"width": 1280, "height": 900}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            context.add_init_script(_vk.ANTIBOT_INIT)
            page = context.new_page()

            log(f"Открываю группу: {group_url}")
            page.goto(group_url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2500)
            if "anonym" in (page.url or "") or not is_logged_in(page):
                return {"ok": False,
                        "error": "ОК открыл страницу как гостю – сессия не действует. "
                                 "Войдите заново в «Настройках» → «Вход в ОК»."}

            log("Открываю форму поста")
            page.click(SEL["create_post"], timeout=15_000)
            page.wait_for_selector(SEL["text"], timeout=15_000)
            page.wait_for_timeout(800)

            log(f"Ввожу текст ({len(text)} знаков)")
            page.click(SEL["text"])
            page.type(SEL["text"], text, delay=8)
            page.wait_for_timeout(500)
            typed = page.eval_on_selector(SEL["text"], "el => el.textContent || ''")
            if text.strip() and not (typed or "").strip():
                return {"ok": False, "error": "Текст не попал в поле поста ОК"}

            if image_paths:
                log(f"Прикрепляю фото: {len(image_paths)}")
                if page.locator(SEL["photo_btn"]).count():
                    page.click(SEL["photo_btn"])
                    page.wait_for_timeout(800)
                inp = page.locator(SEL["file_input"])
                if not inp.count():
                    shot = _debug_shot(project_id, page, "no-file-input")
                    return {"ok": False, "error": "Не нашли поле загрузки фото в ОК"
                                                  + (f" (снимок: {shot})" if shot else "")}
                inp.first.set_input_files(image_paths)
                page.wait_for_timeout(2500)

            # Отложка. Селектор часов не подтверждён вживую – идём по кандидатам,
            # и если никто не нашёлся, честно останавливаемся СО СНИМКОМ, не
            # публикуя пост сейчас (немедленный пост вместо отложки – хуже отказа).
            log(f"Ищу отложку, время {when.strftime('%d.%m.%Y %H:%M')} (Екатеринбург)")
            toggler = _first_present(page, SEL["postpone_candidates"])
            if not toggler:
                shot = _debug_shot(project_id, page, "no-postpone")
                return {"ok": False,
                        "error": "Не нашли кнопку отложенной публикации в форме ОК – "
                                 "нужен один живой прогон для уточнения (пришлите снимок"
                                 + (f": {shot})" if shot else ")")}
            page.click(toggler)
            page.wait_for_timeout(800)

            date_sel = _first_present(page, SEL["date_candidates"])
            time_sel = _first_present(page, SEL["time_candidates"])
            if not (date_sel and time_sel):
                shot = _debug_shot(project_id, page, "no-datetime")
                return {"ok": False,
                        "error": "Окно отложки открылось, но поля даты/времени не "
                                 "распознаны – пришлите снимок"
                                 + (f": {shot}" if shot else "")}
            for sel, val in ((date_sel, when.strftime("%d.%m.%Y")),
                             (time_sel, when.strftime("%H:%M"))):
                page.click(sel, click_count=3)
                page.type(sel, val, delay=25)
                page.wait_for_timeout(250)

            page.click(SEL["submit"], timeout=10_000)
            page.wait_for_timeout(1500)
            if page.locator(SEL["text"]).count():
                shot = _debug_shot(project_id, page, "form-open")
                return {"ok": False, "error": "Форма поста не закрылась после отправки – "
                                              "похоже, отложка не встала"
                                              + (f" (снимок: {shot})" if shot else "")}
            log("Форма закрылась – отложка ОК принята")
            yb._save_storage_state(context, session_path(project_id))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        finally:
            browser.close()
