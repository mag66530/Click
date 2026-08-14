"""
tests_zen.py – самопроверка Дзена: разбор статьи и механика отложки.

Запуск:  python tests_zen.py

Без сети и без браузера. Документ Word для проверки собирается прямо здесь,
из тех же кусков, что и настоящая статья заказчика: заголовок стилем
Heading2, подзаголовки Heading3, абзацы, маркированный список и таблица.
Так проверяется ровно то, что ломается на живых файлах, – а не выдуманный
идеальный случай.

Браузерную часть (zen_browser) проверяем в той мере, в какой она чистая:
пояс из окна отложки, сверка даты и времени, сборка HTML для вставки. Сам
Playwright тут не нужен и не запускается; если он не установлен вовсе, эти
проверки честно пропускаются, а разбор документа идёт как обычно.
"""

from __future__ import annotations

import sys
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import zen_doc  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED.append(name)
        print(f"  ❌ {name}" + (f" – {detail}" if detail else ""))


# ─── Сборка документа Word для проверки ─────────────────────────────
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _p(text: str, style: str = "", bold: bool = False, lst: bool = False) -> str:
    pPr = ""
    if style or lst:
        inner = f'<w:pStyle w:val="{style}"/>' if style else ""
        inner += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>' if lst else ""
        pPr = f"<w:pPr>{inner}</w:pPr>"
    rPr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f"<w:p>{pPr}<w:r>{rPr}<w:t>{text}</w:t></w:r></w:p>"


def _table(rows: list[list[str]]) -> str:
    trs = ""
    for r in rows:
        tcs = "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r)
        trs += f"<w:tr>{tcs}</w:tr>"
    return f"<w:tbl>{trs}</w:tbl>"


def make_docx() -> bytes:
    """Документ статьи – по образцу настоящего файла заказчика."""
    body = (
        _p("Медный провод плюс алюминиевый: жди беды или есть решение?", "Heading2", bold=True)
        + _p("Каждый, кто держал пассатижи, слышал правило: не скручивай медь с алюминием.")
        + _p("В чем корень зла", "Heading3", bold=True)
        + _p("Между металлами начинается гальваническая коррозия.")
        + _p("Постоянный нагрев в месте контакта.", lst=True)
        + _p("Искрение внутри распределительной коробки.", lst=True)
        + _table([["Характеристика", "Медь", "Алюминий"],
                  ["Электропроводность", "100%", "~60%"]])
        + _p("Главный вывод", "Heading3", bold=True)
        + _p("Напрямую скручивать нельзя, через клемму – можно.")
    )
    doc = f'<?xml version="1.0"?><w:document {W}><w:body>{body}</w:body></w:document>'
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


# ─── Разбор документа ───────────────────────────────────────────────
def test_parse_docx() -> None:
    print("Статья из документа Word")
    art = zen_doc.parse_docx(make_docx())

    check("заголовок статьи взят из Heading2",
          art["title"] == "Медный провод плюс алюминиевый: жди беды или есть решение?",
          art["title"])
    kinds = [b["kind"] for b in art["blocks"]]
    check("заголовок не попал в тело второй раз",
          all(b.get("text") != art["title"] for b in art["blocks"] if b["kind"] == "head"))
    check("подзаголовки распознаны", kinds.count("head") == 2, str(kinds))
    check("абзацы на месте", kinds.count("para") == 3, str(kinds))
    check("список собран в один блок", kinds.count("list") == 1, str(kinds))
    check("таблица отдельным блоком", kinds.count("table") == 1, str(kinds))

    lst = next(b for b in art["blocks"] if b["kind"] == "list")
    check("в списке два пункта", len(lst["items"]) == 2, str(lst["items"]))
    tbl = next(b for b in art["blocks"] if b["kind"] == "table")
    check("шапка таблицы прочитана", tbl["rows"][0] == ["Характеристика", "Медь", "Алюминий"],
          str(tbl["rows"][0]))
    check("данные таблицы прочитаны", tbl["rows"][1][1] == "100%", str(tbl["rows"][1]))
    check("порядок блоков сохранён – таблица стоит после списка",
          kinds.index("table") > kinds.index("list"), str(kinds))
    check("жалоб нет", not art["warnings"], str(art["warnings"]))

    info = zen_doc.counts(art)
    check("счётчик считает таблицу", info["table"] == 1 and info["chars"] > 100, str(info))


def test_parse_plain() -> None:
    print("Статья из обычного текста")
    art = zen_doc.parse_plain("Как выбрать швеллер\n\nПервый абзац.\n- пункт один\n- пункт два\n\nВывод.")
    check("первая строка – заголовок", art["title"] == "Как выбрать швеллер", art["title"])
    kinds = [b["kind"] for b in art["blocks"]]
    check("абзацы и список разобраны", kinds == ["para", "list", "para"], str(kinds))
    check("пункты списка без дефисов",
          next(b for b in art["blocks"] if b["kind"] == "list")["items"] == ["пункт один", "пункт два"])


def test_doc_links() -> None:
    print("Ссылка на документ")
    check("гугл-документ – ссылка",
          zen_doc.is_doc_link("https://docs.google.com/document/d/1QaBcDeFgHiJkLmNoPqRs/edit"))
    check("текст поста ссылкой не считается",
          not zen_doc.is_doc_link("Отгрузили трубы, подробности на сайте https://smu.ru"))
    check("пустая ячейка – не ссылка", not zen_doc.is_doc_link("   "))
    check("id документа",
          zen_doc.file_id("https://docs.google.com/document/d/1QaBcDeFgHiJkLmNoPqRsTu/edit")
          == "1QaBcDeFgHiJkLmNoPqRsTu")
    check("id файла на Диске",
          zen_doc.file_id("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStU/view")
          == "1AbCdEfGhIjKlMnOpQrStU")
    check("негугловая ссылка – без id",
          zen_doc.file_id("https://example.com/article.docx") == "")

    # Текст в ячейке – статья из него же, без похода в сеть.
    art = zen_doc.article_for({"text": "Заголовок\n\nТело статьи."})
    check("текст ячейки разобран без сети", art["title"] == "Заголовок", art["title"])
    try:
        zen_doc.article_for({"text": ""})
        check("пустая ячейка – понятная ошибка", False, "ошибки не было")
    except RuntimeError as e:
        check("пустая ячейка – понятная ошибка", "ссылки" in str(e).lower(), str(e))


def test_long_title() -> None:
    print("Слишком длинный заголовок")
    long = "Очень длинное название статьи. " * 20
    art = zen_doc.parse_plain(long + "\n\nТело.")
    check("заголовок обрезан по лимиту Дзена", len(art["title"]) <= zen_doc.TITLE_LIMIT,
          str(len(art["title"])))


# ─── Механика отложки (без браузера) ────────────────────────────────
def test_browser_logic() -> None:
    print("Отложка Дзена: пояс, сверка, HTML")
    try:
        import zen_browser as zb
    except ImportError as e:                  # playwright не установлен – не беда
        print(f"  ⏭ пропускаю: {e}")
        return

    # Пояс. Дзен пишет его в окне отложки, и «GMT+5» ровно совпадает с
    # Екатеринбургом – а вот московский аккаунт показал бы GMT+3.
    check("GMT+5 прочитан", zb.zone_offset("Опубликовать позже GMT+5") == 5)
    check("GMT+3 прочитан", zb.zone_offset("Часовой пояс GMT+3") == 3)
    check("GMT-2 прочитан", zb.zone_offset("GMT-2") == -2)
    check("пояса нет – None", zb.zone_offset("Опубликовать позже") is None)

    ekb = timezone(timedelta(hours=5))
    when = datetime(2026, 8, 29, 21, 40, tzinfo=ekb)
    same = zb.when_in_zone(when, 5)
    check("тот же пояс – время не трогаем", same.strftime("%d.%m %H:%M") == "29.08 21:40",
          same.isoformat())
    moscow = zb.when_in_zone(when, 3)
    check("московский пояс – время пересчитано",
          moscow.strftime("%d.%m %H:%M") == "29.08 19:40", moscow.isoformat())
    check("пояс неизвестен – ставим как есть", zb.when_in_zone(when, None) == when)

    # Сверка перед нажатием: цена ошибки – статья, вышедшая сейчас же.
    check("дата словами сошлась", zb.date_caption_ok("29 августа 2026", when))
    check("другой день не проходит", not zb.date_caption_ok("28 августа 2026", when))
    check("другой месяц не проходит", not zb.date_caption_ok("29 июля 2026", when))
    check("пустое поле не проходит", not zb.date_caption_ok("", when))
    check("время сошлось", zb.time_caption_ok("21:40", when))
    check("время не сошлось", not zb.time_caption_ok("18:49", when))

    # HTML для вставки: подзаголовки и списки должны доехать тегами, а
    # таблица – не доехать вовсе (она уходит картинкой).
    art = zen_doc.parse_docx(make_docx())
    html = zb.blocks_to_html(art["blocks"])
    check("подзаголовки тегами", "<h3>" in html, html[:120])
    check("список тегами", "<ul><li>" in html)
    check("абзацы тегами", "<p>" in html)
    check("таблицы в HTML нет – она пойдёт картинкой", "<table" not in html)

    tbl = next(b for b in art["blocks"] if b["kind"] == "table")
    page = zb.table_html(tbl["rows"])
    check("картинка таблицы: шапка отдельно", "<th>Характеристика</th>" in page)
    check("картинка таблицы: данные на месте", "<td>100%</td>" in page)


def test_session_source(tmp: Path | None = None) -> None:
    print("Вход в Дзен берётся от Яндекс.Бизнеса")
    try:
        import zen_browser as zb
    except ImportError as e:
        print(f"  ⏭ пропускаю: {e}")
        return
    import json
    import tempfile

    import paths
    import yb_playwright as yb

    with tempfile.TemporaryDirectory() as d:
        old_root = paths.data_root
        old_yb = getattr(yb, "USERS_DATA", None)
        try:
            paths.data_root = lambda: Path(d)                      # noqa: E731
            yb.USERS_DATA = Path(d)
            pid = "SMU"
            check("без сессий входа нет", not zb.has_saved_session(pid))

            # Кладём яндексовую сессию ЯБ – Дзен обязан ею воспользоваться.
            yb_file = yb.session_path(pid)
            yb_file.write_text(json.dumps({"cookies": [
                {"name": "session_id", "value": "x", "domain": ".yandex.ru"}]}),
                encoding="utf-8")
            check("сессия ЯБ годится для Дзена", zb.has_saved_session(pid))
            check("берём именно её", zb.source_session(pid) == yb_file)
            check("человеку сказано, откуда вход", "Яндекс.Бизнеса" in zb.session_note(pid),
                  zb.session_note(pid))

            # Появилась своя дзеновская – она главнее.
            own = zb.session_path(pid)
            own.write_text(json.dumps({"cookies": [
                {"name": "session_id", "value": "y", "domain": ".yandex.ru"}]}),
                encoding="utf-8")
            check("своя сессия главнее", zb.source_session(pid) == own)

            # Анонимные куки за вход не считаем: с ними в студию не пустят.
            own.write_text(json.dumps({"cookies": [
                {"name": "yandexuid", "value": "z", "domain": ".yandex.ru"}]}),
                encoding="utf-8")
            yb_file.unlink()
            check("анонимные куки – это не вход", not zb.has_saved_session(pid))
        finally:
            paths.data_root = old_root
            if old_yb is not None:
                yb.USERS_DATA = old_yb


def test_registry_wiring() -> None:
    print("Дзен в реестре и в формировании")
    import content_plan as cp
    import crosspost_form
    import crosspost_state as cps

    check("дзен – поддерживаемая площадка", "zen" in cp.SUPPORTED)
    check("дзен берёт статьи", "zen" in cp.ARTICLE_NETWORKS)
    check("имя площадки по-русски", cps.network_ru("zen") == "Дзен")
    check("формирование дзена есть", hasattr(crosspost_form, "form_zen_all"))

    # Статья уходит только в Дзен; видео – никуда.
    def row(fmt: str, nets: tuple[str, ...]) -> dict:
        return {"brand": "SMU", "date": "2099-01-10", "time": "11:00", "when": "x",
                "post_type": "", "format": fmt, "text": "https://docs.google.com/document/d/1x/edit",
                "images": [], "row": 1,
                "targets": [{"network": n, "raw": n, "published_link": ""} for n in nets]}

    art = cp.posts_to_form([row("Статья", ("vk", "zen"))], today=None)
    check("у статьи остаётся один адресат",
          art and [t["network"] for t in art[0]["targets"]] == ["zen"],
          str(art))
    video = cp.posts_to_form([row("Видео", ("vk", "zen"))], today=None)
    check("видео не формируем вовсе", video == [], str(video))
    post = cp.posts_to_form([row("Пост", ("vk", "zen"))], today=None)
    check("обычный пост идёт и в ВК, и в Дзен",
          post and sorted(t["network"] for t in post[0]["targets"]) == ["vk", "zen"],
          str(post))


def main() -> int:
    print("═" * 60)
    test_parse_docx()
    test_parse_plain()
    test_doc_links()
    test_long_title()
    test_browser_logic()
    test_session_source()
    test_registry_wiring()
    print("\n" + "═" * 60)
    if FAILED:
        print(f"  ПРОВАЛЕНО {len(FAILED)} из {PASSED + len(FAILED)}:")
        for name in FAILED:
            print(f"    • {name}")
        return 1
    print(f"  ВСЁ ХОРОШО – {PASSED} проверок пройдено")
    print("═" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
