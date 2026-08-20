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

import re
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Callable

import paths
import yb_playwright as yb

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

# Вход ведём на vk.ru – это домен, которым пользуется заказчик (ссылки на
# сообщества у неё тоже vk.ru). Домены vk.ru и vk.com разные, и куки у них
# РАЗНЫЕ: войти на одном и постить на другом не выйдет.
BASE = "https://vk.ru"

# Часовой пояс браузера. Календарь ВК показывает время по часам браузера,
# поэтому жёстко ставим Екатеринбург – тот же пояс, в котором живёт весь Click
# (apptime). Иначе на сервере с UTC отложка уехала бы на 5 часов.
TIMEZONE_ID = "Asia/Yekaterinburg"

# ВК показывает проверку «вы не робот» скрытому браузеру заметно чаще
# обычного. Этот ключ убирает у Chromium признак «мной управляет робот»
# (navigator.webdriver), а скрипт ниже прячет остатки следа в самой странице.
# Только для соцсетей: прогоны Яндекса и 2ГИС стартуют как раньше.
ANTIBOT_ARGS = ["--disable-blink-features=AutomationControlled"]
ANTIBOT_INIT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"


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
    # Проверка «вы не робот». ВК показывает её поверх формы входа, чаще –
    # скрытому браузеру. Обычно это одна кнопка «Продолжить»; иногда за ней
    # прячется головоломка, которую может решить только человек в окне.
    "captcha_box": ('text="Проверяем, что вы не робот"', "#captcha", ".vkc__Captcha",
                    'iframe[src*="captcha"]', '[data-testid="captcha"]'),
    "captcha_continue": ('button:has-text("Продолжить")', 'button:has-text("Начать")',
                         '[data-testid="captcha-continue"]'),
}

MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь",
             "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def session_path(project_id: str) -> Path:
    d = paths.data_root() / project_id / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d / "vk-state.json"


def same_domain(url: str) -> str:
    """
    Привести ссылку на сообщество к тому домену, где лежит наша сессия.

    Зачем. vk.ru и vk.com – РАЗНЫЕ сайты для браузера, и куки входа с одного
    на другой не отправляются. Вошли на vk.ru, а в настройках вписана ссылка
    вида vk.com/club… – ВК откроет её как гостю, кнопки «Создать» не будет, и
    человек будет думать, что сломался вход. Ровно это и случилось на первой
    пробной отложке. Ссылку человек пишет любую – домен подставляем сами.
    """
    if not url:
        return url
    host = BASE.split("//", 1)[1]                      # vk.ru
    other = "vk.com" if host == "vk.ru" else "vk.ru"
    for prefix in (f"https://{other}", f"http://{other}",
                   f"https://m.{other}", f"http://m.{other}",
                   f"https://www.{other}", f"http://www.{other}"):
        if url.startswith(prefix):
            return BASE + url[len(prefix):]
    return url


# Слова, по которым узнаём экраны VK ID. Проверено по живым снимкам
# (2026-08-10): вход идёт через виджет id.vk.com, и экранов больше, чем
# «телефон → пароль»: сначала QR, потом код может прийти в МАКС, а вместо
# кода бывает звонок-сброс.
SCREEN_MARKS = {
    "captcha": ("не робот", "Проверяем, что вы"),
    "qr": ("Наведите камеру", "Войти другим способом", "QR-код"),
    "code": ("Введите код", "код из", "звонок-сброс", "последние 6 цифр",
             "Код отправлен", "Подтвердить другим способом"),
    "password": ("Введите пароль", "Забыли или не установили пароль"),
    "phone": ("Вход ВКонтакте", "Телефон"),
}


def vk_frames(page):
    """Главная страница и все её кадры – форма VK ID живёт в одном из них."""
    try:
        return [page] + [f for f in page.frames if f != page.main_frame]
    except Exception:  # noqa: BLE001
        return [page]


# Читать страницу надо ПО ВСЕМ КОРНЯМ, а не только по body.
#
# Проверено экспериментом (2026-08-10): капча ВК живёт в ТЕНЕВОМ КОРНЕ
# внутри кадра, и обычный inner_text("body") возвращает для неё ПУСТУЮ
# строку. Именно поэтому Click видел на снимке «Подтвердите, что вы не
# робот», а в тексте страницы – ничего, и честно писал «не разобрал, что
# за шаг». Тот же приём давно применяется в 2ГИС (см. gis_playwright).
_DEEP_TEXT_JS = """() => {
  const parts = [], seen = new Set();
  const walk = (root) => {
    if (!root || seen.has(root)) return;
    seen.add(root);
    try { parts.push(root.body ? root.body.innerText : (root.textContent || '')); } catch (e) {}
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of all) if (el.shadowRoot) walk(el.shadowRoot);
  };
  walk(document);
  return parts.join('\\n');
}"""

# Нажать по тексту – тоже сквозь теневые корни. Галочку узнаём по подписи
# рядом с ней: у самого <input> текста нет.
_DEEP_CLICK_JS = """(labels) => {
  const seen = new Set(), roots = [];
  const walk = (root) => {
    if (!root || seen.has(root)) return;
    seen.add(root); roots.push(root);
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of all) if (el.shadowRoot) walk(el.shadowRoot);
  };
  walk(document);
  for (const label of labels) {
    for (const root of roots) {
      let all = [];
      try {
        all = root.querySelectorAll(
          'button, a, label, input[type=checkbox], [role=button], [role=checkbox]');
      } catch (e) { continue; }
      for (const el of all) {
        const own = (el.innerText || el.textContent || '').trim();
        const near = el.tagName === 'INPUT'
          ? ((el.closest('label') || {}).innerText || '') : own;
        if ((own && own.includes(label)) || (near && near.includes(label))) {
          el.click();
          return label;
        }
      }
    }
  }
  return '';
}"""


def frame_text(fr) -> str:
    """Весь видимый текст кадра, включая теневые корни."""
    try:
        return (fr.evaluate(_DEEP_TEXT_JS) or "")[:8000]
    except Exception:  # noqa: BLE001 – кадр мог отвалиться
        try:
            return (fr.inner_text("body") or "")[:8000]
        except Exception:  # noqa: BLE001
            return ""


def deep_click(fr, labels) -> str:
    """Нажать элемент с одной из подписей, сквозь теневые корни. Что нажали."""
    try:
        return fr.evaluate(_DEEP_CLICK_JS, list(labels)) or ""
    except Exception:  # noqa: BLE001
        return ''


def find_frame(page, marks) -> object | None:
    """Первый кадр, где встречается любое из слов. None – не нашли."""
    for fr in vk_frames(page):
        text = frame_text(fr)
        if any(m in text for m in marks):
            return fr
    return None


def captcha_frame(page):
    """
    Кадр, в котором сейчас висит проверка «вы не робот». None – проверки нет.

    ИСКАТЬ НАДО ВО ВСЕХ КАДРАХ. Форма входа и капча ВК рисуются внутри
    iframe, а обычный поиск по странице внутрь кадров не заглядывает –
    из-за этого Click видел картинку с формой, но не находил ни полей, ни
    кнопок, и честно писал «не разобрал, что за шаг».
    """
    return find_frame(page, SCREEN_MARKS["captcha"])


def click_in_frames(page, texts, timeout: int = 5000) -> bool:
    """
    Нажать кнопку/ссылку с одним из текстов – в любом кадре, включая
    теневые корни. True, если нажали.

    Сначала обычный поиск (он умеет ждать и прокручивать), потом обход
    корней: часть кнопок ВК живёт в теневом корне, где обычный поиск их
    не видит вовсе.
    """
    for fr in vk_frames(page):
        for label in texts:
            for sel in (f'button:has-text("{label}")', f'a:has-text("{label}")',
                        f'text="{label}"'):
                try:
                    el = fr.locator(sel).first
                    if el.count():
                        el.click(timeout=timeout)
                        return True
                except Exception:  # noqa: BLE001
                    continue
    for fr in vk_frames(page):
        if deep_click(fr, texts):
            return True
    return False


def press_captcha_in(frame) -> bool:
    """
    Пройти проверку «вы не робот» в кадре. True – нашли и нажали.

    Проверка бывает двух видов, оба живые: ГАЛОЧКА «Я не робот» и кнопка
    «Продолжить». И то и другое лежит в теневом корне, поэтому основной
    путь – обход корней (deep_click); обычный поиск оставлен как запасной.

    Крестик закрытия не трогаем никогда: он однажды уже схлопнул проверку,
    и ВК выбросил на главную. Лучше не нажать ничего.
    """
    labels = ("Я не робот", "Продолжить", "Начать")
    if deep_click(frame, labels):
        return True
    for sel in ('input[type="checkbox"]', 'label:has-text("Я не робот")',
                'button:has-text("Продолжить")', 'button:has-text("Начать")'):
        try:
            el = frame.locator(sel).first
            if el.count():
                el.click(timeout=6000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


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
        browser = yb._launch(pw, engine, headless=headless, extra_args=ANTIBOT_ARGS)
        page = None
        try:
            context = browser.new_context(
                storage_state=str(session_path(project_id)),
                viewport={"width": 1280, "height": 900}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            context.add_init_script(ANTIBOT_INIT)
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
                page.goto(same_domain(group_url), wait_until="domcontentloaded",
                          timeout=45_000)
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


def import_session(project_id: str, raw: bytes) -> tuple[bool, str]:
    """
    Принять готовый файл сессии (storage_state Playwright) и сохранить как
    свой. Возвращает (получилось?, сообщение словами).

    Зачем это есть. Вход в ВК из облака – борьба вслепую: QR, проверка
    «вы не робот», код в МАКС, звонок-сброс. Окна браузера в облаке нет,
    и пройти проверку пальцем невозможно. Поэтому даём честный обходной
    путь: войти где угодно (на своём компьютере, в готовом инструменте) и
    принести сюда файл сессии. Дальше Click работает как обычно.

    Проверяем не «похоже на JSON», а наличие настоящей куки входа: файл
    без неё бесполезен, и узнать об этом лучше сейчас, а не в час поста.
    """
    import json

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"Это не файл сессии: {e}"
    if not isinstance(data, dict) or "cookies" not in data:
        return False, ("В файле нет раздела cookies. Нужен storage_state "
                       "Playwright – файл, который сохраняет браузер вместе с сессией.")
    cookies = data.get("cookies") or []
    has_auth = any(str(c.get("name", "")).startswith(AUTH_COOKIES) and c.get("value")
                   for c in cookies)
    if not has_auth:
        return False, ("В файле нет куки входа ВК (remixsid) – значит он снят у "
                       "гостя, а не у вошедшего. Войдите в ВК и сохраните сессию заново.")
    fp = session_path(project_id)
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return True, f"Сессия ВК принята: {len(cookies)} куки, признак входа на месте."


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
            self.browser = yb._launch(self._pw, engine, headless=self.headless,
                                      extra_args=ANTIBOT_ARGS)
            state = session_path(self.project_id)
            self.context = self.browser.new_context(
                storage_state=str(state) if state.exists() else None,
                viewport={"width": 1000, "height": 760}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            self.context.add_init_script(ANTIBOT_INIT)
            self.page = self.context.new_page()
            self.page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=40_000)
            # Виджет VK ID грузится в кадре и не сразу: без паузы мы смотрим
            # на пустую страницу и не находим ни кнопок, ни полей.
            self.page.wait_for_timeout(4000)
            # По умолчанию ВК показывает вход по QR-коду, а камеры у нас нет.
            # Уходим на телефон сразу – кнопка живёт в кадре виджета, поэтому
            # ищем её по всем кадрам, а не только на главной странице.
            if click_in_frames(self.page, ("Войти другим способом", "Другой способ")):
                self.page.wait_for_timeout(3000)
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

    def request_code_instead(self) -> dict:
        """
        Уйти с пароля на вход по SMS-коду.

        У рабочих аккаунтов брендов пароля обычно нет вовсе – входят по
        телефону и коду. ВК прячет этот путь за ссылкой «Забыли или не
        установили пароль?»: в открывшемся окне выбираем «Нет, восстановить
        пароль» и жмём «Отправить код». Порядок проверен вживую.
        """
        try:
            self.page.click("text=Забыли или не установили пароль?", timeout=6000)
            self.page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001 – ссылки нет, возможно код уже просят
            pass
        for label in ("Нет, восстановить пароль", "Отправить код", "Получить код"):
            try:
                self.page.click(f"text={label}", timeout=4000)
                self.page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001 – шага может не быть, идём дальше
                continue
        self.page.wait_for_timeout(1500)
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
    def _has_captcha(self) -> bool:
        """Висит ли поверх формы проверка «вы не робот»."""
        return captcha_frame(self.page) is not None

    # ─── действия на экранах ────────────────────────────────────────
    def press_captcha_continue(self) -> dict:
        """
        Нажать «Продолжить» в проверке «вы не робот».

        Часто этого достаточно – ВК убеждается, что кнопку нажал живой
        человек. Если за ней окажется головоломка, следующий снимок это
        покажет, и решать её придётся руками в настоящем окне браузера.
        """
        fr = captcha_frame(self.page)
        if fr is not None:
            press_captcha_in(fr)
            self.page.wait_for_timeout(4000)
        return self.state()

    def _wait_screen(self, tries: int = 12) -> dict:
        """
        Подождать, пока экран станет понятным.

        Виджет VK ID грузится не мгновенно: на снимке заказчика был виден
        кружок загрузки, а Click в этот момент уже решал, что «не разобрал,
        что за шаг». Фиксированной паузы мало – опрашиваем раз в секунду,
        пока шаг не определится (до ~12 секунд).
        """
        st = self.page_state()
        for _ in range(tries):
            if st.get("step") != "unknown":
                return st
            self.page.wait_for_timeout(1000)
            st = self.page_state()
        return st

    def restart_login(self) -> dict:
        """
        Вернуться к форме входа с нуля.

        ВК легко выбрасывает на гостевую главную: закрыли капчу, истекло
        время кода, нажали не туда. Раньше это был тупик – кнопок нет,
        полей нет, «не разобрал, что за шаг». Теперь просто открываем
        форму заново и пропускаем QR.
        """
        try:
            self.page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=40_000)
            self.page.wait_for_timeout(4000)
            if click_in_frames(self.page, ("Войти другим способом", "Другой способ")):
                self.page.wait_for_timeout(3000)
        except Exception:  # noqa: BLE001
            pass
        return self.state()

    def press_other_way(self) -> dict:
        """
        «Войти другим способом» – уйти с экрана QR на вход по телефону.
        По умолчанию ВК показывает именно QR, а камеры у нас нет.
        """
        click_in_frames(self.page, ("Войти другим способом", "Другой способ"))
        self.page.wait_for_timeout(2500)
        return self.state()

    def press_other_confirm(self) -> dict:
        """
        «Подтвердить другим способом» – сменить способ подтверждения
        (звонок-сброс → код в МАКС/SMS и наоборот). Живой экран показал,
        что ВК предлагает звонок, когда код удобнее, и наоборот.
        """
        click_in_frames(self.page, ("Подтвердить другим способом",
                                    "Использовать другой способ"))
        self.page.wait_for_timeout(2500)
        return self.state()

    # ─── ввод ───────────────────────────────────────────────────────
    def submit_phone(self, phone: str) -> dict:
        for fr in vk_frames(self.page):
            try:
                inp = fr.locator(SEL["login_phone"]).first
                if inp.count():
                    inp.fill(phone.strip())
                    if not click_in_frames(self.page, ("Войти", "Продолжить")):
                        fr.locator(SEL["login_phone"]).first.press("Enter")
                    self.page.wait_for_timeout(4000)
                    break
            except Exception:  # noqa: BLE001
                continue
        return self.state()

    def submit_password(self, password: str) -> dict:
        for fr in vk_frames(self.page):
            try:
                inp = fr.locator(SEL["login_password"]).first
                if inp.count():
                    inp.fill(password)
                    if not click_in_frames(self.page, ("Продолжить", "Войти")):
                        inp.press("Enter")
                    self.page.wait_for_timeout(4000)
                    break
            except Exception:  # noqa: BLE001
                continue
        return self.state()

    def request_code_instead(self) -> dict:
        """Уйти с пароля на код: у аккаунтов брендов пароля обычно нет."""
        for label in ("Забыли или не установили пароль?", "Нет, восстановить пароль",
                      "Отправить код", "Получить код"):
            if click_in_frames(self.page, (label,), timeout=4000):
                self.page.wait_for_timeout(1500)
        self.page.wait_for_timeout(1500)
        return self.state()

    def submit_code(self, code: str) -> dict:
        """
        Ввести код подтверждения. Поле бывает двух видов: одно на весь код
        или шесть клеток по цифре – встречались оба, поэтому поддерживаем
        оба, и в любом кадре.
        """
        code = code.strip()
        for fr in vk_frames(self.page):
            try:
                single = fr.locator(SEL["login_code_single"])
                if single.count() >= 1:
                    single.first.fill(code)
                    single.first.press("Enter")
                    self.page.wait_for_timeout(4000)
                    return self.state()
                boxes = fr.locator(SEL["login_code_boxes"])
                if boxes.count() >= len(code):
                    for i, digit in enumerate(code):
                        boxes.nth(i).fill(digit)
                    self.page.wait_for_timeout(4000)
                    return self.state()
            except Exception:  # noqa: BLE001
                continue
        return self.state()

    # ─── что на экране ──────────────────────────────────────────────
    def page_state(self) -> dict:
        """
        Какой шаг входа сейчас. Ищем ПО ВСЕМ КАДРАМ: форма VK ID живёт в
        iframe, и поиск только по главной странице ничего не находил.

        Порядок проверок – от самого «перекрывающего» к обычному: капча
        висит поверх всего, экран QR не даёт ввести телефон, и только
        потом идут поля.
        """
        try:
            if is_logged_in(self.page):
                return {"step": "done"}
            if find_frame(self.page, SCREEN_MARKS["captcha"]) is not None:
                return {"step": "captcha"}

            # Поля важнее слов: если есть куда вводить – это и есть шаг.
            for fr in vk_frames(self.page):
                try:
                    if fr.locator(SEL["login_password"]).count():
                        return {"step": "password"}
                    if (fr.locator(SEL["login_code_boxes"]).count() >= 4
                            or fr.locator(SEL["login_code_single"]).count()):
                        return {"step": "code"}
                except Exception:  # noqa: BLE001
                    continue

            if find_frame(self.page, SCREEN_MARKS["code"]) is not None:
                return {"step": "code"}
            # Экран QR: телефон ввести негде, нужна кнопка «Войти другим способом».
            if find_frame(self.page, SCREEN_MARKS["qr"]) is not None:
                for fr in vk_frames(self.page):
                    try:
                        if fr.locator(SEL["login_phone"]).count():
                            return {"step": "phone"}
                    except Exception:  # noqa: BLE001
                        continue
                return {"step": "qr"}
            for fr in vk_frames(self.page):
                try:
                    if fr.locator(SEL["login_phone"]).count():
                        return {"step": "phone"}
                except Exception:  # noqa: BLE001
                    continue
            # Гостевая главная ВК: полей входа нет, зато есть «Войти» и
            # «Регистрация». Значит нас выкинуло с формы – не «непонятный
            # экран», а понятная ситуация: надо открыть форму заново.
            try:
                if self.page.locator(
                        'button:has-text("Регистрация"), a:has-text("Регистрация")').count():
                    return {"step": "start-over"}
            except Exception:  # noqa: BLE001
                pass
            return {"step": "unknown"}
        except Exception:  # noqa: BLE001
            return {"step": "unknown"}

    def state(self) -> dict:
        st = self._wait_screen()
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


# Что делать человеку, когда проверку «вы не робот» не удалось пройти.
# Программой её не пройти по замыслу – она ровно для этого и сделана.
_CAPTCHA_ADVICE = (
    "ВК показал проверку «Проверяем, что вы не робот» и не пустил дальше. "
    "Пройти её программой нельзя – она для того и сделана. Что делать: на "
    "своём компьютере запустите файл VHOD-VK-OK-MAX-TG.py (в папке Click), "
    "войдите в ВК руками и пройдите проверку мышкой, а полученный vk-session.json "
    "загрузите в «Настройках» → «Вход в ВК» → «Загрузить готовый файл сессии». "
    "Такую сессию ВК проверками больше не донимает"
)


def _pass_captcha(page, log: Callable[[str], None], tries: int = 3) -> bool:
    """
    Пройти проверку «вы не робот», если она висит. True – путь свободен.

    Часто это одна кнопка «Продолжить», и тогда она проходится сама. Если
    за кнопкой прячется настоящая головоломка – честно возвращаем False:
    решать её мы не умеем и притворяться не будем.
    """
    frame = captcha_frame(page)
    if frame is None:
        return True
    log("  ВК показал проверку «вы не робот» – пробую пройти")
    for attempt in range(1, tries + 1):
        if not press_captcha_in(frame):
            break
        page.wait_for_timeout(2_500)
        frame = captcha_frame(page)
        if frame is None:
            log(f"  проверка пройдена с {attempt}-й попытки")
            return True
    log("  проверку пройти не вышло")
    return False


def _open_post_form(page, log: Callable[[str], None], tries: int = 3) -> None:
    """
    «Создать» → «Пост». Меню – выпадашка, и она капризна.

    Было: клик по «Создать», пауза 400 мс наугад, клик по «Пост». С первого
    раза это иногда не срабатывало – «Timeout 10000ms exceeded, waiting for
    locator(text="Пост")». Причин две, и обе не лечатся ожиданием подольше:
    меню могло не открыться вовсе (клик пришёлся раньше, чем страница ожила)
    либо открыться и закрыться само. Поэтому не ждём дольше, а ПОВТОРЯЕМ:
    открыли меню – убедились, что пункт виден – кликнули. Не вышло – ещё раз.

    Пункт ищем несколькими способами: у ВК это то пункт меню, то ссылка, и
    держаться за одну форму записи – значит ломаться на каждой их правке.
    """
    item_selectors = (
        '[role="menuitem"]:has-text("Пост")',
        '[role="menu"] >> text="Пост"',
        'text="Пост"',
    )
    last = ""
    for attempt in range(1, tries + 1):
        # Проверка «вы не робот» умеет выскочить прямо посреди работы – в
        # живом случае она накрыла страницу как раз после первого клика по
        # «Создать». Дальше кликать бесполезно: экран закрыт её окном.
        if captcha_frame(page) is not None and not _pass_captcha(page, log):
            raise RuntimeError(_CAPTCHA_ADVICE)
        try:
            page.click('text="Создать"', timeout=15_000)
        except Exception as e:  # noqa: BLE001
            last = f"кнопка «Создать» не нажалась: {e}"
            page.wait_for_timeout(800)
            continue
        # Ждём именно ПОЯВЛЕНИЯ пункта, а не «сколько-нибудь миллисекунд».
        for sel in item_selectors:
            try:
                page.wait_for_selector(sel, state="visible", timeout=2_500)
                page.click(sel, timeout=5_000)
                if attempt > 1:
                    log(f"  форма поста открылась с {attempt}-й попытки")
                return
            except Exception as e:  # noqa: BLE001 – пробуем следующий способ
                last = str(e).split("\n")[0]
        log(f"  меню «Создать» не открылось (попытка {attempt}) – пробую снова")
        page.keyboard.press("Escape")      # закрыть возможный призрак меню
        page.wait_for_timeout(1_200)
    # Последнее слово – за проверкой «вы не робот»: если она всё ещё висит,
    # виновата она, а не права аккаунта. Совет про администратора в этом
    # случае только сбивает с толку – заказчица по нему и пошла проверять
    # права, хотя дело было в проверке.
    if captcha_frame(page) is not None:
        raise RuntimeError(_CAPTCHA_ADVICE)
    raise RuntimeError(
        "Не удалось открыть форму поста: меню «Создать» не показало пункт «Пост». "
        f"Последняя причина: {last}. Проверьте на снимке, что страница сообщества "
        "открылась целиком и вы вошли под администратором")


def _close_dropdown(page, picker) -> None:
    """
    Закрыть список, если он остался открытым после выбора.

    vkui закрывает его сам не всегда – на снимке отказа список минут висел
    поверх «Добавить в очередь». Открытый список перехватывает клик, и
    подтверждение уходит в пустоту: снаружи это выглядит как «попап не
    закрылся, похоже, не сработало». Закрываем повторным щелчком по самому
    полю: Escape тут запрещён – он пересобирает блок времени и отрывает
    ссылки на соседние поля.
    """
    try:
        if not page.locator('[role="option"]').first.is_visible(timeout=500):
            return
    except Exception:  # noqa: BLE001 – списка нет, и хорошо
        return
    try:
        picker.click()
        page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001 – не закрылся, дальше разберётся проверка
        pass


def _picker_digits(page, get_picker) -> str:
    """
    Только цифры из значения поля. Пусто – прочитать не вышло.

    Читаем ТРЕМЯ способами, и порядок тут важен. Поля времени у ВК – это
    компонент vkui: снаружи он выглядит надписью, а значение живёт внутри,
    в <select> или в <input>. Текст элемента (inner_text) значения полей
    ввода НЕ содержит – отсюда и «Час: значение прочитать не вышло» в логе
    заказчицы, хотя на экране стояло 19. Поэтому сперва спрашиваем сами
    поля, и только потом смотрим на текст.
    """
    try:
        picker = get_picker()
    except Exception:  # noqa: BLE001
        return ""
    try:
        value = picker.evaluate(
            """el => {
                const sel = el.querySelector('select');
                if (sel && sel.value) return String(sel.value);
                const inp = el.querySelector('input');
                if (inp && inp.value) return String(inp.value);
                return (el.innerText || '').trim().split('\\n')[0];
            }""")
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        if digits:
            return digits
    except Exception:  # noqa: BLE001 – не Playwright (например, тест) либо не вышло
        pass
    try:
        return "".join(ch for ch in _read_picker_title(page, picker) if ch.isdigit())
    except Exception:  # noqa: BLE001
        return ""


def _scheduled_time_shown(page) -> str:
    """
    Время, которое ВК САМ показывает рядом с «Добавить в очередь»
    («Сегодня в 19:20»). Пусто – не нашли.

    Это и есть правда о назначенном времени: пока Click спорил с полями,
    ВК уже писал внизу окна верные 19:20. Смотрим только рядом с кнопкой
    подтверждения, а не по всему окну: в тексте самого поста тоже могут
    попасться цифры с двоеточием, и принять их за время – хуже, чем не
    найти ничего.
    """
    try:
        text = page.eval_on_selector(
            SEL["postponed_confirm"],
            """el => {
                let box = el.parentElement;
                for (let i = 0; i < 2 && box && box.parentElement; i++) {
                    box = box.parentElement;
                }
                return box ? (box.innerText || '') : '';
            }""") or ""
    except Exception:  # noqa: BLE001
        return ""
    found = re.search(r"\b(\d{1,2}):(\d{2})\b", str(text))
    return f"{int(found.group(1))}:{found.group(2)}" if found else ""


def _time_is_set(page, when, log: Callable[[str], None], tries: int = 15) -> bool:
    """
    Стоит ли в календаре нужное время. Ждём до трёх секунд.

    Верим двум источникам, и первому – больше. Первый: подпись самого ВК
    под календарём («Сегодня в 19:20») – это его собственный ответ на
    вопрос «когда выйдет», надёжнее не бывает. Второй, запасной: значения
    в полях часа и минут, если подпись прочитать не удалось.
    """
    want = f"{when.hour}:{when.minute:02d}"
    for _ in range(tries):
        shown = _scheduled_time_shown(page)
        if shown == want:
            return True
        if (_picker_shows(page, lambda: page.locator(SEL["time_picker"]).first, when.hour)
                and _picker_shows(page, lambda: page.locator(SEL["time_picker"]).last,
                                  when.minute)):
            return True
        page.wait_for_timeout(200)
    return False


def _picker_shows(page, get_picker, expected: int) -> bool:
    """Стоит ли в поле нужное число ПРЯМО СЕЙЧАС, без ожидания."""
    digits = _picker_digits(page, get_picker)
    return bool(digits) and int(digits) == expected


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
    # Час и минуты вписываем, как это делает человек: набрать число и нажать
    # Enter (подсказка заказчицы – «надо просто вписать время и нажать либо
    # энтер, либо на пустое, и всё встаёт»). Список остаётся запасным путём.
    #
    # А вот ПРОВЕРЯЕМ теперь иначе, и это главное. Раньше сверялись с каждым
    # полем по отдельности – и дважды завалили отложку на ровном месте: поля
    # у ВК читаются ненадёжно (значение живёт внутри компонента, снаружи его
    # не видно), из-за чего «Час: значение прочитать не вышло», а следом
    # «Минута не принялась: ждали 20, в поле 00» – при том, что сам ВК внизу
    # окна писал верное «Сегодня в 19:20». Теперь спрашиваем у ВК: какое
    # время ты считаешь назначенным? Его ответ и есть правда.
    def minutes_field():
        return page.locator(SEL["time_picker"]).last

    _type_picker_value(page, page.locator(SEL["time_picker"]).first, when.hour)
    page.wait_for_timeout(400)
    try:
        _type_picker_value(page, minutes_field(), when.minute)
    except Exception as e:  # noqa: BLE001 – поля для ввода нет, пойдём списком
        log(f"  минуты впечатать не вышло ({str(e).split(chr(10))[0]})")

    if not _time_is_set(page, when, log):
        log("  время печатью не встало – пробую минуты списком")
        try:
            _click_dropdown_option(page, minutes_field(), f"{when.minute:02d}")
        except Exception as e:  # noqa: BLE001
            log(f"  список минут не открылся ({str(e).split(chr(10))[0]})")
        # Список минут любит остаться открытым поверх «Добавить в очередь».
        _close_dropdown(page, minutes_field())
        if not _time_is_set(page, when, log):
            shown = _scheduled_time_shown(page)
            raise RuntimeError(
                f"Не удалось выставить время {when.hour}:{when.minute:02d}: "
                + (f"ВК показывает «{shown}»" if shown
                   else "и прочитать, какое время он принял, тоже не вышло")
                + ". Смотрите снимок экрана ниже")
    log(f"  время {when.hour}:{when.minute:02d} стоит")
    _close_dropdown(page, minutes_field())

    _confirm_schedule(page, log)
    log("Отложка подтверждена: попап закрылся")


def _form_closed(page, timeout_ms: int = 5_000) -> bool:
    """Закрылась ли форма поста – признак того, что ВК запись принял."""
    for selector in (SEL["postponed_confirm"], SEL["dialog"]):
        try:
            page.wait_for_selector(selector, state="hidden", timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001 – пробуем второй признак
            timeout_ms = 500          # первый уже подождал, второму хватит взгляда
    return False


def _confirm_schedule(page, log: Callable[[str], None], tries: int = 3) -> None:
    """
    Нажать «Добавить в очередь» и убедиться, что запись принята.

    Почему нажатий может понадобиться несколько. Календарь ВК открывается
    ПОВЕРХ экрана настроек, а кнопка «Добавить в очередь» живёт на самом
    экране, под ним. Первый клик по ней уходит на закрытие календаря – и
    только второй нажимает саму кнопку. Со стороны это выглядело как
    «Click жмёт кнопку, а ничего не происходит»: заказчица видела нажатие
    своими глазами, а отложенных записей в сообществе не появлялось.

    Между нажатиями обязательно проверяем, не закрылась ли форма: если
    первый клик сработал по-настоящему, второго не будет – иначе легко
    получить две одинаковые записи вместо одной.
    """
    page.wait_for_selector(SEL["postponed_confirm"], timeout=10_000)
    disabled = page.eval_on_selector(
        SEL["postponed_confirm"],
        "el => el.disabled || el.getAttribute('aria-disabled') === 'true'")
    if disabled:
        raise RuntimeError("Кнопка «Добавить в очередь» неактивна – календарь не принял дату/время")

    for attempt in range(1, tries + 1):
        try:
            page.click(SEL["postponed_confirm"], timeout=8_000)
        except Exception as e:  # noqa: BLE001 – может быть перекрыта календарём
            log(f"  «Добавить в очередь» не нажалась: {str(e).split(chr(10))[0]}")
        if _form_closed(page):
            log("Отложка подтверждена: форма закрылась"
                + (f" (нажатий: {attempt})" if attempt > 1 else ""))
            return
        if attempt < tries:
            log("  форма не закрылась – похоже, клик ушёл на закрытие календаря, "
                "жму ещё раз")
            page.wait_for_timeout(700)

    raise RuntimeError(
        "ВК не принял отложку: форма поста осталась открытой даже после "
        f"{tries} нажатий «Добавить в очередь». "
        + (_dialog_complaint(page) or
           "Видимой жалобы в окне нет – смотрите снимок экрана ниже и лог по шагам.")
        + " Проверьте «Отложенные записи» сообщества: если запись всё же "
          "появилась, формировать заново не нужно – будет дубль")


def _dialog_complaint(page) -> str:
    """
    Что ВК написал в окне: подсказка под полем, красный текст, всплывашка.
    Пусто – ничего похожего на жалобу не нашли.

    Берём короткие видимые строки (жалоба – это фраза, а не пол-страницы) и
    отсеиваем подписи самих кнопок: они есть всегда и ничего не объясняют.
    """
    skip = {"добавить в очередь", "запланировать", "далее", "назад", "отмена",
            "создать", "пост", "готово", "сегодня", "завтра"}
    try:
        found = page.evaluate(
            """() => {
                const out = [];
                const nodes = document.querySelectorAll(
                    '[role="dialog"] *, [class*="ubtle"], [class*="rror"], [role="alert"]');
                for (const el of nodes) {
                    if (el.children.length) continue;          // только листья
                    const t = (el.textContent || '').trim();
                    if (!t || t.length > 160) continue;
                    const box = el.getBoundingClientRect();
                    if (!box.width || !box.height) continue;   // невидимое мимо
                    out.push(t);
                }
                return out;
            }"""
        ) or []
    except Exception:  # noqa: BLE001 – диагностика не должна ронять отложку
        return ""
    words = []
    for line in found:
        low = line.lower().strip(" .!:")
        if low in skip or line in words or low.isdigit():
            continue
        words.append(line)
    # Жалоба почти всегда содержит одно из этих слов; если такие строки есть,
    # показываем именно их, иначе – весь короткий текст окна, как есть.
    marks = ("нельз", "ошиб", "не удал", "поздн", "прошл", "лимит", "превыш",
             "недоступ", "попроб", "невер", "должн", "макс", "не мож")
    hot = [w for w in words if any(m in w.lower() for m in marks)]
    pick = hot or words
    return ("ВК пишет: «" + " · ".join(pick[:4]) + "».") if pick else ""


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
        browser = yb._launch(pw, engine, headless=headless, extra_args=ANTIBOT_ARGS)
        try:
            context = browser.new_context(
                storage_state=str(session_path(project_id)),
                viewport={"width": 1280, "height": 900}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            context.add_init_script(ANTIBOT_INIT)
            page = context.new_page()

            target = same_domain(group_url)
            log(f"Открываю сообщество: {target}")
            page.goto(target, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2500)

            # Проверка «вы не робот». Она встречала не только вход, но и
            # отложку – а отложка про неё не знала: страница сообщества под
            # проверкой не открывается, и Click честно, но бесполезно писал
            # «кнопка Создать не нажалась, проверьте права администратора».
            if not _pass_captcha(page, log):
                return {"ok": False, "shot": _debug_shot(project_id, page, "captcha"),
                        "error": _CAPTCHA_ADVICE}
            if not is_logged_in(page):
                return {"ok": False, "shot": _debug_shot(project_id, page, "session"),
                        "error": "ВК открыл страницу как гостю – сессия не действует. "
                                 "Войдите заново в «Настройках» → «Вход в ВК»."}

            # «Создать» → «Пост». Меню – hover-флайаут: между кликами пауза
            # короткая, иначе меню закроется раньше, чем найдём пункт.
            log("Открываю форму поста")
            _open_post_form(page, log)
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

            # ВК цепляет к посту карточку сайта, увидев адрес в тексте, и она
            # оттесняет нашу картинку. Человек в этом месте жмёт крестик на
            # карточке – жмём и мы, но только если это точно её крестик.
            page.wait_for_timeout(1_200)
            yb.drop_link_card(page, yb.text_domains(text), log,
                              diag_dir=paths.data_root() / project_id / "crosspost")

            # «Далее» – к экрану, где живёт «Запланировать».
            page.click(f'{dlg} >> text="Далее"', timeout=15_000)
            page.wait_for_timeout(1000)

            # Последний заход на карточку сайта: ВК подтягивает её с задержкой
            # и мог успеть, пока Click жал «Далее».
            yb.drop_link_card(page, yb.text_domains(text), log, tries=1,
                              diag_dir=paths.data_root() / project_id / "crosspost")

            log(f"Ставлю таймер на {when.strftime('%d.%m.%Y %H:%M')} (Екатеринбург)")
            _set_schedule(page, when, log)

            yb._save_storage_state(context, session_path(project_id))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001 – наружу словами, решает вызывающий
            return {"ok": False, "error": str(e),
                    "shot": _debug_shot(project_id, page, "error") if page else None}
        finally:
            browser.close()
