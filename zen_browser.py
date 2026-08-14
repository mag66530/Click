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

# Куки, по которым видно ВХОД. Яндексовые – те же, что у ЯБ (session_id и
# компания на .yandex.ru); дзеновские площадка ставит уже сама.
YANDEX_AUTH = {"session_id", "sessionid2", "yandex_login"}

MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря")

# Селекторы. Классы Дзена собраны сборщиком и несут хвост-хеш
# («…__addButton-1Z»): при следующем релизе хвост сменится, а середина имени
# останется. Поэтому везде, где можно, ищем по data-testid и по КУСКУ класса,
# а не по классу целиком – иначе модуль ломался бы от каждой выкладки Дзена.
SEL = {
    "add_publication": ['[data-testid="add-publication-button"]',
                        '[class*="author-studio-header__addButton"]'],
    "write_article": ['label[aria-label="Написать статью"]',
                      '[class*="new-publication-dropdown"] [class*="buttonTitle"]'],
    "title_field": ['[data-testid="article-title"]',
                    '[placeholder*="Заголовок"]',
                    '[contenteditable="true"][class*="title"]',
                    'textarea[class*="title"]'],
    "body_field": ['[data-testid="article-body"]',
                   '[contenteditable="true"][class*="body"]',
                   '[contenteditable="true"][class*="content"]'],
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

def _looks_logged_out(page) -> bool:
    """Дзен показывает вход, а не студию автора."""
    url = (page.url or "").lower()
    if "passport.yandex" in url or "/auth" in url:
        return True
    body = _body_text(page)
    return ("Войдите удобным способом" in body
            or "Войти через Яндекс ID" in body
            or "Введите номер телефона" in body)


def pick_account(page, email: str, log: Callable[[str], None]) -> str:
    """
    Экран «Выберите аккаунт для входа» – выбрать аккаунт проекта.

    Сравниваем по логину (часть до «@»), как это делает проверка аккаунта в
    ЯБ: паспорт рядом с адресом показывает и имя, и привязанные почты, и
    точное совпадение целой строки подводит.

    Возвращает пустую строку, если всё хорошо (аккаунт выбран либо экрана
    выбора не было), иначе – причину словами.
    """
    body = _body_text(page)
    if "Выберите аккаунт" not in body:
        return ""

    login = (email or "").strip().lower().split("@")[0]
    if not login:
        return ("Дзен просит выбрать аккаунт, а в «Настройках» не указан email "
                "проекта – Click не знает, какой из них ваш.")
    try:
        # Ищем строку аккаунта по видимому тексту почты и жмём по ней.
        item = page.locator(f'text=/{re.escape(login)}@/i').first
        if not item.count():
            found = ", ".join(re.findall(r"[\w.+-]+@[\w.-]+", body)[:6]) or "ни одного"
            return (f"В Яндексе не нашёлся аккаунт {email}. Что предложено: {found}. "
                    "Войдите нужным аккаунтом руками или поправьте email в «Настройках».")
        log(f"Выбираю аккаунт {email}")
        item.click()
        page.wait_for_timeout(4_000)
        return ""
    except Exception as e:  # noqa: BLE001
        return f"Не получилось выбрать аккаунт {email}: {e}"


def ensure_studio(page, editor_url: str, email: str, log: Callable[[str], None]) -> str:
    """
    Открыть студию автора и убедиться, что мы вошли. Пусто – всё хорошо,
    иначе причина словами.
    """
    log(f"Открываю студию Дзена: {editor_url}")
    page.goto(editor_url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3_000)

    if not _looks_logged_out(page):
        return ""

    # Дзен предлагает войти. Куки Яндекса у нас есть, поэтому чаще всего
    # достаточно нажать «Войти через Яндекс ID» и выбрать аккаунт.
    log("Дзен показывает вход – пробую войти сохранённой сессией Яндекса")
    _click_first(page, ['text="Войти через Яндекс ID"', 'text="Войти"'], timeout=6_000)
    page.wait_for_timeout(4_000)

    why = pick_account(page, email, log)
    if why:
        return why

    # После выбора аккаунта Дзен возвращает в студию сам, но не мгновенно.
    for _ in range(20):
        if not _looks_logged_out(page):
            break
        page.wait_for_timeout(1_000)

    if _looks_logged_out(page):
        return ("Дзен не пустил сохранённой сессией Яндекса: он просит вход "
                "заново (телефон или пароль). Войдите в Яндекс в «Настройках» – "
                "тем же входом, что для Яндекс.Бизнеса.")

    if editor_url not in (page.url or ""):
        page.goto(editor_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2_500)
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


def render_table_png(context, rows: list[list[str]], out_path: Path) -> str:
    """
    Нарисовать таблицу и снять её в PNG. Возвращает путь или пустую строку.

    Рисуем ТЕМ ЖЕ браузером, что публикует: второй движок ради картинки –
    лишние полгигабайта памяти в облаке, на этом Click уже падал.
    """
    page = None
    try:
        page = context.new_page()
        page.set_viewport_size({"width": 1000, "height": 800})
        page.set_content(table_html(rows), wait_until="domcontentloaded")
        page.wait_for_timeout(300)
        page.locator("#t").screenshot(path=str(out_path))
        return str(out_path)
    except Exception:  # noqa: BLE001 – без картинки статья всё равно выйдет
        return ""
    finally:
        try:
            if page:
                page.close()
        except Exception:  # noqa: BLE001
            pass


def _paste_html(page, selector: str, html: str) -> bool:
    """
    Вставить HTML в поле редактора – ровно так, как это делает Ctrl+V.

    Редактор Дзена слушает событие вставки и разбирает буфер сам: абзацы,
    подзаголовки и списки встают на места. Пишем в буфер через DataTransfer
    и шлём настоящее событие paste – к системному буферу в облаке доступа
    нет, да он там и не нужен.
    """
    try:
        page.locator(selector).first.click()
        page.wait_for_timeout(400)
        return bool(page.evaluate(
            """([sel, html]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                el.focus();
                const dt = new DataTransfer();
                dt.setData('text/html', html);
                dt.setData('text/plain', html.replace(/<[^>]+>/g, ' '));
                const ev = new ClipboardEvent('paste', {
                    bubbles: true, cancelable: true, clipboardData: dt });
                return el.dispatchEvent(ev);
            }""", [selector, html]))
    except Exception:  # noqa: BLE001
        return False


def _type_blocks(page, selector: str, blocks: list[dict]) -> None:
    """
    Запасной путь: набрать статью построчно.

    Форматирование при этом теряется (подзаголовки станут обычными
    абзацами) – зато текст доедет целиком. Лучше статья без подзаголовков,
    чем пустой черновик.
    """
    page.locator(selector).first.click()
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


def _attach_image(page, path: str) -> bool:
    """Отдать картинку редактору через файловое поле (их у него несколько)."""
    try:
        inputs = page.locator(SEL["file_input"][0])
        if not inputs.count():
            return False
        inputs.first.set_input_files(path)
        page.wait_for_timeout(3_000)
        return True
    except Exception:  # noqa: BLE001
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


def _open_calendar_and_pick(page, when: datetime, log: Callable[[str], None]) -> str:
    """
    Выбрать день в календаре Дзена. Пусто – получилось, иначе причина.

    Вписать дату в поле нельзя, оно readonly – только клик по числу. Если
    нужный месяц ещё не открыт, листаем вперёд стрелкой; назад не листаем
    никогда: отложка в прошлое невозможна, и там нам делать нечего.
    """
    date_sel = _first_visible(page, SEL["date_input"], timeout=6_000)
    if not date_sel:
        return "не нашли поле даты в окне отложки"
    page.locator(date_sel).first.click()
    page.wait_for_timeout(800)

    target_month = f"{MONTHS_RU[when.month - 1]}"
    for _ in range(14):                       # год вперёд с запасом
        body = _body_text(page)
        if re.search(rf"{target_month}\s*{when.year}", body, re.I) or target_month in body.lower():
            break
        moved = _click_first(page, ['[class*="calendar"] [class*="next"]',
                                    '[aria-label*="Следующий"]',
                                    '[class*="arrow"][class*="right"]'], timeout=2_000)
        if not moved:
            break
        page.wait_for_timeout(500)

    # Число кликаем ТОЧНЫМ совпадением текста: иначе «2» попадёт в «22».
    try:
        day = page.locator(
            f'[class*="calendar"] >> text="{when.day}"').first
        if not day.count():
            day = page.get_by_text(str(when.day), exact=True).last
        day.click()
        page.wait_for_timeout(900)
    except Exception as e:  # noqa: BLE001
        return f"не удалось нажать число {when.day} в календаре ({e})"

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

            # ─── Новая статья ───
            log("Нажимаю «＋» → «Написать статью»")
            if not _click_first(page, SEL["add_publication"], timeout=15_000):
                return {"ok": False, "error": "Не нашли кнопку «＋» в студии Дзена",
                        "shot": _debug_shot(project_id, page, "no-add")}
            page.wait_for_timeout(1_200)
            if not _click_first(page, SEL["write_article"], timeout=10_000):
                return {"ok": False, "error": "В меню не нашлось «Написать статью»",
                        "shot": _debug_shot(project_id, page, "no-article-item")}

            # Редактор открывается отдельной страницей и грузится не мгновенно.
            page.wait_for_timeout(6_000)
            title_sel = _first_visible(page, SEL["title_field"], timeout=25_000)
            if not title_sel:
                return {"ok": False, "error": "Редактор статьи не открылся: не нашли поле заголовка",
                        "shot": _debug_shot(project_id, page, "no-editor")}

            # ─── Заголовок ───
            log(f"Заголовок: {article['title'][:80]}")
            page.locator(title_sel).first.click()
            page.keyboard.type(article["title"], delay=4)
            page.wait_for_timeout(800)

            # ─── Тело ───
            body_sel = _first_visible(page, SEL["body_field"], timeout=10_000)
            if not body_sel:
                # У «чистого листа» тело – следующий редактируемый блок;
                # добраться до него можно просто Enter'ом из заголовка.
                page.keyboard.press("Enter")
                page.wait_for_timeout(600)
                body_sel = _first_visible(page, SEL["body_field"], timeout=6_000)
            if not body_sel:
                return {"ok": False, "error": "Не нашли поле для текста статьи",
                        "shot": _debug_shot(project_id, page, "no-body")}

            blocks = article.get("blocks") or []
            html = blocks_to_html(blocks)
            info = zen_doc.counts(article)
            log(f"Вставляю текст: {info['chars']} знаков, {info['para']} абзацев, "
                f"{info['head']} подзаголовков, {info['list']} списков")
            if not _paste_html(page, body_sel, html):
                log("Вставка не прошла – набираю текст построчно")
                warnings.append("Текст набран построчно: подзаголовки могли стать обычными абзацами.")
                _type_blocks(page, body_sel, blocks)
            page.wait_for_timeout(2_500)

            # ─── Таблицы картинками ───
            tables = [b for b in blocks if b["kind"] == "table"]
            for n, tbl in enumerate(tables, 1):
                log(f"Таблица {n} из {len(tables)}: рисую картинкой")
                shot_path = temp / f"zen-table-{n}.png"
                made = render_table_png(context, tbl["rows"], shot_path)
                if not made or not _attach_image(page, made):
                    warnings.append(f"Таблицу {n} вставить не удалось – "
                                    "добавьте её картинкой руками в черновике.")
                    log(f"⚠️ Таблица {n} не вставилась")

            page.wait_for_timeout(1_500)

            # ─── Публикация с отложкой ───
            log("Открываю окно публикации")
            if not _click_first(page, SEL["publish_button"], timeout=15_000):
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
            if not _click_first(page, SEL["publish_button"], timeout=8_000):
                return {"ok": False, "error": "Не нашли кнопку подтверждения в окне публикации",
                        "shot": _debug_shot(project_id, page, "no-confirm")}

            # Ответ площадки. Дзен уводит в студию и показывает статью в
            # отложенных; ждём не торопясь – в облаке всё медленнее.
            for _ in range(30):
                body = _body_text(page)
                if any(mark in body for mark in ("Отложенные", "запланирована",
                                                 "Публикация запланирована", "Черновики")):
                    log("Дзен подтвердил: статья в отложенных")
                    yb._save_storage_state(context, session_path(project_id))
                    return {"ok": True, "warnings": warnings}
                page.wait_for_timeout(500)

            # Подтверждения не дождались. Это не обязательно провал – могло
            # просто не смениться содержимое, – поэтому говорим прямо, куда
            # заглянуть, вместо бодрого «готово».
            yb._save_storage_state(context, session_path(project_id))
            return {"ok": False,
                    "error": "Дзен не подтвердил отложку словами. Загляните в студию: "
                             "если статья в «Отложенных» – формировать заново не нужно",
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
