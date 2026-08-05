"""
kp_audit.py – сверка КП с тем, что на самом деле заведено в Яндекс.Бизнесе.

Задача: взять лист КП, взять все организации аккаунта и ответить на три
вопроса по каждому городу.

  1. Есть ли в Яндексе организация для этого города?
  2. Не завелось ли их несколько (дубли – главная беда: посты и актуализация
     уходят в одну карточку, а вторая живёт своей жизнью)?
  3. Совпадают ли сайт, телефон и почта с тем, что записано в КП?

На выходе – то же самое КП, только с добавленными колонками. Исходные строки
не трогаем вообще: человек открывает файл и видит свою таблицу, просто шире.

Сеть в этом модуле не нужна: сюда приходят уже прочитанные строки КП и уже
собранный список организаций. Поэтому всю логику можно проверить тестами, что
и сделано в tests_audit.py.

Как сопоставляем город и организацию
────────────────────────────────────
Название у всех карточек одно («Авиапромсталь»), так что различает их только
адрес и сайт:

  • адрес Яндекса – «Южный федеральный округ, Ростовская область, городской
    округ Шахты, Шахты». Режем по запятым, у каждой части снимаем приставку
    («городской округ», «г.», «посёлок») и сравниваем с городом из КП;
  • сайт у проекта городской – shahty.aviastal.ru. Совпал хост – это тот же
    город, даже если адрес записан странно.

Совпадение по сайту весит больше, чем по адресу: сайт человек вписывает
руками в обе таблицы, а адрес Яндекс пишет сам и по-разному.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

# Метка сборки: streamlit_app сверяет её и перезагружает модуль при расхождении.
BUILD = "2026-08-06-kp-audit"

# ─── Нормализация ───────────────────────────────────────────────────

# Приставки перед названием населённого пункта. Снимаем их, чтобы «городской
# округ Шахты» и «Шахты» стали одним и тем же.
_PLACE_PREFIX_RX = re.compile(
    r"^(?:"
    r"городской\s+округ|муниципальный\s+округ|городское\s+поселение|"
    r"сельское\s+поселение|внутригородской\s+район|"
    r"посёлок\s+городского\s+типа|поселок\s+городского\s+типа|пгт\.?|"
    r"рабочий\s+посёлок|рабочий\s+поселок|рп\.?|"
    r"город|г\.|гор\.|посёлок|поселок|пос\.|п\.|село|с\.|деревня|дер\.|д\.|"
    r"станица|ст-ца|ст\.|хутор|х\.|аул|микрорайон|мкр\.?"
    r")\s+",
    re.I,
)

# Части адреса, которые городом быть не могут. Без этого «улица Гагарина»
# рискует совпасть с городом Гагарин.
_NOT_A_PLACE_RX = re.compile(
    r"^(?:улица|ул\.|проспект|просп\.|пр-?кт|переулок|пер\.|шоссе|бульвар|б-р|"
    r"проезд|набережная|наб\.|площадь|пл\.|тупик|линия|аллея|тракт|квартал|"
    r"микрорайон|мкр|корпус|корп\.|строение|стр\.|дом|д\.|литера|этаж|офис|"
    r"помещение|здание|владение|км|территория)\b",
    re.I,
)

# Части адреса верхнего уровня: область, край, республика, округ. Городом не
# считаем, иначе «Ростовская область» перетянет на себя «Ростов-на-Дону».
_REGION_RX = re.compile(
    r"(?:\bобласть\b|\bкрай\b|\bреспублика\b|\bавтономн|федеральный\s+округ|"
    r"\bрайон\b|\bулус\b|\bобл\.|\bкр\.)",
    re.I,
)


def norm_text(s: str) -> str:
    """Строка для сравнения: без лишних пробелов, в нижнем регистре, ё → е."""
    s = re.sub(r"\s+", " ", str(s or "")).strip().lower()
    return s.replace("ё", "е")


def norm_city(name: str) -> str:
    """Название города для сравнения: без приставок, без ё, без хвостов в скобках."""
    s = norm_text(name)
    s = re.sub(r"\s*\([^)]*\)", "", s)          # «Шахты (склад)» → «Шахты»
    s = _PLACE_PREFIX_RX.sub("", s).strip()
    s = re.sub(r"[«»\"']", "", s)
    # Дефисы у Яндекса и в КП пишут разными знаками: Улан-Удэ, Улан‑Удэ.
    s = re.sub(r"[‑–—−]", "-", s)
    s = re.sub(r"\s*-\s*", "-", s)
    return s.strip(" .,")


def city_variants(name: str) -> list[str]:
    """
    Все написания города из одной ячейки КП.

    В таблице города со сменившимся именем записаны через косую черту:
    «Астана/Нур-Султан», «Сумгаит/Сумгайыт», «Нахичевань /Нахчыван». Яндекс
    знает одно из них – значит, проверять надо оба.
    """
    parts = re.split(r"\s*[/|]\s*", str(name or ""))
    out, seen = [], set()
    for p in parts:
        v = norm_city(p)
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# Страна из КП и код региона у Яндекса. Нужны из-за одинаковых названий:
# Армавир есть и в России, и в Армении – без страны карточка одного города
# садится на строку другого.
COUNTRY_CODES = {
    "россия": "RU", "рф": "RU", "российская федерация": "RU",
    "казахстан": "KZ", "рк": "KZ",
    "беларусь": "BY", "белоруссия": "BY", "рб": "BY",
    "азербайджан": "AZ", "армения": "AM", "грузия": "GE",
    "киргизия": "KG", "кыргызстан": "KG", "узбекистан": "UZ",
    "таджикистан": "TJ", "туркменистан": "TM", "молдова": "MD", "молдавия": "MD",
    "украина": "UA",
}


def country_code(name: str) -> str:
    return COUNTRY_CODES.get(norm_text(name), "")


def address_places(address: str) -> set[str]:
    """
    Населённые пункты из адреса Яндекса.

    «Южный федеральный округ, Ростовская область, городской округ Шахты, Шахты»
    → {«шахты»}. Регионы и улицы отбрасываем.
    """
    out: set[str] = set()
    for raw in str(address or "").split(","):
        part = raw.strip()
        if not part or _NOT_A_PLACE_RX.match(part) or _REGION_RX.search(part):
            continue
        if re.fullmatch(r"[\d\W]+", part):        # «123», «116А» – номер дома
            continue
        place = norm_city(part)
        if place and not re.fullmatch(r"\d+[а-я]?", place):
            out.add(place)
    return out


def site_host(url: str) -> str:
    """Хост сайта без схемы, www и пути: https://Shahty.Aviastal.RU/ → shahty.aviastal.ru."""
    s = norm_text(url)
    if not s:
        return ""
    s = re.sub(r"^[a-z]+://", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    s = re.sub(r"^www\.", "", s)
    return s.strip(" .")


def phone_key(phone: str) -> str:
    """
    Телефон для сравнения – последние 10 цифр.

    В КП пишут «7 (391) 271-75-19», Яндекс отдаёт «+7 (391) 271-75-19», а
    иногда номер записан вовсе без кода страны. Десять цифр хвоста совпадают
    во всех трёх случаях.
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) < 6:
        return ""
    return digits[-10:] if len(digits) >= 10 else digits


def split_values(cell: str) -> list[str]:
    """Ячейка КП может держать несколько значений через запятую или перенос."""
    parts = re.split(r"[\n;,]+", str(cell or ""))
    return [p.strip() for p in parts if p.strip()]


def email_key(value: str) -> str:
    return norm_text(value).strip(" .,;")


SPRAV_ID_RX = re.compile(r"(?:sprav|/business/companies/company)/(\d+)", re.I)


def company_id_from_url(url: str) -> str:
    m = SPRAV_ID_RX.search(str(url or ""))
    return m.group(1) if m else ""


# ─── Разбор листа КП ────────────────────────────────────────────────

# Колонки ищем по названию. Названия в КП разных проектов немного разные,
# поэтому для каждой – несколько вариантов, сравнение по нормализованному
# тексту заголовка.
_COLS: dict[str, tuple[str, ...]] = {
    "country": ("страна",),
    "city": ("город", "населенный пункт", "нас. пункт"),
    "site": ("url", "сайт", "site", "адрес сайта"),
    "address": ("адрес", "адрес офиса"),
    "email": ("почта", "email", "e-mail", "эл. почта", "электронная почта"),
    "phone": ("общий город", "телефон", "тел.", "городской"),
    "phone2": ("общий сотовый", "сотовый", "мобильный"),
    "link": ("аккаунт", "ссылка", "яндекс", "яндекс бизнес"),
    "status": ("статус", "яндекс_статус"),
}

SPRAV_RX = re.compile(r"yandex\.[a-z.]+/sprav/\d+", re.I)

# Чужие площадки: их ссылки в КП тоже есть, но сайтом города они не являются.
_FOREIGN_SITE_RX = re.compile(r"yandex\.|2gis\.|2гис|google\.|goo\.gl|vk\.com|vk\.ru|"
                              r"ok\.ru|t\.me|wa\.me|instagram|facebook", re.I)


def _looks_like_site(value: str) -> bool:
    """Похоже ли значение ячейки на сайт города (а не на почту или чужую площадку)."""
    v = norm_text(value)
    if not v or "@" in v or " " in v.strip():
        return False
    if not re.search(r"[a-z0-9-]+\.[a-z]{2,}", v):
        return False
    return not _FOREIGN_SITE_RX.search(v)


def parse_sheet(rows: list[list[str]]) -> dict:
    """
    Разобрать лист КП: где шапка, какие колонки, какие строки – города.

    Возвращает {headerIdx, header, columns, items, width, error}. items –
    список {rowIdx, city, country, site, email, phones, link, status}, где
    rowIdx – номер строки в исходной таблице (по нему потом дописываем
    колонки, ничего не сдвигая).
    """
    out: dict[str, Any] = {
        "headerIdx": -1, "header": [], "columns": {}, "items": [],
        "width": max((len(r) for r in rows), default=0), "error": "",
    }
    if not rows:
        out["error"] = "Лист пустой"
        return out

    # Шапка – строка с «Город». «Страна» бывает не всегда (лист на одну страну).
    header_idx = -1
    for i, row in enumerate(rows[:25]):
        cells = [norm_text(c) for c in row]
        if any(c == "город" for c in cells):
            header_idx = i
            break
    if header_idx < 0:
        out["error"] = "Не нашли шапку: нужна строка с колонкой «Город»"
        return out

    header = [norm_text(c) for c in rows[header_idx]]
    out["headerIdx"] = header_idx
    out["header"] = rows[header_idx]

    def find(names: Iterable[str], start: int = 0) -> int:
        for want in names:
            for c in range(start, len(header)):
                if header[c] == want:
                    return c
        for want in names:                       # мягко: заголовок с уточнением
            for c in range(start, len(header)):
                if header[c].startswith(want):
                    return c
        return -1

    cols = {key: find(names) for key, names in _COLS.items()}
    data = rows[header_idx + 1:]

    # Городской сайт – по содержимому: в КП АПС эта колонка вообще без
    # заголовка, а сайт это главная зацепка при сопоставлении с Яндексом.
    if cols["site"] < 0:
        best, hits = -1, 0
        for c in range(out["width"]):
            n = sum(1 for r in data if c < len(r) and _looks_like_site(str(r[c])))
            if n > hits:
                best, hits = c, n
        if hits >= 3:
            cols["site"] = best

    # Ссылка на карточку: в КП колонок «Аккаунт» несколько (Яндекс, 2ГИС,
    # Google) – берём ту, где реально лежат ссылки на sprav.
    best_col, best_hits = -1, 0
    for c in range(out["width"]):
        hits = sum(1 for r in data if c < len(r) and SPRAV_RX.search(str(r[c])))
        if hits > best_hits:
            best_col, best_hits = c, hits
    if best_col >= 0:
        cols["link"] = best_col
        # «Статус» – ближайшая колонка справа от ссылок, но не чужой площадки.
        for c in range(best_col + 1, min(best_col + 6, len(header))):
            if "статус" in header[c] and not re.search(r"2гис|2gis|гугл|google", header[c]):
                cols["status"] = c
                break
    out["columns"] = cols

    def cell(row: list[str], idx: int) -> str:
        return str(row[idx]).strip() if 0 <= idx < len(row) else ""

    for n, row in enumerate(data):
        city = cell(row, cols["city"])
        if not city or norm_text(city) == "город":
            continue
        phones = split_values(cell(row, cols["phone"])) + split_values(cell(row, cols["phone2"]))
        out["items"].append({
            "rowIdx": header_idx + 1 + n,
            "city": city,
            "country": cell(row, cols["country"]),
            "site": cell(row, cols["site"]),
            "address": cell(row, cols["address"]),
            "email": cell(row, cols["email"]),
            "phones": phones,
            "link": cell(row, cols["link"]),
            "status": cell(row, cols["status"]),
        })
    return out


# ─── Сопоставление ──────────────────────────────────────────────────

SCORE_LINK = 100        # в КП стоит прямая ссылка на эту карточку – спорить не о чем
SCORE_SITE = 10         # совпал хост городского сайта
SCORE_PLACE = 6         # город назван в адресе отдельной частью
SCORE_COUNTRY = 2       # страна КП совпала с регионом карточки
SCORE_INSIDE = 2        # город лишь встречается в строке адреса


def match_score(item: dict, company: dict) -> int:
    """Насколько организация похожа на город КП. 0 – не похожа вовсе."""
    kp_id = company_id_from_url(item.get("link", ""))
    if kp_id and kp_id == str(company.get("id") or ""):
        return SCORE_LINK

    # Разные страны – разговор окончен. Иначе армянский Армавир садится на
    # строку российского: название одно, и по нему они неотличимы.
    kp_country = country_code(item.get("country", ""))
    ya_country = (company.get("regionCode") or "").upper()
    if kp_country and ya_country and kp_country != ya_country:
        return 0

    score = SCORE_COUNTRY if (kp_country and kp_country == ya_country) else 0
    kp_host = site_host(item.get("site", ""))
    ya_hosts = {site_host(u) for u in (company.get("sites") or [company.get("site", "")])}
    ya_hosts.discard("")
    if kp_host and kp_host in ya_hosts:
        score += SCORE_SITE

    places = address_places(company.get("address", ""))
    address = norm_text(company.get("address", ""))
    for city in city_variants(item.get("city", "")):
        if city in places:
            score += SCORE_PLACE
            break
        # Совсем короткие названия («Ош», «РФ») по кусочку строки не ищем:
        # такое совпадение чаще случайное, чем настоящее.
        if len(city) >= 4 and re.search(rf"(?<![а-я]){re.escape(city)}(?![а-я])", address):
            score += SCORE_INSIDE
            break
    return score


def match(items: list[dict], companies: list[dict]) -> dict:
    """
    Разложить организации по городам КП.

    Каждая организация достаётся ОДНОМУ городу – тому, к которому подходит
    лучше всех. Так «Ростов-на-Дону» не утаскивает карточку «Ростова
    Великого»: у второго выше очки за точное совпадение части адреса.

    Возвращает {byRow: {rowIdx: [организации]}, extra: [...], chains: [...]}.
    """
    chains = [c for c in companies if (c.get("type") or "") == "chain"]
    ordinary = [c for c in companies if c not in chains]

    def longest(it: dict) -> int:
        return max((len(v) for v in city_variants(it.get("city", ""))), default=0)

    by_row: dict[int, list[dict]] = {it["rowIdx"]: [] for it in items}
    extra: list[dict] = []
    for co in ordinary:
        best_item, best_score = None, 0
        for it in items:
            s = match_score(it, co)
            if s > best_score or (s == best_score and s and best_item is not None
                                  and longest(it) > longest(best_item)):
                best_item, best_score = it, s
        # Одной страны мало: без совпадения по городу или сайту карточка
        # села бы на первую попавшуюся строку той же страны.
        if best_item is None or best_score <= SCORE_COUNTRY:
            extra.append(co)
            continue
        entry = dict(co)
        entry["matchScore"] = best_score
        entry["matchedBy"] = ("ссылка из КП" if best_score >= SCORE_LINK else
                              "сайт и адрес" if best_score >= SCORE_SITE + SCORE_PLACE else
                              "сайт" if best_score >= SCORE_SITE else
                              "адрес" if best_score >= SCORE_PLACE else "адрес (нестрого)")
        entry["matchedCountry"] = co.get("regionCode") or ""
        by_row[best_item["rowIdx"]].append(entry)

    for row in by_row.values():
        row.sort(key=lambda c: (-int(c.get("matchScore") or 0), str(c.get("id"))))
    return {"byRow": by_row, "extra": extra, "chains": chains}


# ─── Сравнение полей ────────────────────────────────────────────────

def _verdict(kp_values: list[str], ya_values: list[str], key: Callable[[str], str]) -> str:
    """
    Одно слово про поле: совпадает / расходится / нет в Яндексе / нет в КП.

    Совпадением считаем пересечение множеств: у Яндекса телефонов бывает
    несколько, и достаточно, чтобы номер из КП был среди них.
    """
    kp = {key(v) for v in kp_values if key(v)}
    ya = {key(v) for v in ya_values if key(v)}
    if not kp and not ya:
        return "–"
    if not ya:
        return "нет в Яндексе"
    if not kp:
        return "нет в КП"
    if kp & ya:
        return "совпадает" if kp <= ya else "совпадает частично"
    return "расходится"


BAD_VERDICTS = ("расходится", "нет в Яндексе")


def compare(item: dict, companies: list[dict]) -> dict:
    """Сравнить строку КП с найденными организациями. Ничего не меняет."""
    sites, emails, phones = [], [], []
    for co in companies:
        sites += [u for u in (co.get("sites") or []) if u]
        emails += [e for e in (co.get("emails") or []) if e]
        phones += [p for p in (co.get("phones") or []) if p]

    res = {
        "count": len(companies),
        "site": _verdict([item.get("site", "")], sites, site_host),
        "email": _verdict(split_values(item.get("email", "")), emails, email_key),
        "phone": _verdict(item.get("phones") or [], phones, phone_key),
        "yaSites": sites, "yaEmails": emails, "yaPhones": phones,
    }

    # Разногласия и подсказки держим врозь. Пустая ссылка в КП – это не
    # ошибка, а ровно то, что сверка и заполняет: красить из-за неё всю
    # таблицу в «что-то не так» бессмысленно, там таких строк почти все.
    problems: list[str] = []
    hints: list[str] = []
    if not companies:
        res["status"] = "нет"
        problems.append("в Яндексе организации не нашлось")
    elif len(companies) > 1:
        res["status"] = "несколько"
        problems.append(f"в Яндексе {len(companies)} карточки – проверьте дубли")
    else:
        res["status"] = "найдена"

    kp_id = company_id_from_url(item.get("link", ""))
    ya_ids = {str(c.get("id") or "") for c in companies}
    if kp_id and ya_ids and kp_id not in ya_ids:
        problems.append(f"ссылка в КП ведёт на другую карточку ({kp_id})")
    if companies and not kp_id:
        hints.append("в КП нет ссылки на карточку – её можно взять из колонки «Я: ссылка»")

    for field, label in (("site", "сайт"), ("phone", "телефон"), ("email", "почта")):
        if res[field] in BAD_VERDICTS:
            problems.append(f"{label}: {res[field]}")
    for co in companies:
        if co.get("noAccess"):
            problems.append("карточка без доступа")
        if co.get("publishing") and co["publishing"] != "publish":
            problems.append(f"карточка не опубликована ({co['publishing']})")

    res["problems"] = problems
    res["hints"] = hints
    res["notes"] = problems + hints
    res["ok"] = res["status"] == "найдена" and not problems
    return res


# ─── Сборка отчёта ──────────────────────────────────────────────────

STATUS_MARK = {"найдена": "✅ найдена", "несколько": "❗ несколько", "нет": "❌ нет"}

# Колонки, которые дописываем справа к исходному КП.
EXTRA_HEADERS = [
    "Я: статус", "Я: карточек", "Я: название", "Я: адрес", "Я: сайт",
    "Я: телефоны", "Я: почта", "Я: соцсети", "Я: рубрики", "Я: ссылка",
    "Сайт", "Телефон", "Почта", "Что не так",
]


def card_url(company_id: str) -> str:
    return f"https://yandex.ru/sprav/{company_id}/p/edit/" if company_id else ""


def _join(values: Iterable[str]) -> str:
    seen, out = set(), []
    for v in values:
        v = str(v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return "\n".join(out)


def build(rows: list[list[str]], companies: list[dict]) -> dict:
    """
    Сверка целиком: разобрать КП, разложить организации, сравнить поля.

    Возвращает всё, что нужно и экрану, и выгрузке:
      sheet   – разбор листа,
      items   – строки КП с результатом сверки,
      extra   – организации, которым города в КП не нашлось,
      chains  – сетевые карточки (у них нет города, это «зонтик»),
      totals  – сводка цифрами.
    """
    sheet = parse_sheet(rows)
    if sheet["error"]:
        return {"sheet": sheet, "items": [], "extra": [], "chains": [],
                "totals": {}, "error": sheet["error"]}

    found = match(sheet["items"], companies)
    items = []
    totals = {"rows": len(sheet["items"]), "found": 0, "several": 0, "missing": 0,
              "mismatch": 0, "clean": 0, "noLink": 0, "extra": len(found["extra"]),
              "chains": len(found["chains"]), "companies": len(companies)}

    for it in sheet["items"]:
        cos = found["byRow"].get(it["rowIdx"], [])
        cmp = compare(it, cos)
        items.append({**it, "companies": cos, "cmp": cmp})
        if cmp["status"] == "нет":
            totals["missing"] += 1
        elif cmp["status"] == "несколько":
            totals["several"] += 1
            totals["found"] += 1
        else:
            totals["found"] += 1
        if cmp["ok"]:
            totals["clean"] += 1
        elif cmp["status"] != "нет":
            totals["mismatch"] += 1
        if cmp["hints"]:
            totals["noLink"] += 1

    return {"sheet": sheet, "items": items, "extra": found["extra"],
            "chains": found["chains"], "totals": totals, "error": ""}


def extra_cells(item: dict) -> list[str]:
    """Значения дописываемых колонок для одной строки КП."""
    cos = item.get("companies") or []
    cmp = item.get("cmp") or {}
    return [
        STATUS_MARK.get(cmp.get("status", ""), cmp.get("status", "")),
        str(len(cos)),
        _join(c.get("name", "") for c in cos),
        _join(c.get("address", "") for c in cos),
        _join(cmp.get("yaSites") or []),
        _join(cmp.get("yaPhones") or []),
        _join(cmp.get("yaEmails") or []),
        _join(f"{k}: {v}" for c in cos for k, v in (c.get("social") or {}).items()),
        _join(r for c in cos for r in (c.get("rubrics") or [])),
        _join(card_url(str(c.get("id") or "")) for c in cos),
        cmp.get("site", ""),
        cmp.get("phone", ""),
        cmp.get("email", ""),
        "; ".join(cmp.get("notes") or []),
    ]


def to_rows(rows: list[list[str]], result: dict) -> list[list[str]]:
    """
    Исходная таблица плюс новые колонки – ровно то, что просили: «то же КП,
    только со всей информацией из организаций».
    """
    sheet = result.get("sheet") or {}
    width = max(sheet.get("width", 0), max((len(r) for r in rows), default=0))
    by_row = {it["rowIdx"]: it for it in result.get("items") or []}
    header_idx = sheet.get("headerIdx", -1)

    out: list[list[str]] = []
    for i, row in enumerate(rows):
        line = [str(c) for c in row] + [""] * (width - len(row))
        if i == header_idx:
            line += EXTRA_HEADERS
        elif i in by_row:
            line += extra_cells(by_row[i])
        else:
            line += [""] * len(EXTRA_HEADERS)
        out.append(line)
    return out


EXTRA_SHEET_HEADERS = ["Название", "Адрес", "Сайт", "Телефоны", "Почта",
                       "Рубрики", "Публикация", "Ссылка", "Тип"]


def extra_rows(result: dict) -> list[list[str]]:
    """Организации Яндекса, которым в КП города не нашлось (и сетевые карточки)."""
    out = [list(EXTRA_SHEET_HEADERS)]
    for co in list(result.get("extra") or []) + list(result.get("chains") or []):
        out.append([
            co.get("name", ""),
            co.get("address", ""),
            _join(co.get("sites") or []),
            _join(co.get("phones") or []),
            _join(co.get("emails") or []),
            _join(co.get("rubrics") or []),
            co.get("publishing", ""),
            card_url(str(co.get("id") or "")),
            "сеть" if (co.get("type") == "chain") else "обычная",
        ])
    return out


def to_csv(rows: list[list[str]]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    for row in rows:
        w.writerow([str(c).replace("\n", " / ") for c in row])
    return buf.getvalue()


# Цвета плашек в Excel: те же, что на экране, чтобы глазами искалось одинаково.
FILL_OK = "D7F5DD"
FILL_WARN = "FFF3CD"
FILL_BAD = "F8D7DA"


def to_xlsx(rows: list[list[str]], result: dict, sheet_name: str = "Сверка") -> bytes:
    """Готовый .xlsx: лист со сверкой и лист с лишними организациями."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_name or "Сверка")[:31]
    body = to_rows(rows, result)
    for line in body:
        ws.append(line)

    sheet = result.get("sheet") or {}
    header_idx = sheet.get("headerIdx", -1)
    base_width = sheet.get("width", 0)
    status_col = base_width + 1                      # «Я: статус», 1-based
    fills = {"✅ найдена": FILL_OK, "❗ несколько": FILL_WARN, "❌ нет": FILL_BAD}

    if header_idx >= 0:
        for c in range(status_col, status_col + len(EXTRA_HEADERS)):
            cell = ws.cell(row=header_idx + 1, column=c)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="center")

    for i, line in enumerate(body, start=1):
        mark = line[status_col - 1] if status_col - 1 < len(line) else ""
        colour = fills.get(mark)
        if not colour:
            continue
        fill = PatternFill("solid", fgColor=colour)
        for c in range(status_col, status_col + len(EXTRA_HEADERS)):
            ws.cell(row=i, column=c).fill = fill
    for c in range(status_col, status_col + len(EXTRA_HEADERS)):
        ws.column_dimensions[ws.cell(row=1, column=c).column_letter].width = 26

    ws2 = wb.create_sheet("Лишние в Яндексе")
    for line in extra_rows(result):
        ws2.append(line)
    for cell in ws2[1]:
        cell.font = Font(bold=True)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
