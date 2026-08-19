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


def make_docx_with_image() -> bytes:
    """Документ с картинкой на своём месте – как у заказчика: вводный абзац,
    затем картинка, затем подзаголовок и текст."""
    W2 = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')
    # Абзац с картинкой: <w:drawing> с <a:blip r:embed="rId6">.
    pic = ('<w:p><w:r><w:drawing><a:blip r:embed="rId6"/>'
           '</w:drawing></w:r></w:p>')
    body = (_p("Латунь это сплав каких металлов?", "Heading2", bold=True)
            + _p("Увидели блестящий самовар? За блеском скрывается больше.")
            + pic
            + _p("Главный секрет", "Heading3", bold=True)
            + _p("В основе латуни союз двух металлов."))
    doc = f'<?xml version="1.0"?><w:document {W2}><w:body>{body}</w:body></w:document>'
    rels = ('<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId6" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            'Target="media/image1.png"/></Relationships>')
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
           b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
        z.writestr("word/media/image1.png", png)
    return buf.getvalue()


def test_doc_image() -> None:
    print("Картинка из документа: разбор и метки на своих местах")
    art = zen_doc.parse_docx(make_docx_with_image())
    kinds = [b["kind"] for b in art["blocks"]]
    check("картинка стала отдельным блоком", kinds.count("image") == 1, str(kinds))
    check("картинка стоит между вводным абзацем и подзаголовком",
          kinds[:3] == ["para", "image", "head"], str(kinds))
    img = next(b for b in art["blocks"] if b["kind"] == "image")
    check("у картинки есть данные и формат", bool(img.get("data_b64")) and img.get("ext") == "png")

    try:
        import zen_browser as zb
    except ImportError as e:
        print(f"  ⏭ дальше пропускаю (нет browser-части): {e}")
        return

    # Метки в HTML и список медиа идут в одном порядке и совпадают токенами.
    html = zb.blocks_to_html(art["blocks"])
    media = zb.media_items(art["blocks"])
    check("в тексте одна метка медиа", html.count("⟦МЕДИА-1⟧") == 1, html[:200])
    check("медиа-элемент один, с тем же токеном",
          len(media) == 1 and media[0]["token"] == zb.media_token(1), str(media[:1]))
    check("метка стоит после вводного абзаца, до подзаголовка",
          html.index("Увидели") < html.index("⟦МЕДИА-1⟧") < html.index("Главный секрет"))

    # Таблица тоже получает свою метку и идёт следующим номером.
    with2 = {"blocks": [{"kind": "para", "markup": "а"},
                        {"kind": "image", "data_b64": "x", "ext": "png"},
                        {"kind": "para", "markup": "б"},
                        {"kind": "table", "rows": [["к"]]}]}
    media2 = zb.media_items(with2["blocks"])
    check("две метки: картинка ⟦1⟧ и таблица ⟦2⟧",
          [m["token"] for m in media2] == [zb.media_token(1), zb.media_token(2)],
          str([m["token"] for m in media2]))
    html2 = zb.blocks_to_html(with2["blocks"])
    check("метки в тексте по порядку",
          html2.index("⟦МЕДИА-1⟧") < html2.index("⟦МЕДИА-2⟧"))


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


class FakeLocator:
    """Локатор-заглушка: считает нажатия и умеет говорить, сколько нашлось."""

    def __init__(self, page, found: int = 1):
        self.page = page
        self.found = found

    @property
    def first(self):
        return self

    def count(self) -> int:
        return self.found

    def is_visible(self) -> bool:
        return bool(self.found)

    def click(self, **kw) -> None:
        if not self.found:
            raise RuntimeError("нечего нажимать")
        self.page.clicks.append(self.page.pending)

    def locator(self, _sel: str):
        return self

    def input_value(self) -> str:
        return self.page.values.get("date", "")


class FakePage:
    """
    Страница Дзена на бумаге: текст, набор найденных селекторов и журнал
    нажатий. Этого хватает, чтобы проверить логику входа без браузера –
    ровно ту, на которой первый живой прогон и споткнулся.
    """

    def __init__(self, text: str = "", has: tuple[str, ...] = (), url: str = "https://dzen.ru/x"):
        self.text = text
        self.has = has
        self.url = url
        self.clicks: list[str] = []
        self.pending = ""
        self.values: dict[str, str] = {}
        self.buttons: tuple[str, ...] = ()
        # После нажатия на аккаунт экран выбора уходит – как в жизни.
        self.text_after_click = None

    def evaluate(self, _js: str):
        return self.text

    def locator(self, sel: str):
        self.pending = sel
        return FakeLocator(self, 1 if any(h in sel for h in self.has) else 0)

    def get_by_text(self, text: str, exact: bool = False):
        self.pending = f"text:{text}"
        return FakeLocator(self, 1)

    def wait_for_load_state(self, _state: str, timeout: int = 0) -> None:
        self.clicks.append("networkidle")

    def evaluate_handle(self, _js: str, arg):
        """Ищем кнопку по точной надписи – так же, как настоящий поиск."""
        title = arg if isinstance(arg, str) else (arg[0] if arg else "")
        return _FakeHandle(self, title if title in self.buttons else "")

    def wait_for_timeout(self, _ms: int) -> None:
        if self.text_after_click is not None and self.clicks:
            self.text = self.text_after_click

    # ─ то, что нужно закрытию окон и поиску полей ─
    @property
    def keyboard(self):
        return _FakeKeys(self)

    @property
    def mouse(self):
        return _FakeKeys(self)


class _FakeHandle:
    def __init__(self, page, title: str):
        self.page = page
        self.title = title

    def as_element(self):
        return _FakeButton(self.page, self.title) if self.title else None


class _FakeButton:
    def __init__(self, page, title: str):
        self.page = page
        self.title = title

    def click(self, **kw) -> None:
        self.page.clicks.append(f"button:{self.title}")

    def evaluate(self, _js: str) -> None:
        self.page.clicks.append(f"js-button:{self.title}")


class _FakeKeys:
    def __init__(self, page):
        self.page = page

    def press(self, key: str) -> None:
        self.page.clicks.append(f"key:{key}")

    def click(self, x: int, y: int, **kw) -> None:
        self.page.clicks.append(f"click:{x},{y}")

    def type(self, text: str, **kw) -> None:
        self.page.clicks.append(f"type:{text[:20]}")


class FakeEditor(FakePage):
    """Редактор статьи: сколько-то редактируемых блоков и окно обучения."""

    def __init__(self, editable: int = 2, **kw):
        super().__init__(**kw)
        self.editable = editable

    def locator(self, sel: str):
        self.pending = sel
        # Только общий перебор редактируемых блоков; приметы вроде
        # [data-testid*="title"] у настоящего Дзена не срабатывают – ради
        # этого случая всё и написано.
        if sel == '[contenteditable="true"]':
            return _FakeFields(self, self.editable)
        return FakeLocator(self, 1 if any(h in sel for h in self.has) else 0)


class _FakeFields:
    def __init__(self, page, n: int):
        self.page = page
        self.n = n

    def count(self) -> int:
        return self.n

    def nth(self, i: int):
        loc = FakeLocator(self.page, 1 if i < self.n else 0)
        loc.index = i
        return loc

    @property
    def first(self):
        return self.nth(0)


def test_login_flow() -> None:
    print("Вход в Дзен: три нажатия и выбор аккаунта")
    try:
        import zen_browser as zb
    except ImportError as e:
        print(f"  ⏭ пропускаю: {e}")
        return

    # Главный урок живого прогона: со студии Дзен уводит незалогиненного на
    # публичный канал молча. Ни слова про вход – а студии нет.
    channel = FakePage(text="СТАЛЬМЕТУРАЛ | Металлопрокат\nПодписаться\nВойти",
                       has=("login-button",))
    check("публичный канал – это не студия", not zb.in_studio(channel))
    check("и Click понимает, что не вошёл", zb._looks_logged_out(channel))

    studio = FakePage(text="Главное Статистика Публикации", has=("add-publication-button",))
    check("студия узнаётся по кнопке «＋»", zb.in_studio(studio))
    check("в студии вход не требуется", not zb._looks_logged_out(studio))

    # Выбор аккаунта: нужный есть среди нескольких.
    many = FakePage(text=("Выберите аккаунт для входа stalmetural19@yandex.ru СМУ "
                          "mepen88@yandex.ru МПЭ aviastalru@yandex.ru"))
    many.text_after_click = "Главное Статистика"
    why = zb.pick_account(many, "stalmetural19@yandex.ru", lambda m: None)
    check("нужный аккаунт выбран", why == "", why)
    check("нажали именно по нему",
          many.clicks and "stalmetural19@yandex.ru" in many.clicks[0], str(many.clicks))

    # Чужой аккаунт – останавливаемся: не тот бренд не отменить.
    alien = FakePage(text="Выберите аккаунт для входа mepen88@yandex.ru МПЭ")
    why = zb.pick_account(alien, "stalmetural19@yandex.ru", lambda m: None)
    check("чужого аккаунта не выбираем", "не нашёлся" in why, why)
    check("и говорим, что предложено", "mepen88@yandex.ru" in why, why)

    # Аккаунт один, почта проекта не заполнена – жмём его, не упираясь.
    lone = FakePage(text="Выберите аккаунт для входа stalmetural19@yandex.ru СМУ")
    lone.text_after_click = "Главное"
    check("единственный аккаунт выбирается без почты",
          zb.pick_account(lone, "", lambda m: None) == "")

    # Несколько аккаунтов и пустая почта – это уже опасно.
    risky = FakePage(text="Выберите аккаунт для входа a@yandex.ru b@yandex.ru")
    check("несколько аккаунтов без почты – стоп",
          "не указан email" in zb.pick_account(risky, "", lambda m: None))

    check("экрана выбора нет – делать нечего",
          zb.pick_account(FakePage(text="Главное"), "x@yandex.ru", lambda m: None) == "")

    # Все три нажатия описаны селекторами – их и правят, когда Дзен обновится.
    for key in ("login_button", "login_yandex", "add_publication"):
        check(f"селекторы «{key}» на месте", bool(zb.SEL.get(key)))


def test_popups_and_fields() -> None:
    print("Всплывающие окна и поля редактора")
    try:
        import zen_browser as zb
    except ImportError as e:
        print(f"  ⏭ пропускаю: {e}")
        return

    # Реклама донатов в студии и обучение «Статья» поверх редактора – оба
    # встретились на живом прогоне и оба закрывали собой работу.
    donate = FakePage(text="У нас появились донаты! Разовые денежные переводы")
    check("реклама донатов замечена", zb._has_popup(donate))
    lesson = FakePage(text="Статья — это в первую очередь текст, Примеры статей")
    check("обучение «Статья» замечено", zb._has_popup(lesson))
    check("на чистой странице окон нет", not zb._has_popup(FakePage(text="Главное")))

    # Закрываем как человек: крестик, потом Esc, потом нажатие по пустому месту.
    with_cross = FakePage(text="У нас появились донаты!", has=("close",))
    with_cross.text_after_click = "Главное"
    check("окно с крестиком закрывается им", zb.dismiss_popups(with_cross) >= 1)
    check("нажали именно крестик",
          any("close" in c for c in with_cross.clicks), str(with_cross.clicks))

    stubborn = FakePage(text="Примеры статей")
    stubborn.text_after_click = "Заголовок Текст"
    zb.dismiss_popups(stubborn)
    check("без крестика идёт Esc и пустое место",
          any(c.startswith("key:Escape") for c in stubborn.clicks), str(stubborn.clicks))

    # Поля редактора: примет нет, зато порядок железный.
    editor = FakeEditor(editable=2, text="Заголовок Текст")
    title, body = zb.find_editor_fields(editor, timeout_ms=3_000)
    check("заголовок – первый редактируемый блок",
          title is not None and getattr(title, "index", None) == 0)
    check("тело – второй", body is not None and getattr(body, "index", None) == 1)

    empty = FakeEditor(editable=0, text="Пусто")
    t2, b2 = zb.find_editor_fields(empty, timeout_ms=2_000)
    check("редактора нет – честно говорим об этом", t2 is None and b2 is None)


class FakeField:
    """
    Поле редактора: текст растёт, если «редактор» принял вставку.

    Нужно ради одного, зато дорого стоившего случая: dispatchEvent вернул
    false (редактор вызвал preventDefault, то есть вставку ПРИНЯЛ), а Click
    решил, что не вышло, и набрал текст второй раз поверх.
    """

    def __init__(self, accepts: bool = True, text: str = ""):
        self.accepts = accepts
        self.text = text
        self.pasted = None

    def inner_text(self) -> str:
        return self.text

    def click(self, **kw) -> None:
        pass

    def element_handle(self):
        return self

    def evaluate(self, _js: str, html):
        self.pasted = html
        if self.accepts:
            self.text = "Статья целиком, много букв " + html[:200]
        return False          # ровно так и отвечает настоящий редактор


def test_paste_detection() -> None:
    print("Вставка текста: успех считаем по тексту, а не по ответу события")
    try:
        import zen_browser as zb
    except ImportError as e:
        print(f"  ⏭ пропускаю: {e}")
        return

    page = FakePage()
    took = FakeField(accepts=True)
    check("редактор принял вставку – это успех",
          zb._paste_html_into(page, took, "<p>Текст статьи достаточной длины</p>"))
    check("и текст правда лёг в поле", len(took.text) > 20, took.text[:40])

    ignored = FakeField(accepts=False)
    check("редактор вставку не принял – идём набирать",
          not zb._paste_html_into(page, ignored, "<p>Текст</p>"))


def test_calendar() -> None:
    print("Календарь отложки")
    try:
        import zen_browser as zb
    except ImportError as e:
        print(f"  ⏭ пропускаю: {e}")
        return

    when = datetime(2026, 8, 14, 19, 41, tzinfo=timezone(timedelta(hours=5)))

    # Календарь пишет месяц ИМЕНИТЕЛЬНЫМ падежом и показывает сразу два.
    # На этом и погорели: искали «августа», листали до июля 2027.
    two_months = "Опубликовать позже Август 2026 пн вт ср Сентябрь 2026 пн вт"
    check("нужный месяц виден – листать не надо", zb.calendar_shows(two_months, when))
    check("родительный падеж в календаре не ищем",
          not zb.calendar_shows("14 августа 2026", when))
    check("чужой месяц не сходит за нужный",
          not zb.calendar_shows("Июль 2027 пн вт ср", when))
    check("соседний месяц тоже виден",
          zb.calendar_shows(two_months, when.replace(month=9)))

    # А в ПОЛЕ даты падеж родительный – и сверяем мы именно его.
    check("поле даты сверяется родительным", zb.date_caption_ok("14 августа 2026", when))
    check("«14 июля 2027» не сойдёт", not zb.date_caption_ok("14 июля 2027", when))

    check("месяцев в обоих падежах по двенадцать",
          len(zb.MONTHS_RU) == 12 and len(zb.MONTHS_NOM) == 12)


def test_click_day_spillover() -> None:
    """
    Ровно тот календарь, на котором Дзен падал (по живому дампу 19.08):
    два месяца рядом, ячейки – <td class="…datepicker-calendar-cell…">,
    заголовок «Август» и «2026» РАЗНЫМИ узлами без пробела (значит
    textContent.includes('Август 2026') не находит его – на этом и стоял
    прежний выбор дня), прошедшие и соседние дни серые с aria-disabled.

    Проверяем: выбор больше не зависит от заголовка, живое «31 августа»
    находится и нажимается, а число, живое СРАЗУ в двух месяцах (25 августа
    и 25 сентября), выбирается по нужному месяцу за счёт сверки поля даты.
    """
    print("Календарь: выбираем живой день без опоры на заголовок месяца")
    try:
        import zen_browser as zb
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"  ⏭ пропускаю: {e}")
        return
    import calendar as _cal

    CELL = "article-editor-desktop--datepicker-calendar-cell"

    def grid(year: int, month: int, today) -> str:
        cells = []
        for d in _cal.Calendar(firstweekday=0).itermonthdates(year, month):
            adj = d.month != month
            past = d < today
            cls = CELL + ((" " + CELL + "_disabled") if (adj or past) else "")
            dis = 'aria-disabled="true"' if (adj or past) else ""
            cap = f"{d.day} {zb.MONTHS_RU[month - 1]} {year}"
            live = "" if (adj or past) else \
                f"onclick=\"document.getElementById('dt').value='{cap}'\""
            cells.append(f'<td class="{cls}" {dis} {live}><span>{d.day}</span></td>')
        m = zb.MONTHS_NOM[month - 1]
        # Заголовок нарочно РАЗБИТ: между «Август» и «2026» нет текстового
        # пробела – ровно как у Дзена, где includes('Август 2026') не срабатывает.
        head = f'<div class="cap"><span>{m}</span><span>{year}</span></div>'
        return (f'<table class="article-editor-desktop--datepicker-calendar">'
                f'<caption>{head}</caption><tbody><tr>{"".join(cells)}</tr></tbody></table>')

    today = datetime(2026, 8, 19).date()
    html = ('<!doctype html><meta charset=utf-8>'
            f'<style>.{CELL}_disabled{{opacity:.3;pointer-events:none}}</style>'
            '<input id=dt readonly value="19 августа 2026">'
            + grid(2026, 8, today) + grid(2026, 9, today))
    when = datetime(2026, 8, 31, 18, 5, tzinfo=timezone(timedelta(hours=5)))
    when25 = datetime(2026, 8, 25, 18, 5, tzinfo=timezone(timedelta(hours=5)))

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        except Exception:  # noqa: BLE001 – в CI лежит по этому пути, локально – как есть
            try:
                browser = pw.chromium.launch()
            except Exception as e:  # noqa: BLE001 – браузера нет вовсе
                print(f"  ⏭ пропускаю: браузер не запустился ({e})")
                return
        try:
            page = browser.new_page()
            page.set_content(html)

            # Заголовок как цельный текст действительно не находится – значит
            # выбор обязан работать и без него.
            check("заголовок «Август 2026» цельным текстом не ищется",
                  not zb.calendar_shows(page.evaluate(
                      "() => document.body.textContent"), when))

            cands = zb._day_candidates(page, when)
            check("ячейки «31» нашлись", len(cands) >= 1, str(len(cands)))
            check("первым кандидатом идёт живая ячейка (не серая)",
                  "серая" not in zb._cell_note(cands[0]).lower()
                  and (cands[0].get_attribute("aria-disabled") or "") != "true")
            check("выбор 31 сработал без заголовка",
                  zb._click_day_in_month(page, when, "#dt"))
            check("в поле именно 31 августа",
                  page.locator("#dt").first.input_value() == "31 августа 2026")

            # 25-е живое в обоих месяцах – нужный выбирается сверкой поля.
            page.locator("#dt").first.evaluate("e => e.value = '19 августа 2026'")
            check("для «25» кандидатов двое (август и сентябрь)",
                  len(zb._day_candidates(page, when25)) == 2)
            check("выбор 25 августа сработал",
                  zb._click_day_in_month(page, when25, "#dt"))
            check("в поле именно 25 августа",
                  page.locator("#dt").first.input_value() == "25 августа 2026")
        finally:
            browser.close()


def test_image_at_marker() -> None:
    """
    Картинка должна вставать НА МЕСТО метки ⟦МЕДИА-N⟧ и метку убирать. Живой
    прогон показал: программное выделение редактор Дзена игнорировал, картинка
    улетала вверх, а метка ⟦МЕДИА-N⟧ оставалась текстом. Теперь метку выделяем
    настоящим тройным кликом. Проверяем на редакторе-имитаторе: <img> встал
    между нужными абзацами, а текста метки в поле не осталось.
    """
    print("Картинка встаёт на место метки и метку убирает")
    try:
        import zen_browser as zb
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"  ⏭ пропускаю: {e}")
        return
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    tmp = Path("/tmp/zen-marker-test.png")
    tmp.write_bytes(png)
    token = zb.media_token(1)
    html = ("<div id=ed contenteditable=true>"
            "<p>Вводный абзац про латунь.</p>"
            f"<p>{token}</p>"
            "<h2>Главный секрет</h2>"
            "<p>Дальше текст статьи.</p></div>"
            "<script>document.getElementById('ed').addEventListener('paste',e=>{"
            "const f=e.clipboardData&&e.clipboardData.files[0];"
            "if(f){e.preventDefault();const img=document.createElement('img');"
            "img.src=URL.createObjectURL(f);img.className='ins';"
            "const s=window.getSelection();"
            "if(s.rangeCount){const r=s.getRangeAt(0);r.deleteContents();r.insertNode(img);}"
            "else document.getElementById('ed').appendChild(img);}});</script>")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        except Exception:  # noqa: BLE001
            try:
                browser = pw.chromium.launch()
            except Exception as e:  # noqa: BLE001
                print(f"  ⏭ пропускаю: браузер не запустился ({e})")
                return
        try:
            page = browser.new_page()
            page.set_content(html)
            field = page.locator("#ed")

            ok = zb._paste_image_into(page, field, str(tmp), lambda m: None, token=token)
            check("вставка удалась", ok)
            check("картинка появилась в тексте", page.locator("#ed img.ins").count() == 1)
            check("метки ⟦МЕДИА-1⟧ в тексте не осталось",
                  token not in page.locator("#ed").inner_text())
            # img стоит ДО подзаголовка «Главный секрет» – значит на месте метки.
            pos = page.evaluate(
                """() => {
                    const kids = [...document.querySelectorAll('#ed > *')];
                    const img = kids.findIndex(n => n.querySelector && n.querySelector('img.ins')
                        || n.classList && n.classList.contains('ins'));
                    const h2 = kids.findIndex(n => n.tagName === 'H2');
                    return {img, h2};
                }""")
            check("картинка стоит выше подзаголовка (на месте метки)",
                  0 <= pos["img"] < pos["h2"], str(pos))
            # H2 после картинки должен остаться подзаголовком – его нельзя
            # утянуть Backspace'ом при чистке метки (на этом «слетал» заголовок).
            check("подзаголовок H2 уцелел",
                  page.locator("#ed h2").count() == 1
                  and "Главный секрет" in page.locator("#ed h2").inner_text())
        finally:
            browser.close()


def test_confirm_button() -> None:
    print("Подтверждение отложки: кнопка «Опубликовать позже»")
    try:
        import zen_browser as zb
    except ImportError as e:
        print(f"  ⏭ пропускаю: {e}")
        return

    # На экране две кнопки со словом «Опубликовать»: в шапке редактора и
    # внизу окна публикации. С включённой отложкой нижняя называется иначе,
    # и жать надо именно её – верхняя в этот момент накрыта окном.
    both = FakePage()
    both.buttons = ("Опубликовать", "Опубликовать позже")
    check("подтверждаем точной надписью", zb.click_button_titled(both, "Опубликовать позже"))
    check("нажали нижнюю, а не шапку",
          both.clicks == ["button:Опубликовать позже"], str(both.clicks))

    only_header = FakePage()
    only_header.buttons = ("Опубликовать",)
    check("нижней кнопки нет – честно говорим",
          not zb.click_button_titled(only_header, "Опубликовать позже"))
    check("и ничего не нажимаем", not only_header.clicks, str(only_header.clicks))

    # Окно открывается кнопкой шапки – она называется ровно «Опубликовать».
    check("окно открываем кнопкой шапки", zb.click_button_titled(both, "Опубликовать"))

    import inspect
    src = inspect.getsource(zb.schedule_article)
    check("в подтверждении ищется «Опубликовать позже»",
          'click_button_titled(page, "Опубликовать позже")' in src)
    check("успехом считается и закрытие окна",
          '"Опубликовать позже" not in body' in src)


def test_settle_after_publish() -> None:
    print("После нажатия ждём, а не закрываем браузер")
    try:
        import zen_browser as zb
    except ImportError as e:
        print(f"  ⏭ пропускаю: {e}")
        return

    waits: list[int] = []

    class Waiting(FakePage):
        def wait_for_timeout(self, ms: int) -> None:
            waits.append(ms)

    # Отложка ещё летит на сервер: сначала ждём тишины в сети, и только
    # потом отпускаем браузер. Закрытый на середине разговора – теряет её.
    confirmed = Waiting(text="Публикации Отложенные Проверка Click")
    check("подтверждение на экране замечено",
          zb.settle_after_publish(confirmed, True, "Проверка Click", lambda m: None))
    check("сначала дождались тишины в сети", "networkidle" in confirmed.clicks)
    check("и подождали перед закрытием", sum(waits) >= zb.SETTLE_MS, str(waits))

    waits.clear()
    quiet = Waiting(text="Редактор статьи")
    check("прямого подтверждения нет – так и говорим",
          not zb.settle_after_publish(quiet, True, "Проверка Click", lambda m: None))

    # При видимом окне держим дольше: человек должен успеть посмотреть.
    waits.clear()
    zb.settle_after_publish(Waiting(text="Отложенные"), False, "", lambda m: None)
    watched = sum(waits)
    waits.clear()
    zb.settle_after_publish(Waiting(text="Отложенные"), True, "", lambda m: None)
    hidden = sum(waits)
    check("видимое окно держим дольше скрытого", watched > hidden, f"{watched} vs {hidden}")
    check("и это не мгновение", zb.WATCH_HOLD_MS >= 10_000, str(zb.WATCH_HOLD_MS))

    import inspect
    check("ожидание встроено в публикацию",
          "settle_after_publish(" in inspect.getsource(zb.schedule_article))


def test_date_already_set() -> None:
    print("Дата уже стоит нужная – календарь не трогаем")
    try:
        import zen_browser as zb
    except ImportError as e:
        print(f"  ⏭ пропускаю: {e}")
        return

    when = datetime(2026, 8, 14, 19, 58, tzinfo=timezone(timedelta(hours=5)))

    # Дзен подставляет в поле сегодняшнюю дату, и для статьи «на сегодня»
    # она сразу правильная. Открывать календарь незачем: не нажали – нечему
    # и сломаться. Заказчик это и заметил: «она изначально выбрана».
    ready = FakePage(text="Опубликовать позже GMT+5", has=("input__control",))
    ready.values["date"] = "14 августа 2026"
    why = zb._open_calendar_and_pick(ready, when, lambda m: None)
    check("нужная дата принимается как есть", why == "", why)
    check("календарь при этом не открывали", not ready.clicks, str(ready.clicks))

    # А вот чужую дату так оставлять нельзя – придётся открыть календарь.
    other = FakePage(text="Опубликовать позже", has=("input__control",))
    other.values["date"] = "14 июля 2027"
    zb._open_calendar_and_pick(other, when, lambda m: None)
    check("чужая дата – календарь открываем", bool(other.clicks), str(other.clicks))

    missing = FakePage(text="Опубликовать позже")
    check("нет поля даты – говорим прямо",
          "не нашли поле даты" in zb._open_calendar_and_pick(missing, when, lambda m: None))


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
    test_doc_image()
    test_parse_plain()
    test_doc_links()
    test_long_title()
    test_browser_logic()
    test_login_flow()
    test_popups_and_fields()
    test_paste_detection()
    test_calendar()
    test_click_day_spillover()
    test_image_at_marker()
    test_date_already_set()
    test_confirm_button()
    test_settle_after_publish()
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
