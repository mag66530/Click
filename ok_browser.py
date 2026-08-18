"""
ok_browser.py – Одноклассники: вход с сохранением сессии и отложка в группе.

Почему браузер, а не API. Заявку на API-права ОК заказчику отклонили
(2026-08-10), поэтому ОК ведём тем же путём, что ВК: один раз входим,
сессия сохраняется, отложка ставится через родной интерфейс группы –
дальше пост держит и публикует сам ОК. API-клиент (ok_social.py) остаётся
в репозитории на случай, если права всё же дадут.

Откуда селекторы. Вход – проверен вживую (поля st.email/st.password, кнопка
«Войти» строго в форме пароля: на странице есть похожие поля поиска).
Отложка разобрана по живому прогону заказчицы 2026-08-11, её же словами:
«ищем "Создать новую тему" – нажимаем, вводим текст, ставим курсор в самый
верх и добавляем фото (плюсик → "Контентные блоки" → "Фотографии" →
"Загрузить фото"), потом галочку "Время публикации", правим дату и время
и жмём "Сохранить"; снизу появляется уведомление "Тема опубликуется <дата>
в <время>"». Это уведомление и считаем признаком успеха – урок, выученный
на ВК: спрашивать надо площадку, а не поля её формы.

Опираемся на ПОДПИСИ кнопок, а не на классы: классы у ОК меняются от
редизайна к редизайну, слова живут годами. Классы оставлены запасными.

Кнопку «Поделиться» не нажимаем никогда, когда речь об отложке: она
публикует пост СЕЙЧАС. Нужна только «Сохранить», появляющаяся после
галочки «Время публикации».
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

BASE = "https://ok.ru"
TIMEZONE_ID = "Asia/Yekaterinburg"   # как у ВК: календарь живёт по часам браузера

SEL = {
    # вход (проверено вживую)
    "login": 'input[name="st.email"]',
    "password": 'input[name="st.password"]',
    # «Войти через VK ID» – иконка ВК на форме входа ОК. ОСНОВНОЙ путь:
    # у аккаунтов брендов своего пароля ОК обычно нет, вход идёт через ВК.
    # Клик открывает всплывающее окно id.vk.com (проверено вживую 06.07.2026).
    # Разметка снята с живой страницы 2026-08-11:
    #   <a class="h-mod __small __vk_id social-icon-button"
    #      data-module="registration/vkconnect" …>
    # data-module – самый надёжный признак: классы у ОК меняются, а модуль,
    # который открывает окно ВК, называется так же не первый год.
    "vk_id_button": ("a[data-module='registration/vkconnect'], "
                     "a.social-icon-button.__vk_id, a[class*='__vk_id'], "
                     ".external-oauth-login a[class*='vk'], "
                     "a[title*='ВКонтакте'], a[aria-label*='ВКонтакте']"),
    # Плашка про cookie висит поверх формы и перехватывает клики.
    "cookie_accept": ("Разрешить все", "Разрешить всё", "Принять все",
                      "Принять всё", "Принять", "Хорошо"),
    # Проверка профиля: «Имя – это вы? Мы заметили, что этот профиль мог
    # попасть к злоумышленникам». Показывается поверх любой страницы, в том
    # числе поверх группы, и никуда не пускает, пока не подтвердить.
    "profile_marks": ("это вы?", "мог попасть к злоумышленникам",
                      "подтвердить, что это ваш профиль"),
    "profile_yes": ("Да, подтвердить", "Да, это я", "Подтвердить"),
    # Кнопку «Это не мой профиль» НЕ НАЖИМАЕМ НИКОГДА: это жалоба на угон,
    # после неё аккаунт уходит на блокировку и восстановление. Держим её
    # здесь явным списком-запретом, чтобы никто случайно не добавил её в
    # общий перебор подписей.
    "profile_never": ("Это не мой профиль", "Это не я"),
    # Следующий экран после подтверждения профиля: «Получите проверочный
    # код» – ОК шлёт СМС на телефон аккаунта. Кнопки ввода тут ещё нет,
    # только «Получить код», поэтому шаг узнаём по словам.
    "verify_marks": ("Получите проверочный код", "убедимся, что это ваш профиль",
                     "отправим бесплатное СМС", "отправим бесплатное смс"),
    "verify_send": ("Получить код", "Отправить код", "Выслать код", "Получить"),
    "verify_by_mail": ("Подтвердить по эл. почте", "Подтвердить по почте"),
    # Экран ввода кода. Имя поля у ОК разное на разных экранах – берём
    # широкий набор, а сам факт «мы на вводе кода» определяем по наличию
    # такого поля, а не по подписи.
    "verify_code": ('input[name="st.smsCode"]', 'input[name="smsCode"]',
                    'input[name="st.code"]', 'input[name="code"]',
                    'input[autocomplete="one-time-code"]',
                    'input[inputmode="numeric"]', 'input[type="tel"]'),
    "verify_submit": ("Подтвердить", "Далее", "Продолжить", "Готово", "Отправить"),
    # Куда уходит вход. Взято из самой кнопки на живой странице:
    #   data-url="https://connect.vk.com/auth?…"
    # ВК ID открывается ДВУМЯ способами – отдельным окном (window.open) и
    # слоем прямо в странице. Второй случай Click раньше принимал за «клик
    # не сработал»: ждал окна, не дожидался и сдавался.
    "vkid_hosts": ("connect.vk.com", "connect.vk.ru", "id.vk.com", "id.vk.ru",
                   "oauth.vk.com", "oauth.vk.ru", "login.vk.com", "login.vk.ru"),
    "popup_phone": 'input[name="login"]',
    "popup_password": 'input[name="password"]',
    "popup_next": 'button:has-text("Продолжить")',
    # Проверка «вы не робот» – ВК показывает её и во всплывающем окне.
    "captcha_box": ('text="Проверяем, что вы не робот"', "#captcha", ".vkc__Captcha",
                    'iframe[src*="captcha"]'),
    "captcha_continue": ('button:has-text("Продолжить")', 'button:has-text("Начать")'),
    "code_single": 'input[inputmode="numeric"], input[name="code"], input[autocomplete="one-time-code"]',
    "code_boxes": 'input[maxlength="1"]',
    # ─── Форма поста и отложка ──────────────────────────────────────
    # Разобрано по живому прогону заказчицы (2026-08-11), её же словами:
    # «ищем "Создать новую тему" – нажимаем, вводим текст, ставим курсор в
    #  самый верх и добавляем фото (плюсик → "Контентные блоки" →
    #  "Фотографии" → "Загрузить фото"), потом галочку "Время публикации",
    #  правим дату и время и жмём "Сохранить"; снизу появляется уведомление
    #  "Тема опубликуется <дата> в <время>"».
    #
    # Опираемся на ПОДПИСИ, а не на классы: у ОК классы вида pf-head_itx_a
    # меняются от редизайна к редизайну, а слова на кнопках живут годами.
    # Классы оставлены запасными вариантами – они пока работают.
    "create_post": ('text="Создать новую тему"', 'text="Создать тему"',
                    'text="Добавить тему"', "a.pf-head_itx_a",
                    '[data-l*="createTopic"]', '[data-l*="postingForm"]',
                    'text="Напишите заметку"', 'text="Добавить запись"',
                    '.posting-form, .js-posting-form'),
    "text": ('.js-posting-itx[contenteditable="true"]',
             '[role="dialog"] [contenteditable="true"]',
             '[contenteditable="true"]'),
    # Плюсик слева от строки: наводишь – всплывает «Контентные блоки».
    "block_plus": ('[role="dialog"] [class*="add-block"]',
                   '[role="dialog"] button[title*="обав"]',
                   '[role="dialog"] [class*="plus"]'),
    "menu_content_blocks": 'text="Контентные блоки"',
    "menu_photos": ('text="Фотографии"', 'text="Фото"'),
    # Кнопка нижнего ряда – запасной путь к тому же окну выбора фото.
    "photo_btn": ('[role="dialog"] >> text="Фото"', ".js-photos-btn"),
    "upload_photo": ('text="Загрузить фото"', 'text="Загрузить"'),
    "file_input": 'input[type="file"]',
    # Галочка «Время публикации» и поля под ней. Разметка снята заказчицей
    # с живой формы 12.08.2026 – поэтому здесь ИМЕНА полей, а не подписи:
    #   <input name="timer" class="irc gpf-timer-post" type="checkbox">
    #   <span class="irc-vis __n"></span>          ← нарисованный квадратик
    #   <span class="irc_l">Время публикации</span>
    # Сам input невидим, его подменяет span.irc-vis: щёлкать надо по нему
    # или по label, а не по input – по невидимому Playwright не попадёт.
    "schedule_checkbox": 'input[name="timer"], input.gpf-timer-post',
    "schedule_toggle": ('label:has(input[name="timer"]) span.irc-vis',
                        'label:has(input[name="timer"])',
                        'span.irc_l:has-text("Время публикации")',
                        'text="Время публикации"'),
    # Поля даты и времени: имена st.layer.* – самые устойчивые признаки.
    "date_input": ('input[name="st.layer.date"]', "input.pform_delay_form_date",
                   "input.js-time-editor-datepicker"),
    "hours_select": ('select[name="st.layer.hours"]', "select.pform_delay_form_hh",
                     "select.js-time-editor-select-hh"),
    "mins_select": ('select[name="st.layer.mins"]', "select.pform_delay_form_mm",
                    "select.js-time-editor-select-mm"),
    # Одна и та же кнопка: без галочки это «Поделиться» (публикует сейчас),
    # с галочкой её подпись меняется на «Сохранить» (ставит отложку).
    # data-save="Сохранить" – ровно об этом, из разметки заказчицы:
    #   <button data-l="t,button.submit" data-save="Сохранить" title="Поделиться"
    #           class="posting_submit button-pro js-publish-btn">Сохранить</button>
    "submit_scheduled": ("button.js-publish-btn[data-save]", "button.js-publish-btn",
                         'button:has-text("Сохранить")'),
    "submit_now": ('button:has-text("Поделиться")', "button.posting_submit.js-publish-btn"),
    # Ответ самого ОК: «Тема опубликуется 13.08.2026 в 08:59». Это и есть
    # правда о том, встала отложка или нет – урок, выученный на ВК.
    "toast_scheduled": ("Тема опубликуется", "Тема будет опубликована",
                        "будет опубликован"),
}


def delayed_url(group_url: str) -> str:
    """
    Адрес «Отложенных» группы: ok.ru/group/<id>/delayed.

    Там лежат созданные отложки – по нему и проверяем результат, когда
    всплывашка «Тема опубликуется …» уже погасла. Запись надёжнее
    уведомления: уведомление живёт секунды, запись – до публикации.
    """
    url = (group_url or "").strip().rstrip("/")
    if not url:
        return ""
    for tail in ("/topics", "/delayed"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    return url + "/delayed"


def _found_in_delayed(page, group_url: str, text: str,
                      log: Callable[[str], None]) -> str:
    """
    Найти нашу запись в «Отложенных» группы. Возвращает строку с
    подтверждением или пусто.

    Спрашиваем у ОК напрямую, вместо того чтобы гадать по исчезнувшей
    всплывашке. Сверяем по началу текста: заголовков у тем нет, а первые
    слова у пробной и настоящей записи всегда свои.
    """
    where = delayed_url(group_url)
    if not where:
        return ""
    try:
        page.goto(where, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2_500)
        body = page.evaluate(
            "() => document.body ? (document.body.innerText || '') : ''") or ""
    except Exception as e:  # noqa: BLE001
        log(f"  «Отложенные» открыть не вышло: {str(e).split(chr(10))[0]}")
        return ""
    head = " ".join((text or "").split())[:40]
    if head and head in " ".join(body.split()):
        # Рядом с записью ОК пишет «будет опубликовано сегодня в 15:40» –
        # забираем эту строку целиком, она и есть ответ площадки.
        for line in body.split("\n"):
            if "будет опубликовано" in line:
                return " ".join(line.split())
        return "запись найдена в «Отложенных» группы"
    return ""


def topics_url(group_url: str) -> str:
    """
    Адрес вкладки «Темы» группы – именно там живёт «Создать новую тему».

    Разобрано по снимкам заказчицы (12.08.2026). Click открывал группу по
    её обычному адресу и попадал на ЛЕНТУ: там сплошные опубликованные
    посты, а поля для новой темы нет вовсе. Отсюда «не нашли Создать новую
    тему» – искали на странице, где её и быть не могло, и никакие повторы
    с прокруткой помочь не могли.

    Поле лежит на вкладке «Темы»: ok.ru/group/<id>/topics.
    """
    url = (group_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/topics"):
        return url
    return url + "/topics"


def session_path(project_id: str) -> Path:
    d = paths.data_root() / project_id / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d / "ok-state.json"


# Куки, по которым ОК узнаёт вошедшего. Как и у ВК, гостевой заход тоже
# ставит куки (язык, счётчики) – без этой проверки «сессия сохранена»
# показывалось бы и после неудачного входа.
# Признак входа определяем ОТ ОБРАТНОГО – по тому, чего у гостя быть не может.
#
# Раньше здесь стоял список «правильных» имён (AUTH_ID, AUTH_SIG, OK_LOGIN), и
# он подвёл дважды. Заказчица вошла в ОК руками, а файл сессии не сохранился:
# ни одного имени из списка среди её куков не оказалось – ОК зовёт их иначе.
# А в обратную сторону список врал: в нём был JSESSIONID, который ОК выдаёт
# ЛЮБОМУ, даже не входя, – и Click объявлял «сессия сохранена» о пустышке.
#
# Угадывать имена бессмысленно: их меняют, не спросив нас. Зато список
# гостевых кук снимается с живой ok.ru за секунду (сделано 11.08.2026) и
# меняется куда реже. Есть кука сверх гостевых – значит вошли.
GUEST_COOKIES = frozenset({
    "bci", "_statid", "JSESSIONID", "cookieChoice", "ss_wb",
    "TZ", "TZO", "_flashVersion", "tmr_lvid", "tmr_lvidTS",
    "_ym_uid", "_ym_d", "_ym_isad", "_ga", "_gid",
})
# Имена, которые точно означают вход, – быстрый путь. Список неполный, и
# полагаться ТОЛЬКО на него нельзя (см. выше).
AUTH_COOKIES = ("AUTH_ID", "auth_id", "AUTH_SIG", "OK_LOGIN", "AUTHCODE")


def cookie_names(cookies: list) -> list[str]:
    """Имена кук со значением – для проверок и для понятных сообщений."""
    return sorted({str(c.get("name", "")) for c in cookies or [] if c.get("value")})


def looks_logged_in(cookies: list) -> bool:
    """Похоже ли, что в файле сессия ВОШЕДШЕГО, а не гостя."""
    names = set(cookie_names(cookies))
    return bool(names & set(AUTH_COOKIES) or names - GUEST_COOKIES)


def has_saved_session(project_id: str) -> bool:
    """Есть ли сохранённая сессия С ПРИЗНАКОМ ВХОДА (не просто куки гостя)."""
    fp = session_path(project_id)
    if not fp.exists():
        return False
    try:
        import json
        cookies = json.loads(fp.read_text(encoding="utf-8")).get("cookies") or []
        return looks_logged_in(cookies)
    except Exception:  # noqa: BLE001
        return False


def import_session(project_id: str, raw: bytes) -> tuple[bool, str]:
    """
    Принять готовый файл сессии ОК (storage_state) – см. тот же приём в
    vk_social: из облака пройти проверки ВК невозможно, поэтому даём
    принести сессию, полученную там, где браузер видно.
    """
    import json

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"Это не файл сессии: {e}"
    if not isinstance(data, dict) or "cookies" not in data:
        return False, "В файле нет раздела cookies – нужен storage_state Playwright."
    cookies = data.get("cookies") or []
    if not looks_logged_in(cookies):
        # Имена показываем прямо в отказе: без них «нет куки входа» – тупик,
        # в котором непонятно, что нести дальше. С именами видно сразу, гость
        # это или ОК просто назвал куки по-новому.
        found = ", ".join(cookie_names(cookies)[:12]) or "ни одной"
        return False, ("В файле только гостевые куки ОК – похоже, вход не "
                       f"завершился. Что нашли: {found}. Войдите в ОК "
                       "(в окне из VHOD-VK-i-OK.py) и сохраните сессию заново.")
    session_path(project_id).write_text(json.dumps(data, ensure_ascii=False),
                                        encoding="utf-8")
    return True, f"Сессия ОК принята: {len(cookies)} куки, признак входа на месте."


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


def dismiss_cookies(page) -> bool:
    """
    Убрать плашку «Мы используем cookie-файлы» сверху страницы.

    Она висит поверх формы и перехватывает клики: Playwright в таком случае
    жалуется «элемент перекрыт другим элементом», а человек видит только,
    что ничего не нажалось. Нет плашки – ничего не делаем.
    """
    for label in SEL["cookie_accept"]:
        try:
            btn = page.locator(f'button:has-text("{label}")').first
            if btn.count():
                btn.click(timeout=3000)
                page.wait_for_timeout(500)
                return True
        except Exception:  # noqa: BLE001 – плашки может не быть
            continue
    return False


def wait_vk_button(page, timeout_ms: int = 20_000):
    """
    Дождаться значка ВК в ряду иконок под кнопкой «Войти по QR-коду».

    Почему ЖДАТЬ, а не искать один раз. В исходном HTML страницы входа ОК
    эта кнопка лежит внутри <template style="display:none"> – это заготовка,
    которую React (vkid-form-adapter) достаёт и вставляет в страницу уже
    после загрузки. Пока он не отработал, найти её нельзя в принципе:
    внутрь <template> поиск по странице не заходит вообще.

    Ровно на это Click и наступил: смотрел один раз через две секунды и
    честно писал «кнопки нет», хотя на снимке экрана она уже была видна.
    """
    deadline = _time.time() + timeout_ms / 1000.0
    while _time.time() < deadline:
        try:
            frames = [page] + [f for f in page.frames if f != page.main_frame]
        except Exception:  # noqa: BLE001
            frames = [page]
        for fr in frames:
            try:
                btn = fr.locator(SEL["vk_id_button"]).first
                if btn.count() and btn.is_visible():
                    return btn
            except Exception:  # noqa: BLE001 – кадр мог отвалиться
                continue
        try:
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            break
    return None


def asks_profile(page) -> bool:
    """Стоит ли на экране проверка «Это вы?» – она загораживает всё остальное."""
    try:
        text = page.inner_text("body") or ""
    except Exception:  # noqa: BLE001
        return False
    return any(m.lower() in text.lower() for m in SEL["profile_marks"])


def confirm_profile(page, log: Callable[[str], None] | None = None) -> bool:
    """
    Нажать «Да, подтвердить» на проверке профиля ОК.

    ОК периодически спрашивает «Имя – это вы? Мы заметили, что этот профиль
    мог попасть к злоумышленникам». Пока не подтвердить, дальше не пускают
    вообще: ни в ленту, ни в группу. Для нас это выглядело как «сессия не
    действует», хотя вход целый.

    ОСОБО. Вторую кнопку, «Это не мой профиль», не нажимаем никогда – это
    жалоба на угон, после которой аккаунт уходит на блокировку. Поэтому
    ищем строго по подписям подтверждения и ни при каких условиях не
    берём «первую попавшуюся кнопку».
    """
    log = log or (lambda m: None)
    if not asks_profile(page):
        return False
    for label in SEL["profile_yes"]:
        if label in SEL["profile_never"]:        # страховка от правки списка
            continue
        try:
            btn = page.locator(f'button:has-text("{label}"), '
                               f'a:has-text("{label}"), '
                               f'input[value="{label}"]').first
            if btn.count():
                log(f"ОК спрашивает, наш ли это профиль – подтверждаю ({label})")
                btn.click(timeout=8000)
                page.wait_for_timeout(3000)
                return True
        except Exception:  # noqa: BLE001 – пробуем следующую подпись
            continue
    log("ОК просит подтвердить профиль, но кнопку подтверждения не нашли")
    return False


def _page_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:  # noqa: BLE001
        return ""


def _click_label(page, labels, timeout: int = 8000) -> str:
    """Нажать кнопку/ссылку с одной из подписей. Возвращает, что нажали."""
    for label in labels:
        try:
            loc = page.locator(f'button:has-text("{label}"), '
                               f'a:has-text("{label}"), '
                               f'input[value="{label}"]').first
            if loc.count():
                loc.click(timeout=timeout)
                page.wait_for_timeout(2500)
                return label
        except Exception:  # noqa: BLE001 – пробуем следующую подпись
            continue
    return ""


def code_field(page):
    """Поле ввода проверочного кода ОК – или None, если его на экране нет."""
    for sel in SEL["verify_code"]:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def asks_verify(page) -> bool:
    """Экран «Получите проверочный код» – ОК готов выслать СМС."""
    text = _page_text(page).lower()
    return any(m.lower() in text for m in SEL["verify_marks"])


def request_verify_code(page, log: Callable[[str], None] | None = None) -> bool:
    """Нажать «Получить код» – ОК вышлет СМС на телефон аккаунта."""
    log = log or (lambda m: None)
    label = _click_label(page, SEL["verify_send"])
    if label:
        log(f"ОК высылает проверочный код по СМС (нажали «{label}»)")
        return True
    log("ОК просит проверочный код, но кнопки «Получить код» не нашли")
    return False


def request_verify_by_mail(page, log: Callable[[str], None] | None = None) -> bool:
    """Запасной путь – подтверждение письмом, если телефона под рукой нет."""
    log = log or (lambda m: None)
    label = _click_label(page, SEL["verify_by_mail"])
    if label:
        log("ОК высылает подтверждение на почту")
        return True
    return False


def submit_verify_code(page, code: str,
                       log: Callable[[str], None] | None = None) -> bool:
    """Вписать код из СМС и подтвердить."""
    log = log or (lambda m: None)
    field = code_field(page)
    if field is None:
        log("Поле для кода на экране не нашли")
        return False
    try:
        field.fill(code.strip())
    except Exception:  # noqa: BLE001
        return False
    if not _click_label(page, SEL["verify_submit"], timeout=6000):
        # Часть форм ОК подтверждается просто Enter.
        try:
            page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            return False
    log("Код отправлен в ОК")
    return True


def page_block(page) -> str:
    """
    Что заслоняет ОК прямо сейчас. Пусто – ничего не заслоняет.

    Проверки идут ЦЕПОЧКОЙ: сначала «это вы?», потом «получите код», потом
    ввод кода. Каждый экран сам по себе выглядит как «сессия не работает»,
    хотя вход целый – поэтому разбираем их в одном месте и одинаково и для
    входа через ВК, и для входа паролем, и при постановке отложки.
    """
    if asks_profile(page):
        return "profile"
    # Обычная форма входа – это не проверка. Проверяем ДО поля кода: у формы
    # входа поле телефона тоже бывает числовым, и без этой оговорки гостевая
    # страница выглядела бы как «введите код».
    try:
        if page.locator(SEL["password"]).count():
            return ""
    except Exception:  # noqa: BLE001
        pass
    if code_field(page) is not None:
        return "verify-code"
    if asks_verify(page):
        return "verify"
    return ""


def safe_url(url: str) -> str:
    """
    Адрес без «хвоста» – для показа человеку и для логов.

    В ссылке входа ВК едет state с одноразовым токеном. Показывать его на
    экране и писать в журнал нельзя: это, по сути, ключ от входа.
    """
    return (url or "").split("?", 1)[0][:120]


def vkid_frame(page):
    """
    Кадр с формой ВК ID внутри страницы ОК – если вход открылся слоем.
    None – значит слоя нет (либо ВК открылся отдельным окном, либо клик
    вообще ничего не сделал).
    """
    try:
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            url = fr.url or ""
            if any(h in url for h in SEL["vkid_hosts"]):
                return fr
    except Exception:  # noqa: BLE001
        pass
    return None


def wait_vkid_frame(page, timeout_ms: int = 12_000):
    """Дождаться слоя ВК ID: он подгружается не мгновенно."""
    deadline = _time.time() + timeout_ms / 1000.0
    while _time.time() < deadline:
        fr = vkid_frame(page)
        if fr is not None:
            return fr
        try:
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            break
    return None


def _debug_shot(project_id: str, page, name: str) -> bytes | None:
    """
    Снимок экрана в момент неудачи. Возвращает КАРТИНКУ, а не путь к ней.

    Путь возвращать было бессмысленно: в облаке файловая система своя, и
    строка вида /home/appuser/.local/share/click/… человеку недоступна
    никак. Заказчица получила такой путь вместо картинки и справедливо
    попросила «пусть показывает экран, где запинается». Картинку раздел
    показывает прямо на странице.

    На диск тоже кладём – на своём компьютере это удобно, и снимок
    переживёт закрытие вкладки.
    """
    try:
        blob = page.screenshot(type="png", full_page=False)
    except Exception:  # noqa: BLE001
        return None
    try:
        d = paths.data_root() / project_id / "crosspost"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"ok-debug-{name}.png").write_bytes(blob)
    except OSError:
        pass                               # не записалось – картинка всё равно есть
    return blob


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

            dismiss_cookies(self.page)
            btn = wait_vk_button(self.page)
            if btn is None:
                return {**self.state(), "step": "no-vk-button",
                        "note": "На форме входа ОК так и не появился значок ВК – "
                                "он в ряду иконок под кнопкой «Войти по QR-коду». "
                                "Можно войти обычным логином и паролем ОК ниже."}
            # ВК ID открывается двумя способами. Ждём окно недолго – и, если
            # его нет, ищем слой прямо в странице: это такой же штатный
            # вариант, а не «клик не сработал».
            try:
                with self.page.expect_popup(timeout=10_000) as info:
                    btn.click()
                self.popup = info.value
                self.popup.wait_for_load_state("domcontentloaded")
                self.popup.wait_for_timeout(4000)
            except Exception:  # noqa: BLE001 – отдельного окна не было
                if wait_vkid_frame(self.page) is not None:
                    self.popup = self.page          # работаем прямо в странице
                    self.page.wait_for_timeout(2500)
                else:
                    seen = [safe_url(f.url) for f in self.page.frames
                            if f != self.page.main_frame and f.url]
                    return {**self.state(), "step": "no-popup",
                            "note": "Значок ВК нажали, но форма входа ВК не "
                                    "появилась – ни отдельным окном, ни слоем в "
                                    "странице. Кадры на странице сейчас: "
                                    + (", ".join(seen[:6]) or "нет ни одного")}
            # Сессия ВК жива – ВК не спросит ни телефон, ни код, а покажет
            # «Войти как Имя». Это и есть та самая связка, ради которой всё
            # затевалось: подтверждаем сразу, иначе ОК так и стоит на форме.
            self.confirm_account()
            # Сессии нет – ВК сперва предлагает QR, уходим на ввод телефона.
            if not self._popup_gone() and _vk.click_in_frames(
                    self.popup, ("Войти другим способом", "Другой способ")):
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
    @property
    def inline(self) -> bool:
        """Форма ВК открылась слоем в самой странице ОК, а не отдельным окном."""
        return self.popup is not None and self.popup is self.page

    def _popup_gone(self) -> bool:
        """
        Окна ВК больше нет – вход подтверждён, трогать его нельзя.

        Для слоя в странице «закрыться» нечему: там признак завершения –
        то, что ОК нас уже пустил. Это проверяется отдельно, в page_state.
        """
        try:
            if self.popup is None:
                return True
            if self.inline:
                return False
            return self.popup.is_closed()
        except Exception:  # noqa: BLE001
            return True

    # ─── ввод и нажатия по всем кадрам ──────────────────────────────
    #
    # Почему не self.popup.fill(). Форма ВК ID живёт ВНУТРИ кадра, а
    # page.fill() ищет только в главном кадре страницы. Пока вход открывался
    # отдельным окном, это сходило с рук: там форма была сверху. Со слоем в
    # странице тот же вызов не находит ничего. Ищем по всем кадрам сразу –
    # так работает в обоих случаях.
    def _frames(self) -> list:
        import vk_social as _vk
        if self._popup_gone():
            return []
        return _vk.vk_frames(self.popup)

    def _find(self, selector: str):
        """Первый живой элемент по селектору среди всех кадров."""
        for fr in self._frames():
            try:
                loc = fr.locator(selector)
                if loc.count():
                    return loc
            except Exception:  # noqa: BLE001 – кадр мог отвалиться
                continue
        return None

    def _count(self, selector: str) -> int:
        loc = self._find(selector)
        try:
            return loc.count() if loc is not None else 0
        except Exception:  # noqa: BLE001
            return 0

    def _fill(self, selector: str, value: str) -> bool:
        loc = self._find(selector)
        if loc is None:
            return False
        try:
            loc.first.fill(value)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _click_sel(self, selector: str, timeout: int = 5000) -> bool:
        loc = self._find(selector)
        if loc is None:
            return False
        try:
            loc.first.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _wait(self, ms: int) -> None:
        try:
            if not self._popup_gone():
                self.popup.wait_for_timeout(ms)
        except Exception:  # noqa: BLE001
            pass

    def _asks_confirm(self) -> bool:
        """Показывает ли окно ВК вопрос «Войти как Имя?»."""
        import vk_social as _vk
        if self._popup_gone():
            return False
        for fr in _vk.vk_frames(self.popup):
            text = _vk.frame_text(fr)
            if "Войти как" in text or "Продолжить как" in text:
                return True
        return False

    def confirm_account(self) -> dict:
        """
        Нажать «Войти как Имя» в окне ВК.

        Когда сессия ВК жива, ВК не спрашивает ни телефона, ни кода – он
        сразу предлагает войти уже известным ему аккаунтом. Пока эту кнопку
        никто не нажмёт, окно висит, а вкладка ОК остаётся на форме входа.
        Подписи разные («Войти как…», «Продолжить как…», «Подтвердить»),
        поэтому пробуем по очереди, от самой точной к общей.
        """
        import vk_social as _vk

        for labels in (("Войти как", "Продолжить как"),
                       ("Подтвердить", "Разрешить"),
                       ("Продолжить",)):
            if self._popup_gone():
                break
            if _vk.click_in_frames(self.popup, labels, timeout=3000):
                self._wait(3000)
                break
        return self.state()

    def submit_phone(self, phone: str) -> dict:
        self._fill(SEL["popup_phone"], phone.strip())
        self._click_sel(SEL["popup_next"])
        self._wait(3000)
        return self.state()

    def submit_password(self, password: str) -> dict:
        self._fill(SEL["popup_password"], password)
        self._click_sel(SEL["popup_next"])
        self._wait(3000)
        return self.state()

    def request_code_instead(self) -> dict:
        """Уйти с пароля на SMS-код – у аккаунтов брендов пароля обычно нет."""
        import vk_social as _vk
        for label in ("Забыли или не установили пароль?", "Нет, восстановить пароль",
                      "Отправить код", "Получить код"):
            if self._popup_gone():
                break
            if _vk.click_in_frames(self.popup, (label,), timeout=4000):
                self._wait(1500)
        return self.state()

    def press_captcha_continue(self) -> dict:
        """Нажать «Продолжить» в проверке «вы не робот» внутри окна ВК."""
        import vk_social as _vk
        if self._popup_gone():
            return self.state()
        fr = _vk.captcha_frame(self.popup)
        if fr is not None:
            _vk.press_captcha_in(fr)
            self._wait(4000)
        return self.state()

    def submit_code(self, code: str) -> dict:
        code = code.strip()
        single = self._find(SEL["code_single"])
        if single is not None:
            single.first.fill(code)
        else:
            boxes = self._find(SEL["code_boxes"])
            if boxes is not None and boxes.count() >= len(code):
                for i, digit in enumerate(code):
                    boxes.nth(i).fill(digit)
        try:
            self.popup.keyboard.press("Enter")
        except Exception:  # noqa: BLE001 – у кадра своей клавиатуры нет
            self.page.keyboard.press("Enter")
        self._wait(3500)
        return self.state()

    # ─── что на экране ──────────────────────────────────────────────
    def _settle(self) -> str:
        """
        Разобрать загораживающие экраны ОК до того, как определять шаг.

        Что можем сами – делаем сами: «Это вы?» подтверждаем без вопросов,
        человек тут ничего не решает. Что без человека нельзя – код из СМС –
        возвращаем наверх как отдельный шаг. Пусто = ничего не мешает.
        """
        block = page_block(self.page)
        if block == "profile":
            confirm_profile(self.page)
            try:
                self.page.reload(wait_until="domcontentloaded", timeout=30_000)
                self.page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001
                pass
            block = page_block(self.page)
        return block

    def confirm_profile_step(self) -> dict:
        """Кнопка «Да, это наш профиль» из интерфейса – если сами не смогли."""
        self._settle()
        return self.state()

    def request_code_step(self) -> dict:
        """«Получить код» – ОК вышлет СМС на телефон аккаунта."""
        request_verify_code(self.page)
        return self.state()

    def request_mail_step(self) -> dict:
        """Запасной путь: подтверждение письмом, если телефона нет под рукой."""
        request_verify_by_mail(self.page)
        return self.state()

    def submit_verify_step(self, code: str) -> dict:
        """Вписать код из СМС и подтвердить."""
        submit_verify_code(self.page, code)
        try:
            self.page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001
            pass
        return self.state()

    def page_state(self) -> dict:
        # Окно закрылось – ВК подтвердил вход, смотрим на саму вкладку ОК.
        if self._popup_gone():
            try:
                self.page.wait_for_timeout(1500)
                self.page.reload(wait_until="domcontentloaded", timeout=30_000)
                self.page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001
                pass
            block = self._settle()
            if block:
                return {"step": block}
            return {"step": "done"} if is_logged_in(self.page) else {"step": "login"}
        # Вход слоем: закрываться нечему, признак успеха – что ОК уже пустил.
        if self.inline and vkid_frame(self.page) is None:
            block = self._settle()
            if block:
                return {"step": block}
            if is_logged_in(self.page):
                return {"step": "done"}
        try:
            import vk_social as _vk
            if _vk.captcha_frame(self.popup) is not None:
                return {"step": "captcha"}
            # Проверяем ДО телефона и пароля: на этом экране полей ввода нет,
            # иначе шаг определился бы как «unknown» и человек бы застрял.
            if self._asks_confirm():
                return {"step": "consent"}
            if self._count(SEL["popup_password"]):
                return {"step": "password"}
            if (self._count(SEL["code_boxes"]) >= 4
                    or self._count(SEL["code_single"])):
                return {"step": "code"}
            if self._count(SEL["popup_phone"]):
                return {"step": "phone"}
            return {"step": "unknown"}
        except Exception:  # noqa: BLE001
            return {"step": "unknown"}

    def state(self) -> dict:
        st = self.page_state()
        # Снимок всегда со страницы, если окна ВК нет либо оно и есть страница:
        # у кадра своего снимка не бывает.
        shot_from = self.page if self._popup_gone() else self.popup
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

    def _settle(self) -> str:
        """Разобрать загораживающие экраны ОК – см. одноимённый метод выше."""
        block = page_block(self.page)
        if block == "profile":
            confirm_profile(self.page)
            try:
                self.page.reload(wait_until="domcontentloaded", timeout=30_000)
                self.page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001
                pass
            block = page_block(self.page)
        return block

    def confirm_profile_step(self) -> dict:
        """Подтвердить, что профиль наш – та же кнопка, что и у входа через ВК."""
        self._settle()
        return self.state()

    def request_code_step(self) -> dict:
        request_verify_code(self.page)
        return self.state()

    def request_mail_step(self) -> dict:
        request_verify_by_mail(self.page)
        return self.state()

    def submit_verify_step(self, code: str) -> dict:
        submit_verify_code(self.page, code)
        try:
            self.page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001
            pass
        return self.state()

    def page_state(self) -> dict:
        try:
            # Проверки ОК заслоняют всё: разбираем их первыми, иначе вошедший
            # аккаунт выглядит как невошедший.
            block = self._settle()
            if block:
                return {"step": block}
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
def _click_first(page, candidates, timeout: int = 8_000) -> str:
    """Нажать первый попавшийся из кандидатов. Пусто – ни один не нашёлся."""
    if isinstance(candidates, str):
        candidates = (candidates,)
    for sel in candidates:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                el.click(timeout=timeout)
                return sel
        except Exception:  # noqa: BLE001 – пробуем следующего кандидата
            continue
    return ""


def _select_closest(page, selector: str, want: str) -> str:
    """
    Выбрать значение в списке; нет такого – ближайшее НЕ РАНЬШЕ него.
    Возвращает выбранное или пусто, если не вышло.

    Зачем не просто select_option. У ОК в минутах только кратные пяти, и
    попытка выбрать «22» кончалась получасовым ожиданием: Playwright
    честно ждал появления несуществующего пункта, повторяя «did not find
    some options», пока не истечёт таймаут. Мы округляем время заранее, но
    список ОК может смениться (скажем, на шаг в десять минут) – и тогда
    лучше взять соседнее значение, чем повесить прогон.

    Не раньше – потому что пост не должен выйти раньше назначенного.
    """
    try:
        options = page.eval_on_selector(
            selector,
            "el => Array.from(el.options).map(o => o.value)") or []
    except Exception:  # noqa: BLE001
        options = []
    if not options:
        return ""
    target = want if want in options else ""
    if not target:
        try:
            later = sorted((o for o in options if o.isdigit() and int(o) >= int(want)),
                           key=int)
        except ValueError:
            return ""
        if not later:
            # Ничего не раньше нужного нет. Молча взять меньшее нельзя:
            # пост вышел бы РАНЬШЕ назначенного, а это хуже отказа.
            return ""
        target = later[0]
    try:
        page.select_option(selector, target, timeout=8_000)
    except Exception:  # noqa: BLE001
        return ""
    return target


def _letters(s: str) -> str:
    """
    Только буквы и цифры, в нижнем регистре.

    Сверять набранное по ним надёжнее, чем посимвольно: в поле не попадают
    ни звёздочки разметки, ни переносы строк (textContent склеивает абзацы
    без них), а эмодзи ОК рисует картинками.
    """
    import re
    return re.sub(r"\W", "", s or "", flags=re.U).lower()


def _diag_dir(project_id: str) -> Path:
    """Куда складывать разметку для разбора – рядом с логом формирования."""
    d = paths.data_root() / project_id / "crosspost"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_editor_markup(page, text_sel: str, project_id: str,
                        log: Callable[[str], None]) -> None:
    """
    Сохранить разметку поля темы и панели над ним.

    Затем, что жирный в ОК Click так и не выставил, а вслепую его чинить
    больше нельзя: у площадки своя вёрстка, и угадывать её по классам –
    это те самые круги, на которые ушёл день. По этому файлу видно всё:
    есть ли у поля панель форматирования, какая у неё кнопка «Ж» и что
    редактор сделал с нашим текстом.
    """
    try:
        html = page.eval_on_selector(
            text_sel,
            """(el) => {
                 let box = el;
                 for (let i = 0; i < 3 && box.parentElement; i++) box = box.parentElement;
                 return (box.outerHTML || '').slice(0, 12000);
               }""") or ""
        if html:
            (_diag_dir(project_id) / "ok-editor.html").write_text(html, encoding="utf-8")
            log("  разметку поля ОК сохранил рядом с логом (ok-editor.html) – "
                "пришлите её, и жирный будет сделан точно")
    except Exception:  # noqa: BLE001 – диагностика не должна ронять прогон
        pass


def _lines(s: str) -> list[str]:
    """
    Непустые строки текста, по буквам каждой – «форма» поста.

    По ней видно то, чего не видно по буквам целиком: разъехавшиеся строки.
    Редактор ОК на вставленной разметке <b> разносит жирные куски по своим
    абзацам, и пост из «Диаметр: 0,25 мм;» превращается в «Диаметр» и
    «: 0,25 мм;Длина волокна:» – буквы те же, читать невозможно.
    """
    return [_letters(ln) for ln in (s or "").split("\n") if _letters(ln)]


# Разметка НАКЛАДЫВАЕТСЯ НА ГОТОВЫЙ ТЕКСТ, а не вводится вместе с ним.
#
# Так это делает человек: пишет пост, потом выделяет мышью кусок и жмёт «Ж»
# или «вставить ссылку». И только так текст не может пострадать: он уже в
# поле, целый, а разметка ложится поверх. Оба прежних способа – набор с
# Ctrl+B и вставка готового <b> – текст ПЕРЕПИСЫВАЛИ, и один из них развалил
# пост по строкам (14.08.2026), а второй тихо не сработал вовсе, но Click
# отрапортовал «жирный сохранён»: он сверял только буквы, а форматирование
# не проверял ни разу.
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


def _apply_marks(page, text_sel: str, spans: list[dict]) -> dict:
    """Наложить жирный на уже введённый текст. Возвращает отчёт."""
    try:
        return page.evaluate(_MARK_JS, {"sel": text_sel, "spans": spans}) or {}
    except Exception as e:  # noqa: BLE001 – разметка не должна ронять прогон
        return {"error": str(e)}


# Выделить в поле кусок текста по его содержимому – под родной редактор ссылок.
_SELECT_TEXT_JS = r"""
(args) => {
  const el = document.querySelector(args.sel);
  if (!el) return false;
  const nodes = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let full = '';
  while (w.nextNode()) { nodes.push([w.currentNode, full.length]); full += w.currentNode.nodeValue; }
  const idx = full.indexOf(args.text);
  if (idx < 0) return false;
  const at = (pos, end) => {
    for (const [node, start] of nodes) {
      const len = node.nodeValue.length;
      if (pos >= start && pos <= start + len) {
        if (pos === start + len && !end) continue;
        return [node, pos - start];
      }
    }
    return null;
  };
  const a = at(idx, false), b = at(idx + args.text.length, true);
  if (!a || !b) return false;
  const r = document.createRange();
  r.setStart(a[0], a[1]); r.setEnd(b[0], b[1]);
  const sel = window.getSelection();
  sel.removeAllRanges(); sel.addRange(r);
  el.focus();
  return true;
}
"""

# Проверка, что анкор с нашим адресом реально появился в поле.
_HAS_LINK_JS = r"""
(args) => {
  const el = document.querySelector(args.sel);
  if (!el) return false;
  for (const a of el.querySelectorAll('a')) {
    const h = a.getAttribute('href') || a.getAttribute('data-link') || '';
    if (h && h.indexOf(args.host) >= 0) return true;
  }
  return false;
}
"""


def _apply_links_native(page, text_sel: str, link_spans: list[dict],
                        log: Callable[[str], None],
                        project_id: str = "") -> int:
    """
    Вшить ссылку в слова РОДНЫМ редактором ссылок ОК, а не execCommand.

    Почему так. Поле темы – `data-link-labels-enabled="1"`: ОК ведёт ссылки
    своими «ярлыками» и чужой <a> от execCommand('createLink') молча
    выбрасывает – оттого «нашем сайте» и не загоралось анкором (проверено по
    разметке поля 18.08.2026). Человек делает это через окошко «Добавить
    ссылку»: выделяет слова, вписывает адрес, жмёт «Добавить». Повторяем:
      1. выделяем слова анкора прямо в поле;
      2. открываем окно ссылки (Ctrl+K – штатное сочетание rich-редактора);
      3. вписываем адрес в js-field_url, при пустом тексте – и сами слова;
      4. «Добавить» (js-posting-link-editor-confirm) и проверяем, что анкор
         с нашим адресом правда встал.

    Возвращает, сколько ссылок реально вшилось. Ничего не ломает: если окно не
    открылось, кусок остаётся обычным текстом (как и было), а вызывающий потом
    сверит, что текст не пострадал.
    """
    done = 0
    # Кнопку, что ОТКРЫВАЕТ окно ссылки, ещё не знаем: Ctrl+K его не открыл, а
    # Escape после него закрывал всю форму поста (живой прогон 18.08.2026) –
    # оттого ОК и падал. Пока триггера нет, окно НЕ трогаем ни одной клавишей:
    # кусок остаётся обычным текстом (адрес есть в блоке контактов и кликается),
    # а форма цела. Как только пришлют скрин панельки при выделении текста –
    # включим _open_link_editor() и вставку через js-field_url заработает.
    warned = False
    for span in link_spans:
        try:
            if not page.evaluate(_SELECT_TEXT_JS, {"sel": text_sel, "text": span["text"]}):
                continue
            page.wait_for_timeout(350)                # дать панельке форматирования всплыть
            if not _open_link_editor(page, project_id, log):
                if not warned:
                    log("  окно «Добавить ссылку» не открылось – панельку выделения "
                        "сохранил (ok-linktoolbar.html), пришлите, привяжу кнопку точно")
                    warned = True
                continue
            try:
                page.wait_for_selector("input.js-field_url", state="visible", timeout=2500)
            except Exception:  # noqa: BLE001
                continue
            page.locator("input.js-field_url").last.fill(span["url"])
            txt = page.locator("input.js-field_txt").last
            try:
                if not (txt.input_value() or "").strip():
                    txt.fill(span["text"])
            except Exception:  # noqa: BLE001
                pass
            page.click(".js-posting-link-editor-confirm")
            page.wait_for_timeout(600)
            host = span["url"].split("://")[-1].strip("/").split("/")[0]
            if page.evaluate(_HAS_LINK_JS, {"sel": text_sel, "host": host}):
                done += 1
        except Exception as e:  # noqa: BLE001 – ссылка не должна ронять прогон
            log(f"  ссылку вставить не вышло: {e}")
            continue
    return done


# Открыть окно «Добавить ссылку» ОК: выделение уже стоит, над ним всплывает
# панелька форматирования (B I U ¶ H « » 🔗 – иконка ссылки последняя). Находим
# эту панель (небольшая, стоит ВПЛОТНУЮ НАД выделением) и жмём кнопку ссылки,
# опознавая её по подписи/классу/иконке. Escape/Ctrl+K не трогаем – они
# закрывали всю форму. Не опознали кнопку – сохраняем разметку панели, чтобы
# привязать её точно, и ничего не ломаем.
_OPEN_LINK_JS = r"""
() => {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return {ok: false, reason: 'нет выделения'};
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  let bar = null, barR = null;
  for (const el of document.querySelectorAll('div, span, ul, nav')) {
    let btns;
    try { btns = el.querySelectorAll('button, [role="button"], a'); } catch (e) { continue; }
    if (btns.length < 4 || btns.length > 14) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || r.height > 80) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) continue;
    // панель форматирования стоит НАД выделением, вплотную и по центру
    const above = r.bottom <= rect.top + 24 && (rect.top - r.bottom) < 160
      && Math.abs((r.left + r.right) / 2 - (rect.left + rect.right) / 2) < 400;
    if (!above) continue;
    if (!bar || (rect.top - r.bottom) < (rect.top - barR.bottom)) { bar = el; barR = r; }
  }
  if (!bar) return {ok: false, reason: 'панель форматирования не найдена'};
  const html = (bar.outerHTML || '').slice(0, 6000);
  const btns = [...bar.querySelectorAll('button, [role="button"], a')];
  const isLink = (b) => {
    const t = ((b.getAttribute('title') || '') + ' ' + (b.getAttribute('aria-label') || '')
               + ' ' + (b.className || '')).toLowerCase();
    if (t.includes('сылк') || /\blink\b/.test(t)) return true;
    const u = b.querySelector('use');
    const href = u ? (u.getAttribute('href') || u.getAttribute('xlink:href') || '') : '';
    return /link/i.test(href);
  };
  const target = btns.find(isLink);
  if (!target) return {ok: false, reason: 'кнопка ссылки не опознана', html};
  target.click();
  return {ok: true, html};
}
"""


def _open_link_editor(page, project_id: str = "",
                      log: Callable[[str], None] | None = None) -> bool:
    """Нажать кнопку ссылки в панели форматирования ОК. True – окно открылось."""
    try:
        res = page.evaluate(_OPEN_LINK_JS) or {}
    except Exception:  # noqa: BLE001
        res = {}
    if project_id and res.get("html"):
        try:
            (_diag_dir(project_id) / "ok-linktoolbar.html").write_text(
                res["html"], encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    if not res.get("ok"):
        return False
    try:
        page.wait_for_selector("input.js-field_url", state="visible", timeout=1_500)
        return True
    except Exception:  # noqa: BLE001
        return False


# Нажать кнопку «Ж» в той же панели форматирования (первая слева). Жирный так
# ложится ровно по выделению – execCommand же местами «съедал» первую букву
# (живой прогон 18.08.2026). Панель ищем так же, как для ссылки.
_CLICK_BOLD_JS = r"""
() => {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return false;
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  let bar = null, barR = null;
  for (const el of document.querySelectorAll('div, span, ul, nav')) {
    let btns;
    try { btns = el.querySelectorAll('button, [role="button"], a'); } catch (e) { continue; }
    if (btns.length < 4 || btns.length > 14) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || r.height > 80) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) continue;
    const above = r.bottom <= rect.top + 24 && (rect.top - r.bottom) < 160
      && Math.abs((r.left + r.right) / 2 - (rect.left + rect.right) / 2) < 400;
    if (!above) continue;
    if (!bar || (rect.top - r.bottom) < (rect.top - barR.bottom)) { bar = el; barR = r; }
  }
  if (!bar) return false;
  const btns = [...bar.querySelectorAll('button, [role="button"], a')];
  const norm = (b) => ((b.getAttribute('title') || '') + ' ' + (b.getAttribute('aria-label') || '')
                       + ' ' + (b.className || '')).toLowerCase();
  const useHref = (b) => { const u = b.querySelector('use'); return u ? (u.getAttribute('href') || u.getAttribute('xlink:href') || '') : ''; };
  let target = btns.find(b => {
    const t = norm(b);
    return t.includes('полужир') || t.includes('жирн') || /\bbold\b/.test(t) || /bold/i.test(useHref(b));
  }) || btns[0];   // первая кнопка панели — «Ж»
  if (!target) return false;
  target.click();
  return true;
}
"""

_IS_BOLD_JS = r"""
(args) => {
  const el = document.querySelector(args.sel);
  if (!el) return false;
  for (const b of el.querySelectorAll('b, strong')) {
    if ((b.textContent || '').includes(args.text)) return true;
  }
  return false;
}
"""


def _is_bold(page, text_sel: str, text: str) -> bool:
    try:
        return bool(page.evaluate(_IS_BOLD_JS, {"sel": text_sel, "text": text}))
    except Exception:  # noqa: BLE001
        return False


def _apply_bold(page, text_sel: str, bold_spans: list[dict],
                log: Callable[[str], None]) -> int:
    """
    Выставить жирный по кускам через РОДНУЮ кнопку «Ж» панели форматирования.

    Так делает человек, и первая буква не теряется. Если панель не всплыла или
    кнопка не нашлась – запасом идёт execCommand на то же выделение (как было).
    Возвращает, сколько кусков реально стали жирными.
    """
    done = 0
    for span in bold_spans:
        t = span["text"]
        try:
            if not page.evaluate(_SELECT_TEXT_JS, {"sel": text_sel, "text": t}):
                continue
            page.wait_for_timeout(220)               # дать панельке всплыть
            clicked = False
            try:
                clicked = bool(page.evaluate(_CLICK_BOLD_JS))
            except Exception:  # noqa: BLE001
                clicked = False
            page.wait_for_timeout(160)
            if not (clicked and _is_bold(page, text_sel, t)):
                # запас: execCommand на то же выделение
                page.evaluate(_SELECT_TEXT_JS, {"sel": text_sel, "text": t})
                _apply_marks(page, text_sel, [{"kind": "bold", "text": t}])
            if _is_bold(page, text_sel, t):
                done += 1
        except Exception:  # noqa: BLE001 – жирный не должен ронять прогон
            continue
    return done


_ANCHOR_RX = re.compile(r'\[[^\]\n]+\]\((https?://[^\s)]+)\)')
_ANCHOR_LABEL_RX = re.compile(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)')


def _drop_trailing_bare(markup: str) -> str:
    """
    Убрать голый адрес СРАЗУ ПОСЛЕ анкора на тот же адрес (в одной строке).

    Заказчица: «нашем сайте stalmetural.ru.» → анкор на «нашем сайте», а
    хвост «stalmetural.ru» убрать, точку оставить. Блок контактов (адрес с
    новой строки, после «🌐») не трогаем: там адрес не идёт вплотную за анкором.
    """
    out: list[str] = []
    i = 0
    for m in _ANCHOR_RX.finditer(markup):
        out.append(markup[i:m.end()])
        bare = re.sub(r'^https?://', '', m.group(1)).rstrip('/')
        rest = markup[m.end():]
        mm = re.match(r'[ \t]+(?:https?://)?' + re.escape(bare) + r'(?=[\s.,;:!?)]|$)',
                      rest, re.I)
        i = m.end() + mm.end() if mm else m.end()
    out.append(markup[i:])
    return "".join(out)


def _bold_anchors(markup: str) -> str:
    """Анкор сделать ещё и жирным: «нашем сайте» должно гореть и жирным (по
    просьбе заказчицы). Уже жирные (**…**) второй раз не оборачиваем."""
    rx = re.compile(r'(?<!\*)\[[^\]\n]+\]\((https?://[^\s)]+)\)(?!\*)')
    return rx.sub(lambda m: f'**{m.group(0)}**', markup)


def _type_post_text(page, text_sel: str, text: str,
                    log: Callable[[str], None] | None = None,
                    project_id: str = "") -> str:
    """
    Ввести текст темы и наложить на него разметку реестра: жирный и ссылки.

    Порядок ровно тот, каким это делает человек, и он же самый безопасный:
      1. набрать ОБЫЧНЫЙ текст – он ложится целым, без переносов не по
         месту и без чужих абзацев;
      2. выделить нужные куски прямо в поле и включить им жирный, а словам
         вроде «на нашем сайте» – ссылку;
      3. ПРОВЕРИТЬ, что получилось: жирные узлы и ссылки в поле правда
         появились, а текст при этом не изменился ни на букву.

    Третий шаг здесь – главный. Раньше Click сверял только текст, а про
    форматирование верил на слово: 14.08.2026 в логе стояло «жирный из
    реестра сохранён», а в опубликованной теме жирного не было вовсе.
    Теперь в лог идёт то, что реально в поле: сколько жирных кусков и
    ссылок, и если ноль – так и написано.

    Возвращает 'bold' (разметка легла), 'plain' (текст без разметки).
    """
    import post_text

    log = log or (lambda m: None)
    # ОК-подготовка разметки: убрать голый адрес сразу после анкора и сделать
    # сам анкор жирным (по просьбе заказчицы – «нашем сайте» ссылкой и жирным).
    text = _bold_anchors(_drop_trailing_bare(text))
    # Для ПЛОСКОГО текста анкор → просто его ярлык, без дописывания голого
    # адреса (это делает post_text._links_to_words, когда адреса больше нет в
    # тексте – и тогда «нашем сайте» снова обрастало «stalmetural.ru»). Ссылку
    # повесит редактор ОК по anchor_spans, адрес в плоском тексте не нужен.
    label_only = _ANCHOR_LABEL_RX.sub(r"\1", text)
    chunks = post_text.plain_chunks(label_only)
    plain = "".join(t for t, _ in chunks)
    letters = _letters          # общая сверка по буквам – одна на модуль

    def in_field() -> str:
        try:
            return page.eval_on_selector(text_sel, "el => el.innerText || el.textContent || ''") or ""
        except Exception:  # noqa: BLE001
            return ""

    # 1. Обычный текст. Всегда, при любом исходе разметки.
    page.type(text_sel, plain, delay=8)
    page.wait_for_timeout(500)

    bold_spans = [{"kind": "bold", "text": t.strip()} for t, bold in chunks
                  if bold and len(t.strip()) > 1]
    link_spans = [{"kind": "link", "text": t, "url": u}
                  for t, u in post_text.anchor_spans(text)]
    if not bold_spans and not link_spans:
        return "plain"

    # 2. Жирный – родной кнопкой «Ж» панели форматирования (execCommand в
    #    запасе). Так первая буква не теряется.
    bold_done = _apply_bold(page, text_sel, bold_spans, log) if bold_spans else 0

    # 2б. Ссылки – родным редактором ссылок ОК (свой <a> он выбрасывает).
    links_done = _apply_links_native(page, text_sel, link_spans, log, project_id) \
        if link_spans else 0

    # 3. Проверка по факту: и текст цел, и разметка на месте.
    got = in_field()
    if letters(got) != letters(plain) or _lines(got) != _lines(plain):
        log("  разметка изменила текст – убираю её, оставляю обычный текст")
        _clear_editor(page, text_sel)
        page.click(text_sel)
        page.type(text_sel, plain, delay=8)
        if project_id:
            _save_editor_markup(page, text_sel, project_id, log)
        return "plain"

    want_bold = len(bold_spans)
    want_links = len(link_spans)
    log(f"  разметка: жирных кусков {bold_done} из {want_bold}, "
        f"ссылок {links_done} из {want_links}")
    if link_spans and not links_done:
        log("  ссылку редактор ОК не принял – остаётся обычный текст со ссылкой "
            "в блоке контактов")
        if project_id:
            _save_editor_markup(page, text_sel, project_id, log)
    if not bold_done and want_bold:
        log("  жирный редактор ОК не принял – текст ушёл обычным")
        if project_id:
            _save_editor_markup(page, text_sel, project_id, log)
        return "plain"
    return "bold"


def _clear_editor(page, text_sel: str) -> int:
    """
    Опустошить поле темы. Возвращает, сколько знаков было убрано.

    ОК восстанавливает недописанный черновик прошлого раза, и наш текст
    приписывается к нему: заказчица получила «ТестПроверка планировщика
    Click…» – её «Тест» и наш текст слиплись в один пост.
    """
    try:
        was = (page.eval_on_selector(text_sel, "el => el.textContent || ''") or "").strip()
    except Exception:  # noqa: BLE001
        return 0
    if not was:
        return 0
    try:
        page.click(text_sel)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(300)
        # Не поддалось клавишами – чистим напрямую и сообщаем форме, иначе
        # она не заметит и вернёт текст обратно.
        now = (page.eval_on_selector(text_sel, "el => el.textContent || ''") or "").strip()
        if now:
            page.eval_on_selector(
                text_sel,
                """el => {
                    el.textContent = '';
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""")
            page.wait_for_timeout(200)
    except Exception:  # noqa: BLE001
        return len(was)
    return len(was)


def _close_datepicker(page, text_sel: str) -> None:
    """
    Закрыть выпавший календарь: он висит поверх часов и минут.

    Заказчица описала это точно: «после ввода даты надо нажать на пустое
    место, а он завис в непонятках». Календарь ОК остаётся открытым и
    накрывает списки времени, а Playwright ждёт их видимости до таймаута.

    КУДА нажимать – вопрос не праздный, и я на нём уже ошибся. Сначала
    нажимал по подписи «Время публикации» – а она лежит ВНУТРИ label, и
    клик по ней снимает галочку обратно. Со стороны выглядело загадочно:
    «ставит дату, потом что-то происходит и галочка слетает». Заказчица
    подсказала верное место: «надо в поле, например около текста».

    Поэтому нажимаем строго по своему полю текста. Внутри label не
    щёлкаем никогда – там любая точка переключает галочку.
    """
    for selector in [s for s in (text_sel, '[role="dialog"] [contenteditable="true"]') if s]:
        try:
            page.locator(selector).first.click(timeout=1_500)
            page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001 – пробуем следующее «пустое место»
            continue
        try:
            if not page.locator("#ui-datepicker-div").first.is_visible(timeout=500):
                return
        except Exception:  # noqa: BLE001 – календаря нет вовсе, и хорошо
            return


def _round_to_five(when: datetime) -> datetime:
    """
    Округлить минуты ВВЕРХ до кратных пяти – других ОК не принимает.

    В его списке минут ровно 00, 05, 10 … 55 (разметка снята с живой формы
    12.08.2026). Отложку на 15:08 поставить нельзя никак. Округляем вверх,
    а не вниз: пост не должен выйти раньше, чем просили. Для настоящих
    постов (11:00, 09:30) это ничего не меняет – они уже кратные.
    """
    from datetime import timedelta
    if when.minute % 5 == 0:
        return when.replace(second=0, microsecond=0)
    add = 5 - when.minute % 5
    return (when + timedelta(minutes=add)).replace(second=0, microsecond=0)


def _turn_on_schedule(page, log: Callable[[str], None]) -> bool:
    """
    Включить «Время публикации». True – включилось (появилось поле даты).

    Сама галочка невидима: ОК рисует вместо неё span.irc-vis, а input
    прячет. По невидимому Playwright не кликает, поэтому щёлкаем по
    нарисованному квадратику или по label. Если и это не вышло – ставим
    галочку изнутри страницы и сообщаем об этом форме событием change,
    иначе она изменения не заметит.

    Успех проверяем не по клику, а по ПОЯВИВШЕМУСЯ полю даты: галочка
    могла «нажаться» мимо, и тогда всё дальнейшее пошло бы впустую.
    """
    def ready() -> bool:
        # Именно ВИДНО, а не «есть в разметке»: блок времени лежит на
        # странице всегда, но до галочки он спрятан.
        return bool(_first_visible(page, SEL["date_input"]))

    if ready():
        return True
    if _click_first(page, SEL["schedule_toggle"], timeout=6_000):
        page.wait_for_timeout(900)
        if ready():
            return True
    try:
        page.evaluate(
            """(sel) => {
                const box = document.querySelector(sel);
                if (!box) return false;
                if (!box.checked) {
                    box.checked = true;
                    box.dispatchEvent(new Event('change', {bubbles: true}));
                    box.dispatchEvent(new Event('click', {bubbles: true}));
                }
                return true;
            }""", SEL["schedule_checkbox"])
    except Exception:  # noqa: BLE001
        return False
    page.wait_for_timeout(900)
    if ready():
        log("  галочку «Время публикации» включил изнутри страницы")
        return True
    return False


def who_am_i(page) -> str:
    """
    Под каким аккаунтом мы вошли. Пусто – определить не вышло.

    Появилось после дня разбирательств: заказчица собрала куки под одним
    брендом, а пыталась работать в группе другого – и Click упирался в
    «не нашли форму», хотя дело было в чужом аккаунте, у которого в этой
    группе просто нет прав. Имя видно в шапке ОК, и сказать его дешевле,
    чем гадать.
    """
    try:
        return (page.evaluate(
            """() => {
                const el = document.querySelector('a[data-l*="userPage"], '
                    + '.toolbar_nav_a .tico_tx, .user-name, [id$="_navMenu"] .tico_tx');
                return el ? (el.textContent || '').trim() : '';
            }""") or "").strip()[:60]
    except Exception:  # noqa: BLE001
        return ""


def _editor_open(page, timeout_ms: int = 4_000) -> str:
    """Селектор открывшегося поля ввода темы. Пусто – окно не открылось."""
    for candidate in SEL["text"]:
        try:
            page.wait_for_selector(candidate, state="visible", timeout=timeout_ms)
            return candidate
        except Exception:  # noqa: BLE001
            timeout_ms = 700          # первый уже подождал, остальным хватит взгляда
    return ""


def _click_composer_in_page(page) -> str:
    """
    Нажать «Создать новую тему» руками самой страницы. Что нашли, или пусто.

    Зачем в обход обычного клика. Поле на странице ЕСТЬ – заказчица нашла
    его обычным Ctrl+F, – а Click до него не дотягивался. Обычный клик
    Playwright требует, чтобы элемент был не перекрыт: у ОК же поверх ленты
    висят всплывашки («Оксана прислала вам открытку», «Подписаться»), и
    любая из них перехватывает нажатие. Клик изнутри страницы этой проверки
    не делает – он попадает точно в элемент.

    Сперва подтверждённая инспектором ссылка a.pf-head_itx_a, затем поиск
    по точной подписи.
    """
    try:
        return page.evaluate(
            """() => {
                const go = (el, how) => {
                    el.scrollIntoView({block: 'center'});
                    (el.closest('a, button') || el).click();
                    return how;
                };
                const known = document.querySelector('a.pf-head_itx_a');
                if (known) return go(known, 'ссылка формы');
                const marks = ['Создать новую тему', 'Создать тему', 'Добавить тему'];
                for (const el of document.querySelectorAll('a, button, div, span')) {
                    const text = (el.textContent || '').trim();
                    if (marks.includes(text)) return go(el, 'подпись «' + text + '»');
                }
                return '';
            }""") or ""
    except Exception:  # noqa: BLE001
        return ""


def _open_composer(page, log: Callable[[str], None], tries: int = 3) -> bool:
    """
    Открыть форму новой темы. True – открылась.

    Успехом считаем не «клик прошёл», а ПОЯВИВШЕЕСЯ поле ввода: клик может
    угодить во всплывашку и формально удаться, ничего не открыв.

    Прокручиваем НАВЕРХ. Здесь была моя ошибка: на каждой неудаче страница
    уезжала вниз, всё дальше от поля – а оно живёт в самом верху ленты.
    Заказчица это и заметила по снимку отказа: «он как будто в середине
    ленты, а надо в самый верх».
    """
    for attempt in range(1, tries + 1):
        # Наверх и без всплывашек: и то и другое мешает добраться до поля.
        try:
            page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:  # noqa: BLE001
            pass
        dismiss_cookies(page)
        page.wait_for_timeout(400)

        if _click_first(page, SEL["create_post"], timeout=4_000):
            if _editor_open(page):
                log(f"  форма открылась{'' if attempt == 1 else f' с {attempt}-й попытки'}")
                return True
        # Обычный клик не дошёл – жмём изнутри страницы, мимо всплывашек.
        how = _click_composer_in_page(page)
        if how:
            if _editor_open(page):
                log(f"  форма открылась через {how}")
                return True
            log(f"  нажал {how}, но окно не открылось – пробую снова")
        else:
            log(f"  поля новой темы на странице не видно (попытка {attempt})")
        try:
            page.wait_for_load_state("networkidle", timeout=3_000)
        except Exception:  # noqa: BLE001 – лента ОК почти никогда не «затихает»
            pass
        page.wait_for_timeout(1_000)
    return False


def _toast_scheduled(page, when: datetime) -> str:
    """
    Что ОК сам сказал про отложку: «Тема опубликуется 13.08.2026 в 08:59».
    Возвращает найденную фразу либо пусто.

    Это главный признак успеха, и взят он не с потолка: на ВК мы уже
    обожглись, проверяя поля формы вместо ответа площадки. Площадка знает
    лучше – она это и написала.
    """
    try:
        body = page.evaluate("() => document.body ? (document.body.innerText || '') : ''") or ""
    except Exception:  # noqa: BLE001
        return ""
    for mark in SEL["toast_scheduled"]:
        pos = body.find(mark)
        if pos >= 0:
            return " ".join(body[pos:pos + 80].split("\n")[0].split())
    return ""


def _pick_photos(page, image_paths: list[str], log: Callable[[str], None]) -> str:
    """
    Прикрепить фото так, как это делает человек: плюсик → «Контентные
    блоки» → «Фотографии» → «Загрузить фото». Пусто – получилось.

    Курсор перед этим ставим в самое начало текста: у ОК блок встаёт туда,
    где стоит курсор, и заказчица показала – фото должно оказаться НАД
    текстом, иначе пост выглядит не так, как в реестре.
    """
    try:
        page.keyboard.press("Control+Home")
        page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001 – не вышло, блок просто встанет ниже
        pass

    opened = ""
    if _click_first(page, SEL["block_plus"], timeout=4_000):
        page.wait_for_timeout(500)
        _click_first(page, SEL["menu_content_blocks"], timeout=4_000)
        page.wait_for_timeout(400)
        opened = _click_first(page, SEL["menu_photos"], timeout=4_000)
    if not opened:
        # Запасной путь – кнопка «Фото» в нижнем ряду окна. Она ведёт к
        # тому же окну выбора, просто блок встанет не сверху, а по месту.
        log("  плюсик не открылся – беру кнопку «Фото» в нижнем ряду")
        opened = _click_first(page, SEL["photo_btn"], timeout=6_000)
    if not opened:
        return "не нашли, чем добавить фото в форме ОК"
    page.wait_for_timeout(1_200)

    # «Загрузить фото» открывает системное окно выбора файла. Playwright
    # умеет его перехватить; если окна не будет – ищем поле input[type=file]
    # прямо в разметке (у ОК оно обычно спрятано рядом).
    try:
        with page.expect_file_chooser(timeout=6_000) as picked:
            if not _click_first(page, SEL["upload_photo"], timeout=5_000):
                raise RuntimeError("нет кнопки «Загрузить фото»")
        picked.value.set_files(image_paths)
    except Exception:  # noqa: BLE001 – пробуем через скрытое поле
        inp = page.locator(SEL["file_input"])
        if not inp.count():
            return "не нашли, куда отдать файлы фото в окне ОК"
        inp.first.set_input_files(image_paths)
    page.wait_for_timeout(3_000)
    return ""


def _first_present(page, candidates) -> str:
    """Первый кандидат, ЕСТЬ ли такой в разметке (даже скрытый)."""
    if isinstance(candidates, str):
        candidates = (candidates,)
    for sel in candidates:
        try:
            if page.locator(sel).count():
                return sel
        except Exception:  # noqa: BLE001
            continue
    return ""


def _first_visible(page, candidates) -> str:
    """
    Первый кандидат, который ВИДЕН на экране. Пусто – ни одного.

    Отличие от _first_present принципиальное, и оно уже стоило прогона.
    Блок времени у ОК лежит в разметке всегда, но до галочки «Время
    публикации» он спрятан. Проверка «элемент есть» считала спрятанное
    за успех: Click решал, что галочка уже включена, не включал её – и
    полминуты ждал, пока станет видимым список минут, который никто не
    показывал.
    """
    if isinstance(candidates, str):
        candidates = (candidates,)
    for sel in candidates:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
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
        # Заводим ЗАРАНЕЕ: обработчик ошибок ниже снимает по ней экран, а
        # упасть можно и раньше, чем вкладка откроется. Тогда там был бы
        # NameError вместо настоящей причины – ошибка поверх ошибки.
        page = None
        try:
            context = browser.new_context(
                storage_state=str(session_path(project_id)),
                viewport={"width": 1280, "height": 900}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            context.add_init_script(_vk.ANTIBOT_INIT)
            page = context.new_page()

            # Идём СРАЗУ на вкладку «Темы»: поле «Создать новую тему» живёт
            # только там. По обычному адресу группы открывается лента, и
            # искать поле на ней бесполезно – его там нет (см. topics_url).
            open_at = topics_url(group_url)
            log(f"Открываю темы группы: {open_at}")
            page.goto(open_at, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2500)
            # ОК умеет заслонить группу вопросом «Это вы?». Подтверждаем и
            # возвращаемся в группу – иначе это выглядит как слетевшая сессия.
            if confirm_profile(page, log):
                page.goto(open_at, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(2000)
            block = page_block(page)
            if block:
                shot = _debug_shot(project_id, page, block)
                why = {
                    "profile": "ОК просит подтвердить, что профиль наш, и не "
                               "пускает дальше. Нажать подтверждение не удалось.",
                    "verify": "ОК требует проверочный код из СМС – без человека "
                              "это не пройти.",
                    "verify-code": "ОК ждёт проверочный код из СМС.",
                }.get(block, "ОК показывает проверку и не пускает в группу.")
                return {"ok": False,
                        "error": why + " Зайдите в «Настройки» → «Вход в ОК» и "
                                       "пройдите проверку, потом повторите.", "shot": shot}
            if "anonym" in (page.url or "") or not is_logged_in(page):
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "guest"),
                        "error": "ОК открыл страницу как гостю – сессия не действует. "
                                 "Войдите заново в «Настройках» → «Вход в ОК»."}

            # Под кем вошли – в лог всегда. Чужая сессия выглядит как
            # «не нашли форму»: у аккаунта другого бренда в этой группе
            # просто нет прав. День на это уже потрачен.
            who = who_am_i(page)
            if who:
                log(f"Вошли в ОК как: {who}")

            log("Открываю форму поста («Создать новую тему»)")
            if not _open_composer(page, log):
                shot = _debug_shot(project_id, page, "no-composer")
                return {"ok": False,
                        "error": "Не нашли «Создать новую тему» на странице группы"
                                 + (f". Вошли как «{who}» – ЭТОТ ЛИ аккаунт ведёт "
                                    "группу? Куки другого бренда открывают группу "
                                    "как чужую, и формы в ней нет" if who else "")
                                 + ". Если на снимке ниже поле для новой темы есть, "
                                   "значит ОК переименовал кнопку – пришлите снимок",
                        "shot": shot}
            # _open_composer уже дождалась поля – берём его тем же способом,
            # чтобы не разъехались два разных представления об «открылось».
            text_sel = _editor_open(page)
            if not text_sel:
                shot = _debug_shot(project_id, page, "no-editor")
                return {"ok": False, "error": "Окно новой темы не открылось", "shot": shot}
            page.wait_for_timeout(800)

            # ОК восстанавливает недописанный черновик прошлого раза. Без
            # чистки наш текст приписывается к нему: заказчица получила
            # «ТестПроверка планировщика Click…» – её «Тест» и наш текст
            # слиплись. Чистим и УБЕЖДАЕМСЯ, что поле пустое.
            left = _clear_editor(page, text_sel)
            if left:
                log(f"  убрал черновик прошлого раза ({left} знаков)")

            log(f"Ввожу текст ({len(text)} знаков)")
            page.click(text_sel)
            _type_post_text(page, text_sel, text, log, project_id=project_id)
            page.wait_for_timeout(1_200)
            # Карточка сайта по ссылке из текста – убираем крестиком, как руками.
            yb.drop_link_card(page, yb.text_domains(text), log,
                              diag_dir=_diag_dir(project_id))
            typed = (page.eval_on_selector(text_sel, "el => el.textContent || ''") or "")
            if text.strip() and not typed.strip():
                return {"ok": False, "error": "Текст не попал в поле поста ОК",
                        "shot": _debug_shot(project_id, page, "no-text")}
            # Лишнее спереди – верный признак, что черновик всё-таки остался.
            #
            # Сверяем с тем, что РЕАЛЬНО набирается, и по одним буквам. Оба
            # уточнения – из живого отказа 14.08.2026: «В поле поста осталось
            # лишнее от прошлого черновика: «СПЕЦИАЛЬНОЕ ПРЕДЛОЖЕНИЕ НА
            # АРМИРУЮЩУЮ МИКРОФИБРУ…»». В поле лежал ровно наш текст – а
            # сравнивали его с РАЗМЕТКОЙ, в которой заголовок стоит жирным
            # (`**…**`): звёздочки в поле, разумеется, не появляются, и
            # проверка не сходилась никогда, стоило посту начинаться с
            # жирной строки. Заодно: textContent склеивает строки без
            # переносов, поэтому короткий первый абзац тоже ломал сверку.
            import post_text
            plain = "".join(part for part, _ in post_text.plain_chunks(text))
            if _letters(plain) and not _letters(typed).startswith(_letters(plain)[:20]):
                return {"ok": False,
                        "error": "В поле поста осталось лишнее от прошлого черновика: "
                                 f"«{typed.strip()[:60]}…». Откройте форму в ОК и "
                                 "очистите её вручную, потом повторите",
                        "shot": _debug_shot(project_id, page, "draft-left")}

            if image_paths:
                log(f"Прикрепляю фото: {len(image_paths)}")
                trouble = _pick_photos(page, image_paths, log)
                if trouble:
                    shot = _debug_shot(project_id, page, "no-photo")
                    return {"ok": False, "error": trouble, "shot": shot}

            # Отложка: галочка «Время публикации», под ней поле даты и два
            # выпадающих списка – часы и минуты. Кнопка внизу при этом
            # меняется с «Поделиться» на «Сохранить».
            # ОК даёт выбирать минуты только кратные пяти: в его списке
            # 00, 05, 10 … 55. Отложку на 15:08 он принять не может в
            # принципе – округляем ВВЕРХ, чтобы пост не вышел раньше, чем
            # просили. Для реальных постов (11:00, 09:30) это ничего не меняет.
            when = _round_to_five(when)
            # Ещё один заход на карточку сайта – последний перед сохранением.
            # Она приходит с сервера с задержкой и запросто появляется уже
            # после ввода текста, пока Click прикрепляет фото: 14.08.2026 так
            # и вышло – отложка в ОК встала вместе с чужой карточкой.
            yb.drop_link_card(page, yb.text_domains(text), log, tries=1,
                              diag_dir=_diag_dir(project_id))
            log(f"Ставлю время публикации {when.strftime('%d.%m.%Y %H:%M')} (Екатеринбург)")

            if not _turn_on_schedule(page, log):
                shot = _debug_shot(project_id, page, "no-schedule")
                return {"ok": False,
                        "error": "Не нашли галочку «Время публикации» в форме ОК "
                                 "(или она не включилась)", "shot": shot}

            date_sel = _first_visible(page, SEL["date_input"])
            if not date_sel:
                shot = _debug_shot(project_id, page, "no-date")
                return {"ok": False, "error": "Поле даты в форме ОК не распознано", "shot": shot}
            page.click(date_sel, click_count=3)
            page.type(date_sel, when.strftime("%d.%m.%Y"), delay=25)
            page.keyboard.press("Enter")
            page.wait_for_timeout(1_200)
            # Календарь остаётся открытым и накрывает списки времени –
            # «нажимаем на пустое место» вместо человека. Строго по своему
            # полю текста: клик по подписи «Время публикации» снял бы галочку.
            _close_datepicker(page, text_sel)

            # И убеждаемся, что галочка ПЕРЕЖИЛА возню с календарём: любое
            # неверное нажатие снимает её, а вместе с ней прячется и блок
            # времени. Раньше это выливалось в загадочное «не нашли список
            # часы» – список был на месте, просто скрыт.
            if not _turn_on_schedule(page, log):
                shot = _debug_shot(project_id, page, "schedule-lost")
                return {"ok": False,
                        "error": "Галочка «Время публикации» слетела после ввода даты, "
                                 "и вернуть её не вышло", "shot": shot}

            # Часы и минуты – обычные <select> с именами st.layer.hours и
            # st.layer.mins. Выбираем значением, а не печатью.
            for what, key, value in (("часы", "hours_select", f"{when.hour:02d}"),
                                     ("минуты", "mins_select", f"{when.minute:02d}")):
                sel = _first_visible(page, SEL[key])
                if not sel:
                    shot = _debug_shot(project_id, page, "no-time")
                    return {"ok": False,
                            "error": f"Не нашли список «{what}» в форме ОК", "shot": shot}
                picked = _select_closest(page, sel, value)
                if not picked:
                    shot = _debug_shot(project_id, page, "bad-time")
                    return {"ok": False,
                            "error": f"ОК не принял {what} «{value}» – в его списке "
                                     "такого значения нет и близкого не нашлось",
                            "shot": shot}
                if picked != value:
                    log(f"  {what}: ОК даёт только свои значения, взял ближайшее {picked}")
                page.wait_for_timeout(600)

            page.wait_for_timeout(800)
            log("Сохраняю отложку")
            if not _click_first(page, SEL["submit_scheduled"], timeout=10_000):
                shot = _debug_shot(project_id, page, "no-save")
                return {"ok": False,
                        "error": "Не нашли кнопку «Сохранить» – без неё отложка не встанет. "
                                 "Кнопку «Поделиться» намеренно не жмём: она опубликует "
                                 "пост СЕЙЧАС вместо назначенного времени", "shot": shot}

            # Ответ самой площадки: «Тема опубликуется 13.08.2026 в 08:59».
            # Ждём именно его, а не закрытия окна: окно может закрыться и
            # тогда, когда пост ушёл не туда, куда мы просили.
            #
            # Ждём НЕ ТОРОПЯСЬ. Заказчица: «как-то слишком быстро всё делает,
            # пусть чуть-чуть подольше ждёт ответа» – и она права: восемь
            # секунд на ответ живого сайта мало, а цена спешки высока. Пост,
            # объявленный неудачным, зовут формировать заново – и получается
            # дубль там, где всё было в порядке.
            said = ""
            for _ in range(30):
                said = _toast_scheduled(page, when)
                if said:
                    break
                page.wait_for_timeout(500)
            if not said:
                # Уведомление могло промелькнуть и погаснуть. Спросим у ОК
                # напрямую: заглянем в «Отложенные» группы. Это надёжнее
                # всплывашки – там лежит сама запись.
                log("  уведомления не видно – проверяю «Отложенные» в группе")
                said = _found_in_delayed(page, group_url, text, log)
            if not said:
                shot = _debug_shot(project_id, page, "no-toast")
                return {"ok": False,
                        "error": "ОК не подтвердил отложку: ни уведомления «Тема "
                                 "опубликуется …», ни записи в «Отложенных» группы. "
                                 "Загляните туда сами – если тема там есть, "
                                 "формировать заново не нужно", "shot": shot}
            log(f"ОК подтвердил: {said}")
            yb._save_storage_state(context, session_path(project_id))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            # Снимок и здесь: неожиданный сбой без картинки – это тупик,
            # разбирать его по одной строке исключения нечем.
            return {"ok": False, "error": str(e),
                    "shot": _debug_shot(project_id, page, "error") if page else None}
        finally:
            browser.close()
