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
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

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
    const dates = all.filter(el => DATE_RX.test(textOf(el)));
    const dateEl = dates[0] || null;
    const dateText = dateEl ? textOf(dateEl) : '';

    // Ответ компании уже стоит? Кнопка «Ответить» на такой карточке
    // ОСТАЁТСЯ (2ГИС разрешает ответить ещё раз), поэтому опознаём ответ по
    // двум надёжным приметам: у ответа своя дата – в карточке становится две –
    // и рядом с ним появляются «Удалить» и «Отображать как основной».
    const answerMark = Array.from(card.querySelectorAll('button, [role="button"], a, span, div'))
      .some(el => /^(удалить|отображать как основной|скрыть ответ)$/i.test(textOf(el)));
    const answered = answerMark || dates.length > 1;

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

    // Текст отзыва – самый длинный БЛОК, а не самый длинный листок.
    //
    // Листок не годится: длинный отзыв 2ГИС показывает свёрнутым (line="3"),
    // и текст в разметке разбит на несколько кусков. «Самый длинный листок»
    // брал один из них – в очередь попадал обрывок, начинавшийся с середины
    // слова: «адыков Адиль! То не берет телефон…». Берём элемент целиком:
    // его innerText склеивает куски обратно.
    //
    // Блок с ответом компании пропускаем: там текст длиннее самого отзыва,
    // и он бы победил.
    const answerBox = answerMark
      ? (Array.from(card.querySelectorAll('button, [role="button"], a, span, div'))
          .find(el => /^(удалить|отображать как основной)$/i.test(textOf(el))) || null)
      : null;
    const answerRoot = answerBox ? answerBox.closest('div, li, section') : null;

    // Текст берём ПО КУСКАМ из разметки, а не с экрана.
    //
    // Свёрнутый отзыв 2ГИС хранит так: видимая часть, отдельный узел с
    // многоточием и скрытый остаток. На экране (innerText) видно только
    // начало – «…Менеджер С…»; скрытый остаток при этом в разметке лежит.
    // Склеиваем куски ВПЛОТНУЮ, без пробела: свёртка режет строку прямо
    // посреди слова, и «Менеджер С» + «адыков Адиль» должны снова стать
    // «Менеджер Садыков Адиль». Многоточие свёртки выбрасываем.
    const whole = el => {
      const parts = [];
      const walk = n => {
        if (n.nodeType === 3) { parts.push(n.nodeValue || ''); return; }
        if (n.nodeType !== 1) return;
        const t = (n.textContent || '').trim();
        if (t === '…' || t === '...' || t === '..') return;
        for (const c of n.childNodes) walk(c);
      };
      walk(el);
      return norm(parts.join(''));
    };

    let text = '';
    for (const el of card.querySelectorAll('div, p, span')) {
      if (el.closest('a, button, svg, [role="button"]')) continue;
      // Обёртка вокруг кнопки – не текст. Без этого у отзыва из одних звёзд
      // «текстом» становилось слово «Ответить».
      if (el.querySelector('a, button, [role="button"]')) continue;
      if (answerRoot && (answerRoot.contains(el) || el.contains(answerRoot))) continue;
      // Обёртки, в которые попали имя, дата или оценка, – это не текст отзыва.
      if (dates.some(d => el.contains(d))) continue;
      if (badge && el.contains(badge)) continue;
      if (el.querySelector('[class*="rating__front"]')) continue;
      const t = whole(el);
      if (!t || t === author || PLATFORMS.includes(t) || /^\d+$/.test(t)) continue;
      if (t.length > text.length) text = t;
    }

    const branch = card.querySelector('a[href*="/reviews/"]');
    const foreign = foreignLink(card);
    items.push({
      author, rating, text, dateText, answered,
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
    # По НАЧАЛУ текста, а не по всему: свёрнутый отзыв читается то целиком,
    # то обрезанным, и от полного текста номер скакал бы – в очереди
    # появлялись бы двойники одного и того же отзыва.
    raw = "|".join([str(org_id or ""), item.get("author") or "",
                    item.get("dateText") or "", str(item.get("rating") or 0),
                    (item.get("text") or "")[:40]])
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
        # Смотрим на саму карточку, а не на фильтр в адресе. Фильтр «Без
        # ответа» кабинет применяет уже после отрисовки списка, и первым
        # заходом видно ВСЕ отзывы – в очередь попадали уже отвеченные.
        "answered": bool(item.get("answered")),
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

# Кабинет может написать «нет отзывов» по-разному, и угадывать формулировку –
# то же самое, что угадывать классы. Поэтому это подсказка, а не приговор:
# главный признак пустого списка – страница устоялась, а карточек нет.
_EMPTY_LIST_JS = r"""
() => /(нет\s+отзывов|отзывов\s+нет|ничего\s+не\s+найдено|пока\s+нет|все\s+отзывы)/i
       .test(document.body.innerText || '')
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

    # Ждём, пока список УСТОИТСЯ, а не первую попавшуюся отрисовку.
    #
    # Кабинет сначала рисует все отзывы и только потом применяет фильтр из
    # адреса. Первый заход хватал предварительный список – и в очередь
    # попадали отзывы, на которые давно ответили. Считаем список готовым,
    # когда два чтения подряд дают одно и то же число карточек.
    #
    # Пустой список узнаём так же – по устоявшейся странице, а не по словам
    # «нет отзывов». Формулировку кабинет волен сменить, и тогда города, где
    # всё отвечено, получали пугающее «кабинет не отдал список»: прогон ждал
    # двадцать секунд знакомой надписи, не дожидался и объявлял поломку.
    data, same, was = None, 0, -1
    deadline = time.time() + (REVIEWS_WAIT_MS / 1000 if navigate else 5)
    while time.time() < deadline:
        fresh = _reviews_on_page(page, org_id)
        count = len(fresh["items"]) if fresh else -1
        same = same + 1 if count == was and count >= 0 else 0
        was = count
        if fresh and fresh["items"]:
            data = fresh                    # пригодится, если время выйдет
            if same >= 1:
                break
        # Карточек нет, страница устоялась и раздел отзывов на месте – значит,
        # отвечать действительно нечего. Но если разметку карточек мы просто
        # не разобрали (skipped), пустотой это считать нельзя.
        if count == 0 and same >= 2 and not (fresh or {}).get("skipped") and _reviews_ready(page):
            data = {"items": [], "skipped": 0}
            break
        page.wait_for_timeout(700)

    if data is None:
        where = ""
        try:
            where = f" (открыт адрес {page.url})"
        except Exception:  # noqa: BLE001
            where = ""
        out["reason"] = ("Страница отзывов не открылась – кабинет 2ГИС не отдал список"
                         f"{where}. Проверьте вход в 2ГИС в «Настройках»")
        return out

    out.update(ok=True, items=data["items"], shown=len(data["items"]),
               skipped=data.get("skipped", 0))
    return out


def _reviews_ready(page: Page) -> bool:
    """Раздел отзывов на месте: либо кабинет сам сказал «пусто», либо виден список."""
    try:
        return bool(page.evaluate(_EMPTY_LIST_JS)) or bool(page.evaluate(_PAGE_READY_JS))
    except Exception:  # noqa: BLE001
        return False


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
#
# Здесь дважды была ОДНА И ТА ЖЕ ошибка, и оба раза прогон писал в отчёт
# «плашки нет» на все города подряд, хотя плашка висела на экране.
#
#   1. Готовность страницы определялась по словам «Данные о компании» в
#      тексте. Но так называется и пункт меню слева – он есть на ЛЮБОЙ
#      странице кабинета. Проверка была истинной всегда, прогон считал
#      страницу дорисованной через шесть секунд и уходил дальше. А плашку
#      кабинет забирает отдельным запросом и рисует позже – в облаке
#      заметно позже. Отсюда 6.5–9.3 секунды на город в логе.
#
#   2. Надпись искалась как элемент, ВЕСЬ текст которого равен «Данные
#      верны». В живом кабинете это обычный div без роли и без фокуса,
#      и надпись легко оказывается куском строки подлиннее («…они не
#      изменились? Данные верны») или, наоборот, разрезанной на узлы.
#      Тогда такого элемента не существует, и находить нечего.
#
# Лечим корень, а не симптом:
#   • готовность – это не «нашли слово», а «страница перестала меняться»;
#   • текст читаем по ВСЕМ корням (документ, теневые корни, свои кадры);
#   • ищем не элемент, а МЕСТО на экране, где нарисована фраза, и кликаем
#     в него мышью – ровно как человек;
#   • успех – только доказанный: всплыла плашка «Спасибо за подтверждение»
#     или исчез вопрос. Ничего не доказано – так и пишем.

# ── Общий кусок JS: увидеть страницу так, как её видит человек ──────
#
# document.body.innerText сюда не годится: он не заглядывает ни в теневые
# корни веб-компонентов, ни во врезанные кадры, а кабинет 2ГИС собран
# именно так. querySelector по тексту не годится тем более – см. пункт 2
# выше.
#
# Поэтому: собираем все корни, склеиваем их текст в одну строку и помним,
# из какого узла какой кусок пришёл. Найдя фразу регуляркой, строим по
# карте Range ровно на её буквах и спрашиваем у браузера прямоугольник –
# то самое место, куда указывает палец человека.
_DEEP_JS = r"""
  const HIDDEN = /[\u00ad\u200b\u200c\u200d\ufeff]/g;
  const norm = s => (s || '').replace(HIDDEN, '').replace(/\s+/g, ' ').trim();

  function docOf(root) {
    return root.nodeType === 9 ? root : (root.ownerDocument || document);
  }

  // Все места, где может лежать текст: сам документ, теневые корни и свои
  // (не чужого домена) кадры. Для кадра запоминаем сдвиг: координаты
  // внутри него считаются от его собственного левого верхнего угла.
  function collect() {
    const out = [], seen = new Set();
    const add = (root, dx, dy) => {
      if (!root || seen.has(root)) return;
      seen.add(root);
      out.push({ root: root, dx: dx, dy: dy });
      let all = [];
      try { all = root.querySelectorAll('*'); } catch (e) { return; }
      for (const el of all) {
        if (el.shadowRoot) add(el.shadowRoot, dx, dy);
        if (el.tagName === 'IFRAME') {
          let doc = null;
          try { doc = el.contentDocument; } catch (e) { doc = null; }
          if (!doc) continue;
          const r = el.getBoundingClientRect();
          add(doc, dx + r.left, dy + r.top);
        }
      }
    };
    add(document, 0, 0);
    return out;
  }

  // Текст корня одной строкой + карта «этот кусок пришёл из этого узла».
  function flatten(entry) {
    const walker = docOf(entry.root).createTreeWalker(entry.root, NodeFilter.SHOW_TEXT, null);
    const chunks = [];
    let text = '', node;
    while ((node = walker.nextNode())) {
      const parent = node.parentElement;
      if (!parent) continue;
      const tag = parent.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TEMPLATE') continue;
      const value = node.nodeValue || '';
      if (!value.trim()) continue;
      if (text && !/\s$/.test(text)) text += ' ';
      chunks.push({ node: node, start: text.length, len: value.length });
      text += value;
    }
    entry.text = text;
    entry.chunks = chunks;
    return entry;
  }

  const ROOTS = collect().map(flatten);
  const TEXT = ROOTS.map(e => e.text).join(' \n ');

  // Буква под номером i – в каком узле и на каком месте.
  function place(chunks, i) {
    for (const c of chunks) {
      if (i <= c.start) return { node: c.node, offset: 0 };
      if (i <= c.start + c.len) return { node: c.node, offset: i - c.start };
    }
    const last = chunks[chunks.length - 1];
    return last ? { node: last.node, offset: last.len } : null;
  }

  // Первый непустой прямоугольник. Пусто – текста на экране нет
  // (display:none, свёрнутый блок): кликать в него нельзя.
  function boxOf(range) {
    const rects = Array.from(range.getClientRects()).filter(r => r.width > 1 && r.height > 1);
    return rects.length ? rects[0] : null;
  }

  function spotFor(entry, from, to) {
    const a = place(entry.chunks, from), b = place(entry.chunks, to);
    if (!a || !b) return null;
    const range = docOf(entry.root).createRange();
    try {
      range.setStart(a.node, Math.min(a.offset, (a.node.nodeValue || '').length));
      range.setEnd(b.node, Math.min(b.offset, (b.node.nodeValue || '').length));
    } catch (e) { return null; }
    let box = boxOf(range);
    if (!box) return null;
    const host = a.node.parentElement;
    const view = docOf(entry.root).defaultView || window;
    // Фраза за краем экрана – подводим её к середине: мышь бьёт по видимому.
    if (box.top < 4 || box.bottom > view.innerHeight - 4) {
      try { host.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
      box = boxOf(range) || box;
    }
    const cx = box.left + box.width / 2, cy = box.top + box.height / 2;
    // Кто на самом деле лежит сверху в этой точке. Если чужой блок – мышью
    // не попадём, и Python нажмёт по элементу.
    let over = null;
    try { over = docOf(entry.root).elementFromPoint(cx, cy); } catch (e) { over = null; }
    const clear = !!over && (over === host || host.contains(over) || over.contains(host));
    const target = (host.closest && host.closest('button, a, [role="button"]')) || host;
    try { target.setAttribute('data-click-ok', '1'); } catch (e) {}
    return {
      x: cx + entry.dx, y: cy + entry.dy,
      w: box.width, h: box.height,
      covered: !clear,
      over: over ? norm(over.tagName + ' ' + (over.className || '')).slice(0, 60) : '',
      tag: (target.tagName || '').toLowerCase(),
      label: norm(host.textContent).slice(0, 60),
    };
  }

  function find(rx) {
    for (const entry of ROOTS) {
      rx.lastIndex = 0;
      let m;
      while ((m = rx.exec(entry.text)) !== null) {
        const s = spotFor(entry, m.index, m.index + m[0].length);
        if (s) return s;
        if (!rx.global) break;
        if (rx.lastIndex <= m.index) rx.lastIndex = m.index + 1;
      }
    }
    return null;
  }

  // Та же охота, но только рядом с вопросом. Нужна на случай, когда кабинет
  // сменит слова на кнопке: внутри плашки короткий ответ на вопрос – это она
  // и есть, а вот по всей странице так искать нельзя.
  function findNear(rx, anchorRx, span) {
    for (const entry of ROOTS) {
      anchorRx.lastIndex = 0;
      const a = anchorRx.exec(entry.text);
      if (!a) continue;
      const from = a.index, to = Math.min(entry.text.length, a.index + a[0].length + span);
      rx.lastIndex = 0;
      let m;
      while ((m = rx.exec(entry.text)) !== null) {
        if (m.index >= from && m.index < to) {
          const s = spotFor(entry, m.index, m.index + m[0].length);
          if (s) { s.near = norm(m[0]); return s; }
        }
        if (rx.lastIndex <= m.index) rx.lastIndex = m.index + 1;
      }
    }
    return null;
  }

  // Разметка плашки – в лог. Если надпись когда-нибудь снова не найдётся,
  // разбираться будем по этой строчке, а не по переписке со скриншотами.
  function markupOf(rx) {
    let best = null;
    for (const entry of ROOTS) {
      let all = [];
      try { all = entry.root.querySelectorAll('*'); } catch (e) { continue; }
      for (const el of all) {
        if (!rx.test(norm(el.textContent))) continue;
        const len = (el.textContent || '').length;
        if (!best || len < best.len) best = { el: el, len: len };
      }
    }
    if (!best) return '';
    try { return (best.el.outerHTML || '').replace(/\s+/g, ' ').slice(0, 700); }
    catch (e) { return ''; }
  }

  function clean() {
    for (const entry of ROOTS) {
      try {
        entry.root.querySelectorAll('[data-click-ok]')
          .forEach(n => n.removeAttribute('data-click-ok'));
      } catch (e) {}
    }
  }
"""

# Один взгляд на страницу: где мы, что на ней написано, видна ли плашка,
# всплыло ли подтверждение и куда нажимать. Всё сразу – чтобы решение
# принималось по одному снимку, а не по четырём разным.
_ACTUALIZE_LOOK_JS = "() => {\n" + _DEEP_JS + r"""
  clean();

  // Ищем ВХОЖДЕНИЕ, а не «весь текст элемента равен». «Да, данные верны»,
  // «Данные верны» с иконкой, надпись внутри длинной строки – всё подходит.
  const OK_RX = /данные\s+верны/gi;
  const BANNER_RX = /(не\s+обновлял\w*\s+достаточно\s+давно|данные\s+о\s+компании\s+не\s+обновлял|они\s+не\s+изменил)/i;
  const BANNER_G = new RegExp(BANNER_RX.source, 'gi');
  // Запасные слова – только внутри плашки и только если основных нет.
  const ALT_RX = /(вс[её]\s+верно|данные\s+актуальны|подтвердить\s+данные|да,\s*верно)/gi;
  const DONE_RX = /(спасибо\s+за\s+подтверждение|данные\s+подтвержден)/i;

  const seen = norm(TEXT);
  const banner = BANNER_RX.test(seen);
  const spot = find(OK_RX) || (banner ? findNear(ALT_RX, BANNER_G, 200) : null);

  // Где мы – спрашиваем у САМОЙ страницы, а не у адреса.
  //
  // Адрес обманул: у организации с филиалами кабинет уводит с `/company` на
  // `/orgs/<id>/branches/<филиал>` – и это ТА ЖЕ страница «Данные о компании»,
  // только конкретного филиала. Проверка «в адресе есть /company» объявила
  // двадцать городов из двадцати одного «кабинет увёл не туда», хотя
  // приложение стояло ровно там, где надо. Заголовок вкладки честнее: на этой
  // странице он всегда «Личный кабинет 2ГИС: Данные о компании — …».
  const title = norm(document.title || '');
  const path = (location.pathname || '');
  const onCompany = /данные\s+о\s+компании/i.test(title)
                 || /\/company(\/|$)/.test(path)
                 || /\/branches\//.test(path);
  // Карточку удалили из справочника – подтверждать нечего, и это не поломка
  // приложения, а состояние карточки. Кабинет пишет об этом прямым текстом.
  const deleted = /(удален[аоы]\s+из\s+справочника|компания\s+удалена)/i.test(seen);
  return {
    spot: spot,
    banner: banner,
    toast: DONE_RX.test(seen),
    onCompany: onCompany,
    deleted: deleted,
    title: title.slice(0, 120),
    path: path + (location.search || ''),
    size: seen.length,
    roots: ROOTS.length,
    seen: seen.slice(0, 400),
    // Разметку плашки достаём, только когда надпись не нашлась: это
    // единственный случай, когда она нужна, а строка длинная.
    markup: (!spot && banner) ? markupOf(BANNER_RX) : '',
  };
}"""

ACTUALIZE_WAIT_MS = 30_000       # плашка приходит отдельным запросом, позже страницы
ACTUALIZE_QUIET = 3              # столько одинаковых чтений подряд = страница устоялась
ACTUALIZE_PROOF_MS = 12_000      # столько ждём ответа кабинета после нажатия


def look_at_page(page: Page) -> dict:
    """Снимок страницы «Данные о компании» глазами человека. Сбой – пустой снимок."""
    try:
        return page.evaluate(_ACTUALIZE_LOOK_JS) or {}
    except Exception:  # noqa: BLE001 – React перерисовывает, прочитаем в следующий заход
        return {}


def _settle(page: Page) -> dict:
    """
    Дождаться, пока страница перестанет меняться, – и только потом судить.

    Не «нашли слово – значит готово»: слово «Данные о компании» написано в
    меню слева и на пустой странице тоже. Готовность – это когда текст
    несколько чтений подряд один и тот же.
    """
    look, same, was = {}, 0, -1
    deadline = time.time() + ACTUALIZE_WAIT_MS / 1000
    while time.time() < deadline:
        fresh = look_at_page(page)
        look = fresh or look
        if look.get("spot"):
            return look
        size = int(look.get("size") or 0)
        same = same + 1 if size == was and size > 200 else 0
        was = size
        # Страница устоялась, мы на нужном разделе, плашки нет – ждать нечего.
        if same >= ACTUALIZE_QUIET and look.get("onCompany") and not look.get("banner"):
            break
        page.wait_for_timeout(700)
    return look


def _click_spot(page: Page, spot: dict, mouse: bool = True) -> str:
    """
    Нажать туда, где нарисована надпись. Возвращает, чем нажали (пусто – никак).

    Порядок: мышью по точке (так делает человек, и SPA получает настоящие
    события) → по размеченному элементу → событием. Три попытки нужны:
    у надписи может не быть ни кнопки-предка, ни собственного размера, а
    поверх неё может лежать чужой блок.
    """
    x, y = spot.get("x"), spot.get("y")
    if mouse and not spot.get("covered") and x is not None and y is not None:
        try:
            page.mouse.click(float(x), float(y))
            return "мышью"
        except Exception:  # noqa: BLE001
            pass
    try:
        page.locator('[data-click-ok="1"]').first.click(timeout=5_000)
        return "по элементу"
    except Exception:  # noqa: BLE001
        pass
    try:
        page.eval_on_selector('[data-click-ok="1"]', "el => el.click()")
        return "событием"
    except Exception:  # noqa: BLE001
        return ""


def _confirmed(page: Page, had_banner: bool, seconds: float | None = None) -> str:
    """
    Доказательство, что подтверждение прошло, – или пустая строка.

    Доказательств два: всплывающая плашка «Спасибо за подтверждение данных»
    и исчезнувший вопрос. Второе засчитываем, только если вопрос БЫЛ: иначе
    «его нет» – не доказательство, а тавтология.
    """
    deadline = time.time() + (ACTUALIZE_PROOF_MS / 1000 if seconds is None else seconds)
    while time.time() < deadline:
        page.wait_for_timeout(400)
        look = look_at_page(page)
        if look.get("toast"):
            return "плашка «Спасибо за подтверждение данных»"
        if had_banner and look and not look.get("banner"):
            return "вопрос со страницы убрался"
    return ""


def actualize_city(page: Page, task: dict, idx: int = 0, total: int = 1) -> dict:
    """
    Статусы – те же, что у Яндекса, чтобы отчёт был общий:
      'actualized'      – кнопку «Данные верны» нажали, и кабинет это подтвердил
      'not-needed'      – плашки нет, подтверждать нечего (это НЕ ошибка)
      'status-mismatch' – КП обещал живую карточку, а кабинет пишет «удалена
                          из справочника» – это не сбой приложения, а неверный
                          статус в таблице; подтверждать нечего
      'failed'          – страница не открылась, кнопка не нашлась или клик
                          не прошёл
      'no-session'      – сессия 2ГИС не активна

    task['kpStatus'] – значение колонки «Статус» 2ГИС из КП. Сверяем его с
    тем, что видим в кабинете (см. kp_sheet.status_verdict), и кладём в
    cardAlive/statusVerdict/statusNote отдельно от итогового status.
    """
    import kp_sheet

    started = time.time()
    label = f"[{idx + 1}/{total}] {task.get('cityName', '?')}"
    kp_status = (task.get("kpStatus") or "").strip()
    result = {"cityName": task.get("cityName"), "companyUrl": task.get("gisUrl") or task.get("companyUrl"),
              "status": "failed", "reason": "", "durationMs": 0, "platform": "gis",
              "kpStatus": kp_status, "cardAlive": None, "statusVerdict": "", "statusNote": ""}

    def finish(status: str, reason: str) -> dict:
        result["status"] = status
        result["reason"] = reason
        result["durationMs"] = int((time.time() - started) * 1000)
        verdict, note = kp_sheet.status_verdict(kp_status, result["cardAlive"])
        result["statusVerdict"], result["statusNote"] = verdict, note
        return result

    info(f"🏙  {label} – актуализация 2ГИС...")
    url = build_company_url(result["companyUrl"])
    if not url:
        return finish("failed", "Не удалось определить адрес раздела «Данные о компании»")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40_000)
    except Exception as e:  # noqa: BLE001
        return finish("failed", f"Не удалось открыть страницу: {yb._short_error(e)}")

    if looks_like_login_page(page):
        return finish("no-session", "Сессия 2ГИС не активна: открылась страница входа")

    look = _settle(page)
    spot = look.get("spot")
    had_banner = bool(look.get("banner"))

    # Жива ли карточка? «Удалена» – точно нет; «мы на странице „Данные о
    # компании“» или сама плашка нашлась – точно да. Иначе не уверены, и
    # путать неуверенность с расхождением статуса нельзя.
    if look.get("deleted"):
        result["cardAlive"] = False
    elif look.get("onCompany") or spot:
        result["cardAlive"] = True
    verdict, note = kp_sheet.status_verdict(kp_status, result["cardAlive"])
    if verdict == "mismatch":
        warn(f"  🚦 {label}: {note}")

    if not spot:
        if not look:
            return finish("failed", "Страницу «Данные о компании» прочитать не вышло "
                                    "(браузер не ответил). Попробуйте ещё раз")
        # Карточки нет в справочнике – это состояние карточки, а не поломка.
        if look.get("deleted"):
            if verdict == "mismatch":
                return finish("status-mismatch", note)
            out = finish("failed", "Карточка удалена из справочника 2ГИС – подтверждать нечего")
            warn(f"  🗑 {label}: {out['reason']}")
            return out
        if not look.get("onCompany"):
            # Строка для разбора – короткая и по делу. Раньше сюда валился
            # кусок страницы на сто шестьдесят знаков, и это выглядело
            # страшнее любой настоящей беды.
            warn(f"  🔎 {label}: открылась не та страница – "
                 f"«{look.get('title') or 'без заголовка'}» ({look.get('path') or '?'})")
            return finish("failed",
                          f"Открылась не страница «Данные о компании», а «{look.get('title') or '?'}» "
                          f"({look.get('path') or 'адрес неизвестен'}) – подтвердить не смогли")
        if had_banner:
            # Единственный по-настоящему непонятный случай: плашка есть, а
            # надписи не видно. Только здесь и нужна разметка в логе.
            warn(f"  🔎 {label}: плашка на странице есть, а надписи «Данные верны» "
                 "на ней не нашлось")
            if look.get("markup"):
                warn(f"  🔎 {label}: разметка плашки {look['markup']}")
            return finish("failed",
                          "Плашка «Данные о компании не обновлялись» на странице есть, "
                          "а надпись «Данные верны» на ней не нашлась. Подтвердите вручную")
        if int(look.get("size") or 0) < 200:
            return finish("failed",
                          "Страница «Данные о компании» не дорисовалась – "
                          "подтвердить не смогли. Попробуйте ещё раз")
        out = finish("not-needed", "Плашки «Данные верны» нет – подтверждать нечего")
        info(f"  ✓ {label}: {out['reason']} ({out['durationMs'] / 1000:.1f} сек)")
        return out

    how = _click_spot(page, spot)
    proof = _confirmed(page, had_banner) if how else ""
    if not proof:
        # Мышь могла попасть в перекрытие – второй заход по самому элементу.
        again = _click_spot(page, spot, mouse=False)
        if again:
            how, proof = again, _confirmed(page, had_banner, ACTUALIZE_PROOF_MS / 1500)

    if not proof:
        return finish("failed",
                      "Надпись «Данные верны» нашли и нажали, но кабинет ничего не ответил: "
                      "ни плашки «Спасибо за подтверждение данных», ни исчезнувшего вопроса. "
                      "Подтвердите вручную")

    out = finish("actualized", f"Данные подтверждены – {proof}")
    info(f"  ✅ {label}: {out['reason']} (нажали {how}, {out['durationMs'] / 1000:.1f} сек)")
    return out


# ════════════════════════════════════════════════════════════════════
#  Фото в раздел «Фото и видео»
# ════════════════════════════════════════════════════════════════════
#
# Те же снимки, что уходят в «Товары» Яндекса, кладём и сюда – это одна
# отгрузка, просто на двух площадках.
#
# Кнопку жать не надо, и это главное. На экране добавление выглядит по-разному:
# пустой альбом показывает синюю кнопку «Добавить» посередине, непустой –
# круглый «плюс» в правом нижнем углу. Но в разметке и там, и там лежит один и
# тот же настоящий `<input type="file" multiple>` внутри маленькой формы. Файлы
# отдаём прямо ему – так же, как в Яндексе. Тогда нам всё равно, как сегодня
# выглядит кнопка и куда её передвинули.
#
# Адрес раздела – `/orgs/<id>/branches/<филиал>/media`. Номер филиала в КП не
# лежит, а складывать адрес из кусков мы уже пробовали и обожглись на
# `/company`. Поэтому ссылку берём из меню кабинета: она там написана целиком.

# 2ГИС принимает не всё: gif не примет, и молча – просто ничего не произойдёт.
MEDIA_TYPES = (".jpg", ".jpeg", ".png", ".webp")
MEDIA_WAIT_MS = 60_000           # столько ждём, пока снимок появится в альбоме

# Требования 2ГИС к фото (из подсказки в интерфейсе кабинета).
GIS_MIN_PX   = 600               # минимальный размер стороны, px
GIS_MAX_PX   = 7000              # максимальный размер стороны, px
GIS_MAX_SIDE_RATIO = 5           # максимальное соотношение сторон (длинная / короткая)
GIS_MAX_BYTES = 10 * 1024 * 1024 # максимальный размер файла, байт


def _image_dims(path: str) -> tuple[int, int] | None:
    """
    Ширина и высота в пикселях – без PIL, из бинарных заголовков.

    Поддерживает JPEG (SOF-маркеры), PNG (IHDR-чанк) и WEBP (VP8/VP8L/VP8X).
    Возвращает None, если формат не распознан или файл повреждён.
    """
    try:
        with open(path, "rb") as f:
            hdr = f.read(32)

        # ── PNG ────────────────────────────────────────────────
        if hdr[:8] == b'\x89PNG\r\n\x1a\n':
            w, h = struct.unpack('>II', hdr[16:24])
            return w, h

        # ── JPEG ───────────────────────────────────────────────
        if hdr[:2] == b'\xff\xd8':
            with open(path, "rb") as f:
                data = f.read()
            i = 2
            while i + 8 < len(data):
                if data[i] != 0xFF:
                    break
                m = data[i + 1]
                # SOF-маркеры, в которых записаны размеры кадра
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                         0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack('>HH', data[i + 5: i + 9])
                    return w, h
                seg_len = struct.unpack('>H', data[i + 2: i + 4])[0]
                i += 2 + seg_len

        # ── WEBP ───────────────────────────────────────────────
        if hdr[:4] == b'RIFF' and hdr[8:12] == b'WEBP':
            chunk = hdr[12:16]
            with open(path, "rb") as f:
                f.seek(12)
                body = f.read(30)
            if chunk == b'VP8X':          # extended
                w = struct.unpack('<I', body[8:11] + b'\x00')[0] + 1
                h = struct.unpack('<I', body[11:14] + b'\x00')[0] + 1
                return w, h
            if chunk == b'VP8 ':          # lossy
                bits = struct.unpack('<HH', body[14:18])
                return bits[0] & 0x3FFF, bits[1] & 0x3FFF
            if chunk == b'VP8L':          # lossless
                packed = int.from_bytes(body[9:13], 'little')
                return (packed & 0x3FFF) + 1, ((packed >> 14) & 0x3FFF) + 1

    except Exception:  # noqa: BLE001
        pass
    return None


def check_gis_photo(path: str) -> str:
    """
    Проверяет файл на соответствие требованиям 2ГИС.

    Возвращает пустую строку если всё в порядке, иначе – причину отказа.
    """
    size = Path(path).stat().st_size
    if size > GIS_MAX_BYTES:
        mb = size / 1024 / 1024
        return f"файл {mb:.1f} МБ – 2ГИС принимает до 10 МБ"

    dims = _image_dims(path)
    if dims is None:
        return ""   # не смогли прочитать – пусть пробует, 2ГИС сам скажет

    w, h = dims
    name = Path(path).name
    if w < GIS_MIN_PX or h < GIS_MIN_PX:
        return (f"{name}: размер {w}×{h} пкс – 2ГИС требует минимум "
                f"{GIS_MIN_PX}×{GIS_MIN_PX} пкс")
    if w > GIS_MAX_PX or h > GIS_MAX_PX:
        return (f"{name}: размер {w}×{h} пкс – 2ГИС принимает максимум "
                f"{GIS_MAX_PX}×{GIS_MAX_PX} пкс")
    long_side, short_side = max(w, h), min(w, h)
    if short_side > 0 and long_side / short_side > GIS_MAX_SIDE_RATIO:
        ratio = long_side / short_side
        return (f"{name}: соотношение сторон {ratio:.1f}:1 – "
                f"2ГИС допускает не более {GIS_MAX_SIDE_RATIO}:1")
    return ""


def build_media_url(url: str | None) -> str | None:
    """Куда идти за разделом «Фото и видео». Точный адрес возьмём уже на месте."""
    oid = extract_org_id(url)
    return f"{ACCOUNT_HOST}/orgs/{oid}/company" if oid else None


# Ссылка на раздел + сколько снимков сейчас в альбоме.
#
# Считаем не по классам (они хешированные и меняются со сборкой), а по тому,
# что видно человеку: плитка альбома – это картинка заметного размера. Иконки
# и значки рисуются через svg и под этот счёт не попадают. Заодно снимаем
# число со вкладки «Все фото и видео N» – как второй свидетель.
#
# Важно: getBoundingClientRect возвращает 0 для элементов ЗА нижней границей
# viewport (ниже прокрутки). В headless-браузере страница не прокручивается,
# поэтому новые снимки, добавленные в конец длинного альбома, не попадают в
# поле зрения. Используем offsetWidth/offsetHeight как резерв: они возвращают
# CSS-размер элемента независимо от положения на странице.
_MEDIA_LOOK_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  // Берём a.href, а не сам атрибут: в живом кабинете ссылка записана
  // относительной («/orgs/…/media»), а Playwright такой адрес не принимает
  // вовсе – «Invalid url». a.href браузер уже развернул в полный.
  const link = (Array.from(document.querySelectorAll('a[href]'))
    .find(a => {
      const h = a.getAttribute('href') || '';
      return /\/media(\/|$|\?)/.test(h) && /\/orgs\//.test(h);
    }) || {}).href || '';

  let tiles = 0;
  for (const img of document.querySelectorAll('img')) {
    if (!img.getAttribute('src')) continue;
    const r = img.getBoundingClientRect();
    // Новые плитки в конце длинного альбома находятся ниже viewport:
    // getBoundingClientRect возвращает 0, offsetWidth/offsetHeight – нет.
    const w = r.width || img.offsetWidth;
    const h = r.height || img.offsetHeight;
    if (w >= 60 && h >= 60) tiles++;
  }

  let counter = null;
  const body = norm(document.body.innerText || '');
  const m = body.match(/все\s+фото\s+и\s+видео\s+(\d+)/i);
  if (m) counter = parseInt(m[1], 10);

  // Если 2ГИС показал ошибку валидации загруженного файла – подсказка содержит
  // текст о требованиях. Ловим это, чтобы не ждать полный таймаут впустую.
  const uploadError = /600.?600|7000.?7000|соотношение\s+сторон|до\s+10\s+мб/i.test(body);

  const field = document.querySelector('input[type="file"]');
  return {
    link: link,
    tiles: tiles,
    counter: counter,
    hasField: !!field,
    onMedia: /\/media(\/|$)/.test(location.pathname || ''),
    title: norm(document.title || '').slice(0, 120),
    path: location.pathname || '',
    size: norm(body).length,
    uploadError: uploadError,
  };
}
"""

# Кнопка нужна только если поля вдруг нет: на некоторых пустых карточках 2ГИС
# рисует «Добавить» и подставляет форму уже после нажатия.
_MEDIA_ADD_BTN_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-click-add]').forEach(n => n.removeAttribute('data-click-add'));
  for (const el of document.querySelectorAll('button, a, [role="button"], [class*="button__basic"]')) {
    if (!/^(добавить|добавить\s+фото)$/i.test(norm(el.textContent))) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) continue;
    el.setAttribute('data-click-add', '1');
    return true;
  }
  return false;
}
"""


def _media_look(page: Page) -> dict:
    try:
        return page.evaluate(_MEDIA_LOOK_JS) or {}
    except Exception:  # noqa: BLE001 – React перерисовывает, прочитаем ещё раз
        return {}


def _media_settle(page: Page, want_field: bool = True) -> dict:
    """
    Дождаться, пока раздел дорисуется: те же правила, что и у актуализации.

    Ждём не «страница перестала расти», а то, ЗА ЧЕМ пришли: поле загрузки или
    ссылку на раздел в меню. Разница видна на живом кабинете: у Екатеринбурга
    в отчёте стояло «в кабинете не нашлась ссылка на раздел «Фото и видео»» –
    а ссылка там есть, просто меню дорисовывается после остального, и Click
    успевал уйти раньше. Поэтому пока искомого нет, тишина считается вдвое
    строже: страница затихла – ждём ещё, вдруг это пауза, а не конец.
    """
    look, same, was = {}, 0, -1
    deadline = time.time() + MEDIA_WAIT_MS / 1000
    while time.time() < deadline:
        look = _media_look(page) or look
        found = look.get("hasField") if want_field else (look.get("link") or look.get("onMedia"))
        if found:
            return look
        size = int(look.get("size") or 0)
        same = same + 1 if size == was and size > 200 else 0
        was = size
        if same >= ACTUALIZE_QUIET * 2:
            break
        page.wait_for_timeout(700)
    return look


def suitable_photos(paths: list[str]) -> tuple[list[str], list[str]]:
    """Что 2ГИС примет и что отбросит. Отброшенное называем по имени файла."""
    good, bad = [], []
    for p in paths:
        (good if Path(str(p)).suffix.lower() in MEDIA_TYPES else bad).append(str(p))
    return good, bad


def upload_media(page: Page, gis_url: str | None, files: list[str], label: str = "") -> dict:
    """
    Положить снимки в «Фото и видео» карточки 2ГИС.

    Возвращает {'uploaded', 'requested', 'skipped', 'reason'}. Ошибка здесь
    НЕ влияет на публикацию поста: это отдельный шаг, и статус города за него
    не отвечает – ровно как у «Товаров» Яндекса.
    """
    out = {"requested": len(files or []), "uploaded": 0, "skipped": 0, "reason": ""}
    if not files:
        return out

    good, bad = suitable_photos(files)
    out["skipped"] = len(bad)
    if bad:
        names = ", ".join(Path(b).name for b in bad[:3])
        warn(f"  📸 {label}2ГИС не принимает такие файлы (только jpg, png, webp): {names}")

    # Проверяем размеры изображений до отправки: 2ГИС молча показывает красный
    # крест если фото не соответствует требованиям, и в отчёте это выглядит как
    # «файлы переданы, но не появились». Лучше сказать сразу, в чём проблема.
    dim_errors = [check_gis_photo(p) for p in good]
    dim_errors = [e for e in dim_errors if e]
    if dim_errors:
        out["reason"] = "Фото не соответствуют требованиям 2ГИС: " + "; ".join(dim_errors)
        warn(f"  📸 {label}2ГИС: {out['reason']}")
        return out
    if not good:
        out["reason"] = "Ни один файл не подходит для 2ГИС (принимаются jpg, png, webp)"
        return out

    start = build_media_url(gis_url)
    if not start:
        out["reason"] = "Не удалось определить кабинет 2ГИС по ссылке из КП"
        return out

    # Заходим на карточку и берём ссылку на «Фото и видео» из меню.
    try:
        page.goto(start, wait_until="domcontentloaded", timeout=40_000)
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"Кабинет 2ГИС не открылся: {yb._short_error(e)}"
        return out
    if looks_like_login_page(page):
        out["reason"] = "Сессия 2ГИС не действует – войдите в «Настройках»"
        out["noSession"] = True
        return out

    look = _media_settle(page, want_field=False)
    if not look.get("onMedia"):
        link = look.get("link")
        if not link:
            out["reason"] = "В кабинете 2ГИС не нашлась ссылка на раздел «Фото и видео»"
            return out
        # Страховка на случай, если ссылка всё же придёт относительной:
        # Playwright такой адрес не принимает («Invalid url»), а мы бы списали
        # это на недоступный раздел.
        link = urljoin(page.url or ACCOUNT_HOST, link)
        try:
            page.goto(link, wait_until="domcontentloaded", timeout=40_000)
        except Exception as e:  # noqa: BLE001
            out["reason"] = f"Раздел «Фото и видео» не открылся: {yb._short_error(e)}"
            return out
        look = _media_settle(page)

    if not look.get("hasField"):
        # Поля нет – возможно, 2ГИС ждёт нажатия «Добавить».
        try:
            if page.evaluate(_MEDIA_ADD_BTN_JS):
                page.locator('[data-click-add="1"]').first.click(timeout=5_000)
                page.wait_for_timeout(800)
                look = _media_settle(page)
        except Exception:  # noqa: BLE001
            pass
    if not look.get("hasField"):
        out["reason"] = "В разделе «Фото и видео» не нашлось поле загрузки"
        return out

    before = int(look.get("tiles") or 0)
    was_counter = look.get("counter")
    media_url = page.url          # вернуться сюда же после перепроверки
    try:
        page.locator('input[type="file"]').first.set_input_files(good, timeout=15_000)
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"Не удалось передать файлы: {yb._short_error(e)}"
        return out

    # Ждём роста альбома в браузере – но это пока не доказательство, а только
    # надежда. Кабинет рисует плитку сразу, «оптимистично», ещё до того, как
    # сервер файл принял; если сервер его отверг, плитка тихо пропадает. Мы
    # проверяли рост и останавливались, как только его видели, – и в боевом
    # прогоне это дало ложный успех: лог написал «снимков добавлено 1 из 1»,
    # а в самом кабинете альбом остался пустым. Здесь только ждём подольше,
    # решение принимается ниже.
    deadline = time.time() + MEDIA_WAIT_MS / 1000
    upload_rejected = False
    while time.time() < deadline:
        page.wait_for_timeout(700)
        now = _media_look(page)
        if not now:
            continue
        # 2ГИС показал ошибку валидации (красный крест с подсказкой о требованиях):
        # ждать дальше бессмысленно, выходим сразу.
        if now.get("uploadError"):
            upload_rejected = True
            break
        seen = int(now.get("tiles") or 0) - before
        if was_counter is not None and now.get("counter") is not None:
            seen = max(seen, int(now["counter"]) - int(was_counter))
        if seen >= len(good):
            break

    # Доказательство только одно – то, что страница покажет ПОСЛЕ перезагрузки
    # с сервера, а не то, что успел нарисовать браузер по своей инициативе.
    try:
        page.goto(media_url, wait_until="domcontentloaded", timeout=40_000)
        confirmed = _media_settle(page, want_field=False)
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"Не удалось перепроверить альбом после загрузки: {yb._short_error(e)}"
        return out

    grown = int(confirmed.get("tiles") or 0) - before
    if was_counter is not None and confirmed.get("counter") is not None:
        grown = max(grown, int(confirmed["counter"]) - int(was_counter))
    out["uploaded"] = min(grown, len(good)) if grown > 0 else 0
    if out["uploaded"] >= len(good):
        info(f"  📸 {label}2ГИС: снимков добавлено {out['uploaded']} из {len(good)}")
    elif out["uploaded"]:
        out["reason"] = (f"В альбоме появилось {out['uploaded']} из {len(good)} – "
                         "остальные проверьте вручную")
        warn(f"  📸 {label}2ГИС: {out['reason']}")
    elif upload_rejected:
        out["reason"] = ("2ГИС отклонил фото: не соответствует требованиям "
                         "(мин. 600×600 пкс, макс. 7000×7000, соотношение ≤ 1:5, до 10 МБ)")
        warn(f"  📸 {label}2ГИС: {out['reason']}")
    else:
        out["reason"] = ("Файлы переданы, но в альбоме они не появились – "
                         "проверьте раздел «Фото и видео» вручную")
        warn(f"  📸 {label}2ГИС: {out['reason']}")
    return out


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
            # Сначала корень: кабинет сам уводит на форму входа. Если формы
            # не появилось – пробуем явный адрес. Порядок такой, потому что
            # у заказчика в адресной строке было именно account.2gis.com.
            self.page.goto(ACCOUNT_HOST, wait_until="domcontentloaded", timeout=40_000)
            self.page.wait_for_timeout(2500)
            if not self._fields()["hasPass"]:
                try:
                    self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=40_000)
                    self.page.wait_for_timeout(2500)
                except Exception:  # noqa: BLE001 – останемся на корне, снимок покажет что там
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

    def _fields(self) -> dict:
        """Что за поля на странице входа. Ничего не бросает – только смотрит."""
        try:
            return self.page.evaluate(_FIND_LOGIN_FIELDS_JS)
        except Exception:  # noqa: BLE001
            return {"inputs": [], "hasMail": False, "hasPass": False}

    def submit_credentials(self, email: str, password: str) -> dict:
        """
        Вписать почту и пароль и нажать «Войти».

        Поля ищем СВОИМ разбором, а не селектором по типу. У 2ГИС поля –
        самодельные компоненты, и атрибута type у них может не быть вовсе
        (в разметке кабинета встречается голое `<input class="…"
        placeholder="Поиск">`). Первый заход это и уронил: селектор
        `input[type=text]` не нашёл ничего, и вместо понятного сообщения
        человек увидел «Locator.click: Timeout 30000ms exceeded».

        Ничего наружу не бросаем: любая неудача возвращается снимком экрана
        и словами, что именно на странице увидели. Слепой таймаут не
        объясняет ничего, а снимок объясняет сразу.
        """
        page = self.page
        found = self._fields()
        if not (found.get("hasMail") and found.get("hasPass")):
            st = self.state()
            st["note"] = _no_fields_note(found)
            return st

        try:
            mail = page.locator('[data-click-login="mail"]').first
            mail.click(timeout=8_000)
            try:
                mail.fill("")
            except Exception:  # noqa: BLE001 – самодельное поле может не поддерживать fill
                pass
            mail.type(email, delay=30)

            pw = page.locator('[data-click-login="pass"]').first
            pw.click(timeout=8_000)
            try:
                pw.fill("")
            except Exception:  # noqa: BLE001
                pass
            pw.type(password, delay=30)
            page.wait_for_timeout(400)
            if not yb._click_exact_button(page, ["войти", "log in", "sign in"]):
                pw.press("Enter")
        except Exception as e:  # noqa: BLE001
            st = self.state()
            st["note"] = f"Не получилось заполнить форму входа: {yb._short_error(e)}"
            return st

        page.wait_for_timeout(4000)
        st = self.state()
        if st.get("step") == "login":
            # Форма осталась – значит 2ГИС не пустил. Причина обычно написана
            # прямо на странице, поэтому показываем её текст.
            st["note"] = _page_complaint(page) or ("2ГИС остался на форме входа – "
                                                   "проверьте почту и пароль.")
        return st

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


# Поля входа ищем разбором, а не селектором: у 2ГИС это самодельные
# компоненты, и атрибута type у них может не быть.
_FIND_LOGIN_FIELDS_JS = r"""
() => {
  const seen = el => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 40 && r.height > 8 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const all = Array.from(document.querySelectorAll('input')).filter(seen);
  const kind = el => (el.getAttribute('type') || '').toLowerCase();
  const label = el => [el.placeholder, el.name, el.id,
                       el.getAttribute('aria-label')].filter(Boolean).join(' ');

  const pass = all.find(el => kind(el) === 'password') || null;
  const MAIL = /(почт|mail|логин|login|телефон|phone)/i;
  const SEARCH = /(поиск|search)/i;
  let mail = all.find(el => el !== pass && kind(el) !== 'password' && MAIL.test(label(el)));
  if (!mail) mail = all.find(el => el !== pass && kind(el) !== 'password' && !SEARCH.test(label(el)));

  document.querySelectorAll('[data-click-login]').forEach(
    n => n.removeAttribute('data-click-login'));
  if (mail) mail.setAttribute('data-click-login', 'mail');
  if (pass) pass.setAttribute('data-click-login', 'pass');
  return {
    hasMail: !!mail, hasPass: !!pass,
    inputs: all.map(el => ({ type: kind(el), label: label(el).slice(0, 40) })),
  };
}
"""

# Текст жалобы со страницы: 2ГИС пишет причину прямо в форме.
_COMPLAINT_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const RX = /(невер|не найден|ошибк|попроб|заблокир|подтверд|не совпад|некоррект)/i;
  let best = '';
  for (const el of document.querySelectorAll('body *')) {
    const t = norm(el.textContent);
    if (!t || t.length > 200 || !RX.test(t)) continue;
    if (Array.from(el.querySelectorAll('*')).some(c => RX.test(norm(c.textContent)))) continue;
    if (!best || t.length < best.length) best = t;
  }
  return best;
}
"""


def _no_fields_note(found: dict) -> str:
    """Что сказать человеку, если полей входа на странице не видно."""
    inputs = found.get("inputs") or []
    if not inputs:
        return ("На странице входа 2ГИС не видно ни одного поля. Возможно, кабинет "
                "показал что-то другое – капчу, согласие или ошибку. Посмотрите снимок ниже.")
    seen = ", ".join(f"«{i.get('label') or i.get('type') or '?'}»" for i in inputs[:5])
    return (f"Поля входа не опознаны. На странице видно полей: {len(inputs)} ({seen}). "
            "Пришлите снимок ниже – поправим поиск полей.")


def _page_complaint(page: Page) -> str:
    try:
        return (page.evaluate(_COMPLAINT_JS) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


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
