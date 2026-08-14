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

import time
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


EDITOR_WAIT_S = 75          # столько ждём, пока МАКС дорисует чат


def _wait_for_editor(page, log: Callable[[str], None] | None = None,
                     timeout_s: int = EDITOR_WAIT_S) -> str:
    """
    Дождаться поля «Пост», а не заглянуть один раз и уйти.

    Живой отказ в облаке (12.08.2026): «Не нашли поле Пост» через восемь
    секунд после открытия, а на снимке того же мгновения – пустой экран с
    крутящимся логотипом МАКС. Приложение просто ещё грузилось.

    МАКС – одностраничное приложение: сначала приходит пустая страница, и
    только потом скрипты рисуют чат. На своём компьютере это доли секунды,
    в облаке контейнер холодный и канал чужой – там четырёх секунд не
    хватало никогда. Ждём долго и говорим об этом словами, чтобы ожидание
    не выглядело зависанием.
    """
    log = log or (lambda m: None)
    said = 0
    for waited in range(timeout_s):
        sel = _first_visible(page, SEL["text"])
        if sel:
            if waited:
                log(f"МАКС дорисовал чат за {waited} с")
            return sel
        if waited and waited - said >= 15:
            said = waited
            log(f"Жду, пока МАКС загрузится… ({waited} с)")
        page.wait_for_timeout(1_000)
    return ""


# Что бывает на экране вместо чата. Слова – с живых страниц МАКС.
LOGIN_MARKS = ("Войти по номеру телефона", "Введите номер телефона",
               "Войдите в аккаунт", "QR-код", "Войти в MAX", "Войти в МАКС")
APP_MARKS = ("Открыть в приложении", "Скачать MAX", "Установите приложение")


def _why_no_editor(page) -> str:
    """
    Почему поля «Пост» так и не появилось – ответ по тому, ЧТО на экране.

    Три разные беды выглядели одинаково («не нашли поле»), а лечатся
    по-разному: заново собрать сессию, поменять ссылку или формировать не
    из облака. Поэтому спрашиваем саму страницу.
    """
    try:
        body = page.evaluate(
            "() => document.body ? (document.body.innerText || '') : ''") or ""
    except Exception:  # noqa: BLE001
        body = ""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        url = ""
    seen = " ".join(body.split())

    if any(m in body for m in LOGIN_MARKS):
        return ("МАКС показывает экран входа – сессия не действует. Соберите "
                "файл сессий заново (VHOD-VK-i-OK.py) и загрузите его в "
                "«Настройках». Пост НЕ отправлен")
    if any(m in body for m in APP_MARKS):
        return ("МАКС предлагает открыть приложение вместо чата – похоже, "
                "ссылка ведёт не в канал веб-версии. Нужен адрес вида "
                f"web.max.ru/-70916…, а открылось: {url or 'неизвестно'}")
    if len(seen) < 40:
        return (f"МАКС так и не загрузился за {EDITOR_WAIT_S} с – на снимке "
                "пустой экран с логотипом. Из облака это обычное дело: МАКС "
                "не пускает автоматический браузер с чужого адреса. "
                "Сформируйте МАКС на своём компьютере – там же, где делали "
                "файл сессий")
    return ("Не нашли поле «Пост». На экране МАКС было: "
            f"«{seen[:160]}». Пост НЕ отправлен")


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

    def shown() -> int:
        """Что в поле сейчас. Сперва спрашиваем aria-valuenow – это то,
        что МАКС думает сам, а не то, что нарисовано."""
        try:
            el = page.locator(selector).first
            for attr in ("aria-valuenow", "aria-valuetext"):
                raw = el.get_attribute(attr)
                if raw and raw.strip().lstrip("-").isdigit():
                    return int(raw.strip())
            digits = "".join(ch for ch in (el.inner_text() or "") if ch.isdigit())
            return int(digits) if digits else -1
        except Exception:  # noqa: BLE001
            return -1

    try:
        page.locator(selector).first.click()
        page.wait_for_timeout(150)
        page.keyboard.type(want, delay=120)   # крутилка перебивает сама
        page.wait_for_timeout(400)
        if shown() == value:
            return True
        # Не поддалось печатью – доводим стрелками от текущего значения.
        now = shown()
        if now < 0:
            return False
        key = "ArrowUp" if value > now else "ArrowDown"
        for _ in range(abs(value - now)):
            page.keyboard.press(key)
            page.wait_for_timeout(50)
        return shown() == value
    except Exception:  # noqa: BLE001
        return False


# ────────────────────────────── календарь ──────────────────────────────
#
# Первая попытка искала числа внутри [role="dialog"] – и не нашла даже то,
# что человек видел своими глазами (лог от 12.08.2026: «не нашли число 12»,
# а 12-е на снимке обведено кружком). Урок тот же, что и на ОК: не держаться
# за разметку, которой у площадки может не быть. Окно МАКС нарисовано
# обычными div-ами без единого role, поэтому календарь ищем по существу –
# по самой сетке чисел.
#
# Второе, что здесь важно: в сетке числа повторяются. Хвост прошлого месяца
# (27…31) и начало следующего (1…6) выглядят так же, как свои. Кликнуть в
# «1», не разобравшись, – значит поставить пост не в тот месяц. Поэтому
# берём не «первое подходящее число», а нужную клетку по порядку: свой
# месяц начинается с первой единицы в сетке.

_JS_CAL = r"""
const _vis = (el) => {
  const r = el.getBoundingClientRect();
  if (r.width < 4 || r.height < 4) return false;
  const s = getComputedStyle(el);
  return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
};
const _leaves = () => Array.from(document.querySelectorAll('body *')).filter(el => {
  if (el.children.length) return false;
  const t = (el.textContent || '').trim();
  if (!/^\d{1,2}$/.test(t)) return false;
  const n = parseInt(t, 10);
  return n >= 1 && n <= 31 && _vis(el);
});
const _grid = (leaves) => {
  for (const leaf of leaves) {
    let el = leaf;
    for (let i = 0; i < 8 && el; i++) {
      let inside = 0;
      for (const l of leaves) if (el.contains(l)) inside++;
      if (inside >= 20) return el;
      el = el.parentElement;
    }
  }
  return null;
};
const _RX_MONTH =
  /(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*\s+(\d{4})/i;
const _header = (grid) => {
  let el = grid;
  for (let i = 0; i < 8 && el; i++) {
    const m = (el.innerText || '').match(_RX_MONTH);
    if (m) return m[0];
    el = el.parentElement;
  }
  return '';
};
const _headerBox = () => {
  for (const el of Array.from(document.querySelectorAll('body *'))) {
    if (el.children.length) continue;
    const t = (el.textContent || '').trim();
    if (_RX_MONTH.test(t) && t.length < 40 && _vis(el)) return el;
  }
  return null;
};
"""

_MONTHS = (("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4),
           ("мая", 5), ("май", 5), ("июн", 6), ("июл", 7), ("август", 8),
           ("сентябр", 9), ("октябр", 10), ("ноябр", 11), ("декабр", 12))


def month_year(header: str) -> tuple[int, int]:
    """«Август 2026» → (8, 2026). Не разобрали – (0, 0)."""
    import re

    low = (header or "").lower()
    year = re.search(r"(20\d\d)", low)
    for prefix, num in _MONTHS:
        if prefix in low:
            return num, int(year.group(1)) if year else 0
    return 0, 0


def day_index(nums: list[int], day: int) -> int:
    """
    Какая клетка сетки – нужное число СВОЕГО месяца.

    Сетка идёт подряд: хвост прошлого месяца, потом свой месяц с единицы,
    потом начало следующего. Значит первая единица – начало своего месяца,
    а дальше просто отсчёт. -1, если такой клетки нет.
    """
    try:
        start = nums.index(1)
    except ValueError:
        return -1
    idx = start + day - 1
    if 0 <= idx < len(nums) and nums[idx] == day:
        return idx
    return -1


def _calendar(page) -> dict:
    """Что сейчас в календаре: заголовок и все числа сетки по порядку."""
    try:
        return page.evaluate("() => {" + _JS_CAL + """
            const leaves = _leaves();
            const grid = _grid(leaves);
            if (!grid) return {ok: false, header: '', nums: []};
            const cells = leaves.filter(l => grid.contains(l));
            return {ok: true, header: _header(grid),
                    nums: cells.map(c => parseInt((c.textContent || '').trim(), 10))};
        }""") or {"ok": False, "header": "", "nums": []}
    except Exception:  # noqa: BLE001
        return {"ok": False, "header": "", "nums": []}


def _shift_month(page, forward: bool) -> bool:
    """
    Перелистнуть месяц. Стрелки у МАКС – без подписей и без role, зато
    всегда слева и справа от «Август 2026». По этому и находим: ищем то,
    что можно нажать, на той же высоте, что заголовок.
    """
    try:
        return bool(page.evaluate("(fwd) => {" + _JS_CAL + """
            const hdr = _headerBox();
            if (!hdr) return false;
            const hb = hdr.getBoundingClientRect();
            const mid = hb.top + hb.height / 2;
            let best = null, bestGap = 1e9;
            const able = Array.from(document.querySelectorAll(
                'button, [role="button"], svg, [class*="arrow"], [class*="chevron"]'));
            for (const el of able) {
                if (!_vis(el)) continue;
                const r = el.getBoundingClientRect();
                if (Math.abs(r.top + r.height / 2 - mid) > 24) continue;
                const gap = fwd ? r.left - hb.right : hb.left - r.right;
                if (gap < -4 || gap > 260 || gap >= bestGap) continue;
                best = el; bestGap = gap;
            }
            if (!best) return false;
            (best.closest('button, [role="button"]') || best).dispatchEvent(
                new MouseEvent('click', {bubbles: true, cancelable: true}));
            return true;
        }""", forward))
    except Exception:  # noqa: BLE001
        return False


def _pick_day(page, when: datetime,
              log: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """
    Выбрать нужный день. Вернём (получилось, чем объяснить неудачу).
    """
    log = log or (lambda m: None)
    seen = ""
    for _ in range(15):
        cal = _calendar(page)
        if not cal.get("ok"):
            return False, ("в окне МАКС не видно сетки календаря – похоже, "
                           "окно «Запланировать пост» не открылось")
        header = cal.get("header") or ""
        nums = [int(n) for n in cal.get("nums") or []]
        seen = f"на экране «{header or 'без заголовка'}», чисел в сетке {len(nums)}"
        month, year = month_year(header)
        if month and year and (year, month) != (when.year, when.month):
            forward = (year, month) < (when.year, when.month)
            log(f"Листаю календарь: с «{header}» на {when.month:02d}.{when.year}")
            if not _shift_month(page, forward):
                return False, f"не нашли стрелку перелистывания месяца ({seen})"
            page.wait_for_timeout(500)
            continue

        idx = day_index(nums, when.day)
        if idx < 0:
            return False, f"в сетке нет числа {when.day} своего месяца ({seen})"
        try:
            page.evaluate("(i) => {" + _JS_CAL + """
                const leaves = _leaves();
                const grid = _grid(leaves);
                const cells = leaves.filter(l => grid.contains(l));
                const cell = cells[i];
                const hit = cell.closest('button, td, [role="button"]') || cell.parentElement || cell;
                hit.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            }""", idx)
        except Exception as e:  # noqa: BLE001
            return False, f"не смогли нажать на число {when.day}: {e} ({seen})"
        return True, ""
    return False, f"календарь не долистался до нужного месяца ({seen})"


def confirm_caption(page) -> str:
    """Что написано на кнопке «Отправить …» – это ответ самой площадки."""
    try:
        return (page.evaluate("""() => {
            for (const b of Array.from(document.querySelectorAll('button'))) {
                const t = (b.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!/^Отправить\\s+\\S/.test(t)) continue;
                const r = b.getBoundingClientRect();
                if (r.width < 4 || r.height < 4) continue;
                return t;
            }
            return '';
        }""") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _click_confirm(page, caption: str) -> bool:
    """
    Нажать ровно ту кнопку, чью надпись мы только что сверили. Иначе можно
    попасть в другую кнопку со словом «Отправить» и отправить не то.
    """
    if caption:
        try:
            if page.evaluate("""(cap) => {
                for (const b of Array.from(document.querySelectorAll('button'))) {
                    if ((b.innerText || '').replace(/\\s+/g, ' ').trim() !== cap) continue;
                    const r = b.getBoundingClientRect();
                    if (r.width < 4 || r.height < 4) continue;
                    b.click();
                    return true;
                }
                return false;
            }""", caption):
                return True
        except Exception:  # noqa: BLE001
            pass
    return bool(_click_first(page, SEL["confirm"], timeout=8_000))


def caption_ok(caption: str, when: datetime, today=None) -> bool:
    """
    Сходится ли надпись на кнопке с тем, что мы задумали.

    Это последняя проверка перед нажатием: МАКС сам пишет на кнопке, когда
    он собрался публиковать («Отправить завтра в 13:00»). Если он понял нас
    иначе – жать нельзя, иначе пост уйдёт не в то время.
    """
    import re

    if not caption:
        return True                      # нечего сверять – не мешаем
    low = caption.lower()
    if not re.search(rf"\b0?{when.hour}:{when.minute:02d}\b", low):
        return False
    today = today or datetime.now().date()
    days = (when.date() - today).days
    if days == 0 and "сегодня" in low:
        return True
    if days == 1 and "завтра" in low:
        return True
    return re.search(rf"\b{when.day}\b(?!\s*:)", low) is not None


def _editor_node(page, sel: str):
    """
    ТО САМОЕ поле ввода, а не первый узел, подошедший под селектор.

    У МАКС под селектор поля подходит сразу несколько узлов: рядом с живым
    редактором лежит его пустышка-подсказка. `.first` мог взять именно её –
    и тогда клик уходил в пустышку, текст вписывался не туда, а проверки
    читали пустоту и объявляли отправку, которой не было.

    Выбираем по размеру: настоящее поле – самое крупное из подходящих.
    Подсказка нарисована в один пиксель или спрятана вовсе.
    """
    loc = page.locator(sel)
    try:
        count = loc.count()
    except Exception:  # noqa: BLE001
        return loc.first
    if count <= 1:
        return loc.first
    best, best_area = loc.first, -1.0
    for i in range(count):
        item = loc.nth(i)
        try:
            box = item.bounding_box(timeout=1_000) or {}
        except Exception:  # noqa: BLE001
            continue
        area = float(box.get("width") or 0) * float(box.get("height") or 0)
        if area > best_area:
            best, best_area = item, area
    return best


def _editor_text(page, sel: str) -> str:
    """
    Что сейчас лежит в поле поста. Пусто – поле пустое или не прочиталось.

    Читаем выбранный узел, а при осечке – ещё раз: Lexical пересобирает
    разметку прямо во время ввода, и evaluate может прийтись на узел,
    которого уже нет. Пустая строка от такой осечки однажды была объявлена
    «МАКС отправил строку вместо переноса» – хотя в канал ничего не уходило,
    и весь пост стоял в поле (14.08.2026).
    """
    read = "el => (el.innerText != null ? el.innerText : (el.value || ''))"
    for attempt in range(2):
        try:
            return _editor_node(page, sel).evaluate(read) or ""
        except Exception:  # noqa: BLE001 – поле перерисовалось, читаем ещё раз
            pass
        if not attempt:
            page.wait_for_timeout(300)
    return ""


# Текст СТРАНИЦЫ без поля ввода – то есть сама лента канала.
#
# Это ответ на вопрос заказчицы: «ты же можешь посмотреть, что там было до
# этого и что ты написал; если там вчерашний пост – значит программа ничего
# не отправляла». Именно так теперь и проверяем: не по тому, что померещилось
# в поле ввода, а по тому, появилось ли в КАНАЛЕ новое сообщение.
_FEED_JS = r"""
(el) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
  const parts = [];
  let node;
  while ((node = walker.nextNode())) {
    // Всё, что лежит внутри поля ввода, – это ещё не отправленное.
    if (el && el.contains(node)) continue;
    const t = (node.nodeValue || '').trim();
    if (t) parts.push(t);
  }
  return parts.join('\n');
}
"""


def _feed_letters(page, sel: str) -> str:
    """Лента канала одной строкой из букв – для сравнения «было / стало»."""
    try:
        return _letters(_editor_node(page, sel).evaluate(_FEED_JS) or "")
    except Exception:  # noqa: BLE001
        try:
            return _letters(page.evaluate(
                "() => document.body ? (document.body.innerText || '') : ''") or "")
        except Exception:  # noqa: BLE001
            return ""


def _sent_lines(page, sel: str, before: str, lines: list[str]) -> list[str]:
    """
    Какие наши строки ПОЯВИЛИСЬ в канале, которых там не было до ввода.

    Пусто – значит не отправилось ничего, что бы ни показывало поле ввода.
    Старый пост, вчерашний или сегодняшний, сюда не попадёт: он был в ленте
    и до нас.
    """
    now = _feed_letters(page, sel)
    if not now:
        return []
    out = []
    for line in lines:
        mark = _probe(line)
        if mark and mark not in before and mark in now:
            out.append(line)
    return out


def _letters(s: str) -> str:
    """Только буквы и цифры. Значки и пробелы отбрасываем: МАКС рисует
    эмодзи картинками, и в тексте поля их может не оказаться вовсе."""
    import re
    return re.sub(r"\W", "", s, flags=re.U).lower()


def _probe(line: str) -> str:
    """
    Короткий отпечаток ОДНОЙ строки – по нему ищем её в поле.

    Обрезка здесь только для искомой строки. Обрезать заодно и текст поля –
    ровно та ошибка, которая 14.08.2026 сорвала отложку МАКС на ровном
    месте: в «поле» оставались первые 32 буквы, конец текста в них не
    находился никогда, и Click отказывался планировать уже введённый пост.
    """
    return _letters(line)[:32]


def _clear_editor(page, sel: str) -> bool:
    """
    Опустошить поле перед вводом. True – поле пустое.

    Нужно потому, что после неудачной попытки в поле остаётся её текст, и
    следующая попытка дописывает свой в конец. На снимке заказчицы так и
    вышло: в поле лежал хвост прошлого захода, а под ним – весь пост заново.
    Ни одной клавиши, которая может отправить: только выделение и Backspace.
    """
    for attempt in range(3):
        try:
            _editor_node(page, sel).click(timeout=5_000)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
        except Exception:  # noqa: BLE001
            pass
        if not _editor_text(page, sel).strip():
            return True
        # Второй заход – средствами самой страницы: бывает, что выделение
        # ушло мимо поля (всплывшая подсказка перехватила фокус).
        try:
            _editor_node(page, sel).evaluate(
                "el => { el.focus();"
                " const s = window.getSelection(); const r = document.createRange();"
                " r.selectNodeContents(el); s.removeAllRanges(); s.addRange(r);"
                " document.execCommand('delete'); }")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(300)
    return not _editor_text(page, sel).strip()


def _lines_in(got: str) -> int:
    """Сколько непустых строк видно в поле."""
    return len([ln for ln in (got or "").split("\n") if ln.strip()])


# Миниатюры прикреплённых файлов. Веб-приложения показывают выбранный файл
# из памяти браузера, а такая картинка всегда живёт по адресу blob: или
# data: – в отличие от аватарок и значков, которые приходят с сервера.
# Считаем их до и после выбора файла: выросло – фото в форме, не выросло –
# МАКС его не принял.
_THUMBS_JS = r"""
() => {
  let n = 0;
  for (const el of document.querySelectorAll('img, video, canvas')) {
    const src = el.getAttribute('src') || el.currentSrc || '';
    if (!/^(blob:|data:)/.test(src)) continue;
    const r = el.getBoundingClientRect();
    const w = r.width || el.offsetWidth, h = r.height || el.offsetHeight;
    if (w >= 24 && h >= 24) n++;
  }
  return n;
}
"""

THUMB_WAIT_S = 20               # столько ждём миниатюру: файл ещё грузится


def _thumbs(page) -> int:
    """Сколько миниатюр прикреплённых файлов сейчас в форме."""
    try:
        return int(page.evaluate(_THUMBS_JS) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _wait_thumbs(page, before: int, want: int) -> bool:
    """Дождаться, пока миниатюры появятся. False – так и не появились."""
    deadline = time.time() + THUMB_WAIT_S
    while time.time() < deadline:
        page.wait_for_timeout(600)
        if _thumbs(page) - before >= want:
            return True
    return _thumbs(page) > before


def _fill_post_text(page, sel: str, text: str, log: Callable[[str], None]) -> str:
    """
    Вписать текст в поле МАКС, НЕ отправив ни одного сообщения.
    Возвращает '' при успехе, иначе причину отказа словами.

    Здесь случилась худшая поломка Click (13.08.2026): пост из двенадцати
    строк ушёл в канал двенадцатью отдельными сообщениями, сразу и без
    всякой отложки. Причина простая и обидная – текст печатался знак за
    знаком (page.type), а поле поста в МАКС это поле ЧАТА: каждый перевод
    строки в нём Enter, то есть «отправить».

    Отсюда главное правило: НИ ОДНОЙ КЛАВИШИ, которая может отправить.
    Весь текст, вместе с переносами строк, вставляется одним вызовом
    insert_text – это событие ввода, а не нажатие: события keydown не
    возникает вовсе, и отправить оно не может физически. Проверено на поле,
    устроенном как у МАКС (tests_crosspost).

    Shift+Enter остался только запасным путём – на случай, если сборка МАКС
    перестанет принимать переносы в insert_text. И проверяется он теперь по
    КАНАЛУ, а не по полю: «в поле пусто» – это ещё не «ушло сообщение».
    Прежняя проверка смотрела только в поле, читала пустышку (у селектора
    несколько узлов, Lexical перерисовывает разметку) и объявляла отправку,
    которой не было. Заказчица это и увидела: «в канале ничего нет, а Click
    пишет, что отправил».

    Растущее поле и полоса прокрутки – это нормально и никак не проверяется:
    в поле чата так и должно быть.
    """
    lines = text.split("\n")
    filled = [ln for ln in lines if _probe(ln)]
    # Что в канале ДО ввода. Всё, что было тут раньше – вчерашний пост,
    # позавчерашний, – в счёт «мы отправили» не пойдёт.
    before = _feed_letters(page, sel)

    if not _clear_editor(page, sel):
        return ("Поле поста в МАКС не очистилось – в нём остался текст прошлой "
                "попытки. Ничего не вводим: иначе пост уйдёт склеенным. "
                "Откройте канал и очистите поле руками")

    # ── Основной путь: один вызов, ни одной клавиши ──────────────────
    page.keyboard.insert_text(text)
    page.wait_for_timeout(400)
    got = _editor_text(page, sel)
    enough = _lines_in(got) >= len(filled)

    # ── Запасной путь: построчно, с проверкой по каналу ──────────────
    if not enough:
        log("  МАКС не принял переносы одним куском – ввожу построчно")
        if not _clear_editor(page, sel):
            return ("Поле поста в МАКС не очистилось перед вводом построчно. "
                    "Ничего не отправляли, проверьте канал")
        for i, line in enumerate(lines):
            if i:
                page.keyboard.press("Shift+Enter")
                # Единственное, чего тут стоит бояться: что перенос оказался
                # отправкой. Спрашиваем об этом КАНАЛ, а не поле.
                gone = _sent_lines(page, sel, before, lines[:i])
                if gone:
                    return ("МАКС отправил строку вместо переноса: в канале "
                            f"появилось «{gone[0][:40]}». Остановились на первой "
                            "строке – удалите это сообщение в канале и напишите "
                            "нам, поле ввода у МАКС изменилось")
            if line:
                page.keyboard.insert_text(line)
        page.wait_for_timeout(400)
        got = _editor_text(page, sel)

    # ── Сверка по краям: в поле должны быть и начало, и конец ────────
    seen = _letters(got)
    for what, line in (("начало", filled[0] if filled else ""),
                       ("конец", filled[-1] if filled else "")):
        if line and _probe(line) not in seen:
            gone = _sent_lines(page, sel, before, lines)
            if gone:
                return (f"Часть поста ушла в канал сообщениями ({len(gone)} шт.): "
                        f"первое – «{gone[0][:40]}». Отложку не ставим, "
                        "удалите отправленное в канале")
            return (f"В поле МАКС не видно {what} текста, но в канал ничего не "
                    "уходило. Ничего не планируем – попробуйте ещё раз")

    # Ничего не ушло? Это последняя проверка перед отложкой – и главная.
    gone = _sent_lines(page, sel, before, lines)
    if gone:
        return (f"Пока вводили текст, в канал ушло сообщение «{gone[0][:40]}». "
                "Отложку не ставим – удалите его в канале")
    log(f"  текст вписан целиком ({_lines_in(got)} строк), в канал ничего не ушло")
    return ""


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
            page.goto(chat_url, wait_until="domcontentloaded", timeout=60_000)

            text_sel = _wait_for_editor(page, log)
            if not text_sel:
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "no-editor"),
                        "error": _why_no_editor(page)}

            log(f"Ввожу текст ({len(text)} знаков)")
            why = _fill_post_text(page, text_sel, text, log)
            if why:
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "bad-text"),
                        "error": why}
            page.wait_for_timeout(1_000)
            # Карточка сайта по ссылке из текста – убираем крестиком, как руками.
            yb.drop_link_card(page, yb.text_domains(text), log)

            if image_paths:
                log(f"Прикрепляю фото: {len(image_paths)}")
                had = _thumbs(page)
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

                # Файл ПЕРЕДАН – это ещё не «фото в посте». 14.08.2026 МАКС
                # поставил отложку без картинки, а Click отчитался успехом:
                # после set_files он просто ждал три с половиной секунды и
                # шёл дальше. Пост без картинки – это не тот пост, который
                # просили, поэтому теперь ждём миниатюру в самой форме и без
                # неё отложку не ставим.
                if not _wait_thumbs(page, had, len(image_paths)):
                    return {"ok": False,
                            "shot": _debug_shot(project_id, page, "no-thumb"),
                            "error": ("Файл фото передали, но в поле поста МАКС "
                                      "миниатюра так и не появилась. Отложку не "
                                      "ставим: пост без картинки – не тот пост. "
                                      "Проверьте канал и попробуйте ещё раз")}

            # ПРАВОЙ кнопкой: левая отправит пост сейчас же. Это главное
            # место всей механики, и ошибиться тут нельзя.
            log("Открываю «Запланировать пост» (правой кнопкой по отправке)")
            send_sel = _first_visible(page, SEL["send"])
            if not send_sel:
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "no-send"),
                        "error": "Не нашли кнопку отправки в МАКС"}
            # Меню тоже может опоздать – в облаке всё медленнее. Пробуем
            # правую кнопку трижды, между попытками ждём.
            opened = ""
            for _ in range(3):
                # Сначала закрываем всё, что могло всплыть (подсказка, меню
                # смайлов): их подложка ловит клик вместо кнопки, и попытка
                # уходит в тридцатисекундный таймаут – так и было 13.08.2026
                # («backdrop … intercepts pointer events»).
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                except Exception:  # noqa: BLE001
                    pass
                page.locator(send_sel).first.click(button="right")
                page.wait_for_timeout(1_500)
                opened = _click_first(page, SEL["schedule_item"], timeout=6_000)
                if opened:
                    break
                page.wait_for_timeout(1_500)
            if not opened:
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "no-schedule-item"),
                        "error": "Меню «Запланировать пост» не появилось. Пост НЕ "
                                 "отправлен – левой кнопкой мы не жали"}
            page.wait_for_timeout(1_200)

            log(f"Ставлю {when.strftime('%d.%m.%Y %H:%M')} (Екатеринбург)")
            picked, why = _pick_day(page, when, log)
            if not picked:
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "no-day"),
                        "error": (f"Не смогли выбрать {when.strftime('%d.%m.%Y')} "
                                  f"в календаре МАКС: {why}. Пост НЕ отправлен")}
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

            # Последняя сверка перед нажатием: МАКС сам пишет на кнопке, на
            # какое время он собрался. Разошлись – не жмём вовсе, иначе пост
            # уйдёт не тогда, когда нужно.
            cap = confirm_caption(page)
            if cap:
                log(f"МАКС пишет на кнопке: «{cap}»")
            if not caption_ok(cap, when):
                return {"ok": False,
                        "shot": _debug_shot(project_id, page, "wrong-time"),
                        "error": (f"МАКС понял отложку иначе: на кнопке «{cap}», "
                                  f"а нужно {when.strftime('%d.%m %H:%M')}. "
                                  "Ничего не отправили")}

            log("Подтверждаю отложку")
            if not _click_confirm(page, cap):
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
