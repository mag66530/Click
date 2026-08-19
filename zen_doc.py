"""
zen_doc.py – статья для Дзена: взять текст по ссылке и разобрать на блоки.

Зачем отдельный модуль. У постов в соцсети текст лежит прямо в ячейке
реестра. У статей Дзена – нет: в колонке «Пост» стоит ССЫЛКА на документ
(Google Документ или .docx на Диске), а сама статья это лонгрид на 5–7
тысяч знаков с подзаголовками, списками и таблицей. Пихать такое в ячейку
таблицы никто не станет, и правильно.

Поэтому здесь две обязанности, и обе – чистая логика без браузера:
    1) достать документ по ссылке (тем же сервисным аккаунтом, что читает
       реестр и КП – новых ключей не нужно);
    2) превратить его в БЛОКИ, которые редактор Дзена умеет принимать:
       заголовок, абзацы, подзаголовки, списки, таблицы.

Почему блоки, а не сплошной текст. Редактор Дзена – блочный: каждый абзац
это свой элемент, подзаголовок ставится отдельной командой, таблицу он
вовсе не умеет вставлять из буфера. Вылить туда одну простыню – значит
получить статью без единого подзаголовка, где списки слиплись с текстом.
Разбор здесь, вставка – в zen_browser.

Что понимаем в документе (проверено на статье заказчика, 14.08.2026):
    Heading1/Heading2 – заголовок статьи (первый такой) и крупные разделы;
    Heading3/Heading4 – подзаголовки внутри статьи;
    абзац со списком   – маркированный или нумерованный пункт;
    таблица            – отдельным блоком (в Дзен уходит картинкой);
    жирные куски       – разметкой Click (**…**), как в реестре.

Разбор docx делаем сами (zipfile + XML), без python-docx: лишняя
зависимость в облаке – лишний риск при пересборке окружения, а нужного нам
тут на сотню строк.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

import post_text

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Картинки в .docx: сама картинка лежит в word/media, а в тексте на неё
# ссылается <a:blip r:embed="rId…"> внутри <w:drawing>. rId → файл берём из
# word/_rels/document.xml.rels.
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

# Сколько знаков влезает в заголовок Дзена. Длиннее площадка не примет
# (обрежет молча), поэтому режем сами и говорим об этом человеку.
TITLE_LIMIT = 200


# ════════════════════════════════════════════════════════════════════
#  ССЫЛКА НА ДОКУМЕНТ
# ════════════════════════════════════════════════════════════════════

def is_doc_link(text: str) -> bool:
    """
    Текст в реестре – это ссылка на документ, а не сам текст поста?

    Проверяем строго: вся ячейка целиком одна ссылка. Пост, внутри которого
    просто есть ссылка на сайт, документом считать нельзя – это обычный
    текст, и качать по нему нечего.
    """
    s = (text or "").strip()
    if not s or " " in s or "\n" in s:
        return False
    return s.startswith("http://") or s.startswith("https://")


def file_id(url: str) -> str:
    """
    ID файла из ссылки Google: Документ, файл на Диске или ?id=… .

    Пусто – ссылка не гугловая (тогда качаем обычным запросом).
    """
    s = (url or "").strip()
    for rx in (r"/document/d/([A-Za-z0-9_\-]{20,})",
               r"/file/d/([A-Za-z0-9_\-]{20,})",
               r"/spreadsheets/d/([A-Za-z0-9_\-]{20,})",
               r"[?&]id=([A-Za-z0-9_\-]{20,})"):
        m = re.search(rx, s)
        if m:
            return m.group(1)
    return ""


def _google_bytes(fid: str) -> bytes:
    """
    Скачать документ Google сервисным аккаунтом – тем же, что читает КП.

    Гугловый Документ отдаётся только через export (файла как такового на
    Диске нет), залитый .docx – обычным alt=media. Что именно перед нами,
    спрашиваем у Drive заранее: угадывать дороже, чем один запрос.
    """
    import requests

    import kp_sheet
    sa = kp_sheet.service_account_info()
    if not sa:
        raise RuntimeError("Нет доступа к Google: задайте gcp_service_account (как для КП).")
    headers = kp_sheet._auth_headers(sa)
    mime = kp_sheet._file_mime(fid, headers)

    if mime == GOOGLE_DOC_MIME or not mime:
        r = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}/export",
                         params={"mimeType": DOCX_MIME}, headers=headers, timeout=90)
    else:
        r = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}",
                         params={"alt": "media", "supportsAllDrives": "true"},
                         headers=headers, timeout=90)
    if r.status_code == 403:
        raise RuntimeError(
            "Google отказал (403). Расшарен ли документ статьи на "
            f"{sa.get('client_email', 'сервисный аккаунт')} как Читатель? "
            "Реестр расшарен, а документ – отдельный файл, доступ у него свой.")
    if r.status_code == 404:
        raise RuntimeError("Google не нашёл документ по ссылке (404). Ссылка верная?")
    if r.status_code != 200:
        raise RuntimeError(f"Google вернул HTTP {r.status_code} на документ статьи.")
    return r.content


def fetch_bytes(url: str) -> bytes:
    """
    Документ по ссылке: гугловый – аккаунтом, любой другой – как есть.

    Любая беда наружу выходит одним видом – RuntimeError с причиной словами.
    Это не формальность: формирование идёт циклом по постам, и сетевой сбой
    на одном документе не должен ронять весь прогон. Один пост помечается
    ошибкой, остальные формируются дальше.
    """
    import requests

    try:
        fid = file_id(url)
        if fid:
            return _google_bytes(fid)
        r = requests.get(url, timeout=90)
        if r.status_code != 200:
            raise RuntimeError(f"Документ статьи не скачался: HTTP {r.status_code}")
        return r.content
    except RuntimeError:
        raise
    except requests.RequestException as e:
        raise RuntimeError(f"Документ статьи не скачался: {type(e).__name__} – "
                           "проверьте ссылку и связь с интернетом") from e
    except Exception as e:  # noqa: BLE001 – ключ Google, разбор JSON и прочее
        raise RuntimeError(f"Документ статьи не прочитался: {e}") from e


# ════════════════════════════════════════════════════════════════════
#  РАЗБОР DOCX
# ════════════════════════════════════════════════════════════════════

def _runs(para: ET.Element) -> list[tuple[str, bool]]:
    """
    Куски абзаца (текст, жирный?) – ровно как их отдаёт реестр.

    Жирное из документа доезжает до Дзена той же разметкой Click (**…**),
    что и жирное из ячеек: одна разметка на всё приложение, и рендерить её
    умеет post_text.
    """
    out: list[tuple[str, bool]] = []
    for r in para.findall(W + "r"):
        rPr = r.find(W + "rPr")
        bold = rPr is not None and rPr.find(W + "b") is not None
        text = "".join(t.text or "" for t in r.findall(W + "t"))
        # Разрыв строки внутри абзаца (Shift+Enter) – это пробел, а не склейка
        # слов: без этого «конецначало» слипается в одно слово.
        if r.find(W + "br") is not None:
            text += " "
        if text:
            out.append((text, bold))
    return out


def _style(para: ET.Element) -> str:
    pPr = para.find(W + "pPr")
    if pPr is None:
        return ""
    s = pPr.find(W + "pStyle")
    return (s.get(W + "val") or "") if s is not None else ""


def _is_list(para: ET.Element) -> bool:
    pPr = para.find(W + "pPr")
    return pPr is not None and pPr.find(W + "numPr") is not None


def _heading_level(style: str) -> int:
    """Heading2 → 2, «Заголовок 3» → 3. 0 – не заголовок."""
    m = re.search(r"(\d)", style or "")
    if not m:
        return 0
    if not re.match(r"(?i)heading|заголов|title", style or ""):
        return 0
    return int(m.group(1))


def _image_rels(z: zipfile.ZipFile) -> dict:
    """rId → путь к файлу картинки внутри архива (word/media/imageN.png)."""
    try:
        rels = ET.fromstring(z.read("word/_rels/document.xml.rels"))
    except (KeyError, ET.ParseError):
        return {}
    out: dict[str, str] = {}
    for rel in rels.findall(RELS + "Relationship"):
        target = rel.get("Target") or ""
        if "image" in (rel.get("Type") or "").lower() or "/media/" in target:
            # Target вида «media/image1.png» относительно word/.
            path = target if target.startswith("word/") else "word/" + target.lstrip("./")
            out[rel.get("Id") or ""] = path
    return out


def _para_images(para: ET.Element, rels: dict, z: zipfile.ZipFile,
                 warnings: list[str]) -> list[dict]:
    """Картинки этого абзаца → блоки {"kind":"image", "data_b64":…, "ext":…}."""
    import base64
    out: list[dict] = []
    for blip in para.iter(A + "blip"):
        rid = blip.get(R + "embed") or blip.get(R + "link") or ""
        path = rels.get(rid)
        if not path:
            continue
        try:
            raw = z.read(path)
        except KeyError:
            continue
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
        if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
            ext = "png"
        out.append({"kind": "image", "data_b64": base64.b64encode(raw).decode(),
                    "ext": ext})
    return out


def _table_rows(tbl: ET.Element) -> list[list[str]]:
    """Таблица → строки ячеек простым текстом (для картинки разметка не нужна)."""
    rows: list[list[str]] = []
    for tr in tbl.findall(W + "tr"):
        cells: list[str] = []
        for tc in tr.findall(W + "tc"):
            # Ячейка может состоять из нескольких абзацев – склеиваем через
            # пробел: в картинке таблицы переносы всё равно расставит вёрстка.
            parts = [" ".join("".join(t.text or "" for t in p.iter(W + "t")).split())
                     for p in tc.findall(W + "p")]
            cells.append(" ".join(x for x in parts if x).strip())
        if cells:
            rows.append(cells)
    return rows


def parse_docx(raw: bytes) -> dict:
    """
    .docx → {"title": "…", "blocks": [...], "warnings": [...]}.

    Блоки – то, что редактор Дзена умеет принять по отдельности:
        {"kind": "para",  "markup": "текст с **жирным**"}
        {"kind": "head",  "text": "Подзаголовок", "level": 2|3}
        {"kind": "list",  "items": ["пункт", …]}
        {"kind": "table", "rows": [["…", "…"], …]}

    Заголовок статьи – первый заголовочный абзац (Heading1/2). Нет такого –
    берём первый непустой абзац: в документах заказчика заголовок всегда
    идёт первой строкой, и лучше взять его, чем оставить статью безымянной.
    """
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise RuntimeError(f"Это не документ Word: {e}") from e

    body = root.find(W + "body")
    if body is None:
        raise RuntimeError("В документе нет текста.")

    rels = _image_rels(z)
    title = ""
    blocks: list[dict] = []
    warnings: list[str] = []
    list_buf: list[str] = []

    def flush_list() -> None:
        if list_buf:
            blocks.append({"kind": "list", "items": list(list_buf)})
            list_buf.clear()

    for el in list(body):
        tag = el.tag.replace(W, "")

        if tag == "tbl":
            flush_list()
            rows = _table_rows(el)
            if rows:
                blocks.append({"kind": "table", "rows": rows})
            continue

        if tag != "p":
            continue

        # Картинка идёт на своём месте в тексте – блоком там же, где стоит в
        # документе. Абзац с картинкой обычно без текста; если текст есть,
        # картинка встаёт перед ним.
        imgs = _para_images(el, rels, z, warnings) if rels else []
        if imgs:
            flush_list()
            blocks.extend(imgs)

        runs = _runs(el)
        markup = post_text.runs_to_markup(runs).strip()
        if not markup:
            continue

        level = _heading_level(_style(el))
        if _is_list(el):
            list_buf.append(post_text.strip_markup(markup))
            continue

        flush_list()

        if level and not title:
            # Первый заголовок в документе – название статьи, а не раздел.
            title = post_text.strip_markup(markup)
            continue
        if level:
            blocks.append({"kind": "head", "text": post_text.strip_markup(markup),
                           "level": min(max(level, 2), 3)})
            continue

        if not title and not blocks:
            # Заголовков стилями нет вовсе – первая строка работает названием.
            title = post_text.strip_markup(markup)
            continue

        blocks.append({"kind": "para", "markup": markup})

    flush_list()

    if len(title) > TITLE_LIMIT:
        warnings.append(f"Заголовок длиннее {TITLE_LIMIT} знаков – обрезан.")
        title = title[:TITLE_LIMIT].rstrip()
    if not title:
        warnings.append("В документе не нашёлся заголовок статьи.")
    if not blocks:
        warnings.append("В документе нет текста статьи – только заголовок.")

    return {"title": title, "blocks": blocks, "warnings": warnings}


def parse_plain(text: str, title: str = "") -> dict:
    """
    Статья из обычного текста (когда в реестре не ссылка, а сам текст).

    Заголовок – первая строка, тело – всё остальное. Пустые строки делят
    абзацы; строки, начинающиеся с «-» или «•», собираются в список.
    """
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n")]
    lines = [ln for ln in lines]
    blocks: list[dict] = []
    list_buf: list[str] = []

    def flush_list() -> None:
        if list_buf:
            blocks.append({"kind": "list", "items": list(list_buf)})
            list_buf.clear()

    head = title.strip()
    body = lines
    if not head:
        for i, ln in enumerate(lines):
            if ln:
                head = post_text.strip_markup(ln)
                body = lines[i + 1:]
                break

    for ln in body:
        if not ln:
            flush_list()
            continue
        if re.match(r"^[-–—•*]\s+", ln):
            list_buf.append(post_text.strip_markup(re.sub(r"^[-–—•*]\s+", "", ln)))
            continue
        flush_list()
        blocks.append({"kind": "para", "markup": ln})
    flush_list()

    return {"title": head[:TITLE_LIMIT], "blocks": blocks, "warnings": []}


# ════════════════════════════════════════════════════════════════════
#  ЕДИНАЯ ТОЧКА ВХОДА
# ════════════════════════════════════════════════════════════════════

def article_for(post: dict) -> dict:
    """
    Статья для поста реестра: скачать по ссылке либо взять текст ячейки.

    Возвращает то же, что parse_docx. Бросает RuntimeError с причиной
    словами – формирование покажет её в строке поста, и человек сразу
    поймёт, что чинить: доступ к документу или саму ссылку.
    """
    text = (post.get("text") or "").strip()
    if not text:
        raise RuntimeError("В реестре пусто: ни текста статьи, ни ссылки на документ.")
    if not is_doc_link(text):
        return parse_plain(text)

    raw = fetch_bytes(text)
    if raw[:2] == b"PK":
        return parse_docx(raw)
    # Не архив – значит отдали простой текст (или HTML вместо файла).
    try:
        return parse_plain(raw.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise RuntimeError("По ссылке лежит не документ Word и не текст. "
                           "Нужен Google Документ или .docx.") from e


def plain_preview(article: dict, limit: int = 400) -> str:
    """Начало статьи словами – для строки лога и сверки в интерфейсе."""
    parts: list[str] = []
    for b in article.get("blocks", []):
        if b["kind"] == "para":
            parts.append(post_text.strip_markup(b["markup"]))
        elif b["kind"] == "head":
            parts.append(b["text"])
        elif b["kind"] == "list":
            parts.append("; ".join(b["items"]))
        elif b["kind"] == "table":
            parts.append(f"[таблица {len(b['rows'])}×"
                         f"{max((len(r) for r in b['rows']), default=0)}]")
    s = " ".join(" ".join(parts).split())
    return s[:limit] + ("…" if len(s) > limit else "")


def counts(article: dict) -> dict:
    """Сколько чего в статье – для лога: «12 абзацев, 2 списка, 1 таблица»."""
    out = {"para": 0, "head": 0, "list": 0, "table": 0, "image": 0, "chars": 0}
    for b in article.get("blocks", []):
        out[b["kind"]] = out.get(b["kind"], 0) + 1
    out["chars"] = len(plain_preview(article, limit=10 ** 9))
    return out
