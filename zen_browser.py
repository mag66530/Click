"""
zen_browser.py – Дзен: статья в редакторе и родная отложка «Опубликовать позже».

Зачем браузером. У Дзена нет публичного API публикации: ни статьи, ни
отложки через него не поставить. Значит тот же путь, что у ВК, ОК и МАКС –
сохранённая сессия и настоящий редактор. Отложку держит сам Дзен, поэтому
Click в час выхода может быть выключен.

ВХОД – ГЛАВНОЕ ОТЛИЧИЕ ОТ ОСТАЛЬНЫХ СЕТЕЙ. Дзен пускает по Яндекс ID, а
сессия Яндекса у Click УЖЕ ЕСТЬ: ей публикуется Яндекс.Бизнес, и аккаунты
те же самые (СМУ – stalmetural19@, МПЭ – mepen88@, и так далее). Поэтому
отдельного входа Дзену не заводим: своей сессии нет – берём яндексовую от
ЯБ и работаем ею, а дальше храним уже свою (Дзен доставляет к ней куки
dzen.ru, и второй раз ходить в паспорт не придётся). Это ровно то, о чём
спрашивал заказчик: «либо автоматизировано, либо через ЯБ» – через ЯБ и
получается, без единого нового пароля.

У человека в Яндексе много аккаунтов, и паспорт показывает «Выберите
аккаунт для входа». Выбираем по почте проекта («Настройки» → email, тот же,
которым проверяется ЯБ), а не первый попавшийся: опубликовать статью не с
того бренда – ошибка, которую потом не отменить.

ЧТО ЗДЕСЬ ПРОИСХОДИТ ПО ШАГАМ:
    редактор канала → «＋» → «Написать статью» → чистый лист →
    заголовок → тело статьи → «Опубликовать» → галочка «Опубликовать
    позже» → дата в календаре → время → сверка → подтверждение.

ТЕКСТ ВСТАВЛЯЕМ ВСТАВКОЙ, А НЕ НАБОРОМ. Заказчик и руками делает так же:
копирует из документа и вставляет. Редактор Дзена разбирает вставленный
HTML сам – абзацы, подзаголовки и списки встают на свои места. Набирать те
же семь тысяч знаков посимвольно – это минуты ожидания и потерянное
форматирование. Набор оставлен запасным путём: вставку могли и запретить.

ТАБЛИЦЫ – КАРТИНКОЙ. Редактор статей Дзена таблиц не умеет вовсе, и это не
наша прихоть: заказчик так и просил – «таблички вставляем скриншотом».
Рисуем таблицу на отдельной странице тем же браузером, снимаем PNG и
прикладываем как изображение.

ОСТОРОЖНОСТЬ ВОКРУГ «ОПУБЛИКОВАТЬ». Кнопка публикации живёт рядом с
галочкой отложки, и промах мышью означает «вышло прямо сейчас» – отменить
это нельзя. Поэтому: расфокус полей делаем клавишей Tab, а не кликом по
пустому месту (заказчик отдельно предупредил, что клик выше поля снимает
галочку), и перед подтверждением СВЕРЯЕМ, что в окне стоят наши дата и
время. Не сошлось – не жмём вовсе: статья останется черновиком, а черновик
всегда можно доделать руками.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import apptime
import paths
import post_text
import yb_playwright as yb
import zen_doc

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

TIMEZONE_ID = "Asia/Yekaterinburg"

# Сколько ждём ПОСЛЕ нажатия «Опубликовать позже», прежде чем закрыть браузер.
#
# Закрывать сразу нельзя. Нажатие – это только начало: Дзену надо отправить
# отложку на сервер и получить ответ, а браузер, закрытый на середине этого
# разговора, обрывает его вместе с окном. Внешне всё выглядело удачно –
# «быстро-быстро и выключился», как сказал заказчик, – а лежит ли статья в
# отложенных, оставалось на веру.
#
# Поэтому: сначала ждём тишины в сети (запрос ушёл и ответ пришёл), потом
# ещё пауза. При видимом окне пауза длиннее – чтобы человек успел увидеть
# своими глазами, что Дзен принял статью, а не смотрел на мелькнувшее окно.
SETTLE_MS = 6_000
WATCH_HOLD_MS = 12_000

# Куки, по которым видно ВХОД. Яндексовые – те же, что у ЯБ (session_id и
# компания на .yandex.ru); дзеновские площадка ставит уже сама.
YANDEX_AUTH = {"session_id", "sessionid2", "yandex_login"}

# Месяцы в двух падежах, и путать их нельзя. В ПОЛЕ даты Дзен пишет
# «14 августа 2026» (родительный), а в шапке КАЛЕНДАРЯ – «Август 2026»
# (именительный). Первый живой прогон искал в календаре родительный, не
# находил никогда и листал вперёд до июля 2027.
MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря")
MONTHS_NOM = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
              "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")

# Селекторы. Классы Дзена собраны сборщиком и несут хвост-хеш
# («…__addButton-1Z»): при следующем релизе хвост сменится, а середина имени
# останется. Поэтому везде, где можно, ищем по data-testid и по КУСКУ класса,
# а не по классу целиком – иначе модуль ломался бы от каждой выкладки Дзена.
SEL = {
    "add_publication": ['[data-testid="add-publication-button"]',
                        '[class*="author-studio-header__addButton"]'],
    # Вход – три нажатия подряд, и все три обязательны (проверено вживую
    # 14.08.2026). Кнопка «Войти» в шапке публичного канала; в выпавшем окне
    # «Войти через Яндекс ID»; и уже в паспорте – плитка нужного аккаунта.
    "login_button": ['[class*="login-button"]',
                     '[class*="loginButton"]:not([class*="content"])',
                     'button:has-text("Войти")'],
    "login_yandex": ['[class*="login-content_ya"]',
                     'a:has-text("Войти через Яндекс ID")',
                     'text="Войти через Яндекс ID"'],
    "account_row": ['[class*="account"]', '[class*="Account"]', 'li', 'a'],
    "write_article": ['label[aria-label="Написать статью"]',
                      '[class*="new-publication-dropdown"] [class*="buttonTitle"]'],
    # Поля редактора. Явных примет у них почти нет: заголовок и текст – это
    # два contenteditable подряд, с серым плейсхолдером вместо подписи.
    # Поэтому сначала пробуем приметы, а не найдя – берём их по порядку
    # (см. find_editor_fields): первый редактируемый блок – заголовок,
    # второй – тело статьи.
    "title_field": ['[data-testid="article-title"]',
                    '[data-testid*="title"][contenteditable]',
                    '[placeholder*="Заголовок"]',
                    '[data-placeholder*="Заголовок"]',
                    '[aria-label*="Заголовок"]',
                    '[contenteditable="true"][class*="title"]',
                    'textarea[class*="title"]'],
    "body_field": ['[data-testid="article-body"]',
                   '[data-testid*="body"][contenteditable]',
                   '[data-placeholder*="Текст"]',
                   '[contenteditable="true"][class*="body"]',
                   '[contenteditable="true"][class*="content"]'],
    "editable": ['[contenteditable="true"]'],
    # Всплывающие окна Дзена: реклама донатов в студии и обучение «Статья»
    # поверх нового редактора. Оба перекрывают работу и оба закрываются
    # крестиком – либо нажатием по пустому месту, как и делает заказчик.
    "popup_close": ['[class*="modal"] [class*="close"]',
                    '[class*="popup"] [class*="close"]',
                    '[class*="onboarding"] [class*="close"]',
                    'button[aria-label*="Закрыть"]',
                    '[aria-label="Закрыть"]'],
    # Кнопок «Опубликовать» на экране ДВЕ, и путать их нельзя. Одна в шапке
    # редактора – ею окно публикации открывается. Вторая внизу окна – ею
    # подтверждается, и при включённой отложке она называется иначе:
    # «Опубликовать позже». Поиск по вхождению текста цеплял верхнюю,
    # перекрытую окном, и нажатие уходило в никуда (прогон 19:48).
    "publish_button": ['[class*="base-button"]:has-text("Опубликовать")',
                       'button:has-text("Опубликовать")'],
    "later_checkbox_title": ['[class*="checkbox-input__title"]:has-text("Опубликовать позже")',
                             'text="Опубликовать позже"'],
    "later_checkbox_input": ['[class*="checkbox-v2__input"]', 'input[type="checkbox"]'],
    "date_input": ['input[class*="input__control"][readonly]'],
    "time_input": ['input[name="delayedTime"]'],
    "file_input": ['input[type="file"]'],
}


# ════════════════════════════════════════════════════════════════════
#  СЕССИЯ
# ════════════════════════════════════════════════════════════════════

def session_path(project_id: str) -> Path:
    d = paths.data_root() / project_id / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d / "zen_storage_state.json"


def _has_yandex_auth(state: dict) -> bool:
    return any((c.get("name") or "").lower() in YANDEX_AUTH
               and "yandex" in (c.get("domain") or "").lower()
               and c.get("value")
               for c in state.get("cookies") or [])


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def source_session(project_id: str) -> Path | None:
    """
    Какой файл сессии открывать. Своя дзеновская главнее; нет её – берём
    сессию Яндекс.Бизнеса: аккаунт тот же, и в Дзен она пускает.

    Возвращает None, если входа нет нигде – тогда и браузер поднимать незачем.
    """
    own = session_path(project_id)
    if own.exists() and _has_yandex_auth(_read_state(own)):
        return own
    yb_state = yb.session_path(project_id)
    if yb_state.exists() and _has_yandex_auth(_read_state(yb_state)):
        return yb_state
    return None


def has_saved_session(project_id: str) -> bool:
    """Есть ли чем войти в Дзен – своей сессией или яндексовой от ЯБ."""
    return source_session(project_id) is not None


def session_note(project_id: str) -> str:
    """Откуда берётся вход – показываем человеку, чтобы не гадал."""
    src = source_session(project_id)
    if src is None:
        return "входа нет: войдите в Яндекс в «Настройках» (тот же вход, что у Яндекс.Бизнеса)"
    if src == session_path(project_id):
        return "вход свой, дзеновский"
    return "вход берётся от Яндекс.Бизнеса – аккаунт тот же"


# ════════════════════════════════════════════════════════════════════
#  МЕЛКАЯ МЕХАНИКА СТРАНИЦЫ
# ════════════════════════════════════════════════════════════════════

def _first_visible(page, candidates: list[str], timeout: int = 8_000) -> str:
    """Первый селектор из списка, который реально виден. Пусто – ни одного."""
    deadline = timeout
    step = 400
    while deadline > 0:
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    return sel
            except Exception:  # noqa: BLE001 – кривой селектор не должен ронять шаг
                continue
        page.wait_for_timeout(step)
        deadline -= step
    return ""


def _click_first(page, candidates: list[str], timeout: int = 8_000) -> str:
    sel = _first_visible(page, candidates, timeout)
    if not sel:
        return ""
    try:
        page.locator(sel).first.click()
        return sel
    except Exception:  # noqa: BLE001
        return ""


def _debug_shot(project_id: str, page, name: str) -> bytes | None:
    """Снимок экрана в момент отказа – по нему видно, что показал Дзен."""
    try:
        return page.screenshot(full_page=False)
    except Exception:  # noqa: BLE001
        return None


def _body_text(page) -> str:
    try:
        return page.evaluate("() => document.body ? (document.body.innerText || '') : ''") or ""
    except Exception:  # noqa: BLE001
        return ""


# ════════════════════════════════════════════════════════════════════
#  ВХОД
# ════════════════════════════════════════════════════════════════════

def dismiss_popups(page, log: Callable[[str], None] | None = None) -> int:
    """
    Закрыть всё, что Дзен показывает поверх работы. Сколько закрыли.

    Их два вида, и оба встретились на первом же прогоне (14.08.2026):
    реклама «У нас появились донаты!» в студии и обучение «Статья» поверх
    нового редактора. Пока они висят, до полей не добраться, а Click видит
    пустую страницу и говорит «не нашёл поле заголовка».

    Порядок как у человека: сначала крестик, потом Esc, потом нажатие по
    пустому месту («чтобы закрыть надо на пустое место нажать» – заказчик).
    Пустое место берём в ЛЕВОМ поле страницы: там нет ни кнопок, ни текста
    статьи, промахнуться нечем.
    """
    log = log or (lambda m: None)
    closed = 0
    for _ in range(3):                        # окон бывает несколько подряд
        if not _has_popup(page):
            break
        if _click_first(page, SEL["popup_close"], timeout=2_500):
            closed += 1
            page.wait_for_timeout(800)
            continue
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
        except Exception:  # noqa: BLE001
            pass
        if not _has_popup(page):
            closed += 1
            break
        try:
            page.mouse.click(40, 320)
            page.wait_for_timeout(800)
            closed += 1
        except Exception:  # noqa: BLE001
            break
    if closed:
        log(f"Закрыл всплывающие окна Дзена: {closed}")
    return closed


def _has_popup(page) -> bool:
    """Висит ли поверх страницы окно, которого мы не звали."""
    body = _body_text(page)
    marks = ("У нас появились донаты", "Статья — это в первую очередь текст",
             "Примеры статей", "Руководство")
    if any(m in body for m in marks):
        return True
    return bool(_first_visible(page, SEL["popup_close"], timeout=1_200))


def find_editor_fields(page, log: Callable[[str], None] | None = None,
                       timeout_ms: int = 25_000):
    """
    Найти поля заголовка и текста в редакторе статьи. (заголовок, тело).

    Приметы у полей слабые: серый плейсхолдер «Заголовок» и «Текст» – это не
    подпись и не placeholder в привычном смысле, а рисунок редактора. Зато
    порядок железный: первый редактируемый блок на странице – заголовок,
    второй – тело статьи. По нему и берём, если явные селекторы не сработали.

    Возвращает (None, None), если редактор так и не появился.
    """
    log = log or (lambda m: None)
    left = timeout_ms
    while left > 0:
        # Окно обучения могло всплыть уже после открытия редактора.
        dismiss_popups(page, log)

        title_sel = _first_visible(page, SEL["title_field"], timeout=1_500)
        body_sel = _first_visible(page, SEL["body_field"], timeout=1_000)
        if title_sel and body_sel:
            log("Поля редактора нашлись по приметам")
            return page.locator(title_sel).first, page.locator(body_sel).first

        try:
            fields = page.locator(SEL["editable"][0])
            visible = [fields.nth(i) for i in range(min(fields.count(), 6))
                       if fields.nth(i).is_visible()]
        except Exception:  # noqa: BLE001
            visible = []
        if len(visible) >= 2:
            log("Поля редактора взяты по порядку: первое – заголовок, второе – текст")
            return visible[0], visible[1]
        if len(visible) == 1 and title_sel:
            return page.locator(title_sel).first, visible[0]

        page.wait_for_timeout(1_000)
        left -= 2_500
    return None, None


def in_studio(page) -> bool:
    """
    Мы в студии автора? Признак прямой: кнопка «＋» есть только у хозяина
    канала. Всё остальное (адрес, заголовки) врёт – Дзен показывает
    незалогиненному человеку публичный канал по тому же адресу.
    """
    for sel in SEL["add_publication"]:
        try:
            if page.locator(sel).first.count():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _looks_logged_out(page) -> bool:
    """
    Дзен показывает вход, а не студию.

    Первая живая проверка (14.08.2026) обожглась именно здесь. Считалось,
    что незалогиненного видно по тексту «Войдите удобным способом» – а он
    появляется ТОЛЬКО после нажатия «Войти». Со студии же Дзен молча
    уводит на публичный канал: адрес похожий, статьи на месте, никаких слов
    про вход. Click решал, что вошёл, шёл искать «＋» и не находил.

    Поэтому признак теперь обратный и честный: нет кнопки «＋» – мы не в
    студии, чего бы страница про себя ни писала.
    """
    if in_studio(page):
        return False
    url = (page.url or "").lower()
    if "passport.yandex" in url or "/auth" in url:
        return True
    body = _body_text(page)
    if any(m in body for m in ("Войдите удобным способом", "Войти через Яндекс ID",
                               "Введите номер телефона", "Выберите аккаунт")):
        return True
    # Кнопка «Войти» в шапке – самый надёжный признак чужого канала.
    return bool(_first_visible(page, SEL["login_button"], timeout=2_000))


def do_login(page, email: str, log: Callable[[str], None]) -> str:
    """
    Пройти вход: «Войти» → «Войти через Яндекс ID» → плитка аккаунта.

    Все три нажатия обязательны, даже когда сессия Яндекса уже есть:
    заказчик проверил вживую – «вход всё равно надо нажать, потом через
    айди, а потом нажать на акк». Куки лишь избавляют от телефона и пароля,
    а сами нажатия Дзен ждёт от человека.

    Пусто – вошли, иначе причина словами.
    """
    body = _body_text(page)

    # 1. «Войти» в шапке – если окно входа ещё не открыто.
    if "Войдите удобным способом" not in body and "Выберите аккаунт" not in body:
        log("Нажимаю «Войти»")
        if not _click_first(page, SEL["login_button"], timeout=10_000):
            return "не нашли кнопку «Войти» в Дзене"
        page.wait_for_timeout(2_000)

    # 2. «Войти через Яндекс ID» в выпавшем окне.
    if "Выберите аккаунт" not in _body_text(page):
        log("Нажимаю «Войти через Яндекс ID»")
        if not _click_first(page, SEL["login_yandex"], timeout=10_000):
            return "в окне входа не нашлась кнопка «Войти через Яндекс ID»"
        # Паспорт открывается своей страницей – ждём её не торопясь.
        for _ in range(20):
            if "Выберите аккаунт" in _body_text(page) or in_studio(page):
                break
            page.wait_for_timeout(1_000)

    # 3. Плитка аккаунта в паспорте.
    return pick_account(page, email, log)


def pick_account(page, email: str, log: Callable[[str], None]) -> str:
    """
    Экран «Выберите аккаунт для входа» – нажать плитку аккаунта проекта.

    Сравниваем по логину (часть до «@»), как это делает проверка аккаунта в
    ЯБ: паспорт рядом с адресом показывает и имя, и привязанные почты, и
    точное совпадение целой строки подводит.

    Если почта проекта не задана, а аккаунт на экране РОВНО ОДИН – жмём его:
    выбирать не из чего, и упираться тут значило бы застрять на ровном месте.
    А вот когда аккаунтов несколько и непонятно, чей канал, – останавливаемся:
    статья, вышедшая не под тем брендом, не отменяется.

    Возвращает пустую строку, если всё хорошо, иначе – причину словами.
    """
    body = _body_text(page)
    if "Выберите аккаунт" not in body:
        return ""

    emails = re.findall(r"[\w.+-]+@[\w.-]+", body)
    login = (email or "").strip().lower().split("@")[0]

    if login and any(login in e.lower() for e in emails):
        target = next(e for e in emails if login in e.lower())
    elif not login and len(set(emails)) == 1:
        target = emails[0]
        log(f"Почта проекта не задана, но аккаунт один – вхожу как {target}")
    elif login:
        return (f"В Яндексе не нашёлся аккаунт {email}. Что предложено: "
                f"{', '.join(emails[:6]) or 'ни одного'}. Войдите нужным аккаунтом "
                "руками или поправьте email в «Настройках».")
    else:
        return ("Яндекс просит выбрать аккаунт, а в «Настройках» не указан email "
                f"проекта. Предложено: {', '.join(emails[:6]) or 'ни одного'}.")

    log(f"Выбираю аккаунт {target}")
    try:
        page.get_by_text(target, exact=False).first.click()
    except Exception as e:  # noqa: BLE001
        return f"не получилось нажать на аккаунт {target}: {e}"

    # Ждём ухода экрана выбора. Плитка – это вложенный текст, и клик по нему
    # не всегда доходит до самой кнопки; тогда жмём по её ближайшему предку.
    for attempt in range(2):
        for _ in range(15):
            if "Выберите аккаунт" not in _body_text(page):
                return ""
            page.wait_for_timeout(1_000)
        if attempt == 0:
            log("Экран выбора не ушёл – жму плитку целиком")
            try:
                page.get_by_text(target, exact=False).first.locator(
                    "xpath=ancestor::*[self::a or self::button or self::li or self::div][1]"
                ).click()
            except Exception:  # noqa: BLE001
                break
    return ("Яндекс так и не принял выбор аккаунта: экран «Выберите аккаунт» "
            "остался на месте.")


def wait_studio(page, timeout_ms: int = 25_000) -> bool:
    """Дождаться студии: кнопка «＋» появляется не сразу – Дзен это SPA."""
    left = timeout_ms
    while left > 0:
        if in_studio(page):
            return True
        page.wait_for_timeout(700)
        left -= 700
    return False


def ensure_studio(page, editor_url: str, email: str, log: Callable[[str], None]) -> str:
    """
    Открыть студию автора и убедиться, что мы в ней. Пусто – всё хорошо,
    иначе причина словами.

    Порядок выстроен по живому прогону 14.08.2026. Со студии Дзен уводит
    незалогиненного на публичный канал – молча, без единого слова про вход.
    Поэтому «вошли ли мы» решается не по тексту страницы, а по кнопке «＋»:
    она есть только у хозяина канала. Нет её – идём входить, а после входа
    возвращаемся на адрес студии сами: Дзен после паспорта высаживает туда,
    откуда начинали, то есть опять на публичный канал.
    """
    log(f"Открываю студию Дзена: {editor_url}")
    page.goto(editor_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3_000)

    if wait_studio(page, timeout_ms=8_000):
        return ""

    log("Студии не видно – значит не вошли. Вхожу через Яндекс ID")
    why = do_login(page, email, log)
    if why:
        return why

    # После паспорта Дзен возвращает на прежнюю страницу – чаще всего на
    # публичный канал. В студию заходим ещё раз, уже с входом.
    page.wait_for_timeout(3_000)
    if not in_studio(page):
        log("Возвращаюсь в студию после входа")
        page.goto(editor_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_500)

    if not wait_studio(page, timeout_ms=25_000):
        return ("Вошли, но студия автора так и не открылась: кнопки «＋» на "
                f"странице нет. Проверьте адрес студии ({editor_url}) – он должен "
                "быть вида dzen.ru/profile/editor/…, и этот аккаунт должен быть "
                "автором канала.")
    log("Студия открыта, вход есть")
    return ""


# ════════════════════════════════════════════════════════════════════
#  ТЕЛО СТАТЬИ: HTML И КАРТИНКИ ТАБЛИЦ
# ════════════════════════════════════════════════════════════════════

def blocks_to_html(blocks: list[dict]) -> str:
    """
    Блоки статьи → HTML для вставки в редактор.

    Таблицы сюда НЕ попадают: редактор статей Дзена их не умеет, они уходят
    картинками отдельным шагом. На месте таблицы остаётся пустой абзац-метка,
    чтобы картинка встала именно туда, где таблица была в документе.
    """
    out: list[str] = []
    for b in blocks:
        if b["kind"] == "para":
            out.append(f"<p>{post_text.to_html(b['markup'])}</p>")
        elif b["kind"] == "head":
            tag = "h2" if b.get("level", 3) <= 2 else "h3"
            out.append(f"<{tag}>{post_text._esc_html(b['text'])}</{tag}>")
        elif b["kind"] == "list":
            items = "".join(f"<li>{post_text._esc_html(i)}</li>" for i in b["items"])
            out.append(f"<ul>{items}</ul>")
    return "".join(out)


def table_html(rows: list[list[str]]) -> str:
    """
    Таблица → самостоятельная HTML-страница для снимка.

    Вид спокойный и читаемый на телефоне: шапка серой заливкой, крупный
    шрифт, поля. Картинка идёт в статью как есть, поэтому она и должна
    выглядеть как часть статьи, а не как кусок Excel.
    """
    if not rows:
        return ""
    head, body = rows[0], rows[1:]
    th = "".join(f"<th>{post_text._esc_html(c)}</th>" for c in head)
    trs = "".join(
        "<tr>" + "".join(f"<td>{post_text._esc_html(c)}</td>" for c in r) + "</tr>"
        for r in body)
    return f"""<!doctype html><meta charset="utf-8">
<style>
  body {{ margin:0; padding:24px; background:#fff;
         font:17px/1.45 -apple-system,"Segoe UI",Roboto,Arial,sans-serif; color:#1a1a1a; }}
  table {{ border-collapse:collapse; width:900px; }}
  th,td {{ border:1px solid #dcdcdc; padding:12px 14px; text-align:left; vertical-align:top; }}
  th {{ background:#f2f3f5; font-weight:600; }}
  tr:nth-child(even) td {{ background:#fafafa; }}
</style>
<table id="t"><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"""


def render_table_png(context, rows: list[list[str]], out_path: Path,
                     log: Callable[[str], None] | None = None) -> str:
    """
    Нарисовать таблицу и снять её в PNG. Возвращает путь или пустую строку.

    Рисуем ТЕМ ЖЕ браузером, что публикует: второй движок ради картинки –
    лишние полгигабайта памяти в облаке, на этом Click уже падал.
    """
    log = log or (lambda m: None)
    if not rows:
        log("🔬 render_table_png: строк нет – рисовать нечего")
        return ""
    page = None
    try:
        page = context.new_page()
        page.set_viewport_size({"width": 1000, "height": 800})
        page.set_content(table_html(rows), wait_until="domcontentloaded")
        page.wait_for_timeout(300)
        page.locator("#t").screenshot(path=str(out_path))
        return str(out_path)
    except Exception as e:  # noqa: BLE001 – без картинки статья всё равно выйдет
        log(f"🔬 render_table_png: не получилось – {type(e).__name__}: {e}")
        return ""
    finally:
        try:
            if page:
                page.close()
        except Exception:  # noqa: BLE001
            pass


def _field_text(field) -> str:
    """Что сейчас написано в поле редактора."""
    try:
        return (field.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _paste_html_into(page, field, html: str) -> bool:
    """
    Вставить HTML в поле редактора – ровно так, как это делает Ctrl+V.

    Редактор Дзена слушает событие вставки и разбирает буфер сам: абзацы,
    подзаголовки и списки встают на места. Пишем в буфер через DataTransfer
    и шлём настоящее событие paste – к системному буферу в облаке доступа
    нет, да он там и не нужен.

    УСПЕХ СЧИТАЕМ ПО СОДЕРЖИМОМУ ПОЛЯ, а не по ответу dispatchEvent. Это не
    придирка: первый живой прогон вставил текст правильно, но решил, что не
    смог, и набрал его ВТОРОЙ РАЗ поверх – в статье получилось «…можно
    удалиЭто пробная статья…» и обрывок «ть.» отдельным абзацем. Причина
    тонкая и обратная интуиции: dispatchEvent возвращает false, когда
    обработчик вызвал preventDefault() – то есть именно когда вставка
    УДАЛАСЬ. Поэтому смотрим на текст: прибавилось – значит вставилось.

    Работаем с локатором, а не с селектором: у полей Дзена своих примет нет,
    и найдены они по порядку (см. find_editor_fields) – селектора, который
    указывал бы именно на них, попросту не существует.
    """
    before = _field_text(field)
    try:
        handle = field.element_handle()
        if handle is None:
            return False
        handle.evaluate(
            """(el, html) => {
                el.focus();
                const dt = new DataTransfer();
                dt.setData('text/html', html);
                dt.setData('text/plain', html.replace(/<[^>]+>/g, ' '));
                const ev = new ClipboardEvent('paste', {
                    bubbles: true, cancelable: true, clipboardData: dt });
                el.dispatchEvent(ev);
            }""", html)
    except Exception:  # noqa: BLE001
        return False

    # Редактор разбирает вставку не мгновенно – даём ему секунду-другую.
    for _ in range(6):
        page.wait_for_timeout(500)
        now = _field_text(field)
        if len(now) > len(before) + 10:
            return True
    return False


def _type_blocks_into(page, field, blocks: list[dict]) -> None:
    """
    Запасной путь: набрать статью построчно.

    Форматирование при этом теряется (подзаголовки станут обычными
    абзацами) – зато текст доедет целиком. Лучше статья без подзаголовков,
    чем пустой черновик.
    """
    try:
        field.click()
        # Вставка могла лечь наполовину – набирать поверх неё нельзя, иначе
        # текст задвоится. Чистим поле целиком и пишем с нуля.
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001
        pass
    for b in blocks:
        if b["kind"] == "para":
            text = post_text.strip_markup(b["markup"])
        elif b["kind"] == "head":
            text = b["text"]
        elif b["kind"] == "list":
            text = "\n".join(f"• {i}" for i in b["items"])
        else:
            continue
        page.keyboard.type(text, delay=1)
        page.keyboard.press("Enter")


def _image_insert_controls(page, log: Callable[[str], None]) -> None:
    """Выписать в лог кнопки, похожие на «вставить изображение» – чтобы по
    живому прогону узнать, чем Дзен на самом деле добавляет картинку."""
    try:
        found = page.evaluate(
            """() => {
                const out = [];
                const nodes = [...document.querySelectorAll(
                    'button,[role="button"],[aria-label],[title],svg,use')];
                const key = /изображ|картин|фото|image|photo|media|добав|вставит|\\+/i;
                for (const e of nodes) {
                    const lab = (e.getAttribute && (e.getAttribute('aria-label')
                        || e.getAttribute('title') || '')) || '';
                    const txt = (e.textContent || '').trim().slice(0, 24);
                    const hay = lab + ' ' + txt
                        + ' ' + (e.className && e.className.toString ? e.className.toString() : '');
                    if (key.test(hay) && (e.offsetParent || e.getClientRects().length)) {
                        out.push(((e.tagName || '').toLowerCase())
                            + (lab ? ' aria«' + lab.slice(0, 24) + '»' : '')
                            + (txt ? ' txt«' + txt + '»' : ''));
                        if (out.length >= 10) break;
                    }
                }
                return out;
            }""")
        if found:
            log("🔬 похоже на вставку картинки: " + " | ".join(found))
        else:
            log("🔬 кнопок «вставить изображение» не видно на странице")
    except Exception:  # noqa: BLE001
        pass


def _paste_image_into(page, field, path: str, log: Callable[[str], None]) -> bool:
    """Вставить картинку в поле как Ctrl+V из буфера – через DataTransfer с
    настоящим File. Многие редакторы (в т.ч. дзеновский) картинку из вставки
    разбирают сами, и файловое поле для этого не нужно."""
    if field is None:
        return False
    try:
        import base64
        data = base64.b64encode(Path(path).read_bytes()).decode()
        handle = field.element_handle()
        if handle is None:
            return False
        before = _field_text(field)
        handle.evaluate(
            """(el, b64) => {
                el.focus();
                const bin = atob(b64);
                const arr = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
                const file = new File([arr], 'table.png', { type: 'image/png' });
                const dt = new DataTransfer();
                dt.items.add(file);
                const ev = new ClipboardEvent('paste', {
                    bubbles: true, cancelable: true, clipboardData: dt });
                el.dispatchEvent(ev);
            }""", data)
        # Картинка грузится на сервер Дзена – ждём, пока в поле что-то прибавится.
        for _ in range(10):
            page.wait_for_timeout(600)
            if page.locator("img").count() and _field_text(field) != before:
                return True
        return bool(page.locator("img").count())
    except Exception as e:  # noqa: BLE001
        log(f"🔬 вставка картинкой из буфера не прошла – {type(e).__name__}: {e}")
        return False


def _attach_image(page, path: str, log: Callable[[str], None] | None = None,
                  field=None) -> bool:
    """
    Отдать картинку редактору. У статей Дзена нет открытого <input type=file>
    (живой прогон 19.08: «файловых полей 0»), поэтому идём несколькими путями:
    сперва существующее файловое поле, затем вставкой из буфера прямо в текст.
    Что нашли по дороге – пишем в лог, чтобы доводить прицельно.
    """
    log = log or (lambda m: None)
    # 1) Готовое файловое поле, если вдруг есть.
    try:
        inputs = page.locator(SEL["file_input"][0])
        cnt = inputs.count()
        log(f"🔬 _attach_image: файловых полей на странице: {cnt}")
        if cnt:
            inputs.first.set_input_files(path)
            page.wait_for_timeout(3_000)
            return True
    except Exception as e:  # noqa: BLE001
        log(f"🔬 _attach_image: файловое поле не сработало – {type(e).__name__}: {e}")

    # 2) Смотрим, чем редактор вообще вставляет картинку (для лога и разбора).
    _image_insert_controls(page, log)

    # 3) Вставка картинкой из буфера прямо в тело статьи.
    log("🔬 _attach_image: пробую вставить картинку в текст как Ctrl+V")
    if _paste_image_into(page, field, path, log):
        log("🔬 _attach_image: картинка ушла вставкой в текст")
        return True
    return False


# ════════════════════════════════════════════════════════════════════
#  ОТЛОЖКА: ПОЯС, КАЛЕНДАРЬ, ВРЕМЯ
# ════════════════════════════════════════════════════════════════════

def zone_offset(text: str) -> int | None:
    """
    Часовой пояс из окна отложки: «GMT+5» → 5. None – не написано.

    Зачем. Дзен показывает время В СВОЁМ поясе (у аккаунтов заказчика это
    GMT+5, но у другого аккаунта может стоять московский GMT+3). Реестр же
    живёт по Екатеринбургу. Не пересчитать – значит поставить статью на два
    часа мимо и узнать об этом от читателей.
    """
    m = re.search(r"GMT\s*([+-]\s*\d{1,2})", text or "", re.I)
    if not m:
        return None
    return int(m.group(1).replace(" ", ""))


def when_in_zone(when: datetime, offset_hours: int | None) -> datetime:
    """Время выхода в поясе, который показывает Дзен. Пояс неизвестен – как есть."""
    if offset_hours is None:
        return when
    return when.astimezone(timezone(timedelta(hours=offset_hours)))


def date_caption_ok(caption: str, when: datetime) -> bool:
    """
    Дзен пишет в поле дату словами: «29 августа 2026». Сверяем перед тем,
    как жать публикацию: сошлось – жмём, нет – не трогаем вовсе.
    """
    s = " ".join((caption or "").split()).lower()
    if not s:
        return False
    month = MONTHS_RU[when.month - 1]
    return str(when.day) in s and month in s and str(when.year) in s


def time_caption_ok(caption: str, when: datetime) -> bool:
    """«21:40» в поле времени – ровно то, что мы просили."""
    return (caption or "").strip() == when.strftime("%H:%M")


def calendar_shows(body_text: str, when: datetime) -> bool:
    """
    Виден ли в календаре нужный месяц. Дзен показывает СРАЗУ ДВА месяца
    рядом («Август 2026» и «Сентябрь 2026»), и открывается он на текущем –
    то есть чаще всего листать не нужно вовсе.
    """
    return f"{MONTHS_NOM[when.month - 1]} {when.year}" in (body_text or "")


def _day_candidates(page, when: datetime) -> list:
    """
    Ячейки календаря с нужным числом – «живые» (кликабельные) впереди, серые
    (соседний месяц, прошедшая дата) в конце запасным вариантом.

    Живой прогон 19.08 показал, на чём всё стояло. Заголовок Дзен рисует НЕ
    цельным текстом: «Август» и «2026» – разные узлы без пробела между, и
    `textContent.includes("Август 2026")` не находит его никогда. Прежняя
    версия на этом сдавалась (`заголовок не найден → 0 кандидатов`), хотя
    нужная ячейка «31» была тут же и ЖИВАЯ. Поэтому от заголовка отказались
    вовсе: берём ячейки прямо из блока календаря (класс/предок со словом
    datepicker или calendar), а нужный месяц отсекаем не по заголовку, а по
    сверке поля даты после клика – это делает _click_day_in_month.

    Показаны сразу два месяца, и одно и то же число живёт в обоих (25 августа
    и 25 сентября – оба «живые»). Поэтому кандидатов может быть несколько;
    идём по ним по порядку страницы (текущий месяц раньше следующего) и
    оставляем выбор за сверкой поля. Серые уходят в конец: по прошедшей дате
    или соседнему месяцу всё равно не кликнуть.
    """
    try:
        arr = page.evaluate_handle(
            """(day) => {
                const nodes = [...document.querySelectorAll('*')];
                const shown = (e) => !!(e.offsetParent || e.getClientRects().length);
                const cls = (e) => (e.className && e.className.toString
                    ? e.className.toString() : '');

                // Ячейка календаря: она сама или предок помечены как
                // datepicker/calendar. Так отсекаем посторонние числа на
                // странице (чипы времени, счётчики и прочее).
                const inCal = (el) => {
                    for (let e = el; e && e !== document.body; e = e.parentElement)
                        if (/datepicker|calendar/i.test(cls(e))) return true;
                    return false;
                };

                // Серая/неактивная: соседний месяц или прошедший день. Слова
                // datepicker/calendar тут НЕ считаем – это имя самого блока.
                const dead = (el) => {
                    for (let e = el; e && e !== document.body; e = e.parentElement) {
                        if (e.getAttribute && (e.getAttribute('aria-disabled') === 'true'
                            || e.hasAttribute('disabled'))) return true;
                        const c = cls(e);
                        if (/disabl|faded|muted|outside|other|adjac|inactive|foreign|sibling|not-?current|another|weekend-off|prev-|next-month|passed|_past|-past/i.test(c))
                            return true;
                        let st = null;
                        try { st = getComputedStyle(e); } catch (_) {}
                        if (st && (st.pointerEvents === 'none'
                            || parseFloat(st.opacity) < 0.5)) return true;
                    }
                    return false;
                };

                const cells = [];
                const seen = new Set();
                for (const e of nodes) {
                    if (e.children.length === 0 && shown(e)
                        && (e.textContent || '').trim() === day) {
                        // Кликаем по ячейке целиком: число может лежать в
                        // неактивном span внутри кнопки/ячейки дня.
                        const cell = e.closest(
                            'td,button,[role="button"],[class*="day"],[class*="cell"]') || e;
                        if (!inCal(cell)) continue;
                        if (seen.has(cell)) continue;
                        seen.add(cell);
                        cells.push(cell);
                    }
                }
                // Живые впереди, серые – в конец запасным вариантом (стабильно,
                // поэтому текущий месяц остаётся раньше следующего).
                return cells.sort((a, b) => (dead(a) ? 1 : 0) - (dead(b) ? 1 : 0));
            }""", str(when.day))
        cells = []
        try:
            for _, handle in sorted(arr.get_properties().items(),
                                    key=lambda kv: int(kv[0]) if kv[0].isdigit() else 1 << 30):
                el = handle.as_element()
                if el is not None:
                    cells.append(el)
        except Exception:  # noqa: BLE001
            pass
        return cells
    except Exception:  # noqa: BLE001
        return []


def _cell_note(el) -> str:
    """Коротко о ячейке дня для лога: тег, класс, aria-disabled, размер."""
    try:
        tag = (el.evaluate("e => e.tagName") or "?").lower()
    except Exception:  # noqa: BLE001
        tag = "?"
    try:
        cls = (el.get_attribute("class") or "").strip()
    except Exception:  # noqa: BLE001
        cls = ""
    try:
        aria = el.get_attribute("aria-disabled") or ""
    except Exception:  # noqa: BLE001
        aria = ""
    try:
        box = el.bounding_box() or {}
        size = f"{int(box.get('width', 0))}×{int(box.get('height', 0))}@{int(box.get('x', 0))},{int(box.get('y', 0))}"
    except Exception:  # noqa: BLE001
        size = "?"
    bits = [tag]
    if cls:
        bits.append("." + cls.replace(" ", ".")[:60])
    if aria:
        bits.append(f"aria-disabled={aria}")
    bits.append(size)
    return " ".join(bits)


def _click_day_in_month(page, when: datetime, date_sel: str = "",
                        log: Callable[[str], None] | None = None) -> bool:
    """
    Нажать нужное число в блоке нужного месяца.

    Кандидатов может быть несколько (крайние числа месяца дублируются серой
    ведущей неделей). Идём по ним – живые сначала – и после каждого клика
    сверяемся по полю даты: как только оно показало нужный день, дело
    сделано. Так серая «31 июля» больше не выдаёт себя за «31 августа».

    Каждый шаг подробно пишем в лог: сколько нашли ячеек, по какой кликаем,
    что стало в поле даты после клика. По этим строкам видно, где именно
    ломается выбор, а не только итоговое «не удалось нажать».
    """
    log = log or (lambda m: None)
    cells = _day_candidates(page, when)
    log(f"🔬 Ячеек «{when.day}» в блоке месяца: {len(cells)}")
    if not cells:
        log("🔬 Ни одной ячейки с этим числом не нашли внутри блока месяца – "
            "смотрите дамп календаря выше")
        return False
    for i, el in enumerate(cells, 1):
        note = _cell_note(el)
        log(f"🔬 Кандидат {i}/{len(cells)}: {note}")
        clicked = "реальным кликом"
        try:
            el.click(timeout=5_000)
        except Exception as e:  # noqa: BLE001 – серую/перекрытую так не нажать
            log(f"🔬   реальный клик не прошёл ({type(e).__name__}) – пробую JS-клик")
            try:
                el.evaluate("e => e.click()")
                clicked = "JS-кликом"
            except Exception as e2:  # noqa: BLE001
                log(f"🔬   и JS-клик не прошёл ({type(e2).__name__}) – к следующему")
                continue
        # Без поля для сверки верим первому клику (запасной, старый путь).
        if not date_sel:
            log(f"🔬   нажал {clicked}, поля для сверки нет – считаю удачей")
            return True
        page.wait_for_timeout(700)
        try:
            caption = page.locator(date_sel).first.input_value()
        except Exception:  # noqa: BLE001
            caption = ""
        ok = date_caption_ok(caption, when)
        log(f"🔬   нажал {clicked}, в поле даты теперь «{caption or 'пусто'}» – "
            f"{'то что нужно ✓' if ok else 'не то, пробую следующего'}")
        if ok:
            return True
    log("🔬 Ни один кандидат не выставил нужную дату в поле")
    return False


def _dump_calendar(page, when: datetime, log: Callable[[str], None],
                   tag: str = "") -> None:
    """
    Подробно осмотреть календарь и выписать в лог его устройство: нашлись ли
    заголовки месяцев, сколько ячеек-чисел видно, что именно за ячейки с
    нужным числом (тег, класс, aria-disabled, «серость», координаты) и кусок
    исходного HTML блока. По этому видно, ПОЧЕМУ выбор дня не сработал, даже
    когда картинки нет – в облаке скриншот не всегда доходит.
    """
    title = f"{MONTHS_NOM[when.month - 1]} {when.year}"
    nxt_year = when.year + (1 if when.month == 12 else 0)
    next_title = f"{MONTHS_NOM[when.month % 12]} {nxt_year}"
    head = f"🔬 Осмотр календаря{(' [' + tag + ']') if tag else ''}:"
    try:
        info = page.evaluate(
            """([title, day]) => {
                const nodes = [...document.querySelectorAll('*')];
                const shown = e => !!(e.offsetParent || e.getClientRects().length);
                const cls = e => (e.className && e.className.toString
                    ? e.className.toString() : '');
                const inCal = (el) => {
                    for (let e = el; e && e !== document.body; e = e.parentElement)
                        if (/datepicker|calendar/i.test(cls(e))) return true;
                    return false;
                };
                const deadWhy = (el) => {
                    for (let e = el; e && e !== document.body; e = e.parentElement) {
                        if (e.getAttribute && (e.getAttribute('aria-disabled') === 'true'
                            || e.hasAttribute('disabled'))) return 'disabled';
                        const c = cls(e);
                        if (/disabl|faded|muted|outside|other|adjac|inactive|foreign|sibling|not-?current|another|weekend-off|prev-|next-month|passed|_past|-past/i.test(c))
                            return 'class«' + c.slice(0, 40) + '»';
                        let st = null; try { st = getComputedStyle(e); } catch (_) {}
                        if (st && st.pointerEvents === 'none') return 'pointer-events:none';
                        if (st && parseFloat(st.opacity) < 0.5) return 'opacity:' + st.opacity;
                    }
                    return '';
                };
                // Есть ли где-то на странице цельный «Август 2026» (innerText со
                // склейкой пробела) – чтобы видеть, что месяц реально показан.
                const bodyTxt = (document.body && document.body.innerText) || '';
                const headSeen = bodyTxt.includes(title);
                const hits = [], sample = [];
                let dayCells = 0, box = null;
                for (const e of nodes) {
                    if (e.children.length !== 0 || !shown(e)) continue;
                    if (!/^\\d{1,2}$/.test((e.textContent || '').trim())) continue;
                    const cell = e.closest(
                        'td,button,[role="button"],[class*="day"],[class*="cell"]') || e;
                    if (!inCal(cell)) continue;   // только ячейки календаря
                    dayCells++;
                    if (!box) box = cell.closest('[class*="datepicker"],[class*="calendar"]');
                    const t = e.textContent.trim();
                    const r = cell.getBoundingClientRect();
                    const rec = t + ':' + cell.tagName.toLowerCase()
                        + (cls(cell) ? '.' + cls(cell).replace(/\\s+/g, '.').slice(0, 40) : '')
                        + (deadWhy(cell) ? ' серая(' + deadWhy(cell) + ')' : ' ЖИВАЯ')
                        + ' [' + Math.round(r.x) + ',' + Math.round(r.y) + ']';
                    if (sample.length < 14) sample.push(rec);
                    if (t === day) hits.push(rec);
                }
                const html = box ? box.outerHTML.replace(/\\s+/g, ' ') : '';
                return {
                    headSeen, dayCells, hits, sample,
                    htmlLen: html.length, htmlSample: html.slice(0, 1200),
                };
            }""", [title, str(when.day)])
    except Exception as e:  # noqa: BLE001
        log(f"{head} осмотреть не удалось – {type(e).__name__}: {e}")
        return

    try:
        field = page.locator(_first_visible(page, SEL["date_input"], 2_000) or "input").first.input_value()
    except Exception:  # noqa: BLE001
        field = ""
    log(head)
    log(f"🔬 поле даты сейчас: «{field or 'пусто'}»")
    log(f"🔬 «{title}» на странице (innerText): {'виден' if info.get('headSeen') else 'НЕ виден'}")
    log(f"🔬 ячеек-дней в календаре: {info.get('dayCells', 0)}; "
        f"из них «{when.day}»: {len(info.get('hits') or [])}")
    for h in (info.get("hits") or []):
        log(f"🔬   «{when.day}» → {h}")
    if info.get("sample"):
        log("🔬 ячейки календаря: " + " | ".join(info["sample"]))
    if info.get("htmlSample"):
        log(f"🔬 HTML календаря ({info.get('htmlLen', 0)} симв.), начало:")
        log("🔬 " + info["htmlSample"])


def _open_calendar_and_pick(page, when: datetime, log: Callable[[str], None]) -> str:
    """
    Выбрать день в календаре Дзена. Пусто – получилось, иначе причина.

    Вписать дату в поле нельзя, оно readonly – только клик по числу.

    Про листание отдельно. Календарь открывается на ТЕКУЩЕМ месяце и
    показывает сразу два, так что для ближайших недель листать не нужно
    вовсе. Первый живой прогон этого не понимал: он искал в календаре
    «августа» (родительный падеж, как в поле даты), а там написано
    «Август» – совпадения не было никогда, и Click пролистал вперёд до
    июля 2027. Теперь сравниваем правильным падежом, сначала СМОТРИМ, а
    потом листаем, и не больше чем на год вперёд.
    """
    date_sel = _first_visible(page, SEL["date_input"], timeout=6_000)
    if not date_sel:
        return "не нашли поле даты в окне отложки"

    # Дзен подставляет в поле СЕГОДНЯШНЮЮ дату, и для статьи «на сегодня»
    # она уже правильная – заказчик это и заметил: «она изначально выбрана».
    # Тогда календарь не открываем вовсе: не нажали – нечему и сломаться.
    try:
        already = page.locator(date_sel).first.input_value()
    except Exception:  # noqa: BLE001
        already = ""
    if date_caption_ok(already, when):
        log(f"Дата уже стоит нужная: {already}")
        return ""

    log(f"Открываю календарь (в поле стоит «{already or 'пусто'}», нужно "
        f"{when.day} {MONTHS_RU[when.month - 1]} {when.year})")
    page.locator(date_sel).first.click()
    page.wait_for_timeout(900)

    target = f"{MONTHS_NOM[when.month - 1]} {when.year}"
    if not calendar_shows(_body_text(page), when):
        log(f"Месяца «{target}» пока не видно – листаю вперёд")
        for step in range(1, 13):
            if not _click_first(page, ['[class*="calendar"] [class*="next"]',
                                       '[aria-label*="Следующий"]',
                                       '[class*="arrow"][class*="right"]'], timeout=2_000):
                log(f"🔬 стрелку «вперёд» не нашёл на шаге {step} – листать нечем")
                break
            page.wait_for_timeout(600)
            if calendar_shows(_body_text(page), when):
                log(f"Пролистал календарь до «{target}» ({step} шаг(ов))")
                break
        else:
            _dump_calendar(page, when, log, tag="после листания")
            return (f"в календаре не нашёлся «{target}» – пролистал год вперёд "
                    "и остановился, чтобы не уехать в чужой год")
    if not calendar_shows(_body_text(page), when):
        _dump_calendar(page, when, log, tag="месяц не виден")
        return f"в календаре не видно «{target}»"

    # Перед выбором числа осматриваем блок – в лог попадает, из каких ячеек
    # выбираем. Если клик не сработает, дамп уже будет под рукой.
    _dump_calendar(page, when, log, tag="перед выбором дня")
    if not _click_day_in_month(page, when, date_sel, log):
        _dump_calendar(page, when, log, tag="после неудачного клика")
        return f"не удалось нажать {when.day} в блоке «{target}»"
    page.wait_for_timeout(900)

    caption = ""
    try:
        caption = page.locator(date_sel).first.input_value()
    except Exception:  # noqa: BLE001
        pass
    if not date_caption_ok(caption, when):
        return (f"после выбора даты в поле стоит «{caption or 'пусто'}», "
                f"а нужно {when.day} {MONTHS_RU[when.month - 1]} {when.year}")
    log(f"Дата выбрана: {caption}")
    return ""


def _set_time(page, when: datetime, log: Callable[[str], None]) -> str:
    """
    Вписать время. Пусто – получилось, иначе причина словами.

    Расфокус делаем клавишей Tab, а НЕ кликом по пустому месту: заказчик
    отдельно предупредил, что рядом с полем живёт галочка «Опубликовать
    позже» и промах по ней снимает отложку целиком.
    """
    sel = _first_visible(page, SEL["time_input"], timeout=6_000)
    if not sel:
        return "не нашли поле времени в окне отложки"
    field = page.locator(sel).first
    try:
        field.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        field.type(when.strftime("%H:%M"), delay=60)
        page.keyboard.press("Tab")            # безопасный расфокус
        page.wait_for_timeout(900)
    except Exception as e:  # noqa: BLE001
        return f"не удалось вписать время ({e})"

    got = ""
    try:
        got = field.input_value()
    except Exception:  # noqa: BLE001
        pass
    if not time_caption_ok(got, when):
        return f"Дзен показывает время «{got or 'пусто'}», а нужно {when.strftime('%H:%M')}"
    log(f"Время выставлено: {got}")
    return ""


def click_button_titled(page, title: str) -> bool:
    """
    Нажать кнопку с ТОЧНО такой надписью. Берём НИЖНЮЮ из подходящих.

    Зачем точность. На экране две кнопки со словом «Опубликовать»: одна в
    шапке редактора (ею окно и открывается), другая внизу окна публикации –
    ею подтверждают. Поиск «кнопка, в тексте которой есть „Опубликовать“»
    находил верхнюю, а она в этот момент накрыта окном: нажатие уходило в
    подложку, и прогон 19:48 закончился «не нашли кнопку подтверждения».

    Точное сравнение решает и вторую половину задачи. При включённой
    отложке нижняя кнопка называется «Опубликовать позже» – совсем другая
    надпись, и перепутать её с «Опубликовать» уже нельзя. Из нескольких
    подходящих берём последнюю по странице: окно всегда ниже шапки.
    """
    try:
        handle = page.evaluate_handle(
            """(title) => {
                const shown = (e) => !!(e.offsetParent || e.getClientRects().length);
                const buttons = [...document.querySelectorAll(
                    'button,[role="button"],[class*="base-button"],[class*="Button"]')]
                    .filter(e => shown(e) && (e.innerText || '').trim() === title);
                return buttons.length ? buttons[buttons.length - 1] : null;
            }""", title)
        el = handle.as_element() if handle else None
        if el is None:
            return False
        try:
            el.click(timeout=5_000)
        except Exception:  # noqa: BLE001 – кнопку могло накрыть подсказкой
            el.evaluate("e => e.click()")
        return True
    except Exception:  # noqa: BLE001
        return False


def _turn_on_later(page, log: Callable[[str], None]) -> str:
    """Включить «Опубликовать позже». Пусто – включено, иначе причина."""
    # Окно прокручиваем вниз: галочка живёт под текстом настроек публикации.
    try:
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(600)
    except Exception:  # noqa: BLE001
        pass

    if not _click_first(page, SEL["later_checkbox_title"], timeout=8_000):
        return "не нашли «Опубликовать позже» в окне публикации"
    page.wait_for_timeout(900)

    # Убеждаемся, что галочка ВСТАЛА: клик мимо неё ничего не изменит, а
    # дальше мы бы выставляли время у выключенной отложки и в итоге
    # опубликовали статью сейчас же.
    for sel in SEL["later_checkbox_input"]:
        try:
            box = page.locator(sel).first
            if box.count() and box.is_checked():
                log("Галочка «Опубликовать позже» стоит")
                return ""
        except Exception:  # noqa: BLE001
            continue
    # Поле даты появляется только у включённой отложки – это тоже признак.
    if _first_visible(page, SEL["date_input"], timeout=4_000):
        log("Отложка включена: появились поля даты и времени")
        return ""
    return "галочка «Опубликовать позже» не встала"


# ════════════════════════════════════════════════════════════════════
#  ПУБЛИКАЦИЯ
# ════════════════════════════════════════════════════════════════════

def settle_after_publish(page, headless: bool, title: str,
                         log: Callable[[str], None]) -> bool:
    """
    Дать Дзену договорить и убедиться, что статья принята. True – увидели
    подтверждение своими глазами.

    Порядок такой. Сначала ждём тишины в сети: пока идут запросы, отложка
    ещё в пути, и закрывать браузер нельзя. Потом ищем на странице прямое
    подтверждение – слова «Отложенные», «Запланирована» или заголовок нашей
    статьи в списке публикаций. И только после этого держим окно ещё
    немного: при видимом браузере – подольше, чтобы человек успел увидеть.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:  # noqa: BLE001 – Дзен любит держать соединения открытыми
        pass

    seen = False
    marks = ("Отложенные", "Запланирована", "запланирована",
             "Публикация запланирована", "Отложенная публикация")
    for _ in range(10):
        body = _body_text(page)
        if any(m in body for m in marks) or (title and title[:40] in body):
            seen = True
            break
        page.wait_for_timeout(700)

    if seen:
        log("Дзен подтвердил: статья принята и стоит в отложенных")
    else:
        log("Прямого подтверждения на экране нет – доверяю закрытому окну публикации")

    hold = WATCH_HOLD_MS if not headless else SETTLE_MS
    log(f"Жду {hold // 1000} с, чтобы Дзен доотправил отложку" +
        ("" if headless else " (и чтобы вы успели посмотреть)"))
    page.wait_for_timeout(hold)
    return seen


def schedule_article(project_id: str, editor_url: str, article: dict,
                     when: datetime, email: str = "",
                     log: Callable[[str], None] | None = None,
                     headless: bool = True) -> dict:
    """
    Одна статья в Дзен с отложкой на `when` (время Екатеринбурга).

    {"ok": True, "warnings": [...]} либо {"ok": False, "error": "…", "shot": …}.

    Ничего не публикуется «сейчас»: если на любом шаге что-то разошлось с
    ожиданием, мы не жмём подтверждение вовсе. Статья остаётся черновиком в
    Дзене – её видно в студии, и доделать её можно руками.
    """
    log = log or (lambda m: None)
    log(f"Дзен: сборка {BUILD}")
    warnings: list[str] = list(article.get("warnings") or [])

    src = source_session(project_id)
    if src is None:
        return {"ok": False, "error": "Нет входа в Яндекс – войдите в «Настройках» "
                                      "(тот же вход, что у Яндекс.Бизнеса)"}
    if not editor_url:
        return {"ok": False, "error": "Не указана ссылка на студию Дзена "
                                      "(dzen.ru/profile/editor/…)"}
    if not (article.get("title") or "").strip():
        return {"ok": False, "error": "У статьи нет заголовка – Дзен без него не опубликует"}

    from playwright.sync_api import sync_playwright

    engine = yb.resolve_engine()
    temp = paths.data_root() / project_id / "temp"
    temp.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        import vk_social as _vk
        browser = yb._launch(pw, engine, headless=headless, extra_args=_vk.ANTIBOT_ARGS)
        page = None
        try:
            context = browser.new_context(
                storage_state=str(src),
                viewport={"width": 1440, "height": 950}, user_agent=yb.UA,
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS,
                timezone_id=TIMEZONE_ID)
            context.add_init_script(_vk.ANTIBOT_INIT)
            page = context.new_page()

            why = ensure_studio(page, editor_url, email, log)
            if why:
                return {"ok": False, "error": why,
                        "shot": _debug_shot(project_id, page, "no-login")}

            # Сессию сохраняем СРАЗУ после входа, не дожидаясь конца работы.
            # Иначе выходило обидно: вошли, споткнулись на редакторе – и куки
            # пропали вместе с неудачным прогоном, а в следующий раз Дзен
            # снова просит «Войти» → «Яндекс ID» → аккаунт. Теперь вход
            # делается один раз, а дальше студия открывается сразу.
            try:
                yb._save_storage_state(context, session_path(project_id))
                log("Вход сохранён – в следующий раз входить не придётся")
            except Exception as e:  # noqa: BLE001 – не повод бросать работу
                log(f"Вход сохранить не удалось: {e}")

            # ─── Новая статья ───
            dismiss_popups(page, log)
            log("Нажимаю «＋» → «Написать статью»")
            if not _click_first(page, SEL["add_publication"], timeout=15_000):
                return {"ok": False, "error": "Не нашли кнопку «＋» в студии Дзена",
                        "shot": _debug_shot(project_id, page, "no-add")}
            page.wait_for_timeout(1_200)
            if not _click_first(page, SEL["write_article"], timeout=10_000):
                return {"ok": False, "error": "В меню не нашлось «Написать статью»",
                        "shot": _debug_shot(project_id, page, "no-article-item")}

            # Редактор открывается отдельной страницей и грузится не мгновенно,
            # а поверх него Дзен показывает обучение «Статья» – его закрываем.
            page.wait_for_timeout(6_000)
            title, body = find_editor_fields(page, log, timeout_ms=30_000)
            if title is None:
                return {"ok": False,
                        "error": ("Редактор статьи открылся, но полей в нём не нашлось. "
                                  "Если поверх редактора висит окно обучения – закройте "
                                  "его и попробуйте ещё раз; статья уже сохранена "
                                  "черновиком."),
                        "shot": _debug_shot(project_id, page, "no-editor")}

            # ─── Заголовок ───
            log(f"Заголовок: {article['title'][:80]}")
            title.click()
            page.wait_for_timeout(400)
            page.keyboard.type(article["title"], delay=4)
            page.wait_for_timeout(800)

            # ─── Тело ───
            if body is None:
                # У «чистого листа» тело – следующий блок: Enter из заголовка
                # переводит курсор именно туда.
                page.keyboard.press("Enter")
                page.wait_for_timeout(800)
                _, body = find_editor_fields(page, log, timeout_ms=8_000)
            if body is None:
                return {"ok": False, "error": "Не нашли поле для текста статьи",
                        "shot": _debug_shot(project_id, page, "no-body")}

            blocks = article.get("blocks") or []
            html = blocks_to_html(blocks)
            info = zen_doc.counts(article)
            log(f"Вставляю текст: {info['chars']} знаков, {info['para']} абзацев, "
                f"{info['head']} подзаголовков, {info['list']} списков")
            body.click()
            page.wait_for_timeout(400)
            if not _paste_html_into(page, body, html):
                log("Вставка не прошла – набираю текст построчно")
                warnings.append("Текст набран построчно: подзаголовки могли стать обычными абзацами.")
                _type_blocks_into(page, body, blocks)
            page.wait_for_timeout(2_500)

            # ─── Таблицы картинками ───
            tables = [b for b in blocks if b["kind"] == "table"]
            for n, tbl in enumerate(tables, 1):
                rows = tbl.get("rows") or []
                log(f"Таблица {n} из {len(tables)}: {len(rows)} строк – рисую картинкой")
                shot_path = temp / f"zen-table-{n}.png"
                made = render_table_png(context, rows, shot_path, log)
                if not made:
                    log(f"🔬 Таблица {n}: картинку снять НЕ удалось (см. причину выше)")
                    warnings.append(f"Таблицу {n} вставить не удалось – "
                                    "добавьте её картинкой руками в черновике.")
                    log(f"⚠️ Таблица {n} не вставилась")
                    continue
                log(f"🔬 Таблица {n}: картинка готова ({shot_path.name}), отдаю редактору")
                if not _attach_image(page, made, log, field=body):
                    warnings.append(f"Таблицу {n} вставить не удалось – "
                                    "добавьте её картинкой руками в черновике.")
                    log(f"⚠️ Таблица {n} не вставилась")
                else:
                    log(f"✓ Таблица {n} отдана редактору картинкой")

            page.wait_for_timeout(1_500)

            # ─── Публикация с отложкой ───
            # Окно обучения Дзен показывает не только на входе в редактор:
            # оно всплывает и позже. Перед публикацией убираем всё лишнее –
            # иначе нажатие уйдёт в его подложку.
            dismiss_popups(page, log)
            log("Открываю окно публикации")
            if not (click_button_titled(page, "Опубликовать")
                    or _click_first(page, SEL["publish_button"], timeout=8_000)):
                return {"ok": False, "error": "Не нашли кнопку «Опубликовать»",
                        "shot": _debug_shot(project_id, page, "no-publish")}
            page.wait_for_timeout(2_500)

            why = _turn_on_later(page, log)
            if why:
                return {"ok": False, "error": f"{why}. Статья сохранена черновиком – "
                                              "опубликуйте её из студии руками",
                        "shot": _debug_shot(project_id, page, "no-later")}

            # Пояс окна: реестр живёт по Екатеринбургу, Дзен показывает своё.
            offset = zone_offset(_body_text(page))
            local = when_in_zone(when, offset)
            if offset is not None and local != when:
                log(f"Дзен показывает время в GMT{offset:+d}: ставлю "
                    f"{local.strftime('%d.%m %H:%M')} вместо {when.strftime('%d.%m %H:%M')}")

            why = _open_calendar_and_pick(page, local, log)
            if why:
                return {"ok": False, "error": f"{why}. Ничего не опубликовали – "
                                              "статья осталась черновиком",
                        "shot": _debug_shot(project_id, page, "no-date")}

            why = _set_time(page, local, log)
            if why:
                return {"ok": False, "error": f"{why}. Ничего не опубликовали – "
                                              "статья осталась черновиком",
                        "shot": _debug_shot(project_id, page, "no-time")}

            # Последняя сверка перед нажатием. Кнопка публикации стоит рядом с
            # галочкой отложки, и цена ошибки – статья, вышедшая сейчас же.
            try:
                date_now = page.locator(_first_visible(page, SEL["date_input"], 3_000)).first.input_value()
                time_now = page.locator(_first_visible(page, SEL["time_input"], 3_000)).first.input_value()
            except Exception:  # noqa: BLE001
                date_now = time_now = ""
            if not (date_caption_ok(date_now, local) and time_caption_ok(time_now, local)):
                return {"ok": False,
                        "error": (f"Перед подтверждением в окне стоит «{date_now} {time_now}», "
                                  f"а нужно {local.strftime('%d.%m.%Y %H:%M')}. Не жали ничего – "
                                  "статья осталась черновиком"),
                        "shot": _debug_shot(project_id, page, "wrong-when")}

            log(f"Подтверждаю отложку на {local.strftime('%d.%m.%Y %H:%M')}")
            # Кнопка внизу окна называется по тому, что мы выбрали: с
            # галочкой – «Опубликовать позже». Жмём именно её, точной
            # надписью, а не «что-нибудь со словом опубликовать».
            if not (click_button_titled(page, "Опубликовать позже")
                    or click_button_titled(page, "Запланировать")):
                return {"ok": False,
                        "error": ("Не нашли кнопку «Опубликовать позже» внизу окна "
                                  "публикации. Дата и время выставлены – можно нажать "
                                  "её руками, статья лежит черновиком"),
                        "shot": _debug_shot(project_id, page, "no-confirm")}

            # Ответ площадки. Главный признак – окно публикации закрылось:
            # Дзен принял отложку и вернул в редактор или студию. Слова
            # («Отложенные», «запланирована») бывают не всегда, ждать только
            # их – значит пугать человека ложной неудачей после удачи.
            for _ in range(40):
                page.wait_for_timeout(500)
                body = _body_text(page)
                done = any(mark in body for mark in ("Отложенные", "запланирована",
                                                     "Публикация запланирована", "Черновики"))
                if done or "Опубликовать позже" not in body:
                    log("Окно публикации закрылось – Дзен принял отложку"
                        if not done else "Дзен подтвердил: статья в отложенных")
                    # Не закрываем браузер сразу: отложка ещё летит на сервер,
                    # и оборванный на середине разговор её потеряет.
                    if not settle_after_publish(page, headless, article.get("title", ""), log):
                        warnings.append("Дзен не написал словами, что статья в отложенных – "
                                        "загляните в студию и проверьте.")
                    yb._save_storage_state(context, session_path(project_id))
                    return {"ok": True, "warnings": warnings}

            # Окно так и висит – значит Дзен отложку не принял.
            yb._save_storage_state(context, session_path(project_id))
            return {"ok": False,
                    "error": ("Нажали «Опубликовать позже», но окно публикации не "
                              "закрылось. Загляните в студию: если статья в "
                              "«Отложенных» – формировать заново не нужно"),
                    "shot": _debug_shot(project_id, page, "no-confirmation")}

        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e),
                    "shot": _debug_shot(project_id, page, "error") if page else None}
        finally:
            browser.close()


def schedule_postponed_post(project_id: str, editor_url: str, text: str,
                            image_paths: list[str], when: datetime,
                            log: Callable[[str], None] | None = None,
                            headless: bool = True, email: str = "") -> dict:
    """
    Тот же вызов, что у ВК, ОК и МАКС – чтобы Дзен встал в общий конвейер
    формирования и в пробную отложку без особого случая на каждый шаг.

    `text` здесь – либо ссылка на документ статьи, либо готовый текст:
    zen_doc разберётся сам.
    """
    log = log or (lambda m: None)
    try:
        article = zen_doc.article_for({"text": text})
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    return schedule_article(project_id, editor_url, article, when,
                            email=email, log=log, headless=headless)
