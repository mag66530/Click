"""
content_plan.py — чтение реестра постов (контент-плана) из таблицы бренда.

Зачем. Кросспостинг исполняет реестр: строки с датой, текстом, картинкой и
списком соцсетей. Раньше это была памятка для человека — теперь исполняемый
план. Этот модуль превращает таблицу в список постов, из которых планировщик
делает отложки и задания.

Откуда данные. Та же Google-таблица и тот же сервисный аккаунт, что у КП
(`kp_sheet.py`): заказчик расшаривает файл реестра на него Читателем — новых
ключей не нужно. Для локальной проверки читаем и обычный .xlsx с диска.

Структура реестра (файл заказчика, разобран 2026-08-07). Лист на бренд
(СМУ/ИМП/МПЭ/АПС/…). Пост — БЛОК строк:
  • первая строка блока несёт ДАТУ, ТЕКСТ, ФОТО, ТИП;
  • каждая строка блока (включая первую) — одна СОЦСЕТЬ (колонка «Соцсеть»)
    со своей колонкой «Ссылка» на опубликованный пост;
  • у будущих постов «Ссылка» пуста — это и есть «ещё не сформировано».
Справа в листе отдельный блок статистики — его не трогаем (читаем только
колонки, найденные по заголовкам «Когда выложить», «Соцсеть», «Ссылка»,
«Формат», «Тип», «Пост», «Фото»).

Время. В реестре только дата. Час выхода — из настройки бренда (заводские
значения ниже), по Екатеринбургу — как всё видимое время в Click (`apptime`).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable

import apptime

# Метка сборки — одна на всё приложение (см. build.py). streamlit_app сверяет
# её и перезагружает модуль при расхождении.
from build import BUILD  # noqa: F401

# ─── Бренды ─────────────────────────────────────────────────────────
# Заводской час публикации на бренд (Екатеринбург). Ответ заказчика 2026-08-07.
# Правится в «Настройках»; переопределяется колонкой времени в реестре, если
# заказчик её заведёт.
DEFAULT_TIMES = {
    "SMU": "11:00",
    "IMP": "09:00",
    "MPE": "10:00",
    "MPI": "09:30",
    "APS": "10:30",
}

# Название листа в реестре → код проекта Click. Лист МПИ заказчик добавит —
# парсер к нему готов, отдельного кода не нужно.
SHEET_TO_BRAND = {
    "СМУ": "SMU",
    "ИМП": "IMP",
    "МПЭ": "MPE",
    "МПИ": "MPI",
    "АПС": "APS",
}

# ─── Соцсети ────────────────────────────────────────────────────────
# Как площадка называется в реестре → канонический код цели. У Телеграма два
# канала на бренд, поэтому tg-staff и tg-client различаем.
SUPPORTED = ("vk", "ok", "tg-staff", "tg-client", "tg", "max")


def canonical_network(raw: str) -> str:
    """Название соцсети из реестра → код цели. Пусто, если площадка не наша."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if "вконтакт" in s or s == "вк":
        return "vk"
    if "одноклас" in s or s == "ок":            # в реестре встречается «Однокласники»
        return "ok"
    if "telegram" in s or "телеграм" in s or s == "тг":
        if "сотруд" in s:
            return "tg-staff"
        if "клиент" in s:
            return "tg-client"
        return "tg"
    if "max" in s or "макс" in s:
        return "max"
    if "дзен" in s or "zen" in s:
        return "zen"                            # площадка вне scope — распознаём, но не постим
    return ""


# ─── Заголовки колонок ──────────────────────────────────────────────
# Колонку ищем по заголовку, а не по букве: так порядок колонок можно менять.
_COL_MATCHERS: dict[str, Callable[[str], bool]] = {
    "date":  lambda s: "когда выложить" in s or s == "дата",
    "net":   lambda s: "соцсет" in s or "соц.сет" in s,
    "link":  lambda s: s == "ссылка",
    "format": lambda s: "формат" in s,
    "type":  lambda s: s == "тип",
    "text":  lambda s: s == "пост" or s == "текст",
    "photo": lambda s: "фото" in s or "картинк" in s,
}


def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]]:
    """Строка заголовков и карта «поле → индекс колонки». (-1, {}) — не нашли."""
    for i, row in enumerate(rows[:30]):        # шапка всегда наверху
        norm = [(c or "").strip().lower() for c in row]
        has_date = any(_COL_MATCHERS["date"](c) for c in norm)
        has_net = any(_COL_MATCHERS["net"](c) for c in norm)
        if not (has_date and has_net):
            continue
        cols: dict[str, int] = {}
        for field, match in _COL_MATCHERS.items():
            for j, c in enumerate(norm):
                if match(c):
                    cols[field] = j
                    break
        return i, cols
    return -1, {}


# ─── Дата и время ───────────────────────────────────────────────────
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                 "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y")


def parse_date(value: str) -> date | None:
    """Дата из ячейки. Разные источники пишут её по-разному — пробуем набор форматов."""
    s = (value or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def brand_default_time(brand: str) -> str:
    return DEFAULT_TIMES.get(brand, "10:00")


def when_iso(d: date, hhmm: str) -> str:
    """Дата + час бренда → ISO с поясом Екатеринбурга («2026-08-12T11:00:00+05:00»)."""
    hh, mm = (hhmm or "10:00").split(":")[:2]
    dt = datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=apptime.TZ)
    return dt.isoformat()


# ─── Картинки ───────────────────────────────────────────────────────
def _split_images(cell: str) -> list[str]:
    """Одна или несколько ссылок на фото из ячейки (разделители: перенос, пробел, запятая, ;)."""
    parts = re.split(r"[\s,;]+", (cell or "").strip())
    return [p for p in parts if p.startswith("http")]


# ─── Разбор листа ───────────────────────────────────────────────────
def parse_sheet(rows: list[list[str]], brand: str) -> list[dict]:
    """
    Лист реестра → список постов. Каждый пост:
      {brand, date (ISO), time (HH:MM), when (ISO+пояс), post_type, format,
       text, images:[...], targets:[{network, raw, published_link}], row}
    Возвращаются ВСЕ посты (и прошлые, и будущие); что именно формировать —
    решает posts_to_form(). Так проще и тестировать, и показывать план целиком.
    """
    hdr, cols = _find_header(rows)
    if hdr < 0 or "date" not in cols or "net" not in cols:
        return []

    def cell(row: list[str], field: str) -> str:
        j = cols.get(field, -1)
        return (row[j].strip() if 0 <= j < len(row) else "")

    time = brand_default_time(brand)
    posts: list[dict] = []
    cur: dict | None = None

    for idx in range(hdr + 1, len(rows)):
        row = rows[idx]
        d = parse_date(cell(row, "date"))
        net_raw = cell(row, "net")

        if d is not None:
            # новая строка с датой — начало нового блока-поста
            cur = {
                "brand": brand,
                "date": d.isoformat(),
                "time": time,
                "when": when_iso(d, time),
                "post_type": cell(row, "type"),
                "format": cell(row, "format") or "Пост",
                "text": cell(row, "text"),
                "images": _split_images(cell(row, "photo")),
                "targets": [],
                "row": idx + 1,               # 1-based, как в редакторе таблиц
            }
            posts.append(cur)

        if net_raw and cur is not None:
            cur["targets"].append({
                "network": canonical_network(net_raw),
                "raw": net_raw,
                "published_link": cell(row, "link"),
            })

    return [p for p in posts if p["targets"]]


# ─── Что формировать ────────────────────────────────────────────────
def posts_to_form(posts: list[dict], today: date | None = None) -> list[dict]:
    """
    Отобрать посты и цели, которые ещё надо сформировать:
      • дата сегодня или впереди;
      • формат «Пост» (видео/статьи — вне scope);
      • есть текст;
      • цель — наша площадка (SUPPORTED) и «Ссылка» по ней пуста.
    Возвращает копии постов с урезанным targets (только несформированные цели).
    """
    if today is None:
        today = apptime.now().date()
    out: list[dict] = []
    for p in posts:
        d = parse_date(p["date"])
        if d is None or d < today:
            continue
        if (p.get("format") or "Пост").strip().lower() != "пост":
            continue
        if not (p.get("text") or "").strip():
            continue
        pending = [t for t in p["targets"]
                   if t["network"] in SUPPORTED and not (t.get("published_link") or "").strip()]
        if pending:
            out.append({**p, "targets": pending})
    return out


# ─── Живой источник: Google-таблица (тот же аккаунт, что у КП) ───────
def load_from_google(sheet_url: str, brand: str) -> list[dict]:
    """
    Прочитать лист бренда из Google-таблицы реестра через сервисный аккаунт КП.
    Требует настроенного gcp_service_account (см. kp_sheet). Бросает RuntimeError.
    """
    import kp_sheet
    sa = kp_sheet.service_account_info()
    if not sa:
        raise RuntimeError("Нет доступа к Google: задайте gcp_service_account (как для КП).")
    fid = kp_sheet.sheet_id(sheet_url)
    if not fid:
        raise RuntimeError("Не разобрал ссылку на таблицу реестра.")
    titles, read, _ = kp_sheet.open_book(fid, sa, kp_sheet.sheet_gid(sheet_url))
    title = _pick_brand_sheet(titles, brand)
    if not title:
        raise RuntimeError(f"В таблице нет листа для бренда {brand}. Листы: {', '.join(titles)}")
    return parse_sheet(read(title), brand)


def load_from_xlsx(path: str, brand: str) -> list[dict]:
    """Прочитать лист бренда из .xlsx на диске — для локальной проверки."""
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        title = _pick_brand_sheet(wb.sheetnames, brand)
        if not title:
            raise RuntimeError(f"В файле нет листа для бренда {brand}. Листы: {', '.join(wb.sheetnames)}")
        ws = wb[title]
        rows = [[("" if c.value is None else str(c.value)) for c in row] for row in ws.iter_rows()]
    finally:
        wb.close()
    return parse_sheet(rows, brand)


def _pick_brand_sheet(titles: list[str], brand: str) -> str:
    """Название листа для бренда: сначала точное соответствие, потом по коду."""
    for t in titles:
        if SHEET_TO_BRAND.get(t.strip()) == brand:
            return t
    for t in titles:
        if t.strip().upper() == brand:
            return t
    return ""
