"""
gis_playwright.py – 2ГИС: вход, отзывы, актуализация.

Второй брат `yb_playwright.py`. Всё низкоуровневое – выбор движка, запуск
браузера, сохранение сессии, лог – берём оттуда же: браузер один и тот же,
второй копии ему не надо. Здесь только то, чем 2ГИС отличается от Яндекса.

А отличается он сильно.

**Готового состояния страницы нет.** У Яндекса раздел отзывов отдаёт весь
список объектом `window.__PRELOAD_DATA` – бери и читай. Кабинет 2ГИС –
React-приложение: в HTML приезжает пустой `<div id="root">`, всё остальное
дорисовывает скрипт, а данные забирает отдельным запросом (`…/comments`).
Поэтому здесь мы разбираем карточки на странице – как человек глазами.

**Классы бесполезны наполовину.** `aYDODrXf`, `XtmNsYE0` – случайные, при
следующей сборке 2ГИС станут другими. Но у общих элементов имя осмысленное с
хвостом: `rating__front-5nKiy`, `Stars__star-2aLep`, `button__basic-1agAe`.
Имя переживает пересборку, хвост меняется – ищем по вхождению имени, а
карточку находим от звёзд вверх, по составу, а не по названию.

**Фильтр «Без ответа» – это адрес.** `…/reviews?withoutAnswer=true` открывает
ровно те отзывы, что нам нужны. Кнопку жать не надо, и список заведомо не
содержит уже отвеченных – меньше поводов ошибиться.

**Отвечать можно не на всё.** Кабинет показывает отзывы ещё и с Flamp,
Otello, Booking, НетМонет, Т-Банка, СберЧаевых. У них вместо кнопки
«Ответить» стоит ссылка «Посмотреть на …». Такие отзывы мы собираем и
показываем человеку со ссылкой, но черновиков не пишем: отправить их отсюда
всё равно нельзя.

Проверяется всё это на `tests_fixtures/gis-reviews-page.html` – настоящей
странице из браузера заказчика.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Page

import paths
import yb_playwright as yb

# Метка сборки – одна на всё приложение, лежит в build.py.
from build import BUILD  # noqa: F401

USERS_DATA = paths.data_root()

# Лог общий с Яндексом: прогон ставит логгер один раз через yb.set_logger,
# и строки 2ГИС попадают туда же, куда всё остальное.
info, warn, error = yb.info, yb.warn, yb.error

ACCOUNT_HOST = "https://account.2gis.com"
LOGIN_URL = f"{ACCOUNT_HOST}/login"

# Площадки, отзывы которых кабинет показывает вместе со своими. Отвечать
# через 2ГИС можно только на «2GIS»; остальные – к человеку.
OWN_PLATFORM = "2gis"
KNOWN_PLATFORMS = ("2GIS", "Flamp", "Otello", "Booking", "НетМонет", "Т-Банк", "СберЧаевые")

REVIEWS_WAIT_MS = 20_000        # React рисует список не мгновенно
ANSWER_TEXT_LIMIT = 2000        # столько разрешает поле ответа в кабинете


# ════════════════════════════════════════════════════════════════════
#  Адреса
# ════════════════════════════════════════════════════════════════════

_ORG_RX = re.compile(r"account\.2gis\.[a-z.]+/orgs/(\d+)", re.I)
_DIGITS_RX = re.compile(r"^\d{6,}$")


def extract_org_id(url: str | None) -> str | None:
    """
    Номер организации из ссылки КП («Аккаунт» в блоке 2ГИС).

    В таблице лежит `https://account.2gis.com/orgs/70000001077855513/` – иногда
    с хвостом `/dashboard`, иногда без. Номер филиала из колонки «Карта»
    (`2gis.ru/moscow/firm/…`) – ДРУГОЙ, для кабинета он не годится.
    """
    if not url:
        return None
    m = _ORG_RX.search(url)
    if m:
        return m.group(1)
    s = url.strip()
    return s if _DIGITS_RX.fullmatch(s) else None


def build_reviews_url(url: str | None, only_unanswered: bool = True) -> str | None:
    """Раздел «Отзывы» организации. По умолчанию – сразу с фильтром «Без ответа»."""
    oid = extract_org_id(url)
    if not oid:
        return None
    tail = "?withoutAnswer=true" if only_unanswered else ""
    return f"{ACCOUNT_HOST}/orgs/{oid}/reviews{tail}"


def build_company_url(url: str | None) -> str | None:
    """Раздел «Данные о компании» – там живёт кнопка «Данные верны»."""
    oid = extract_org_id(url)
    return f"{ACCOUNT_HOST}/orgs/{oid}/company" if oid else None


# ════════════════════════════════════════════════════════════════════
#  Сессия и браузер
# ════════════════════════════════════════════════════════════════════

# Куки, по которым 2ГИС узнаёт вошедшего. Имена взяты не на глаз: кабинет
# сам называет их в window.APP_CONFIG (cookie.accessToken / refreshToken).
AUTH_COOKIES = {"dg_session_token", "dg_refresh_token"}


def session_path(project_id: str) -> Path:
    d = USERS_DATA / project_id / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d / "gis_storage_state.json"


def has_saved_session(project_id: str) -> bool:
    """Есть ли НАСТОЯЩАЯ сессия 2ГИС, а не просто файл с какими-то куками."""
    path = session_path(project_id)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return any(
        (c.get("name") or "") in AUTH_COOKIES and "2gis" in (c.get("domain") or "").lower()
        for c in data.get("cookies") or []
    )


def browser(project_id: str, headless: bool = True) -> yb.YbBrowser:
    """Тот же браузер, что у Яндекса, только с сессией 2ГИС."""
    return yb.YbBrowser(project_id, headless=headless, session_file=session_path(project_id))


# ════════════════════════════════════════════════════════════════════
#  Чтение отзывов
# ════════════════════════════════════════════════════════════════════
#
# Карточку ищем от звёзд вверх: поднимаемся от жёлтого ряда, пока не найдём
# первый блок, в котором есть и оценка, и текст, и кнопка «Ответить» (или
# ссылка на чужую площадку). Это и есть карточка.
#
# Так надёжнее, чем «взять div с классом aYDODrXf»: класс сменится с ближайшей
# сборкой 2ГИС, а состав карточки – нет. И надёжнее, чем «подниматься, пока
# оценка в предке одна»: когда неотвеченный отзыв на странице один, такой
# подъём дошёл бы до <body> и принял за отзыв всю страницу.

_READ_REVIEWS_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const DATE_RX = /^\d{1,2}\s+(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\S*\s+\d{4}$/i;
  const PLATFORMS = ['2GIS', 'Flamp', 'Otello', 'Booking', 'НетМонет', 'Т-Банк', 'СберЧаевые'];
  const ANSWER_RX = /^ответить$/i;

  const rect = el => el.getBoundingClientRect();
  const textOf = el => norm(el.textContent);

  // Листья: элементы без вложенных элементов – в них и лежит весь текст.
  const leaves = root => Array.from(root.querySelectorAll('*'))
    .filter(el => el.children.length === 0 && norm(el.textContent));

  const answerButton = card => Array.from(
      card.querySelectorAll('button, [role="button"], [class*="button__basic"]'))
    .find(b => ANSWER_RX.test(textOf(b))) || null;

  const foreignLink = card => Array.from(card.querySelectorAll('a'))
    .find(a => /посмотреть\s+на\s/i.test(textOf(a))) || null;

  // Карточка = первый предок звёзд, где есть и текст, и действие.
  const cardOf = front => {
    let el = front;
    for (let i = 0; i < 12 && el && el.parentElement; i++) {
      el = el.parentElement;
      if (el.querySelectorAll('[class*="rating__front"]').length > 1) return null;
      if (answerButton(el) || foreignLink(el)) return el;
    }
    return null;
  };

  const items = [];
  let skipped = 0;
  const seen = new Set();

  for (const front of document.querySelectorAll('[class*="rating__front"]')) {
    const card = cardOf(front);
    if (!card || seen.has(card)) { skipped++; continue; }
    seen.add(card);

    // Оценка: жёлтый ряд обрезан по ширине, серый – всегда полный.
    const back = card.querySelector('[class*="rating__back"]');
    const full = back ? rect(back).width : 0;
    const filled = rect(front).width;
    let rating = 0;
    if (full > 0 && filled >= 0) rating = Math.round(5 * filled / full);
    if (!rating) {
      // Запасной путь: считаем звёзды, если ширины почему-то нулевые.
      rating = front.querySelectorAll('[class*="Stars__star"]').length;
    }
    rating = Math.max(0, Math.min(5, rating));

    // Площадка отзыва – подпись рядом с именем автора. Берём самый глубокий
    // элемент с таким текстом: внутри подписи лежит ещё и значок площадки
    // (svg с потрохами), поэтому «элемент без детей» тут не подходит.
    let badge = null, platform = '';
    for (const el of card.querySelectorAll('*')) {
      const t = textOf(el);
      if (!PLATFORMS.includes(t)) continue;
      if (Array.from(el.querySelectorAll('*')).some(c => textOf(c) === t)) continue;
      badge = el; platform = t; break;
    }

    const all = leaves(card);
    const dateEl = all.find(el => DATE_RX.test(textOf(el)));
    const dateText = dateEl ? textOf(dateEl) : '';

    // Автор – соседняя с площадкой подпись; если площадки нет, первый
    // короткий листок до даты.
    let author = '';
    if (badge && badge.parentElement) {
      for (const sib of badge.parentElement.children) {
        if (sib === badge || sib.contains(badge)) continue;
        const t = textOf(sib);
        if (t) { author = t; break; }
      }
    }
    if (!author) {
      for (const el of all) {
        const t = textOf(el);
        if (!t || el === dateEl || PLATFORMS.includes(t)) continue;
        if (t.length <= 60 && !/^\d+$/.test(t)) { author = t; break; }
      }
    }

    // Текст отзыва – самый длинный листок, не считая служебных: ссылок
    // (там адрес филиала), кнопок, счётчиков, даты и имени.
    let text = '';
    for (const el of all) {
      if (el.closest('a, button, svg, [role="button"]')) continue;
      const t = textOf(el);
      if (!t || el === dateEl || el === badge) continue;
      if (t === author || PLATFORMS.includes(t) || /^\d+$/.test(t)) continue;
      if (t.length > text.length) text = t;
    }

    const branch = card.querySelector('a[href*="/reviews/"]');
    const foreign = foreignLink(card);
    items.push({
      author, rating, text, dateText,
      platform: platform || (foreign ? norm(foreign.textContent).replace(/^посмотреть\s+на\s+/i, '') : ''),
      canAnswer: !!answerButton(card),
      foreignUrl: foreign ? foreign.href : '',
      branchUrl: branch ? branch.href : '',
      branchName: branch ? textOf(branch) : '',
    });
  }
  return { items, skipped };
}
"""


_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7,
           "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}


def parse_date(text: str) -> int:
    """«5 июля 2024» → миллисекунды. Не разобрали – 0, это не ошибка."""
    m = re.match(r"^\s*(\d{1,2})\s+([А-Яа-яЁё]+)\s+(\d{4})", text or "")
    if not m:
        return 0
    day, word, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = next((n for stem, n in _MONTHS.items() if word.startswith(stem)), 0)
    if not month:
        return 0
    try:
        return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return 0


def review_id(org_id: str | None, item: dict) -> str:
    """
    Свой номер отзыва: 2ГИС его на странице не показывает.

    Считаем от того, что не меняется – организация, автор, дата, текст.
    Нужен, чтобы очередь на подтверждение не плодила дубли при повторном
    прогоне по тому же городу.
    """
    raw = "|".join([str(org_id or ""), item.get("author") or "",
                    item.get("dateText") or "", (item.get("text") or "")[:200]])
    return "gis-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _abs(url: str) -> str:
    """Ссылка филиала приходит относительной – доводим до полной."""
    u = (url or "").strip()
    if not u or u.startswith("http"):
        return u
    return ACCOUNT_HOST + ("" if u.startswith("/") else "/") + u


def normalize(item: dict, org_id: str | None = None) -> dict:
    """Карточка со страницы – к тому же плоскому виду, что у Яндекса."""
    return {
        "id": review_id(org_id, item),
        "author": item.get("author") or "",
        "rating": int(item.get("rating") or 0),
        "text": (item.get("text") or "").strip(),
        "time_created": parse_date(item.get("dateText") or ""),
        # Список открыт с фильтром «Без ответа»: всё, что здесь, – без ответа.
        "answered": False,
        "platform": item.get("platform") or "",
        "canAnswer": bool(item.get("canAnswer")),
        "foreignUrl": item.get("foreignUrl") or "",
        "branchUrl": _abs(item.get("branchUrl") or ""),
        "branchName": item.get("branchName") or "",
    }


def is_own(item: dict) -> bool:
    """Отзыв самого 2ГИС – на такой мы можем ответить из кабинета."""
    return bool(item.get("canAnswer")) and (item.get("platform") or OWN_PLATFORM).lower() == OWN_PLATFORM


def _reviews_on_page(page: Page, org_id: str | None = None) -> dict | None:
    """Отзывы с УЖЕ открытой страницы. None – список ещё не дорисован."""
    try:
        data = page.evaluate(_READ_REVIEWS_JS)
    except Exception:  # noqa: BLE001 – React перерисовывает, попробуем ещё раз
        return None
    if not data or data.get("items") is None:
        return None
    return {"items": [normalize(it, org_id) for it in data["items"]],
            "skipped": int(data.get("skipped") or 0)}


# Пустой список и «список ещё не нарисован» – разные вещи. Отличаем по
# признакам готовой страницы: заголовок «Отзывы» и кнопка «Без ответа».
_PAGE_READY_JS = r"""
() => {
  const t = (document.body.innerText || '');
  return /без\s+ответа/i.test(t) || /отзыв/i.test(t);
}
"""

_EMPTY_LIST_JS = r"""
() => /(нет\s+отзывов|отзывов\s+нет|ничего\s+не\s+найдено|пока\s+нет)/i.test(document.body.innerText || '')
"""


def read_reviews(page: Page, url: str, navigate: bool = True,
                 org_id: str | None = None) -> dict:
    """
    Открыть «Отзывы → Без ответа» и вернуть {'ok', 'items', 'shown', 'reason', 'url'}.

    navigate=False – страница уже открыта. На отправке пачки ответов это
    главное: один заход на город вместо перезагрузки под каждый ответ.
    """
    out = {"ok": False, "items": [], "shown": 0, "skipped": 0, "reason": "", "url": url}
    if not url:
        out["reason"] = "Не удалось определить адрес раздела «Отзывы» (нет ссылки на кабинет 2ГИС)"
        return out

    if navigate:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40_000)
        except Exception as e:  # noqa: BLE001
            out["reason"] = f"Не удалось открыть страницу отзывов: {yb._short_error(e)}"
            return out
        if looks_like_login_page(page):
            out["reason"] = "2ГИС увёл на страницу входа – сессия не действует"
            # Прогону это знак остановиться, а не идти дальше: остальные города
            # упрутся в ту же страницу входа. У Яндекса ровно так же.
            out["noSession"] = True
            return out

    data, ready = None, False
    deadline = time.time() + (REVIEWS_WAIT_MS / 1000 if navigate else 4)
    while time.time() < deadline:
        data = _reviews_on_page(page, org_id)
        if data and data["items"]:
            break
        try:
            ready = bool(page.evaluate(_PAGE_READY_JS)) and bool(page.evaluate(_EMPTY_LIST_JS))
        except Exception:  # noqa: BLE001
            ready = False
        if ready:
            data = data or {"items": [], "skipped": 0}
            break
        page.wait_for_timeout(500)

    if data is None:
        out["reason"] = ("Страница отзывов не открылась – кабинет 2ГИС не отдал список. "
                         "Проверьте вход в 2ГИС в «Настройках»")
        return out

    out.update(ok=True, items=data["items"], shown=len(data["items"]),
               skipped=data.get("skipped", 0))
    return out


def looks_like_login_page(page: Page) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if "/login" in url or "/signin" in url:
        return True
    try:
        return bool(page.evaluate(
            "() => !!document.querySelector('input[type=password]') && "
            "!document.querySelector('a[href*=\"/orgs/\"]')"))
    except Exception:  # noqa: BLE001
        return False


# ════════════════════════════════════════════════════════════════════
#  Проверки: тот ли аккаунт и тот ли город
# ════════════════════════════════════════════════════════════════════

_HEADER_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const body = norm(document.body.innerText);
  const mail = body.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
  return { email: mail ? mail[0] : '', text: body.slice(0, 400) };
}
"""


def account_email(page: Page) -> str:
    """Почта, под которой сидим: кабинет пишет её в правом верхнем углу."""
    try:
        return (page.evaluate(_HEADER_JS) or {}).get("email") or ""
    except Exception:  # noqa: BLE001
        return ""


def verify_account(page: Page, expected_email: str) -> dict:
    """
    Тот ли аккаунт открыт. Публиковать ответы от чужого имени нельзя, а
    один браузер на все проекты – ровно тот случай, когда это возможно.
    """
    got = account_email(page)
    if not expected_email:
        return {"ok": True, "email": got, "reason": ""}
    ok = got.strip().lower() == expected_email.strip().lower()
    return {"ok": ok or not got, "email": got,
            "reason": "" if ok or not got else
            f"В 2ГИС открыт аккаунт {got}, а проект ждёт {expected_email}"}


# Город организации кабинет пишет под её названием в шапке («Стальметурал /
# Краснодар»). Сверяем с городом из КП: ответ не тому филиалу не отменить.
_ORG_CITY_JS = r"""
(payload) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(payload.city);
  if (!want) return true;
  const head = norm((document.body.innerText || '').slice(0, 600));
  return head.includes(want);
}
"""


def city_matches(page: Page, city: str) -> bool:
    try:
        return bool(page.evaluate(_ORG_CITY_JS, {"city": city or ""}))
    except Exception:  # noqa: BLE001
        return True          # не смогли проверить – не мешаем работе


# ════════════════════════════════════════════════════════════════════
#  Ответ на отзыв
# ════════════════════════════════════════════════════════════════════
#
# Порядок ровно тот, что человек делает руками: найти карточку нужного
# отзыва → «Ответить» → вписать текст → «Опубликовать» → дождаться, что
# ответ реально появился.

_FIND_CARD_JS = r"""
(payload) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const needle = norm(payload.text).slice(0, 60);
  if (!needle) return null;

  document.querySelectorAll('[data-click-card]').forEach(n => n.removeAttribute('data-click-card'));

  // Самый узкий блок, в котором есть и текст отзыва, и кнопка «Ответить»
  // (или уже раскрытое поле ответа).
  let best = null, bestLen = Infinity;
  for (const el of document.querySelectorAll('div, li, article, section')) {
    const t = norm(el.innerText);
    if (!t.includes(needle)) continue;
    const act = Array.from(el.querySelectorAll('button, [role="button"], [class*="button__basic"]'))
      .some(b => /^(ответить|опубликовать)$/i.test(norm(b.textContent)));
    if (!act && !el.querySelector('textarea')) continue;
    if (t.length < bestLen) { best = el; bestLen = t.length; }
  }
  if (!best) return null;
  best.setAttribute('data-click-card', '1');
  return { length: bestLen };
}
"""

_CARD_BUTTON_JS = r"""
(payload) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const card = document.querySelector('[data-click-card="1"]');
  if (!card) return null;
  document.querySelectorAll('[data-click-btn]').forEach(n => n.removeAttribute('data-click-btn'));
  const rx = new RegExp(payload.rx, 'i');
  for (const b of card.querySelectorAll('button, [role="button"], [class*="button__basic"]')) {
    if (!rx.test(norm(b.textContent))) continue;
    const r = b.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
    b.setAttribute('data-click-btn', '1');
    return { text: norm(b.textContent).slice(0, 40) };
  }
  return null;
}
"""

_CARD_FIELD_JS = r"""
() => {
  const card = document.querySelector('[data-click-card="1"]');
  if (!card) return null;
  document.querySelectorAll('[data-click-answer]').forEach(n => n.removeAttribute('data-click-answer'));
  const field = card.querySelector('textarea, [contenteditable="true"]');
  if (!field) return null;
  field.setAttribute('data-click-answer', '1');
  return { tag: field.tagName.toLowerCase() };
}
"""

# Зелёная плашка слева внизу – первый признак, что ответ ушёл.
_ANSWER_TOAST_JS = r"""
() => /ответ\s+на\s+отзыв\s+размещ[её]н|ответ\s+(отправлен|опубликован|добавлен)/i
       .test(document.body.innerText || '')
"""

# Ответ встал под отзывом: в карточке появился наш текст, и он уже не в поле
# ввода. Проверяем по живой странице – перезагружать тяжёлую SPA незачем.
_ANSWER_SHOWN_JS = r"""
(payload) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const needle = norm(payload.review).slice(0, 60);
  const mark = norm(payload.answer).slice(0, 40);
  if (!needle || !mark) return false;
  for (const el of document.querySelectorAll('div, li, article, section')) {
    const t = norm(el.innerText);
    if (!t.includes(needle) || !t.includes(mark)) continue;
    const field = el.querySelector('textarea, [contenteditable="true"]');
    if (!field || !norm(field.value || field.innerText).includes(mark)) return true;
  }
  return false;
}
"""


def publish_review_answer(page: Page, url: str, review_text: str, text: str,
                          navigate: bool = True) -> dict:
    """
    Опубликовать подтверждённый человеком ответ на отзыв 2ГИС.

    Отзыв опознаём по его тексту: своего номера 2ГИС на странице не
    показывает, а текст в пределах карточки уникален.

    'answered' возвращаем, только когда ответ реально виден. «Кнопку нажали,
    подтверждения нет» – это не успех: отзыв ушёл бы из очереди, а в 2ГИС
    ничего бы не появилось.
    """
    body = (text or "").strip()
    if not body:
        return {"status": "failed", "reason": "Пустой текст ответа"}
    if len(body) > ANSWER_TEXT_LIMIT:
        return {"status": "failed",
                "reason": f"Ответ длиннее {ANSWER_TEXT_LIMIT} знаков – 2ГИС столько не примет"}
    if not (review_text or "").strip():
        return {"status": "failed", "reason": "Нечем опознать отзыв на странице"}

    if navigate:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40_000)
        except Exception as e:  # noqa: BLE001
            return {"status": "failed", "reason": f"Не удалось открыть отзывы: {yb._short_error(e)}"}
        if looks_like_login_page(page):
            return {"status": "failed", "reason": "2ГИС увёл на страницу входа – сессия не действует"}

    # Карточку ждём: список дорисовывается скриптом.
    found = None
    deadline = time.time() + REVIEWS_WAIT_MS / 1000
    while time.time() < deadline:
        try:
            found = page.evaluate(_FIND_CARD_JS, {"text": review_text})
        except Exception:  # noqa: BLE001
            found = None
        if found:
            break
        page.wait_for_timeout(500)

    if not found:
        # Отзыва на странице нет. Мы открывали список «Без ответа» – значит,
        # на него успели ответить без нас. Второго ответа не будет.
        return {"status": "already",
                "reason": "Отзыва нет в списке «Без ответа» – на него уже ответили. Публикацию отменили"}

    # «Ответить» раскрывает поле. Если поле уже открыто – кнопки нет, и это норма.
    if _mark_button(page, "^ответить$"):
        try:
            page.locator('[data-click-btn="1"]').first.click(timeout=8_000)
        except Exception as e:  # noqa: BLE001
            return {"status": "failed", "reason": f"Не нажалась кнопка «Ответить»: {yb._short_error(e)}"}
        page.wait_for_timeout(600)

    field = None
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            field = page.evaluate(_CARD_FIELD_JS)
        except Exception:  # noqa: BLE001
            field = None
        if field:
            break
        page.wait_for_timeout(400)
    if not field:
        return {"status": "failed", "reason": "Не нашли поле ответа у этого отзыва"}

    target = page.locator('[data-click-answer="1"]').first
    try:
        target.scroll_into_view_if_needed(timeout=3_000)
    except Exception:  # noqa: BLE001
        pass
    try:
        target.click(timeout=8_000)
        if field.get("tag") == "textarea":
            target.fill(body, timeout=8_000)
        else:
            page.keyboard.insert_text(body)
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "reason": f"Не удалось вписать ответ: {yb._short_error(e)}"}

    # «Опубликовать» становится активной только с текстом в поле.
    label = None
    deadline = time.time() + 8
    while time.time() < deadline:
        label = _mark_button(page, "^опубликовать$")
        if label:
            break
        page.wait_for_timeout(400)
    if not label:
        return {"status": "failed",
                "reason": "Текст вписали, но кнопка «Опубликовать» не стала активной"}

    try:
        page.locator('[data-click-btn="1"]').first.click(timeout=8_000)
        info("  🔘 Ответ отправлен кнопкой «Опубликовать»")
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "reason": f"Кнопка «Опубликовать» не нажалась: {yb._short_error(e)}"}

    deadline = time.time() + 15
    toast = False
    while time.time() < deadline:
        try:
            if page.evaluate(_ANSWER_SHOWN_JS, {"review": review_text, "answer": body}):
                return {"status": "answered", "reason": "Ответ опубликован – 2ГИС его показывает"}
            toast = toast or bool(page.evaluate(_ANSWER_TOAST_JS))
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(700)

    if toast:
        return {"status": "answered", "reason": "Ответ опубликован (плашка 2ГИС подтвердила)"}
    return {"status": "failed",
            "reason": "Кнопку нажали, но ответ на карточке не появился. "
                      "Ответьте вручную по ссылке из списка"}


def _mark_button(page: Page, rx: str) -> dict | None:
    try:
        return page.evaluate(_CARD_BUTTON_JS, {"rx": rx})
    except Exception:  # noqa: BLE001
        return None


# ════════════════════════════════════════════════════════════════════
#  Актуализация
# ════════════════════════════════════════════════════════════════════
#
# На странице «Данные о компании» кабинет иногда показывает плашку «Данные о
# компании не обновлялись достаточно давно, они не изменились?» с кнопкой
# «Данные верны». Нажали – слева внизу всплывает «Спасибо за подтверждение
# данных». Плашки нет – значит, подтверждать нечего, и это не ошибка.

_ACTUALIZE_BTN_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-click-ok]').forEach(n => n.removeAttribute('data-click-ok'));
  // Надпись может быть и кнопкой, и ссылкой, и просто раскрашенным div –
  // берём самый глубокий элемент с этим текстом, чтобы не поймать всю плашку.
  const hits = [];
  for (const el of document.querySelectorAll('button, a, [role="button"], span, div')) {
    if (!/^данные\s+верны$/i.test(norm(el.textContent))) continue;
    if (Array.from(el.querySelectorAll('*')).some(
        c => /^данные\s+верны$/i.test(norm(c.textContent)))) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 8) continue;
    hits.push(el);
  }
  if (!hits.length) return null;
  const el = hits[0];
  const target = el.closest('button, a, [role="button"]') || el;
  target.setAttribute('data-click-ok', '1');
  return { text: norm(el.textContent).slice(0, 40) };
}
"""

_ACTUALIZE_TOAST_JS = r"""
() => /спасибо\s+за\s+подтверждение|данные\s+подтвержден/i.test(document.body.innerText || '')
"""


def actualize_city(page: Page, task: dict, idx: int = 0, total: int = 1) -> dict:
    """
    Статусы – те же, что у Яндекса, чтобы отчёт был общий:
      'actualized' – кнопку «Данные верны» нажали
      'not-needed' – плашки нет, подтверждать нечего (это НЕ ошибка)
      'failed'     – страница не открылась или клик не прошёл
    """
    started = time.time()
    label = f"[{idx + 1}/{total}] {task.get('cityName', '?')}"
    result = {"cityName": task.get("cityName"), "companyUrl": task.get("gisUrl") or task.get("companyUrl"),
              "status": "failed", "reason": "", "durationMs": 0, "platform": "gis"}

    def finish(status: str, reason: str) -> dict:
        result["status"] = status
        result["reason"] = reason
        result["durationMs"] = int((time.time() - started) * 1000)
        return result

    info(f"🏙  {label} – актуализация 2ГИС...")
    url = build_company_url(result["companyUrl"])
    if not url:
        return finish("failed", "Не удалось определить адрес раздела «Данные о компании»")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40_000)
        page.wait_for_timeout(2500)          # React рисует плашку не сразу
    except Exception as e:  # noqa: BLE001
        return finish("failed", f"Не удалось открыть страницу: {yb._short_error(e)}")

    if looks_like_login_page(page):
        return finish("no-session", "Сессия 2ГИС не активна: открылась страница входа")

    btn = None
    deadline = time.time() + 6
    while time.time() < deadline:
        btn = _mark_ok_button(page)
        if btn:
            break
        page.wait_for_timeout(500)

    if not btn:
        out = finish("not-needed", "Плашки «Данные верны» нет – подтверждать нечего")
        info(f"  ✓ {label}: {out['reason']} ({out['durationMs'] / 1000:.1f} сек)")
        return out

    try:
        page.locator('[data-click-ok="1"]').first.click(timeout=8_000)
    except Exception as e:  # noqa: BLE001
        return finish("failed", f"Ошибка клика «Данные верны»: {yb._short_error(e)}")

    toast = False
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            toast = bool(page.evaluate(_ACTUALIZE_TOAST_JS))
        except Exception:  # noqa: BLE001
            toast = False
        if toast:
            break
        page.wait_for_timeout(400)

    out = finish("actualized",
                 "Данные подтверждены (плашка ответила)" if toast
                 else "Клик прошёл (плашка не появилась, но кнопка нажата)")
    info(f"  {'✅' if toast else '🟡'} {label}: {out['reason']} ({out['durationMs'] / 1000:.1f} сек)")
    return out


def _mark_ok_button(page: Page) -> dict | None:
    try:
        return page.evaluate(_ACTUALIZE_BTN_JS)
    except Exception:  # noqa: BLE001
        return None


# ════════════════════════════════════════════════════════════════════
#  Вход
# ════════════════════════════════════════════════════════════════════
#
# Вход простой: почта, пароль, «Войти». Кода подтверждения 2ГИС при входе с
# сохранёнными куками устройства обычно не просит – но если попросит, шаг
# распознаётся и поле для кода в интерфейсе появится.


class GisLoginFlow:
    """Пошаговый вход в кабинет 2ГИС – со скриншотом вместо окна браузера."""

    def __init__(self, project_id: str, headless: bool = True):
        self.project_id = project_id
        self.headless = headless
        self._pw = None
        self.browser = None
        self.context = None
        self.page: Page | None = None

    # ─── жизненный цикл ─────────────────────────────────────────────
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
                locale=yb.LOCALE, extra_http_headers=yb.LANG_HEADERS)
            self.page = self.context.new_page()
            self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=40_000)
            self.page.wait_for_timeout(1500)
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
    def page_state(self) -> dict:
        """На каком экране мы сейчас: 'login' | 'code' | 'done' | 'unknown'."""
        try:
            info_ = self.page.evaluate(_LOGIN_STATE_JS)
        except Exception:  # noqa: BLE001
            return {"step": "unknown", "title": "", "url": ""}
        return info_

    def state(self) -> dict:
        st = self.page_state()
        st["url"] = self.page.url if self.page else ""
        st["screenshot"] = self.screenshot()
        return st

    def screenshot(self) -> bytes | None:
        try:
            return self.page.screenshot(type="png", full_page=False)
        except Exception:  # noqa: BLE001
            return None

    def submit_credentials(self, email: str, password: str) -> dict:
        """Вписать почту и пароль и нажать «Войти»."""
        page = self.page
        mail = page.locator('input[type="email"], input[name="email"], '
                            'input[type="text"]:not([type="password"])').first
        mail.click()
        try:
            mail.fill("")
        except Exception:  # noqa: BLE001
            pass
        mail.type(email, delay=30)
        pw = page.locator('input[type="password"]').first
        pw.click()
        pw.type(password, delay=30)
        page.wait_for_timeout(300)
        if not yb._click_exact_button(page, ["войти", "log in", "sign in"]):
            pw.press("Enter")
        page.wait_for_timeout(3500)
        return self.state()

    def submit_code(self, code: str) -> dict:
        page = self.page
        field = page.locator('input[type="text"], input[inputmode="numeric"], input:not([type])').first
        field.click()
        field.type(code, delay=40)
        page.wait_for_timeout(300)
        if not yb._click_exact_button(page, ["подтвердить", "войти", "далее", "продолжить"]):
            field.press("Enter")
        page.wait_for_timeout(3000)
        return self.state()


_LOGIN_STATE_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const text = norm(document.body.innerText);
  const title = norm((document.querySelector('h1, h2, h3') || {}).textContent || '');
  const url = location.href;
  if (/\/orgs\//.test(url) || document.querySelector('a[href*="/orgs/"]'))
    return { step: 'done', title, url };
  if (document.querySelector('input[type="password"]'))
    return { step: 'login', title, url };
  if (/код|code/i.test(text) && document.querySelector('input'))
    return { step: 'code', title, url };
  return { step: 'unknown', title, url };
}
"""
