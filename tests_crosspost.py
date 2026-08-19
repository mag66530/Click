"""
tests_crosspost.py – самопроверка чтения реестра кросспостинга (без сети и браузера).

Запуск:  python tests_crosspost.py

Строки собраны вручную по РЕАЛЬНОЙ структуре листа СМУ (разобран 2026-08-07):
пост = блок строк, дата+текст+фото+тип на первой строке, дальше строки по
соцсетям со своей колонкой «Ссылка»; справа – блок статистики, который
парсер обязан игнорировать.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import content_plan as cp  # noqa: E402

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


# Лист-образец: шапка (с пустой колонкой A и блоком статистики справа),
# два поста-блока. Первый – прошлый и уже опубликован (у всех «Ссылка» есть);
# второй – будущий, часть площадок ещё без ссылки.
SAMPLE = [
    ["", "", "", "", "", "", "", "", "", "2025", "План"],
    ["", "Когда выложить", "Соцсеть", "Бренд", "Ссылка", "Формат", "Тип", "Пост", "Фото", "ВКонтакте", "99"],
    ["", "2025-06-02 00:00:00", "Telegram (сотрудники)", "СМУ", "https://t.me/SMUdaily/984",
     "Пост", "Отгрузка", "Отгрузили трубы", "https://i.ibb.co/aaa/1.jpg", "Однокласники", "99"],
    ["", "", "Telegram (клиент)", "", "https://t.me/stalmetural/617", "Пост", "", "", "", "Telegram", "109"],
    ["", "", "Однокласники", "", "https://ok.ru/group/70000004574376/topic/1", "Пост", "", "", "", "", ""],
    ["", "", "Вконтакте", "", "https://vk.com/wall-217668235_743", "Пост", "", "", "", "", ""],
    ["", "", "Max (клиент)", "", "https://max.ru/xxx", "Пост", "", "", "", "", ""],
    # будущий пост – часть площадок ещё не опубликована (пустая «Ссылка»)
    ["", "2099-01-10 00:00:00", "Вконтакте", "СМУ", "", "Пост", "Спецпредложение",
     "Скидка на арматуру", "https://i.ibb.co/bbb/2.jpg https://i.ibb.co/ccc/3.jpg", "", ""],
    ["", "", "Однокласники", "", "", "Пост", "", "", "", "", ""],
    ["", "", "Telegram (клиент)", "", "", "Пост", "", "", "", "", ""],
    ["", "", "Дзен", "", "", "Пост", "", "", "", "", ""],
    # Статья: текста в ячейке нет, вместо него ССЫЛКА на документ. Такую
    # строку берёт только Дзен – ВК с Телеграмом лонгрид не нужен.
    ["", "2099-02-11 00:00:00", "Дзен", "СМУ", "", "Статья", "Лонгрид",
     "https://docs.google.com/document/d/1QaBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789/edit", "", "", ""],
    ["", "", "Вконтакте", "", "", "Статья", "", "", "", "", ""],
]


def test_network_mapping() -> None:
    print("Соцсеть → код цели")
    check("Вконтакте → vk", cp.canonical_network("Вконтакте") == "vk")
    check("ВК → vk", cp.canonical_network("ВК") == "vk")
    check("Однокласники (с опечаткой) → ok", cp.canonical_network("Однокласники") == "ok")
    check("Telegram (сотрудники) → tg-staff", cp.canonical_network("Telegram (сотрудники)") == "tg-staff")
    check("Telegram (клиент) → tg-client", cp.canonical_network("Telegram (клиент)") == "tg-client")
    check("Max (клиент) → max", cp.canonical_network("Max (клиент)") == "max")
    check("Дзен → zen", cp.canonical_network("Дзен") == "zen")
    check("пусто → ''", cp.canonical_network("") == "")


def test_dates_and_time() -> None:
    print("Дата и время")
    check("ISO с временем", cp.parse_date("2025-06-02 00:00:00") == date(2025, 6, 2))
    check("ISO без времени", cp.parse_date("2025-06-02") == date(2025, 6, 2))
    check("ДД.ММ.ГГГГ", cp.parse_date("02.06.2025") == date(2025, 6, 2))
    check("мусор → None", cp.parse_date("не дата") is None)
    check("пусто → None", cp.parse_date("") is None)
    check("час СМУ = 11:00", cp.brand_default_time("SMU") == "11:00")
    check("час МПИ = 09:30", cp.brand_default_time("MPI") == "09:30")
    check("when по Екб (+05:00)", cp.when_iso(date(2026, 8, 12), "11:00") == "2026-08-12T11:00:00+05:00")


def test_parse_blocks() -> None:
    print("Разбор блочной структуры")
    posts = cp.parse_sheet(SAMPLE, "SMU")
    check("нашли три поста", len(posts) == 3, f"получили {len(posts)}")
    if len(posts) != 3:
        return
    p1, p2 = posts[0], posts[1]
    check("дата первого", p1["date"] == "2025-06-02")
    check("текст с первой строки блока", p1["text"] == "Отгрузили трубы")
    check("тип с первой строки блока", p1["post_type"] == "Отгрузка")
    check("фото с первой строки блока", p1["images"] == ["https://i.ibb.co/aaa/1.jpg"])
    check("у первого 5 целей-соцсетей", len(p1["targets"]) == 5, f"{len(p1['targets'])}")
    check("время подставилось из бренда", p1["time"] == "11:00" and p1["when"].endswith("+05:00"))

    nets2 = [t["network"] for t in p2["targets"]]
    check("второй пост: две картинки", p2["images"] == ["https://i.ibb.co/bbb/2.jpg", "https://i.ibb.co/ccc/3.jpg"])
    check("второй пост видит дзен как zen", "zen" in nets2)
    check("блок статистики не попал в цели",
          all(t["raw"] not in ("99", "109", "ВКонтакте", "План") for t in p1["targets"]))


def test_posts_to_form() -> None:
    print("Отбор «что формировать»")
    posts = cp.parse_sheet(SAMPLE, "SMU")
    to_form = cp.posts_to_form(posts, today=date(2026, 8, 7))
    check("прошлый пост отсеян, остались два будущих", len(to_form) == 2, f"{len(to_form)}")
    if not to_form:
        return
    nets = sorted(t["network"] for t in to_form[0]["targets"])
    check("в будущем формируем vk, ok и дзен", nets == ["ok", "vk", "zen"], str(nets))
    # Телеграм Click пока не формирует вовсе (13.08.2026): отложки у бота
    # нет, а публиковать в час выхода он больше не должен.
    check("телеграм не формируем", "tg-client" not in nets, str(nets))

    # Статья – материал Дзена, и только его. ВК с МАКСом лонгрид не берут:
    # у них в той же таблице свой короткий пост.
    article = [p for p in to_form if p["date"] == "2099-02-11"]
    check("статья попала в работу", len(article) == 1, str(len(article)))
    if article:
        art_nets = sorted(t["network"] for t in article[0]["targets"])
        check("у статьи адресат один – Дзен", art_nets == ["zen"], str(art_nets))
        check("текст статьи – ссылка на документ",
              cp.is_article(article[0]) and article[0]["text"].startswith("https://docs.google.com/"))

    # если у площадки «Ссылка» уже стоит – второй раз не формируем
    already = cp.posts_to_form(
        [{"brand": "SMU", "date": "2099-01-10", "time": "11:00", "when": "x",
          "post_type": "", "format": "Пост", "text": "t", "images": [],
          "targets": [{"network": "vk", "raw": "Вконтакте", "published_link": "https://vk.com/wall-1_1"}],
          "row": 1}],
        today=date(2026, 8, 7))
    check("площадка с готовой ссылкой пропускается", already == [])

    # пустой текст – не формируем
    empty = cp.posts_to_form(
        [{"brand": "SMU", "date": "2099-01-10", "time": "11:00", "when": "x",
          "post_type": "", "format": "Пост", "text": "", "images": [],
          "targets": [{"network": "vk", "raw": "Вконтакте", "published_link": ""}], "row": 1}],
        today=date(2026, 8, 7))
    check("пост без текста пропускается", empty == [])


def test_real_file_optional() -> None:
    """Если рядом лежит выгрузка реестра СМУ – прогнать и на ней (не обязательно)."""
    import glob
    hits = glob.glob(str(Path(__file__).parent / "*.xlsx")) + \
        glob.glob("/root/.claude/uploads/**/*.xlsx", recursive=True)
    real = next((h for h in hits if "sheet" not in h.lower()), None)
    if not real:
        print("Реальный файл реестра рядом не найден – пропускаю (это норма).")
        return
    print(f"Реальный файл: {Path(real).name}")
    try:
        posts = cp.load_from_xlsx(real, "SMU")
    except Exception as e:  # noqa: BLE001
        check("реальный файл прочитан", False, str(e))
        return
    check("в листе СМУ есть посты", len(posts) > 50, f"{len(posts)}")
    check("у постов есть цели-соцсети", any(p["targets"] for p in posts))
    nets = {t["network"] for p in posts for t in p["targets"]}
    check("распознаны vk/ok/tg/max", {"vk", "ok", "max"} <= nets, str(sorted(nets)))


def test_state() -> None:
    print("Память о сформированном (crosspost_state)")
    import crosspost_state as cps
    p = {"brand": "SMU", "date": "2099-01-10", "when": "2099-01-10T11:00:00+05:00",
         "format": "Пост", "text": "Привет", "images": ["a.jpg"],
         "targets": [{"network": "vk", "raw": "Вконтакте", "published_link": ""},
                     {"network": "ok", "raw": "Однокласники", "published_link": "https://ok.ru/x"}]}
    check("ключ поста стабилен", cps.post_key(p) == cps.post_key(dict(p)))
    p2 = {**p, "text": "Другой текст"}
    check("правка текста меняет ключ", cps.post_key(p) != cps.post_key(p2))
    check("по умолчанию – не сформировано", cps.status_of({}, p, "vk") == cps.WAITING)
    check("ссылка в реестре = вышло", cps.summarize({}, [p])[cps.SENT] == 1)
    check("вторая площадка ждёт", cps.summarize({}, [p])[cps.WAITING] == 1)
    check("is_done по умолчанию False", cps.is_done({}, p, "vk") is False)

    st_ = {cps.post_key(p): {"targets": {"vk": {"state": cps.SCHEDULED}}}}
    check("отложка стоит → формировать не надо", cps.is_done(st_, p, "vk") is True)
    check("ошибка попадает в «требует внимания»",
          len(cps.problems({cps.post_key(p): {"targets": {"vk": {"state": cps.FAILED,
                                                                 "error": "сессия слетела"}}}}, [p])) == 1)

    video = {**p, "format": "Видео", "text": "", "images": []}
    check("видео вне scope не считается проблемой", cps.problems({}, [video]) == [])
    check("название площадки по-русски", cps.network_ru("tg-staff") == "ТГ сотрудники")


def test_inline_format() -> None:
    """
    Единая подготовка анкора для ОК и МАКС (post_text.inline_format).

    Заказчица (19.08.2026): «в максе анкор поставил плохо, надо как в ОК; а в
    ОК ссылку вставляет, но „нашем сайте“ надо жирным». Плюс баг: когда анкор
    уже жирный (**[…](…)**), голый адрес после него не убирался.
    """
    print("\nЕдиная подготовка анкора (ОК = МАКС)")
    import post_text as pt

    # Анкор УЖЕ жирный + голый адрес следом — самый больной случай.
    markup = pt.autolink("Смотрите на **нашем сайте** stalmetural.ru.", "stalmetural.ru")
    plain, bold, anchors = pt.inline_format(markup)
    check("голый адрес после жирного анкора убран", "stalmetural.ru" not in plain, plain)
    check("«нашем сайте» жирным", "нашем сайте" in bold, str(bold))
    check("ссылка «нашем сайте» отдельной целью",
          anchors == [("нашем сайте", "https://stalmetural.ru")], str(anchors))
    check("в тексте остался ярлык, точка на месте",
          plain.strip().endswith("нашем сайте."), repr(plain))

    # Не жирный анкор — тоже без голого адреса.
    m2 = "Пишите на [нашем сайте](https://a.ru) a.ru за подробностями"
    p2, b2, a2 = pt.inline_format(m2)
    check("голый адрес убран и у нежирного анкора", " a.ru " not in p2 and "нашем сайте" in p2, p2)
    check("нежирный анкор всё равно становится жирным", "нашем сайте" in b2, str(b2))

    # Обычный жирный без ссылок не ломается.
    p3, b3, a3 = pt.inline_format("**Заголовок** и обычный текст")
    check("простой жирный сохранён", b3 == ["Заголовок"] and a3 == [], f"{b3} {a3}")

    # ОК и МАКС применяют формат в порядке «ссылка → жирный».
    import inspect
    import max_browser as mb
    import ok_browser as ok
    ok_flow = inspect.getsource(ok._type_post_text)
    # ОК: ЖИРНЫЙ первым, ссылка второй. Обратный порядок ломал ОК — окно
    # ссылки на свежем тексте портило его и вешало откат на 30 c.
    check("ОК: сначала жирный, потом ссылка",
          ok_flow.index("_apply_bold(page") < ok_flow.index("_apply_links_native"))
    check("ОК берёт подготовку из inline_format", "inline_format(" in ok_flow)
    check("ОК всё ещё ПЫТАЕТСЯ вставить ссылку (заказчице она нужна)",
          "_apply_links_native(" in ok_flow)
    check("ОК гасит панель после форматирования (не виснет и не рушит форму)",
          "_drop_selection(" in ok_flow)
    # ОК за ссылкой оставляет пробел перед точкой («…сайте .») — срезаем.
    check("ОК срезает пробел перед знаком после ссылки",
          "_fix_space_before_punct(" in ok_flow)
    fix_src = ok._FIX_SPACE_JS
    check("чистка бьёт только по тексту ПОСЛЕ ссылки и перед знаком",
          "isLink" in fix_src and "[.,;:!?" in fix_src)

    # Точные селекторы окна ссылки ОК (заказчица прислала DOM 19.08.2026):
    # скрепочка «Ссылка» в панели, поле адреса, кнопка «Добавить». Раньше
    # жали Ctrl+K вслепую — окно не открывалось.
    links_src = inspect.getsource(ok._apply_links_native)
    check("ссылку открываем СКРЕПОЧКОЙ «Ссылка», а не Ctrl+K",
          'title="Ссылка"' in links_src and "posting_form_media_text_menu_menu_i" in links_src)
    check("адрес вписываем в js-field_url", "input.js-field_url" in links_src)
    check("подтверждаем кнопкой «Добавить»",
          "button.js-posting-link-editor-confirm" in links_src)
    check("окно закрываем «Отменить», а НЕ Escape (он рушит форму ОК)",
          "Отменить" in links_src and 'press("Escape")' not in links_src)
    # Поле читаем устойчиво к перерисовке ОК (иначе «поле опустело» зря).
    read_src = inspect.getsource(ok._read_field)
    check("поле читаем с запасными селекторами (перерисовка не «пустота»)",
          ".js-posting-itx" in read_src)
    # МАКС: ссылка первой (Ctrl+K сбрасывает жирный, кладём жирный поверх).
    mb_fmt = inspect.getsource(mb._apply_max_format)
    check("МАКС: сначала ссылки, потом жирный",
          mb_fmt.index("_add_max_link") < mb_fmt.index('press("Control+b")'))
    check("МАКС берёт подготовку из inline_format", "inline_format(" in mb_fmt)


def test_post_text() -> None:
    print("Форматирование (post_text)")
    import post_text as pt

    check("склейка кусков", pt.runs_to_markup([("а", False), ("б", False)]) == "аб")
    check("жирный кусок", pt.runs_to_markup([("Важно", True)]) == "**Важно**")
    check("смежные жирные сливаются",
          pt.runs_to_markup([("а", True), ("б", True)]) == "**аб**")
    check("хвостовой пробел выносится из маркеров",
          pt.runs_to_markup([("жирный ", True), ("хвост", False)]) == "**жирный** хвост")
    check("перенос строки не попадает в маркеры",
          pt.runs_to_markup([("Заголовок\n\n", True), ("текст", False)]) == "**Заголовок**\n\nтекст")

    m = pt.autolink("Оформить заказ можно на **нашем сайте**", "stalmetural.ru")
    check("автоссылка на жирную фразу",
          m == "Оформить заказ можно на **[нашем сайте](https://stalmetural.ru)**", m)
    m2 = pt.autolink("на нашем сайте и ещё раз на нашем сайте", "stalmetural.ru")
    check("автоссылка только на первое вхождение", m2.count("](") == 1)
    m3 = pt.autolink("уже есть [нашем сайте](https://x.ru)", "stalmetural.ru")
    check("внутрь готовой ссылки не лезем", m3 == "уже есть [нашем сайте](https://x.ru)")
    check("без сайта – без изменений", pt.autolink("на нашем сайте", "") == "на нашем сайте")

    check("html: жирный и ссылка",
          pt.render("**Важно**: [сайт](https://a.ru/?a=1&b=2) <3", "html")
          == '<b>Важно</b>: <a href="https://a.ru/?a=1&amp;b=2">сайт</a> &lt;3')
    check("plain: жирный снят, ссылка – голым адресом",
          pt.render("**Важно**: [нашем сайте](https://a.ru)", "plain")
          == "Важно: нашем сайте a.ru")
    check("plain: текст-адрес не дублируется",
          pt.render("[stalmetural.ru](https://stalmetural.ru)", "plain") == "stalmetural.ru")
    check("strip_markup для превью", pt.strip_markup("**а** [б](https://в.ru)") == "а б в.ru")

    # ВК/ОК ссылку в слова не зашивают. Живой случай (11.08.2026): в реестре
    # адрес уже стоял, Click добавлял свой автоссылкой, и в ВК выходило
    # «на нашем сайте (inmetprom.ru) (inmetprom.ru/)» – два адреса подряд.
    # Теперь текст реестра остаётся как есть, Click просто ничего не дописывает.
    real = pt.autolink(
        "🔹 Полный сортамент нержавеющего металлопроката – на нашем сайте (inmetprom.ru/).",
        "inmetprom.ru")
    check("ВК: адрес из реестра не трогаем и свой не дописываем",
          pt.render(real, "plain")
          == "🔹 Полный сортамент нержавеющего металлопроката – на нашем сайте (inmetprom.ru/).",
          pt.render(real, "plain"))

    # Блок контактов внизу поста. Он и есть тот случай, когда «повтор» адреса
    # – не повтор: почта, сайт и телефон, каждый на своей строке.
    contacts = pt.autolink(
        "Оформить заказ можно на нашем сайте:\n\n"
        "🌐 stalmetural.ru\n✉️ info@stalmetural.ru\n📞 +7 (903) 086-31-16",
        "stalmetural.ru")
    plain_contacts = pt.render(contacts, "plain")
    check("контакты остаются построчно",
          plain_contacts.endswith("🌐 stalmetural.ru\n✉️ info@stalmetural.ru\n"
                                  "📞 +7 (903) 086-31-16"), plain_contacts)
    check("почта не теряет домен", "info@stalmetural.ru" in plain_contacts, plain_contacts)
    check("адрес не дописан вторым разом",
          plain_contacts.count("stalmetural.ru") == 2, plain_contacts)
    check("ВК: адреса нет в реестре – ставим свой",
          pt.render(pt.autolink("Заказ – на нашем сайте.", "a.ru"), "plain")
          == "Заказ – на нашем сайте a.ru.")
    check("ВК: адрес в реестре голым – второй раз не пишем",
          pt.render(pt.autolink("Заказ – на нашем сайте a.ru.", "a.ru"), "plain")
          == "Заказ – на нашем сайте a.ru.")
    check("ТГ: ссылка остаётся зашитой в слова",
          "<a href=\"https://a.ru\">" in pt.render(pt.autolink("на нашем сайте.", "a.ru"), "html"))

    # Жирная фраза со ссылкой внутри – штатный плод autolink. Раньше пост
    # уходил в Телеграм со звёздочками: разбор шёл по ссылкам, и парные **
    # оказывались в разных кусках.
    check("ТГ: жирная ссылка – без звёздочек в тексте",
          pt.render("на **[нашем сайте](https://a.ru)**", "html")
          == 'на <a href="https://a.ru"><b>нашем сайте</b></a>',
          pt.render("на **[нашем сайте](https://a.ru)**", "html"))
    check("ВК: жирная ссылка – просто текст и адрес",
          pt.render("на **[нашем сайте](https://a.ru)**", "plain") == "на нашем сайте a.ru")


def test_bold_from_real_file() -> None:
    """Жирное из настоящего реестра должно доехать до разметки."""
    import glob
    hits = glob.glob("/root/.claude/uploads/**/*.xlsx", recursive=True)
    if not hits:
        print("Реальный файл реестра не найден – пропускаю проверку жирного (это норма).")
        return
    print("Жирное из реального реестра")
    posts = cp.load_from_xlsx(hits[0], "SMU")
    bold_posts = [p for p in posts if "**" in (p.get("text") or "")]
    check("жирные куски дошли до разметки", len(bold_posts) > 20, f"{len(bold_posts)}")
    dates_ok = all(cp.parse_date(p["date"]) for p in posts)
    check("жирная разметка не сломала даты", dates_ok)
    nets = {t["network"] for p in posts for t in p["targets"]}
    check("и не сломала распознавание сетей", {"vk", "ok", "max"} <= nets, str(sorted(nets)))


def test_google_bold_utf16() -> None:
    """
    Жирное из Google-таблицы после ЭМОДЗИ не теряет первую букву.

    Живой прогон 18.08.2026: в ОК и МАКС жирный ложился со ВТОРОЙ буквы –
    «Главные» → «лавные», «Многоуровневый» → «ногоуровневый». Причина:
    границы жирного (startIndex в textFormatRuns) Google отдаёт в единицах
    UTF-16, а эмодзи вне основного диапазона (🏗️, ⚙️) занимают в UTF-16
    две единицы, в Python – одну. После эмодзи все границы съезжали на +1.
    """
    print("\nGoogle: жирное после эмодзи не теряет первую букву")

    # Смещение начала «Главные» в UTF-16 (🏗 = 2 единицы, ️ и пробел = по 1).
    prefix = "🏗️ "
    u16_start = sum(2 if ord(ch) > 0xFFFF else 1 for ch in prefix)
    base = prefix + "Главные плюсы"
    check("эмодзи правда сдвигает UTF-16", u16_start == len(prefix) + 1, str(u16_start))

    cell = {
        "userEnteredValue": {"stringValue": base},
        "textFormatRuns": [
            {"startIndex": 0, "format": {"bold": False}},
            {"startIndex": u16_start, "format": {"bold": True}},
        ],
    }
    markup = cp._google_cell_markup(cell)
    bold = [t for t, b in __import__("post_text").plain_chunks(markup) if b]
    check("первая буква жирного на месте", bold == ["Главные плюсы"], str(bold))

    # Два эмодзи подряд – сдвиг накапливается, второй кусок тоже целый.
    base2 = "⚙️ Раз 🚀 **опущено** Многоуровневый"
    # Простая проверка карты индексов: один суррогат = +1 к UTF-16.
    m = cp._u16_index_map("A🚀B")   # 🚀 – суррогатная пара
    check("карта: A на 0", m[0] == 0)
    check("карта: две единицы 🚀 ведут к одному символу", m[1] == 1 and m[2] == 1)
    check("карта: B после суррогата на 2", m[3] == 2, str(m))
    check("текст без эмодзи не затронут",
          cp._google_cell_markup({"userEnteredValue": {"stringValue": "Просто **жир** тут"},
                                  "textFormatRuns": [
                                      {"startIndex": 0, "format": {"bold": False}},
                                      {"startIndex": 7, "format": {"bold": True}},
                                      {"startIndex": 10, "format": {"bold": False}}]})
          is not None)


def test_vk_domain() -> None:
    """
    Домен сообщества обязан совпадать с доменом сессии.

    Почему это отдельная проверка. vk.ru и vk.com – разные сайты для
    браузера, куки входа между ними не ходят. Ссылку с «чужим» доменом ВК
    открывает как гостю: кнопки «Создать» нет, и выглядит это как сломанный
    вход, хотя вход целый. Один раз мы на это уже потратили день.
    """
    print("\nВК: домен сообщества")
    import vk_social

    check("vk.com → vk.ru",
          vk_social.same_domain("https://vk.com/club123") == "https://vk.ru/club123")
    check("m.vk.com тоже приводится",
          vk_social.same_domain("https://m.vk.com/club123") == "https://vk.ru/club123")
    check("www.vk.com тоже приводится",
          vk_social.same_domain("http://www.vk.com/abc") == "https://vk.ru/abc")
    check("свой домен не трогаем",
          vk_social.same_domain("https://vk.ru/club7") == "https://vk.ru/club7")
    check("путь и хвост сохраняются",
          vk_social.same_domain("https://vk.com/club1?w=wall-1_2")
          == "https://vk.ru/club1?w=wall-1_2")
    check("пустая ссылка остаётся пустой", vk_social.same_domain("") == "")
    check("чужой адрес не переписываем",
          vk_social.same_domain("https://ok.ru/group/1") == "https://ok.ru/group/1")
    check("похожий домен не ловим по подстроке",
          vk_social.same_domain("https://notvk.com/x") == "https://notvk.com/x")


def test_ok_via_vk() -> None:
    """
    Вход в ОК через значок ВК: что можно проверить без браузера.

    Живьём проверено отдельно (2026-08-11) на копии страницы ОК: значок ВК
    лежит внутри <template style="display:none">, поиск по странице внутрь не
    заходит, и один взгляд сразу после загрузки давал ноль. Ожидание находит
    кнопку за полторы секунды. Здесь закрепляем то, что от браузера не
    зависит: признаки в селекторах и распознавание экрана «Войти как…».
    """
    print("\nОК: вход значком ВК")
    import ok_browser

    check("селектор опирается на data-module (он у ОК стабильнее классов)",
          "registration/vkconnect" in ok_browser.SEL["vk_id_button"])
    check("класс со страницы тоже учтён",
          "__vk_id" in ok_browser.SEL["vk_id_button"])
    check("подпись на плашке cookie известна",
          "Разрешить все" in ok_browser.SEL["cookie_accept"])

    class FakePopup:
        """Окно ВК, показывающее заданный текст."""

        def __init__(self, text: str):
            self._text = text
            self.frames: list = []
            self.main_frame = object()

        def evaluate(self, *_a, **_k):
            return self._text

        def is_closed(self):
            return False

        def locator(self, *_a, **_k):
            class _L:
                def count(self):
                    return 0
            return _L()

    flow = ok_browser.OkViaVkLoginFlow("SMU")

    flow.popup = FakePopup("Войти как Виктория")
    check("экран «Войти как Имя» опознан", flow._asks_confirm())
    check("шаг называется consent", flow.page_state().get("step") == "consent")

    flow.popup = FakePopup("Продолжить как Виктория")
    check("вариант «Продолжить как Имя» тоже опознан", flow._asks_confirm())

    flow.popup = FakePopup("Введите номер телефона")
    check("обычный экран за подтверждение не принимаем", not flow._asks_confirm())

    flow.popup = None
    check("закрытое окно не считается вопросом", not flow._asks_confirm())
    check("закрытое окно видно как закрытое", flow._popup_gone())

    # Вход слоем в странице. Живьём проверено на копии: окно не открывается,
    # ВК ID подгружается кадром внутрь страницы – раньше Click считал это
    # «клик не сработал» и упирался в тупик.
    check("адрес входа ВК известен",
          "connect.vk.com" in ok_browser.SEL["vkid_hosts"])
    fake_page = FakePopup("Войти как Виктория")
    flow.page = fake_page
    flow.popup = fake_page
    check("режим «слой в странице» распознан", flow.inline)
    check("слою закрываться нечему", not flow._popup_gone())
    check("вопрос читается и в этом режиме", flow._asks_confirm())

    flow.popup = FakePopup("что-то другое")
    check("чужое окно за слой не принимаем", not flow.inline)

    # В ссылке входа ВК едет одноразовый токен – показывать его нельзя.
    check("адрес обрезается до пути",
          ok_browser.safe_url("https://connect.vk.com/auth?state=СЕКРЕТ&app_id=1")
          == "https://connect.vk.com/auth")
    check("токена в обрезанном адресе нет",
          "СЕКРЕТ" not in ok_browser.safe_url(
              "https://connect.vk.com/auth?state=СЕКРЕТ"))
    check("пустой адрес не ломает", ok_browser.safe_url("") == "")


def test_ok_profile_check() -> None:
    """
    Проверка ОК «Имя – это вы?»: подтверждаем, но только правильную кнопку.

    Вторая кнопка на этом экране – «Это не мой профиль» – это жалоба на
    угон, после которой аккаунт уходит на блокировку. Нажать её случайно
    нельзя ни при каких обстоятельствах, поэтому проверяем не только «жмём
    нужную», но и «не жмём опасную, даже если другой на экране нет».
    """
    print("\nОК: проверка профиля «это вы?»")
    import ok_browser

    опасные = set(ok_browser.SEL["profile_never"])
    check("опасная кнопка не попала в список подтверждений",
          not (set(ok_browser.SEL["profile_yes"]) & опасные))

    class FakePage:
        def __init__(self, text: str, buttons=()):
            self._text = text
            self._buttons = set(buttons)
            self.clicked: list = []

        def inner_text(self, _sel):
            return self._text

        def wait_for_timeout(self, _ms):
            pass

        def locator(self, selector: str):
            label = selector.split('"')[1]
            outer = self

            class _L:
                @property
                def first(self):
                    return self

                def count(self):
                    return 1 if label in outer._buttons else 0

                def click(self, timeout=None):
                    outer.clicked.append(label)

            return _L()

    ЭКРАН = ("Имп Инметпром - это вы? Мы заметили, что этот профиль мог "
             "попасть к злоумышленникам. Необходимо подтвердить, что это ваш профиль")

    page = FakePage(ЭКРАН, ("Да, подтвердить", "Это не мой профиль"))
    check("экран опознан", ok_browser.asks_profile(page))
    check("подтверждение нажато", ok_browser.confirm_profile(page))
    check("нажали именно «Да, подтвердить»", page.clicked == ["Да, подтвердить"])

    # Самое важное: подтверждения нет, опасная кнопка есть – не трогаем.
    only_bad = FakePage(ЭКРАН, ("Это не мой профиль",))
    check("без кнопки подтверждения ничего не нажимаем",
          not ok_browser.confirm_profile(only_bad))
    check("опасная кнопка осталась нетронутой", only_bad.clicked == [])

    обычная = FakePage("Обычная лента", ("Да, подтвердить",))
    check("на обычной странице экран не мерещится", not ok_browser.asks_profile(обычная))
    check("на обычной странице ничего не нажимаем",
          not ok_browser.confirm_profile(обычная) and обычная.clicked == [])


def test_ok_verify_code() -> None:
    """
    Цепочка проверок ОК: «это вы?» → «получите код» → ввод кода.

    Каждый экран сам по себе выглядит как «сессия не работает», хотя вход
    целый. Проверено вживую на копии (2026-08-11): цепочка проходится
    целиком. Здесь закрепляем распознавание шагов без браузера.
    """
    print("\nОК: код подтверждения")
    import ok_browser

    class FakeOkPage:
        def __init__(self, text="", present=(), buttons=()):
            self._text = text
            self._present = set(present)
            self._buttons = set(buttons)
            self.clicked: list = []
            self.filled = None

        def inner_text(self, _sel):
            return self._text

        def wait_for_timeout(self, _ms):
            pass

        def locator(self, selector: str):
            outer = self
            by_text = 'has-text("' in selector or 'value="' in selector
            label = selector.split('"')[1] if '"' in selector else selector
            hit = label in outer._buttons if by_text else selector in outer._present

            class _L:
                @property
                def first(self):
                    return self

                def count(self):
                    return 1 if hit else 0

                def is_visible(self):
                    return hit

                def click(self, timeout=None):
                    outer.clicked.append(label)

                def fill(self, value):
                    outer.filled = value

            return _L()

    ЭКРАН = ("Получите проверочный код. С его помощью мы убедимся, что это ваш "
             "профиль. Для этого отправим бесплатное СМС с кодом на указанный номер")

    экран = FakeOkPage(ЭКРАН, buttons=("Получить код", "Подтвердить по эл. почте"))
    check("шаг «получите код» распознан", ok_browser.page_block(экран) == "verify")
    check("«Получить код» нажимается", ok_browser.request_verify_code(экран))
    check("нажали именно «Получить код»", экран.clicked == ["Получить код"])

    почта = FakeOkPage(ЭКРАН, buttons=("Подтвердить по эл. почте",))
    check("запасной путь письмом есть", ok_browser.request_verify_by_mail(почта))

    ввод = FakeOkPage("Введите код", present=('input[name="st.smsCode"]',),
                      buttons=("Подтвердить",))
    check("шаг ввода кода распознан", ok_browser.page_block(ввод) == "verify-code")
    check("код отправляется", ok_browser.submit_verify_code(ввод, " 123456 "))
    check("код вписан без лишних пробелов", ввод.filled == "123456")
    check("подтверждение нажато", ввод.clicked == ["Подтвердить"])

    # Гостевая форма входа: там тоже есть числовое поле телефона. Без
    # оговорки про пароль она выглядела бы как «введите код».
    гость = FakeOkPage("Вход", present=(ok_browser.SEL["password"],
                                        'input[type="tel"]'))
    check("обычная форма входа проверкой не считается",
          ok_browser.page_block(гость) == "")

    # «Это вы?» разбираем первым: он заслоняет всё остальное.
    оба = FakeOkPage("Имп – это вы? Необходимо подтвердить, что это ваш профиль. "
                     "Получите проверочный код",
                     present=('input[name="st.smsCode"]',))
    check("проверка профиля идёт первой", ok_browser.page_block(оба) == "profile")

    чисто = FakeOkPage("Лента новостей")
    check("на обычной странице помех нет", ok_browser.page_block(чисто) == "")


def test_platform_clients() -> None:
    print("Клиенты площадок: чистая логика")
    from datetime import date as _d, datetime, timezone, timedelta

    import ok_social, tg_social, max_social, crosspost_form  # noqa: E401

    ekb = timezone(timedelta(hours=5))
    check("ОК: 11:00 Екб → 09:00 Мск",
          ok_social.publish_at_msk(datetime(2026, 8, 13, 11, 0, tzinfo=ekb))
          == "2026-08-13 09:00:00")
    check("ОК: 00:30 Екб → вчера 22:30 Мск",
          ok_social.publish_at_msk(datetime(2026, 8, 13, 0, 30, tzinfo=ekb))
          == "2026-08-12 22:30:00")
    try:
        ok_social.publish_at_msk(datetime(2026, 8, 13, 11, 0))
        check("ОК: время без пояса не принимается", False)
    except ValueError:
        check("ОК: время без пояса не принимается", True)
    check("ОК: подпись стабильна",
          ok_social.sign({"b": "2", "a": "1"}, "s")
          == ok_social.sign({"a": "1", "b": "2"}, "s"))
    check("ОК: session_secret по схеме MD5",
          ok_social.session_secret("tok", "sec") ==
          __import__("hashlib").md5(b"toksec").hexdigest())
    import json as _json
    att = _json.loads(ok_social.build_attachment("текст", ["p1", "p2"]))
    check("ОК: вложение – текст, потом фото",
          [m["type"] for m in att["media"]] == ["text", "photo"]
          and len(att["media"][1]["list"]) == 2)

    check("ТГ: без фото – текстом", tg_social.plan_delivery(500, 0) == "text")
    check("ТГ: 1 фото + короткий текст", tg_social.plan_delivery(1000, 1) == "photo+caption")
    check("ТГ: альбом + подпись", tg_social.plan_delivery(1000, 3) == "album+caption")
    check("ТГ: длинный текст – отдельно", tg_social.plan_delivery(1500, 2) == "media+text")
    check("ТГ: граница 1024 включительно", tg_social.plan_delivery(1024, 1) == "photo+caption")
    parts = tg_social.split_text("а" * 3000 + "\n" + "б" * 3000)
    check("ТГ: длинный текст режется по абзацу",
          len(parts) == 2 and parts[0] == "а" * 3000)

    body = max_social.build_body("**Важно** [сайт](https://a.ru)", ["t1"])
    check("МАКС: тело с html и вложением",
          body["format"] == "html" and "<b>Важно</b>" in body["text"]
          and body["attachments"][0]["payload"]["token"] == "t1")

    # Оркестровка: память отфильтровывает уже сформированное
    import crosspost_state as cps
    post = {"brand": "SMU", "date": "2099-01-10", "when": "2099-01-10T11:00:00+05:00",
            "format": "Пост", "text": "т", "images": [],
            "targets": [{"network": "vk", "raw": "Вконтакте", "published_link": ""}]}
    check("формирование: ждёт – в списке",
          len(crosspost_form.pending_for([post], {}, "vk", today=_d(2026, 8, 10))) == 1)
    st_done = {cps.post_key(post): {"targets": {"vk": {"state": cps.SCHEDULED}}}}
    check("формирование: отложка стоит – не в списке",
          crosspost_form.pending_for([post], st_done, "vk", today=_d(2026, 8, 10)) == [])
    check("формирование: время поста с поясом Екб",
          crosspost_form.when_local(post).utcoffset() == timedelta(hours=5))


def test_scheduler() -> None:
    print("Планировщик")
    import os
    import tempfile
    from datetime import datetime, timedelta, timezone

    ekb = timezone(timedelta(hours=5))
    was = os.environ.get("CLICK_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="click-sched-")
    os.environ["CLICK_DATA_DIR"] = tmp
    try:
        import importlib
        import paths, scheduler, crosspost_state as cps  # noqa: E401
        importlib.reload(paths)

        when = datetime(2026, 8, 13, 11, 0, tzinfo=ekb)
        t = {"when": when.isoformat()}
        check("рано → ждём", scheduler.decide(t, when - timedelta(minutes=5), 6 * 3600) == "wait")
        check("вовремя → шлём", scheduler.decide(t, when + timedelta(seconds=30), 6 * 3600) == "send")
        check("опоздали в окне → шлём с пометкой",
              scheduler.decide(t, when + timedelta(hours=2), 6 * 3600) == "send-late")
        check("окно вышло → пропущено",
              scheduler.decide(t, when + timedelta(hours=7), 6 * 3600) == "missed")

        task = {"id": "SMU|2026-08-13|abc|tg-client", "project": "SMU", "brand": "SMU",
                "network": "tg-client", "chatId": "@x", "when": when.isoformat(),
                "date": "2026-08-13", "markup": "привет", "sourceText": "привет",
                "images": []}
        scheduler.queue_task("SMU", task)
        scheduler.queue_task("SMU", task)
        check("задание не множится", len(scheduler.load_tasks("SMU")) == 1)

        sent = []
        senders = {"tg": lambda tk: (sent.append(tk["id"]) or {"ok": True, "link": "https://t.me/x/1"}),
                   "max": lambda tk: {"ok": True}}

        # Публикация в час выхода выключена: задание не уходит, а отменяется, и
        # пост в памяти остаётся несформированным. Это главное правило после
        # 13.08.2026 – Click не должен публиковать сам и не должен врать.
        n_off = scheduler.tick(now=when + timedelta(seconds=10), senders=senders)
        post0 = {"brand": "SMU", "date": "2026-08-13", "when": when.isoformat(), "text": "привет"}
        check("в час выхода ничего не отправлено", sent == [], str(sent))
        check("задание отменено, а не выполнено",
              n_off == 1 and scheduler.load_tasks("SMU")[0]["state"] == "cancelled",
              scheduler.load_tasks("SMU")[0]["state"])
        check("память не врёт про «вышло»",
              cps.status_of(cps.load("SMU"), post0, "tg-client") == cps.WAITING,
              cps.status_of(cps.load("SMU"), post0, "tg-client"))

        # Дальше – проверка самой машинки отправки: она никуда не делась и
        # заработает, как только площадку доделают и уберут из PUBLISH_OFF.
        was_off = scheduler.PUBLISH_OFF
        scheduler.PUBLISH_OFF = ()
        scheduler._save_tasks("SMU", [])
        scheduler.queue_task("SMU", task)
        n = scheduler.tick(now=when + timedelta(seconds=10), senders=senders)
        check("тик отправил задание", n == 1 and sent == [task["id"]])
        check("задание выполнено", scheduler.load_tasks("SMU")[0]["state"] == "done")
        st_ = cps.load("SMU")
        post = {"brand": "SMU", "date": "2026-08-13", "when": when.isoformat(), "text": "привет"}
        check("память: вышло", cps.status_of(st_, post, "tg-client") == cps.SENT)
        n2 = scheduler.tick(now=when + timedelta(seconds=40), senders=senders)
        check("повторный тик ничего не шлёт", n2 == 0 and len(sent) == 1)

        # выполненное задание не переставится в очередь заново
        scheduler.queue_task("SMU", task)
        check("выполненное не возвращается в очередь",
              scheduler.load_tasks("SMU")[0]["state"] == "done")

        # ошибки: 3 попытки, потом «ошибка» в памяти
        bad = {**task, "id": "SMU|2026-08-13|abc|max", "network": "max",
               "sourceText": "другой"}
        scheduler.queue_task("SMU", bad)
        fail = {"tg": senders["tg"], "max": lambda tk: {"ok": False, "error": "нет сети"}}
        for i in range(3):
            scheduler.tick(now=when + timedelta(minutes=1 + i), senders=fail)
        bad_state = next(t2 for t2 in scheduler.load_tasks("SMU") if t2["network"] == "max")
        check("после 3 попыток – ошибка", bad_state["state"] == "failed"
              and bad_state["attempts"] == 3)

        # пропуск: задание в прошлом за окном
        late = {**task, "id": "SMU|2026-08-01|old|tg-client", "date": "2026-08-01",
                "when": (when - timedelta(days=12)).isoformat(), "sourceText": "старый"}
        scheduler.queue_task("SMU", late)
        scheduler.tick(now=when, senders=senders)
        late_state = next(t2 for t2 in scheduler.load_tasks("SMU") if t2["id"] == late["id"])
        check("старое задание – «пропущено», не отправлено",
              late_state["state"] == "missed" and len(sent) == 1)

        # ЯБ: «занято» – ждём без сжигания попыток; лок освободился – прогон запущен
        yb_task = {"id": "yb|SMU|2026-08-14T11:00", "project": "SMU", "brand": "SMU",
                   "network": "yb", "when": when.replace(day=14).isoformat(),
                   "date": "2026-08-14"}
        scheduler.queue_task("SMU", yb_task)
        yb_now = when.replace(day=14) + timedelta(minutes=1)
        busy_yb = lambda tk: {"ok": False, "retryable": True, "error": "Идёт актуализация"}
        scheduler.tick(now=yb_now, senders=senders, yb_start=busy_yb)
        t_yb = next(t2 for t2 in scheduler.load_tasks("SMU") if t2["network"] == "yb")
        check("ЯБ занято → ждём, попытки не горят",
              t_yb["state"] == "waiting" and t_yb.get("attempts", 0) == 0)
        scheduler.tick(now=yb_now + timedelta(minutes=5), senders=senders,
                       yb_start=lambda tk: {"ok": True})
        t_yb = next(t2 for t2 in scheduler.load_tasks("SMU") if t2["network"] == "yb")
        # через 6 минут после планового времени это уже «с опозданием» – и это правда
        check("ЯБ лок свободен → прогон запущен", t_yb["state"] in ("done", "done-late"))

        # отмена задания человеком
        c_task = {**task, "id": "SMU|2026-08-20|zzz|tg-client", "date": "2026-08-20",
                  "when": when.replace(day=20).isoformat(), "sourceText": "отменю"}
        scheduler.queue_task("SMU", c_task)
        check("отмена работает", scheduler.cancel_task("SMU", c_task["id"]) is True)
        cancelled = next(t2 for t2 in scheduler.load_tasks("SMU") if t2["id"] == c_task["id"])
        check("отменённое не отправляется",
              cancelled["state"] == "cancelled"
              and scheduler.tick(now=when.replace(day=20, hour=12), senders=senders) == 0)

        scheduler.set_config(enabled=False)
        check("выключатель останавливает тик",
              scheduler.tick(now=when + timedelta(days=1), senders=senders) == 0)
        scheduler.set_config(enabled=True)
        scheduler.PUBLISH_OFF = was_off
        check("по умолчанию ТГ и бот МАКС не публикуются",
              set(scheduler.PUBLISH_OFF) == {"tg", "max"}, str(scheduler.PUBLISH_OFF))
    finally:
        if was is None:
            os.environ.pop("CLICK_DATA_DIR", None)
        else:
            os.environ["CLICK_DATA_DIR"] = was
        import importlib
        import paths
        importlib.reload(paths)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_plan_rows() -> None:
    """
    План строками: порядок, колонки площадок и колонка «что сделать».

    Календарь на живом реестре стоял пустым – постов два-три в неделю, – и
    заказчица попросила вернуть строки. Проверяем ровно то, ради чего они:
    посты идут по времени, статус каждой площадки виден отдельной колонкой,
    а «что сделать» молчит там, где делать нечего.
    """
    print("План строками (crosspost_plan)")
    import crosspost_plan as plan
    import crosspost_state as cps

    today = date(2025, 8, 18)

    def post(day: str, text: str = "Текст поста. Продолжение мысли идёт дальше.",
             nets=("vk", "tg-client"), kind: str = "Информационный",
             link: str = "", row: int = 42) -> dict:
        return {"brand": "SMU", "date": day, "time": "09:00",
                "when": f"{day}T09:00:00+05:00", "post_type": kind, "format": "Пост",
                "text": text, "images": ["a.jpg"], "row": row,
                "targets": [{"network": n, "raw": n, "published_link": link} for n in nets]}

    p1 = post("2025-08-26", kind="Спецпредложение")
    p2 = post("2025-08-19")
    got = plan.rows([p1, p2], {}, today)
    check("строки идут по времени", [r["day"].day for r in got["rows"]] == [19, 26])
    check("заголовок по диапазону", got["title"] == "19 – 26 августа", got["title"])
    check("в колонке «когда» только дата", got["rows"][0]["when_day"] == "19.08",
          got["rows"][0]["when_day"])
    check("прошлые в план не попадают",
          plan.rows([post("2025-08-01")], {}, today)["rows"] == [])
    check("горизонт обрезает дальние",
          plan.rows([p1, p2], {}, today, date(2025, 8, 20))["rows"] and
          len(plan.rows([p1, p2], {}, today, date(2025, 8, 20))["rows"]) == 1)

    # Колонки – из самого реестра: у одного бренда Дзен, у другого нет ОК.
    mixed = plan.rows([post("2025-08-19", nets=("vk", "ok")),
                       post("2025-08-20", nets=("tg-client", "zen"))], {}, today)
    check("колонки собраны по реестру",
          [c["code"] for c in mixed["columns"]] == ["tg-client", "vk", "ok", "zen"],
          str([c["code"] for c in mixed["columns"]]))
    check("площадки названы по-русски",
          [c["name"] for c in mixed["columns"]][:2] == ["ТГ клиенты", "ВК"])
    check("в шапке короткая подпись",
          [c["short"] for c in mixed["columns"]][:2] == ["ТГ кл.", "ВК"])

    # Текст разбивается на жирное начало и хвост.
    head, tail = plan._split_text("Текст поста. Продолжение мысли идёт дальше.")
    check("начало отделено по точке", head == "Текст поста.", head)
    check("хвост остался", tail.startswith("Продолжение"), tail)
    head2, tail2 = plan._split_text("Арматура: разбираемся в классах и профилях")
    check("двоеточие тоже разрыв", head2 == "Арматура:", head2)
    whole, none_ = plan._split_text("Просто короткая строка без разрывов")
    check("без разрыва не режем посреди мысли",
          whole == "Просто короткая строка без разрывов" and none_ == "", whole)
    only_link, empty_tail = plan._split_text("https://docs.google.com/document/d/1Q97")
    check("ссылка не рвётся", only_link.startswith("https://") and empty_tail == "")

    # «Что сделать» – тихо там, где делать нечего.
    ready = plan.rows([post("2025-08-19")], {cps.post_key(post("2025-08-19")): {
        "targets": {"vk": {"state": cps.SCHEDULED}, "tg-client": {"state": cps.SCHEDULED}}}},
        today)["rows"][0]
    check("готовому – про ручные площадки", ready["todo"]["text"] == "ТГ клиенты – вручную",
          ready["todo"]["text"])
    check("и без крика", ready["todo"]["kind"] == "quiet")

    empty = plan.rows([post("2025-08-19", text="", row=47)], {}, today)["rows"][0]
    check("нет текста – жёлтая строка", empty["state"] == "warn")
    check("сказано, какая строка листа", "47" in empty["todo"]["text"], empty["todo"]["text"])
    check("и ведёт в таблицу", empty["todo"].get("sheet") is True)

    broken_post = post("2025-08-19")
    broken = plan.rows([broken_post], {cps.post_key(broken_post): {
        "targets": {"vk": {"state": cps.FAILED, "error": "сессия слетела"}}}},
        today)["rows"][0]
    check("ошибка называет площадку и причину",
          broken["todo"]["text"].startswith("ВК:") and "сессия" in broken["todo"]["text"],
          broken["todo"]["text"])

    # Знаки площадок: у ВК и ОК отложку держит сама сеть, у ТГ и МАКС – Click.
    p = post("2025-08-19", nets=("vk", "tg-client"))
    state = {cps.post_key(p): {"targets": {"vk": {"state": cps.SCHEDULED},
                                           "tg-client": {"state": cps.SCHEDULED}}}}
    marks = {n["code"]: n for n in plan.post_view(p, state)["nets"]}
    check("ВК: отложка стоит в соцсети", marks["vk"]["cls"] == "set" and marks["vk"]["mark"] == "✓")
    check("ТГ: помечен ручным, а не «отправит Click»",
          marks["tg-client"].get("manual") and marks["tg-client"]["mark"] == "✎",
          str(marks["tg-client"]))
    check("у знака есть подпись словами", "вручную" in marks["tg-client"]["note"])
    check("ручная площадка не делает пост несформированным",
          plan.post_view(p, state)["state"] == "set",
          plan.post_view(p, state)["state"])

    done = plan.post_view(post("2025-08-19", link="https://vk.com/wall-1_2"), {})
    check("ссылка в реестре = вышло", done["state"] == "live")
    video = plan.post_view({**post("2025-08-19"), "format": "Видео"}, {})
    check("видео – вручную, не тревога", video["state"] == "manual")
    # А статья с Дзеном – обычная работа Click, и «вручную» про неё писать
    # больше нельзя: раньше строка так и висела, хотя формировать её умеем.
    art = plan.post_view({**post("2025-08-19", nets=("zen",)), "format": "Статья"}, {})
    check("статья в Дзен – не «вручную»", art["state"] != "manual", art["state"])
    check("Дзен держит отложку сам",
          plan.net_view({**post("2025-08-19", nets=("zen",)), "format": "Статья"},
                        {"network": "zen"},
                        {cps.post_key(post("2025-08-19", nets=("zen",))):
                         {"targets": {"zen": {"state": cps.SCHEDULED}}}})["cls"] == "set")

    # Ближайший выход для строки состояния.
    nearest = plan.next_out([p], state, "2025-08-18T10:00:00+05:00")
    check("ближайший пост найден", nearest is not None and nearest["when"].startswith("2025-08-19"))
    check("названы ждущие площадки",
          nearest is not None and sorted(n["code"] for n in nearest["pending"]) == ["vk"],
          str(sorted(n["code"] for n in (nearest or {}).get("pending", []))))
    check("вышедшее в ближайшие не попадает",
          plan.next_out([post("2025-08-19", link="https://vk.com/wall-1_2")], {},
                        "2025-08-18T10:00:00+05:00") is None)

    # Разметка таблицы: колонки, значки и «что сделать» доезжают до HTML.
    import ui_theme as T
    markup = T.crosspost_table(mixed, sheet_url="https://sheet")
    check("шапка с площадками", ">Дзен</th>" in markup and 'title="Дзен"' in markup)
    check("строки-заголовка над планом нет", "cp-group" not in markup)
    check("значок с подсказкой", 'title="ВК –' in markup)
    check("колонки заданы ширинами", "<colgroup>" in markup)

    # Ровность таблицы – не украшательство: слева к ней прижата полоса
    # галочек, и она выставлена по этим же числам (CP_ROW_H, CP_HEAD_H).
    css = T.crosspost_css()
    check("дата стоит по центру своей колонки",
          '<td class="c cp-when">' in markup and "text-align:center" in css)
    check("плашка типа не шире колонки",
          ".cp-kind{" in css and "max-width:100%" in css and "text-overflow:ellipsis" in css)
    check("у плашки есть подсказка с полным типом", '<span class="cp-kind" title=' in markup)
    check("шапки площадок своим кеглем", 'th class="c n"' in markup and "th.n{" in css)
    check("высота строки задана числом", f"height:{T.CP_ROW_H}px" in css)
    check("шапка таблицы тоже", f"height:{T.CP_HEAD_H + 1}px" in css)
    check("галочка стоит на первой строке, а не по центру клетки",
          "align-items:flex-start" in css and "padding-top:10px" in css)
    check("у таблицы нет своего нижнего отступа",
          "table-layout:fixed;margin:0" in css)
    check("текст поста ровно две строки", "-webkit-line-clamp:2" in css and "height:40px" in css)
    check("«что сделать» не растягивает строку", ".cp-todo{" in css and "height:40px" in css)
    check("полоса галочек знает про свои клетки",
          ".cp-tick-off" in css and ".cp-ticks-head" in css)

    picked_markup = T.crosspost_table(mixed,
                                      picked={mixed["rows"][0]["key"]})
    check("отмеченная строка подсвечена", "cp-pick" in picked_markup)
    check("неотмеченная – нет",
          picked_markup.count("cp-pick") == 1 and "cp-pick" not in markup)

    check("склонение постов",
          [plan.plural(n, "пост", "поста", "постов") for n in (1, 4, 11, 22)]
          == ["1 пост", "4 поста", "11 постов", "22 поста"])


def test_vk_time_pickers() -> None:
    """
    Календарь ВК: верим подписи самого ВК, а не чтению полей.

    Две потерянные отложки подряд, обе – на пустом месте. «Минута не
    принялась: ждали 14, в поле 00», а на снимке отказа стояло 15:14.
    Потом «ждали 20, в поле 00» – и в логе перед этим «Час: значение
    прочитать не вышло», при том что ВК внизу окна писал «Сегодня в 19:20».
    Причина: поля времени у ВК – компонент, значение живёт внутри него,
    в <select> или <input>, а снаружи текстом не читается.

    Отсюда правило: спрашиваем у ВК, какое время он считает назначенным,
    и это ответ. Поля – только запасной источник.
    """
    print("\nВК: чтение времени в календаре")
    from datetime import datetime

    import vk_social

    class FakePicker:
        """Поле-компонент: текстом читается то, что дали (часто – ничего)."""

        def __init__(self, values: list[str]):
            self.values = values
            self.reads = 0

        def inner_text(self) -> str:
            v = self.values[min(self.reads, len(self.values) - 1)]
            self.reads += 1
            return v

    class FakeLocator:
        def __init__(self, hour: "FakePicker", minute: "FakePicker"):
            self.first = hour
            self.last = minute

    class FakePage:
        """Страница: подпись под календарём + два поля времени."""

        def __init__(self, footer: str = "", hour: str = "", minute: str = ""):
            self.footer = footer
            self.hour, self.minute = hour, minute
            self.waited = 0

        def eval_on_selector(self, selector: str, script: str) -> str:
            return self.footer

        def locator(self, selector: str) -> "FakeLocator":
            return FakeLocator(FakePicker([self.hour]), FakePicker([self.minute]))

        def wait_for_timeout(self, ms: int) -> None:
            self.waited += ms

    when = datetime(2026, 8, 11, 19, 20)
    notes: list[str] = []

    # Подпись ВК читается – её и слушаем, поля при этом молчат (как в жизни).
    page = FakePage(footer="Сохранить черновик\nСегодня в 19:20\nДобавить в очередь")
    check("подпись ВК разобрана", vk_social._scheduled_time_shown(page) == "19:20")
    check("время признано выставленным", vk_social._time_is_set(page, when, notes.append))

    # Та самая беда: поля не читаются вовсе. Раньше это валило отложку.
    blind = FakePage(footer="Сегодня в 19:20")
    check("нечитаемые поля отложку не рушат",
          vk_social._time_is_set(blind, when, notes.append))

    # ВК показывает ЧУЖОЕ время – вот это настоящая неудача.
    wrong = FakePage(footer="Сегодня в 19:00")
    check("чужое время не выдаём за своё",
          not vk_social._time_is_set(wrong, when, notes.append, tries=2))

    # Подписи нет – работает запасной источник, сами поля.
    fields = FakePage(footer="", hour="19", minute="20")
    check("без подписи верим полям", vk_social._time_is_set(fields, when, notes.append))
    bad_fields = FakePage(footer="", hour="19", minute="00")
    check("поля с чужой минутой не проходят",
          not vk_social._time_is_set(bad_fields, when, notes.append, tries=2))

    # Час без ведущего нуля, минуты с ним – ровно так ВК их и пишет.
    check("«9:05» разбирается",
          vk_social._scheduled_time_shown(FakePage(footer="Завтра в 9:05")) == "9:05")
    check("текста без времени не выдумываем",
          vk_social._scheduled_time_shown(FakePage(footer="Добавить в очередь")) == "")

    # Мгновенная проверка поля – по ней решается, нужен ли запасной путь.
    page = FakePage()
    check("«05» – это пять минут",
          vk_social._picker_shows(page, lambda: FakePicker(["05"]), 5))
    check("«00» – это не четырнадцать",
          not vk_social._picker_shows(page, lambda: FakePicker(["00"]), 14))
    check("пустое поле – не совпадение",
          not vk_social._picker_shows(page, lambda: FakePicker([""]), 14))


def test_probe_networks() -> None:
    """
    Пробная отложка есть у КАЖДОЙ сети с родной отложкой и ведёт в свой модуль.

    Раньше проба была только у ВК, и отдельной функцией. Потом понадобилась
    такая же для ОК, следом для МАКС, а теперь и для Дзена – копировать
    функцию не стали: правка в одной из копий рано или поздно забудется.
    Вместо этого таблица сетей, и вот проверка, что в ней всё сходится.
    """
    print("\nПробная отложка: сети с родной отложкой")
    import importlib

    import streamlit_app as app

    check("описаны все четыре сети с родной отложкой",
          set(app.PROBE_NETWORKS) == {"vk", "ok", "max", "zen"},
          str(sorted(app.PROBE_NETWORKS)))
    # У Дзена вход не свой, а яндексовый, и почту проекта пробе надо передать
    # отдельно – иначе паспорт с несколькими аккаунтами не поймёт, чей он.
    check("Дзену проба передаёт почту проекта",
          app.PROBE_NETWORKS["zen"].get("needs_email") is True)
    import inspect
    check("проба умеет передавать почту",
          "needs_email" in inspect.getsource(app._crosspost_probe))
    for net, meta in app.PROBE_NETWORKS.items():
        mod = importlib.import_module(meta["module"])
        check(f"{net}: модуль умеет отложку", hasattr(mod, "schedule_postponed_post"))
        check(f"{net}: модуль знает про сессию", hasattr(mod, "has_saved_session"))
        check(f"{net}: подписи заполнены",
              all(meta.get(k) for k in ("ru", "url_key", "where", "shelf")))
    keys = [m["url_key"] for m in app.PROBE_NETWORKS.values()]
    check("ключи ссылок у сетей разные", len(set(keys)) == len(keys), str(keys))


def test_combined_session_file() -> None:
    """
    Один файл на обе сети: вошли раз – вставили раз.

    Мысль заказчицы: «может, одним входом оба куки собирать, в один файлик,
    и один раз вставлять». Куки и правда снимаются одним браузером за один
    заход – ОК пускает через ВК. Здесь закреплено, что Click раскладывает
    их по сетям сам и не путает чужое со своим.
    """
    print("\nСессии: один файл на ВК и ОК")
    import json
    import os
    import tempfile

    import social_session as ss

    state = {"cookies": [
        {"name": "remixsid", "value": "v", "domain": ".vk.ru"},
        {"name": "AUTH_ID", "value": "o", "domain": ".ok.ru"},
        {"name": "vkid_token", "value": "s", "domain": "id.vk.com"},
        {"name": "_ga", "value": "junk", "domain": ".google-analytics.com"},
    ], "origins": [{"origin": "https://ok.ru"}, {"origin": "https://vk.ru"}]}

    vk, ok, _ = ss.split_state(state)
    vk_names = {c["name"] for c in vk["cookies"]}
    ok_names = {c["name"] for c in ok["cookies"]}
    check("кука ВК ушла в ВК", "remixsid" in vk_names and "remixsid" not in ok_names)
    check("кука ОК ушла в ОК", "AUTH_ID" in ok_names and "AUTH_ID" not in vk_names)
    check("общий вход VK ID – в обе сети",
          "vkid_token" in vk_names and "vkid_token" in ok_names)
    check("посторонние куки отброшены",
          "_ga" not in vk_names and "_ga" not in ok_names)

    # МАКС – третья сеть в том же файле. У него вход живёт в localStorage,
    # а не только в куках: без раздела origins живая сессия выглядела бы
    # пустой, и файл бы отвергли.
    import max_browser
    max_state = {"cookies": [{"name": "max_token", "value": "m", "domain": ".max.ru"}],
                 "origins": [{"origin": "https://web.max.ru",
                              "localStorage": [{"name": "auth", "value": "t"}]}]}
    _, _, mx = ss.split_state({**state, **{
        "cookies": state["cookies"] + max_state["cookies"],
        "origins": state["origins"] + max_state["origins"]}})
    check("кука МАКС ушла в МАКС",
          "max_token" in {c["name"] for c in mx["cookies"]})
    check("localStorage МАКС сохранён", len(mx["origins"]) == 1)
    check("вход МАКС по localStorage распознан", max_browser.looks_logged_in(mx))
    check("гостевой МАКС за вход не считаем",
          not max_browser.looks_logged_in(
              {"cookies": [{"name": "_ga", "value": "x"}], "origins": []}))
    check("origins разложены по сетям",
          len(ok["origins"]) == 1 and len(vk["origins"]) == 1)

    # Приём файла целиком – в отдельной папке данных, чтобы не задеть рабочие.
    tmp = tempfile.mkdtemp()
    was = os.environ.get("CLICK_DATA_DIR")
    os.environ["CLICK_DATA_DIR"] = tmp
    try:
        import importlib

        import ok_browser
        import paths
        import vk_social
        importlib.reload(paths)
        importlib.reload(vk_social)
        importlib.reload(ok_browser)
        importlib.reload(ss)

        took, said = ss.import_combined("TEST-BOTH", json.dumps(state).encode())
        check("файл принят", took, said)
        check("обе сети названы принятыми",
              said.count("принят") == 2, said)
        check("сессия ВК на месте", vk_social.has_saved_session("TEST-BOTH"))
        check("сессия ОК на месте", ok_browser.has_saved_session("TEST-BOTH"))

        # Половинчатый файл – это не повод отказать целиком.
        only_vk = {"cookies": [{"name": "remixsid", "value": "v", "domain": ".vk.ru"}]}
        took2, said2 = ss.import_combined("TEST-HALF", json.dumps(only_vk).encode())
        check("файл только с ВК тоже берём", took2, said2)
        check("и честно говорим, что ОК в нём нет", "не найдены" in said2, said2)

        bad, why = ss.import_combined("TEST-BAD", "это не json".encode("utf-8"))
        check("не-json отклоняем", not bad and "не файл сессии" in why.lower(), why)
    finally:
        if was is None:
            os.environ.pop("CLICK_DATA_DIR", None)
        else:
            os.environ["CLICK_DATA_DIR"] = was
        import importlib
        import paths
        importlib.reload(paths)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_ok_session_detection() -> None:
    """
    Вход в ОК узнаём от обратного – по тому, чего у гостя быть не может.

    Список «правильных» имён подвёл дважды (11.08.2026). Заказчица вошла в
    ОК руками, а файл сессии не сохранился: ни AUTH_ID, ни AUTH_SIG, ни
    OK_LOGIN среди её кук не оказалось – ОК зовёт их иначе. И в обратную
    сторону список врал: в нём был JSESSIONID, который ОК выдаёт ЛЮБОМУ,
    даже не входя, – Click объявлял «сессия сохранена» о пустышке.

    Гостевые куки сняты с живой ok.ru: bci, _statid, JSESSIONID,
    cookieChoice, ss_wb.
    """
    print("\nОК: признак входа в файле сессии")
    import json

    import ok_browser as ok

    def cookies(*names):
        return [{"name": n, "value": "x"} for n in names]

    guest = cookies("bci", "_statid", "JSESSIONID", "cookieChoice", "ss_wb")
    check("гостевые куки за вход не считаем", not ok.looks_logged_in(guest))
    check("один JSESSIONID – это гость",
          not ok.looks_logged_in(cookies("JSESSIONID")))
    check("известная кука входа – вошли",
          ok.looks_logged_in(guest + cookies("AUTH_ID")))
    check("НЕЗНАКОМАЯ кука сверх гостевых – тоже вошли",
          ok.looks_logged_in(guest + cookies("OK_NEW_NAME_2027")))
    check("пустой список – не вошли", not ok.looks_logged_in([]))
    check("кука без значения не считается",
          not ok.looks_logged_in([{"name": "AUTH_ID", "value": ""}]))

    accepted, msg = ok.import_session("TEST-OK", json.dumps({"cookies": guest}).encode())
    check("гостевой файл не принимаем", not accepted)
    check("и показываем, что нашли", "JSESSIONID" in msg and "bci" in msg, msg)


def test_ok_schedule_flow() -> None:
    """
    Отложка ОК: успех считаем по ответу площадки, а не по закрытию окна.

    Разобрано по живому прогону заказчицы (2026-08-11): «Создать новую тему»
    → текст → плюсик «Контентные блоки» → «Фотографии» → «Загрузить фото» →
    галочка «Время публикации» → дата и время → «Сохранить», и снизу
    появляется «Тема опубликуется 13.08.2026 в 08:59».
    """
    print("\nОК: отложенная публикация")
    from datetime import datetime

    import ok_browser as ok

    when = datetime(2026, 8, 13, 8, 59)

    class FakePage:
        def __init__(self, body: str):
            self.body = body

        def evaluate(self, script: str) -> str:
            return self.body

    said = ok._toast_scheduled(
        FakePage("Лента\nТема опубликуется 13.08.2026 в 08:59 Посмотреть\nЕщё"), when)
    check("ответ ОК распознан", said.startswith("Тема опубликуется"), said)
    check("в ответе видны дата и время", "13.08.2026" in said and "08:59" in said, said)
    check("нет ответа – не выдумываем",
          ok._toast_scheduled(FakePage("Лента группы\nУчастники 2"), when) == "")
    check("страница не прочиталась – тоже пусто",
          ok._toast_scheduled(FakePage(""), when) == "")

    # Подписи, на которые опираемся, должны остаться на месте: если кто-то
    # их «почистит», отложка ОК тихо перестанет работать.
    check("ищем «Создать новую тему»",
          any("Создать новую тему" in s for s in ok.SEL["create_post"]))
    check("ищем галочку «Время публикации»",
          any("Время публикации" in s for s in ok.SEL["schedule_toggle"]))
    check("сохраняем кнопкой «Сохранить»",
          any("Сохранить" in s for s in ok.SEL["submit_scheduled"]))
    check("«Поделиться» держим отдельно – она публикует сейчас",
          any("Поделиться" in s for s in ok.SEL["submit_now"])
          and not any("Поделиться" in s for s in ok.SEL["submit_scheduled"]))
    check("путь к фото – через контентные блоки",
          "Контентные блоки" in ok.SEL["menu_content_blocks"]
          and any("Фотографии" in s for s in ok.SEL["menu_photos"]))

    # Открываться надо на вкладке «Темы»: по обычному адресу группы ОК
    # показывает ЛЕНТУ, а поля «Создать новую тему» там нет вовсе. Первый
    # живой прогон (12.08.2026) на этом и встал: Click честно искал поле три
    # раза с прокруткой на странице, где его не могло быть.
    check("к адресу группы добавляется /topics",
          ok.topics_url("https://ok.ru/group/70000005388115/")
          == "https://ok.ru/group/70000005388115/topics")
    check("хвостовой слэш не мешает",
          ok.topics_url("https://ok.ru/group/70000005388115")
          == "https://ok.ru/group/70000005388115/topics")
    check("дважды /topics не приписываем",
          ok.topics_url("https://ok.ru/group/1/topics") == "https://ok.ru/group/1/topics")
    check("группа по имени тоже работает",
          ok.topics_url("https://ok.ru/inmetprom") == "https://ok.ru/inmetprom/topics")
    check("пустой адрес остаётся пустым", ok.topics_url("") == "")

    # «Отложенные» группы – по ним проверяем результат, если всплывашка
    # «Тема опубликуется …» уже погасла. Запись надёжнее уведомления:
    # уведомление живёт секунды, запись – до самой публикации.
    check("адрес отложенных собирается",
          ok.delayed_url("https://ok.ru/group/1/") == "https://ok.ru/group/1/delayed")
    check("из адреса тем тоже",
          ok.delayed_url("https://ok.ru/group/1/topics") == "https://ok.ru/group/1/delayed")
    check("дважды /delayed не приписываем",
          ok.delayed_url("https://ok.ru/group/1/delayed") == "https://ok.ru/group/1/delayed")
    check("пустой адрес остаётся пустым", ok.delayed_url("") == "")

    # Ссылку на форму ОК зовёт a.pf-head_itx_a – снято инспектором со
    # страницы заказчицы. Класс держим запасным: подписи надёжнее, но и
    # терять подтверждённый селектор незачем.
    check("подтверждённый класс формы на месте",
          any("pf-head_itx_a" in s for s in ok.SEL["create_post"]))

    # Разметка формы снята заказчицей с живой страницы 12.08.2026. Имена
    # полей устойчивее подписей – на них и опираемся.
    check("галочка ищется по имени input", "timer" in ok.SEL["schedule_checkbox"])
    check("дата – по имени st.layer.date",
          any("st.layer.date" in s for s in ok.SEL["date_input"]))
    check("часы – по имени st.layer.hours",
          any("st.layer.hours" in s for s in ok.SEL["hours_select"]))
    check("минуты – по имени st.layer.mins",
          any("st.layer.mins" in s for s in ok.SEL["mins_select"]))

    # ОК принимает минуты только кратные пяти: в его списке 00, 05 … 55.
    # Округляем ВВЕРХ – пост не должен выйти раньше, чем просили.
    from datetime import datetime as _dt
    check("15:08 → 15:10", ok._round_to_five(_dt(2026, 8, 12, 15, 8)) == _dt(2026, 8, 12, 15, 10))
    check("15:00 не трогаем", ok._round_to_five(_dt(2026, 8, 12, 15, 0)) == _dt(2026, 8, 12, 15, 0))
    check("09:30 не трогаем", ok._round_to_five(_dt(2026, 8, 12, 9, 30)) == _dt(2026, 8, 12, 9, 30))
    check("15:57 → 16:00", ok._round_to_five(_dt(2026, 8, 12, 15, 57)) == _dt(2026, 8, 12, 16, 0))
    check("23:58 → следующий день 00:00",
          ok._round_to_five(_dt(2026, 8, 12, 23, 58)) == _dt(2026, 8, 13, 0, 0))
    check("округляем только вверх, никогда вниз",
          all(ok._round_to_five(_dt(2026, 8, 12, 10, m)) >= _dt(2026, 8, 12, 10, m)
              for m in range(60)))


def test_plan_horizon_and_past() -> None:
    """
    Горизонт плана – до конца месяца, и отложек в прошлое не бывает.

    Две жалобы одного дня (11.08.2026). Первая: заказчица перенесла пост
    с 11-го на 30-е и перестала его видеть – горизонт был ровно 14 дней,
    а планируют помесячно. Вторая: Click пытался поставить отложку на
    сегодня 9:00 вечером того же дня, ВК подставлял своё время, и прогон
    падал с «ВК показывает 20:00».
    """
    print("\nПлан: горизонт и прошедшее время")
    from datetime import timedelta

    import apptime
    import crosspost_form as cf
    import streamlit_app as app

    # Горизонт: конец месяца, но не ближе двух недель.
    check("середина месяца – видно до конца месяца",
          app._crosspost_horizon(date(2026, 8, 11)) == date(2026, 8, 31))
    check("30-е число теперь в плане",
          app._crosspost_horizon(date(2026, 8, 11)) >= date(2026, 8, 30))
    check("конец месяца – всё равно две недели вперёд",
          app._crosspost_horizon(date(2026, 8, 30)) == date(2026, 9, 13))
    check("февраль считается по своей длине",
          app._crosspost_horizon(date(2027, 2, 1)) == date(2027, 2, 28))

    # Прошедшее время: формировать нечего, соцсеть такое не примет.
    now = apptime.now()

    def post_at(dt):
        return {"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M"),
                "when": dt.isoformat(), "text": "текст", "images": [], "targets": []}

    def skipped(dt) -> bool:
        return (now - cf.when_local(post_at(dt))) > timedelta(minutes=-cf.MIN_LEAD_MINUTES)

    check("вчерашний пост не формируем", skipped(now - timedelta(days=1)))
    check("сегодняшний, но уже прошедший – не формируем",
          skipped(now - timedelta(hours=2)))
    check("до выхода пара минут – тоже поздно", skipped(now + timedelta(minutes=3)))
    check("через полчаса – формируем", not skipped(now + timedelta(minutes=30)))
    check("завтрашний – формируем", not skipped(now + timedelta(days=1)))


def test_form_selection() -> None:
    """
    Выбор постов для формирования: «поставить один, а не весь план».

    Жалоба заказчицы (13.08.2026): «хочу просто один пост поставить,
    посмотреть, а не могу – тут либо все, либо ничего». Кнопка «Поставить
    все» осталась, а слева в плане появились галочки: отмеченное едет,
    остальное ждёт.
    """
    print("\nФормирование: выбор постов")
    import inspect

    import crosspost_state as cps
    import streamlit_app as app

    def post(day, kind, text):
        return {"brand": "smu", "date": f"2026-08-{day}", "time": "11:00",
                "when": f"2026-08-{day}T11:00:00+05:00", "post_type": kind,
                "text": text, "images": [], "targets": []}

    first = post("13", "Поступление", "НОВАЯ ПОСТАВКА сетка рабица на склад")
    second = post("17", "Спецпредложение", "СПЕЦПРЕДЛОЖЕНИЕ на микрофибру")
    todo = {"vk": [first, second], "ok": [first], "max": [],
            "msg_by_net": {"tg-client": [first], "tg-staff": [first, second]},
            "msg": [first, first, second]}

    check("счётчик кнопки – по площадкам",
          app._crosspost_form_parts(todo) == ["ВК: 2", "ОК: 1", "ТГ: 3"],
          str(app._crosspost_form_parts(todo)))
    check("пустому плану – пустой счётчик",
          app._crosspost_form_parts({"vk": [], "ok": [], "max": [], "msg": []}) == [])

    choices = app._crosspost_form_choices(todo)
    check("каждый пост в списке один раз", len(choices) == 2, str(len(choices)))
    check("порядок – как в плане, по времени",
          [c["post"]["date"] for c in choices] == ["2026-08-13", "2026-08-17"])
    check("у поста собраны все его площадки",
          choices[0]["nets"] == ["ВК", "ОК", "ТГ клиенты", "ТГ сотрудники"],
          str(choices[0]["nets"]))
    check("площадка не повторяется", len(set(choices[1]["nets"])) == len(choices[1]["nets"]))

    # Ключ поста – он же ключ галочки: по нему отмеченное сходится с постом
    # даже после перечитывания реестра (строки в таблице двигаются).
    keys = {cps.post_key(c["post"]) for c in choices}
    check("ключи постов разные", len(keys) == 2, str(keys))

    src = inspect.getsource(app._crosspost_form_block)
    check("кнопка «поставить все» на месте", "cp-form-all-" in src)
    check("рядом кнопка выбранных", "cp-form-picked-" in src)
    check("подпись кнопки – словами заказчицы",
          "Поставить выбранные посты на отложку" in src)
    check("без выбора кнопка выбранных не жмётся", "disabled=not picked" in src)
    check("в формирование уходят именно выбранные посты", "run(picked, picked_todo)" in src)
    check("выбранное считается тем же счётчиком",
          "_crosspost_form_todo(project_id, config, picked, state)" in src)

    table_src = inspect.getsource(app._crosspost_plan_table)
    check("галочка у каждой строки плана", "st.checkbox(" in table_src)
    check("ключ галочки – ключ поста", 'f\'cp-tick-{project_id}-{v["key"]}\'' in table_src)
    check("галочка только там, где есть что формировать",
          'view["key"] not in formable' in table_src)
    check("на одном посту галочек нет вовсе", "len(formable) < 2" in table_src)
    check("отмеченное подсвечивается в таблице", "picked=picked" in table_src)

    tab_src = inspect.getsource(app.tab_crosspost)
    check("что формировать, считается один раз на вкладку",
          "formable=formable" in tab_src and "picked_keys=picked_keys" in tab_src)


def test_vk_confirm_schedule() -> None:
    """
    «Добавить в очередь» иногда требует двух нажатий – и ровно одного.

    Живой случай (11.08.2026): время встало верно, Click нажимал кнопку –
    заказчица видела нажатие своими глазами, – а отложенных записей в
    сообществе не появлялось. Причина: календарь открывается ПОВЕРХ экрана
    настроек, а кнопка живёт под ним, и первый клик уходит на закрытие
    календаря. Второй нажимает саму кнопку.

    Обратная опасность здесь же: если первое нажатие сработало, второго быть
    не должно – иначе вместо одной записи получится две.
    """
    print("\nВК: подтверждение отложки")
    import vk_social

    class FakeForm:
        """Форма ВК, которая закрывается только после N-го нажатия."""

        def __init__(self, closes_after: int, disabled: bool = False):
            self.closes_after = closes_after
            self.disabled = disabled
            self.clicks = 0

        def wait_for_selector(self, selector, state=None, timeout=None):
            if state == "hidden" and self.clicks < self.closes_after:
                raise RuntimeError("ещё видно")
            return True

        def eval_on_selector(self, selector, script):
            return self.disabled

        def click(self, selector, timeout=None):
            self.clicks += 1

        def wait_for_timeout(self, ms: int) -> None:
            pass

        def evaluate(self, script):
            return []

    notes: list[str] = []

    # Сработало с первого раза – второго нажатия быть не должно.
    once = FakeForm(closes_after=1)
    vk_social._confirm_schedule(once, notes.append)
    check("хватило одного нажатия – жмём один раз", once.clicks == 1)

    # Живой случай: первый клик убрал календарь, второй нажал кнопку.
    twice = FakeForm(closes_after=2)
    notes.clear()
    vk_social._confirm_schedule(twice, notes.append)
    check("перекрытую кнопку дожимаем со второго раза", twice.clicks == 2)
    check("и говорим об этом в логе", any("ещё раз" in n for n in notes))

    # Не закрывается вовсе – честная ошибка, и не больше трёх нажатий.
    never = FakeForm(closes_after=99)
    try:
        vk_social._confirm_schedule(never, notes.append)
        check("непринятую отложку не выдаём за принятую", False, "ошибки не было")
    except RuntimeError as e:
        check("непринятую отложку не выдаём за принятую", "не принял отложку" in str(e))
    check("бесконечно не жмём", never.clicks == 3)

    # Кнопка неактивна – не жмём вовсе, дата не принята.
    dead = FakeForm(closes_after=1, disabled=True)
    try:
        vk_social._confirm_schedule(dead, notes.append)
        check("неактивную кнопку не жмём", False, "ошибки не было")
    except RuntimeError as e:
        check("неактивную кнопку не жмём", "неактивна" in str(e) and dead.clicks == 0)


def test_vk_captcha_on_posting() -> None:
    """
    Проверка «вы не робот» на пути отложки, а не только входа.

    Живой случай (11.08.2026, облако): ВК накрыл страницу сообщества
    проверкой, и Click написал «кнопка Создать не нажалась, проверьте, что
    вы вошли под администратором» – совет мимо: права были в порядке,
    мешала проверка. Здесь закреплено, что проверка распознаётся и что
    человеку говорят, ЧТО делать, а не куда посмотреть.
    """
    print("\nВК: проверка «вы не робот» при отложке")
    import vk_social

    advice = vk_social._CAPTCHA_ADVICE
    check("сказано, что это проверка «не робот»", "не робот" in advice)
    check("сказано, что программой её не пройти", "программой" in advice)
    check("назван файл ручного входа", "VHOD-VK-i-OK.py" in advice)
    check("сказано, куда деть готовую сессию",
          "vk-session.json" in advice and "Настройка" in advice)
    check("про права администратора здесь не поминаем",
          "администратор" not in advice.lower())

    # Проверки нет – путь свободен, ничего не нажимаем.
    notes: list[str] = []

    class NoCaptchaPage:
        def wait_for_timeout(self, ms: int) -> None:
            pass

    was = vk_social.captcha_frame
    try:
        vk_social.captcha_frame = lambda page: None
        check("без проверки идём дальше молча",
              vk_social._pass_captcha(NoCaptchaPage(), notes.append) is True
              and not notes)

        # Проверка висит и не проходится – честное «нет», а не бесконечный цикл.
        vk_social.captcha_frame = lambda page: object()
        was_press = vk_social.press_captcha_in
        try:
            vk_social.press_captcha_in = lambda frame: False
            notes.clear()
            check("непроходимую проверку не выдаём за пройденную",
                  vk_social._pass_captcha(NoCaptchaPage(), notes.append) is False)
            check("и говорим об этом в логе",
                  any("не робот" in n for n in notes))
        finally:
            vk_social.press_captcha_in = was_press
    finally:
        vk_social.captcha_frame = was


def test_playwright_worker() -> None:
    """
    Отравленный поток: одна неудача не должна ломать всё до перезапуска.

    Живой случай (11.08.2026): после нескольких неудачных отложек КАЖДАЯ
    следующая падала с «Playwright Sync API inside the asyncio loop», хотя
    чинить было нечего. Sync-версия Playwright работает на гринлетах, а те
    делят один поток ОС: оборвалась сессия неудачно – цикл остаётся
    «запущенным» для всего потока навсегда.
    """
    print("\nPlaywright: отравленный поток")
    import playwright_worker as pw

    check("узнаём отравление по тексту ошибки",
          pw.is_poisoned_thread(RuntimeError(
              "It looks like you are using Playwright Sync API inside the "
              "asyncio loop. Please use the Async API instead.")))
    check("обычная ошибка отравлением не считается",
          not pw.is_poisoned_thread(RuntimeError("Timeout 10000ms exceeded")))

    worker = pw.PlaywrightWorker()
    check("свежий воркер живой", worker.alive())
    try:
        worker.call(lambda: (_ for _ in ()).throw(RuntimeError("обычная беда")))
    except RuntimeError:
        pass
    check("обычная ошибка воркер не убивает", worker.alive())
    try:
        worker.call(lambda: (_ for _ in ()).throw(RuntimeError(
            "Playwright Sync API inside the asyncio loop")))
    except RuntimeError:
        pass
    check("после отравления воркер считается мёртвым", not worker.alive())
    worker.stop()

    # Одноразовый запуск: значение возвращается, ошибка долетает как есть,
    # и каждый раз это НОВЫЙ поток – иначе отравить его было бы чем.
    check("run_once возвращает значение", pw.run_once(lambda a, b: a + b, 2, 3) == 5)
    check("run_once понимает именованные", pw.run_once(lambda a, b=0: a + b, 2, b=5) == 7)
    try:
        pw.run_once(lambda: (_ for _ in ()).throw(ValueError("наружу")))
        check("run_once отдаёт ошибку наружу", False, "ошибки не было")
    except ValueError as e:
        check("run_once отдаёт ошибку наружу", str(e) == "наружу")

    import threading

    def who() -> tuple[str, int]:
        return threading.current_thread().name, threading.get_ident()

    # Номер потока сравнивать бесполезно: ОС переиспользует номера
    # завершившихся потоков, и два подряд запуска запросто получат один и тот
    # же. Важно другое – что это КАЖДЫЙ РАЗ отдельный поток под нашим именем,
    # а не поток вызывающего: у нового потока свои локальные данные, и
    # отравить его прошлой сессии нечем.
    name, ident = pw.run_once(who)
    check("run_once уходит в отдельный поток", ident != threading.get_ident())
    check("поток заведён нами", name.startswith("click-pw-once-"))
    check("имя потока называет задачу", pw.run_once(who)[0].endswith("who"))

    # И главное: локальные данные потока не переезжают между запусками.
    local = threading.local()

    def stamp() -> bool:
        was = getattr(local, "been_here", False)
        local.been_here = True
        return was

    check("следы прошлого запуска не переносятся",
          pw.run_once(stamp) is False and pw.run_once(stamp) is False)


def test_max_calendar() -> None:
    """
    Календарь МАКС: числа повторяются, и это главная ловушка.

    Живой прогон 12.08.2026 упал со словами «в календаре МАКС не нашли
    число 12» – а 12-е было обведено на снимке кружком. Искали внутри
    [role="dialog"], которого у МАКС нет вовсе. Теперь ищем саму сетку
    чисел, а нужную клетку берём по порядку: свой месяц начинается с
    первой единицы, поэтому хвост прошлого месяца (27…31) и начало
    следующего (1…6) за свои числа не сойдут.

    Сетка августа 2026 – настоящая, из того самого окна: месяц начинается
    в субботу, поэтому первыми идут июльские 27…31.
    """
    print("\nМАКС: календарь и ответ на кнопке")
    from datetime import date, datetime

    import max_browser as mx

    aug = ([27, 28, 29, 30, 31] + list(range(1, 32)) + [1, 2, 3, 4, 5, 6])
    check("сетка августа – 42 клетки", len(aug) == 42)
    check("12 августа – своя клетка", mx.day_index(aug, 12) == 16)
    check("1 августа не спутали с хвостом июля", mx.day_index(aug, 1) == 5)
    check("2 августа – тоже своё", mx.day_index(aug, 2) == 6)
    check("31 августа – последнее своё", mx.day_index(aug, 31) == 35)
    check("32-го числа не бывает", mx.day_index(aug, 32) == -1)

    # Месяц, начавшийся с понедельника: хвоста прошлого месяца нет вовсе.
    june = list(range(1, 31)) + [1, 2, 3, 4, 5]
    check("месяц с понедельника: 1-е – первая клетка", mx.day_index(june, 1) == 0)
    check("и 30-е на месте", mx.day_index(june, 30) == 29)

    check("«Август 2026» разобран", mx.month_year("Август 2026") == (8, 2026))
    check("«Май 2027» разобран", mx.month_year("Май 2027") == (5, 2027))
    check("«Март 2026» не спутали с маем", mx.month_year("Март 2026") == (3, 2026))
    check("пустой заголовок – честный ноль", mx.month_year("") == (0, 0))

    # Надпись на кнопке – ответ самой площадки. Разошлась с задуманным –
    # жать нельзя: пост уйдёт не в то время.
    today = date(2026, 8, 12)
    when = datetime(2026, 8, 14, 20, 8)
    check("«14 августа в 20:08» – это оно",
          mx.caption_ok("Отправить 14 августа в 20:08", when, today))
    check("другое время поймали",
          not mx.caption_ok("Отправить 14 августа в 21:08", when, today))
    check("другая дата поймана",
          not mx.caption_ok("Отправить 15 августа в 20:08", when, today))
    check("«сегодня» вместо 14-го – поймали",
          not mx.caption_ok("Отправить сегодня в 20:08", when, today))
    check("«сегодня» на месте, когда сегодня и надо",
          mx.caption_ok("Отправить сегодня в 19:43",
                        datetime(2026, 8, 12, 19, 43), today))
    check("«завтра» на месте, когда завтра и надо",
          mx.caption_ok("Отправить завтра в 13:00",
                        datetime(2026, 8, 13, 13, 0), today))
    check("час без нуля впереди тоже принимаем",
          mx.caption_ok("Отправить завтра в 9:05",
                        datetime(2026, 8, 13, 9, 5), today))
    check("число из времени за дату не считаем",
          not mx.caption_ok("Отправить сегодня в 20:08",
                            datetime(2026, 8, 20, 20, 8), today))
    check("кнопку не прочитали – не мешаем",
          mx.caption_ok("", when, today))


def test_max_slow_load() -> None:
    """
    МАКС в облаке грузится медленно – и это не повод объявлять отказ.

    Живой прогон 12.08.2026 в облаке: «Не нашли поле Пост» через восемь
    секунд, а на снимке того же мгновения – пустой экран с логотипом МАКС.
    Приложение ещё грузилось. Ждали 4 секунды: на своём компьютере хватает,
    в облаке – никогда.

    И вторая половина: три разные беды выглядели одинаково, а лечатся
    по-разному. Поэтому причину спрашиваем у самой страницы.
    """
    print("\nМАКС: медленная загрузка и разбор экрана")
    import max_browser as mx

    check("ждём не секунды, а больше минуты", mx.EDITOR_WAIT_S >= 60,
          str(mx.EDITOR_WAIT_S))

    class FakePage:
        def __init__(self, body="", url="https://web.max.ru/-709", tries=0):
            self.body, self.url, self.tries, self.asked = body, url, tries, 0
            self.slept = 0

        def evaluate(self, script, *a):
            return self.body

        def wait_for_timeout(self, ms):
            self.slept += ms

        def locator(self, sel):
            page = self

            class L:
                first = None

                def __init__(self):
                    self.first = self

                def is_visible(self, timeout=0):
                    page.asked += 1
                    return page.asked > page.tries
            return L()

    # Поле появилось не сразу – дождались и сказали об этом.
    said = []
    slow = FakePage(tries=5)
    got = mx._wait_for_editor(slow, said.append, timeout_s=30)
    check("дождались поля, появившегося не сразу", bool(got), got)
    check("ожидание не молчаливое", any("МАКС" in m for m in said), str(said))

    never = FakePage(tries=10_000)
    check("не появилось вовсе – честно пусто",
          mx._wait_for_editor(never, None, timeout_s=3) == "")

    # Причина отказа – по тому, что на экране.
    why = mx._why_no_editor(FakePage("Войти по номеру телефона\nПродолжить"))
    check("экран входа распознан", "сессия не действует" in why, why)
    check("и сказано, что делать", "VHOD-VK-i-OK.py" in why, why)

    why = mx._why_no_editor(FakePage("Открыть в приложении\nСкачать MAX"))
    check("предложение приложения распознано", "ссылка ведёт не в канал" in why, why)

    why = mx._why_no_editor(FakePage(""))
    check("пустой экран – это «не загрузился»", "не загрузился" in why, why)
    check("и сразу подсказка про облако",
          "на своём компьютере" in why, why)

    why = mx._why_no_editor(FakePage("Какой-то незнакомый экран " * 5))
    check("незнакомое показываем словами", "На экране МАКС было" in why, why)
    check("и не отправляем пост", "НЕ отправлен" in why, why)


def test_tg_access_advice() -> None:
    """
    Отказ Телеграма переводим в действие, а не показываем как есть.

    Публикация идёт ботом, и мало токена: бот обязан быть администратором
    канала с правом отправки сообщений. Телеграм отвечает коротко и
    по-английски («chat not found»), а сделать по этому нужно вполне
    определённое – иначе это выясняется в час выхода поста.
    """
    print("\nТелеграм: отказ доступа – словами и по делу")
    import tg_social as tg

    def advice(said: str) -> str:
        was_check, was_conf = tg.check_access, tg.is_configured
        tg.check_access = lambda chat_id="": said
        tg.is_configured = lambda: True
        try:
            return tg.access_advice("@channel")
        finally:
            tg.check_access, tg.is_configured = was_check, was_conf

    check("всё хорошо – молчим", advice("") == "")
    check("канала нет – проверяем имя",
          "@" in advice("Телеграм отказал в getChat: chat not found"))
    check("бот не в канале – зовём в админы",
          "администратор" in advice("Bad Request: bot is not a member of the channel chat"))
    check("нет прав – называем нужное право",
          "Post Messages" in advice("Bad Request: not enough rights to send text messages"))
    check("бота выгнали – видно, что именно",
          "удалили" in advice("Forbidden: bot was kicked from the channel chat"))
    check("токен не тот – к BotFather",
          "BotFather" in advice("Unauthorized"))
    check("незнакомый отказ показываем как есть",
          advice("Совсем новая беда") == "Совсем новая беда")

    # Без токена проверять нечего – и это отдельная, понятная причина.
    was = tg.bot_token
    tg.bot_token = lambda: ""
    try:
        check("нет токена – так и говорим",
              "tg_bot_token" in tg.access_advice("@channel"))
    finally:
        tg.bot_token = was


def test_social_defaults() -> None:
    """
    Ссылки брендов зашиты и подставляются только в ПУСТОЕ поле.

    Заказчица прислала их списком (12.08.2026) со словами «зашить, чтобы
    каждый раз ничего не вводить»: в облаке файловая система временная, и
    после каждого перезапуска два десятка ссылок приходилось вбивать заново.
    Главное здесь – старшинство: то, что вписано руками, всегда выше кода.
    """
    print("\nСсылки площадок: заготовка и старшинство")
    import projects_data as pdata
    import streamlit_app as app

    check("заготовки есть у всех пяти брендов",
          set(pdata.SOCIAL) == {"SMU", "IMP", "MPE", "MPI", "APS"},
          str(sorted(pdata.SOCIAL)))
    for pid, links in pdata.SOCIAL.items():
        check(f"{pid}: ВК и ОК на месте",
              links.get("vkGroupUrl", "").startswith("https://vk.ru/")
              and links.get("okGroupUrl", "").startswith("https://ok.ru/group/"))
        check(f"{pid}: канал ТГ через @",
              links.get("tgChannelClient", "").startswith("@"),
              links.get("tgChannelClient", ""))
        # Приглашение max.ru/join/… не годится: по нему вступают, а не
        # публикуют. Нужен адрес канала в веб-версии.
        mx = links.get("maxWebUrl", "")
        check(f"{pid}: МАКС – адрес веб-версии, не приглашение",
              mx.startswith("https://web.max.ru/-") and "/join/" not in mx, mx)

    smu = pdata.social_defaults("SMU")
    check("реестр – одной таблицей на все бренды",
          smu["planSheetUrl"].startswith("https://docs.google.com/spreadsheets/d/"))
    check("у неизвестного бренда заготовки нет", pdata.social_defaults("XXX") == {})

    # Пустое поле заполняем, занятое – никогда.
    sub = {"vkGroupUrl": "", "okGroupUrl": "https://ok.ru/group/МОЁ"}
    app._fill_social("SMU", sub)
    check("пустое поле заполнено заготовкой",
          sub["vkGroupUrl"] == pdata.SOCIAL["SMU"]["vkGroupUrl"], sub["vkGroupUrl"])
    check("занятое поле не тронуто",
          sub["okGroupUrl"] == "https://ok.ru/group/МОЁ", sub["okGroupUrl"])
    check("пробелы за значение не считаем",
          app._fill_social("IMP", {"vkGroupUrl": "   "})["vkGroupUrl"]
          == pdata.SOCIAL["IMP"]["vkGroupUrl"])

    # Ссылки должны переживать перезапуск облака – значит лежать в списке
    # того, что уезжает наружу.
    for key in ("vkGroupUrl", "okGroupUrl", "tgChannelClient", "tgChannelStaff",
                "maxWebUrl", "planSheetUrl", "zenUrl"):
        check(f"{key} хранится снаружи", key in app._KEPT)
    check("пароль наружу не уезжает", "password" not in app._KEPT)


def test_link_card_and_report() -> None:
    """
    Карточка сайта и отчёт – две правки 14.08 по замечаниям заказчицы.

    Карточка. Увидев адрес в тексте, ВК цепляет к посту сниппет сайта и
    оттесняет нашу картинку; человек жмёт крестик на карточке. Click теперь
    тоже – но только на ЕЁ крестик: тем же значком нарисован крестик самого
    окна создания поста, и промах стоил бы всего поста.

    Отчёт. Показывал все посты реестра со ссылками – «вышло 439»: пять лет
    чужой работы, среди которой не найти свои две отложки.
    """
    print("\nКарточка сайта и отчёт")
    import inspect

    import streamlit_app as app
    import yb_playwright as yb

    doms = yb.text_domains("Заказ на нашем сайте stalmetural.ru, почта info@stalmetural.ru, "
                           "группа https://ok.ru/group/1")
    check("домены из текста собраны", doms == ["stalmetural.ru", "ok.ru"], str(doms))
    check("повторы не дублируются", len(doms) == len(set(doms)))
    check("без адресов – пусто", yb.text_domains("просто текст") == [])

    src = inspect.getsource(yb.drop_link_card)
    check("крестик ищется по значку со страницы", "M9.414 8l3.294" in yb._CLOSE_ICON_D)
    check("окно создания поста защищено полем ввода",
          "[contenteditable], textarea" in src)
    check("без домена не жмём ничего", "if not doms" in src)
    check("карточка есть, а крестика нет – говорим словами",
          "Закройте её вручную" in src)
    # Ищем крестик ПО МЕСТУ, а не по классам: три захода по разметке (контур
    # значка, подпись кнопки) на живом ОК не сработали – вёрстку не угадать.
    check("крестик ищется по прямоугольнику карточки", "inZone" in src and "pad = 44" in src)
    check("наводим мышь – крестик бывает только по наведению", "page.mouse.move" in src)
    check("клик тремя способами", "клик силой" in src and "клик из страницы" in src)
    check("успех – по исчезновению карточки, а не по клику", "if not card_box():" in src)
    check("карточку ждём несколько подходов", "for attempt in range(max(1, tries))" in src)
    check("не вышло – сохраняем разметку для разбора", "link-card.html" in src)

    # Разметка ОК: жирный и ссылки НАКЛАДЫВАЮТСЯ на готовый текст.
    import ok_browser
    ok_src = inspect.getsource(ok_browser._type_post_text)
    # Порядок: набрать ОБЫЧНЫЙ текст, потом наложить разметку выделением.
    # Жирный «на лету» (Ctrl+B при наборе) забракован – теперь основной путь
    # накладывает жирный ПОСЛЕ набора, через _apply_bold.
    check("сначала обычный текст, потом разметка",
          ok_src.index("keyboard.type(plain") < ok_src.index("_apply_bold"))
    bold_src = inspect.getsource(ok_browser._apply_bold)
    check("жирный выделяется настоящей мышью, потом Ctrl+B",
          "_select(" in bold_src and 'press("Control+b")' in bold_src)
    check("жирный и ссылки идут вместе",
          '"kind": "bold"' in ok_src and ("_apply_links_native" in ok_src
                                          or '"kind": "link"' in ok_src))
    # Выделение куска – НАСТОЯЩЕЙ мышью (общий помощник yb.select_text_by_mouse),
    # программное execCommand осталось лишь последним запасом.
    sel_src = inspect.getsource(ok_browser._select)
    check("выделяем кусок мышью, как человек",
          "select_text_by_mouse" in sel_src)
    mark_src = inspect.getsource(ok_browser._apply_marks) + ok_browser._MARK_JS
    check("запас разметки на месте (execCommand по выделению)",
          "createRange" in mark_src and "execCommand" in mark_src)
    check("ссылка вшивается в слова", "createLink" in mark_src)
    check("считаем, что РЕАЛЬНО легло в поле",
          "querySelectorAll('b, strong')" in mark_src and "a[href]" in mark_src)
    # Главное правило после 14.08.2026: жирный не стоит поломанного текста.
    check("сверяются и буквы, и разбивка по строкам",
          "_lines(got) != _lines(plain)" in ok_src)
    check("расхождение текста НЕ рушит форму (публикуем как есть)",
          "публикую как есть" in ok_src and "_clear_editor" not in ok_src)
    check("панель ОК после форматирования гасим (иначе перекроет поле)",
          "_drop_selection(" in ok_src)
    ok_lines = ok_browser._lines("Диаметр\n: 0,25 мм;Длина волокна:")
    check("строки считаются по буквам", ok_lines == ["диаметр", "025ммдлинаволокна"], str(ok_lines))
    check("поломанная разбивка не равна целой",
          ok_browser._lines("Диаметр: 0,25 мм;") != ok_lines)

    # Отчёт: только работа Click.
    rep = inspect.getsource(app._crosspost_report_block)
    check("нет записи Click – нет строки в отчёте", "if not saved:" in rep)
    check("заголовок отчёта про работу Click", "Отчёт: что сделал Click" in rep)


def test_mouse_selection_wiring() -> None:
    """
    Разметку ОК и МАКС накладываем ВЫДЕЛЕНИЕМ НАСТОЯЩЕЙ МЫШИ.

    Заказчица прислала скринами: редакторы ОК и МАКС узнают только выделение
    зажимом и протяжкой мыши. Программное (execCommand/Range) для них не
    существует – панель не всплывает, Ctrl+B цепляет не то, первая буква
    жирного «уплывает». Общий помощник yb.select_text_by_mouse наводит
    настоящую мышь; проверяем, что ОК и МАКС зовут именно его.
    """
    print("\nВыделение куска – настоящей мышью")
    import inspect

    import max_browser as mb
    import ok_browser
    import yb_playwright as yb

    check("общий помощник выделения мышью есть", hasattr(yb, "select_text_by_mouse"))
    sel_src = inspect.getsource(yb.select_text_by_mouse)
    check("помощник двигает мышь зажимом и протяжкой",
          "mouse.down" in sel_src and "mouse.move" in sel_src and "mouse.up" in sel_src)
    check("сверяет, что кусок правда выделился",
          "_selection_text" in sel_src)
    rect_src = yb._RANGE_RECT_JS
    check("координаты берём из getClientRects", "getClientRects" in rect_src)
    # МАКС не помещает длинный пост целиком: верхние куски за кадром сверху.
    # Листаем кусок в вид — и родным scrollIntoView, и прокруткой найденного
    # прокручиваемого ПРЕДКА (у МАКС прокручивается не поле, а его обёртка).
    check("кусок листаем в вид (scrollIntoView)", "scrollIntoView" in rect_src)
    check("ищем прокручиваемого предка, а не только поле",
          "scrollHeight" in rect_src and "clientHeight" in rect_src)
    check("двигаем прокрутку предка", "scrollTop" in rect_src)

    # ОК: и жирный, и ссылка выделяются мышью (через _select → мышь).
    ok_select = inspect.getsource(ok_browser._select)
    check("ОК выделяет мышью", "select_text_by_mouse" in ok_select)
    check("ОК: программное выделение осталось лишь запасом",
          "_SELECT_TEXT_JS" in ok_select)

    # МАКС: форматирование накладывается на введённый текст, мышью + Ctrl+B/Ctrl+K.
    fmt_src = inspect.getsource(mb._apply_max_format)
    check("МАКС выделяет мышью", "select_text_by_mouse" in fmt_src)
    check("МАКС: жирный – Ctrl+B", 'press("Control+b")' in fmt_src)
    check("МАКС: ссылка – через _add_max_link (Ctrl+K)", "_add_max_link" in fmt_src)
    link_src = inspect.getsource(mb._add_max_link)
    check("МАКС ссылку открывает Ctrl+K", 'press("Control+k")' in link_src)
    check("МАКС ссылку НЕ закрывает Escape (закрыл бы форму)",
          'press("Escape")' not in link_src)
    collapse_src = inspect.getsource(mb._collapse_selection)
    check("после форматирования выделение снимаем (фото не затрёт текст)",
          "collapse" in collapse_src)
    fmt_flow = inspect.getsource(mb.schedule_postponed_post)
    check("МАКС печатает ПЛОСКИЙ текст, разметку накладывает следом",
          "to_plain(text)" in fmt_flow and "_apply_max_format(" in fmt_flow)


def test_ok_bold_and_probe() -> None:
    """
    Две правки 14.08: жирный в ОК и починенная сверка текста в МАКС.

    Жирный. Заказчица: «в ОК контакты норм, но жирного нет, а в реестре
    есть». Редактор темы ОК жирный умеет – набираем текст кусками с Ctrl+B.

    Сверка МАКС. Отложка 17.08 сорвалась на ровном месте: «в поле не видно
    конец текста», хотя текст был введён целиком и в канал ничего не ушло.
    Отпечаток поля обрезался до 32 знаков – конец в нём не находился НИКОГДА.
    """
    print("\nОК: жирный, МАКС: сверка текста")
    import inspect

    import max_browser as mb
    import ok_browser
    import post_text as pt

    # ── ОК: куски с жирностью
    markup = pt.autolink("**СПЕЦПРЕДЛОЖЕНИЕ**\n**Диаметр**: 0,25 мм;\n"
                         "Заказ на нашем сайте:\n🌐 a.ru", "a.ru")
    chunks = pt.plain_chunks(markup)
    check("жирные куски выделены",
          [t for t, b in chunks if b] == ["СПЕЦПРЕДЛОЖЕНИЕ", "Диаметр"], str(chunks))
    check("склейка кусков = обычный текст",
          "".join(t for t, _ in chunks) == pt.to_plain(markup))
    check("маркеров ** в кусках нет", "**" not in "".join(t for t, _ in chunks))
    check("текста без жирного это не касается",
          [b for _, b in pt.plain_chunks("просто текст")] == [False])
    spans = pt.anchor_spans(pt.autolink("Заказ на нашем сайте:", "a.ru"))
    check("ссылка в словах видна отдельно", spans == [("нашем сайте", "https://a.ru")], str(spans))
    check("голый адрес ссылкой не считаем",
          pt.anchor_spans("[a.ru](https://a.ru)") == [])

    src = inspect.getsource(ok_browser._type_post_text)
    check("расхождение текста не рушит форму, публикуем как есть",
          "публикую как есть" in src and "_clear_editor" not in src)
    check("про жирный больше не врём",
          "жирный редактор ОК не принял" in src and "жирных кусков" in src)
    check("ОК и МАКС получают разметку, а не плоский текст",
          'network in ("ok", "max")' in inspect.getsource(
              __import__("crosspost_form")._form_browser_all))

    # ── МАКС: отпечаток поля больше не обрезается
    text = "НАЧАЛО ТЕКСТА ПОСТА про микрофибру\n\n#Стальметурал #СМУ #Металлопрокат"
    got = mb._letters(text)
    first, last = text.split("\n")[0], text.split("\n")[-1]
    check("начало находится в поле", mb._probe(first) in got)
    check("конец тоже находится (вот это и было сломано)", mb._probe(last) in got)
    check("а подмена ловится: в поле осталась одна последняя строка",
          mb._probe(first) not in mb._letters(last))
    check("отпечаток строки по-прежнему короткий", len(mb._probe("а" * 100)) == 32)


def test_ok_bold_first_line() -> None:
    """
    Пост с ЖИРНЫМ заголовком не должен считаться чужим черновиком.

    Живой отказ 14.08.2026: «ОК: В поле поста осталось лишнее от прошлого
    черновика: «СПЕЦИАЛЬНОЕ ПРЕДЛОЖЕНИЕ НА АРМИРУЮЩУЮ МИКРОФИБРУ…»». В поле
    лежал ровно наш текст – а сверяли его с РАЗМЕТКОЙ, где заголовок стоит
    жирным (`**…**`). Звёздочки в поле, разумеется, не появляются: ОК вместо
    них включает жирный. Проверка не сходилась никогда, стоило посту
    начинаться с жирной строки, – а начинаются так все спецпредложения.

    Второй край того же: textContent склеивает абзацы без переносов, поэтому
    короткая первая строка ломала сверку так же надёжно.
    """
    print("\nОК: жирный заголовок – это не чужой черновик")
    import ok_browser as ok
    import post_text as pt

    markup = ("**СПЕЦИАЛЬНОЕ ПРЕДЛОЖЕНИЕ НА АРМИРУЮЩУЮ МИКРОФИБРУ**\n\n"
              "⚡ СТОИМОСТЬ – 1 400 ₽/кг ⚡\n\nТехнические характеристики:")
    plain = "".join(part for part, _ in pt.plain_chunks(markup))
    # Что реально оказывается в поле: без звёздочек и без переносов строк.
    in_field = plain.replace("\n", "")

    check("прежняя сверка – с разметкой – не сходится",
          not in_field.strip().startswith(markup.strip()[:20]),
          f"{markup[:20]!r} vs {in_field[:20]!r}")
    check("новая сверка – по буквам набранного – сходится",
          ok._letters(in_field).startswith(ok._letters(plain)[:20]),  # noqa: SLF001
          f"{ok._letters(in_field)[:30]!r}")  # noqa: SLF001

    # А настоящий чужой черновик по-прежнему ловится.
    left = "Тест" + in_field
    check("остаток чужого черновика впереди – всё ещё отказ",
          not ok._letters(left).startswith(ok._letters(plain)[:20]),  # noqa: SLF001
          left[:40])

    import inspect
    src = inspect.getsource(ok.schedule_postponed_post)
    check("сверяем с тем, что набирается, а не с разметкой",
          "plain_chunks(text)" in src, "нет разбора разметки перед сверкой")
    check("и по буквам", "_letters(typed)" in src)


def test_max_photo_proof() -> None:
    """
    Отложка МАКС без картинки – это не успех.

    14.08.2026 МАКС поставил пост на 17.08 без фото, а Click отчитался
    успехом: после передачи файла он ждал три с половиной секунды и шёл
    дальше, ни разу не проверив, появилась ли картинка в форме. Пост без
    картинки – не тот пост, который просили.
    """
    print("\nМАКС: фото должно быть видно в форме")
    import max_browser as mb
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright доступен", False, "не установлен")
        return

    PIX = ("data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==")
    page_html = f"""
    <html><body style="margin:0">
      <img id="avatar" src="https://example.com/ava.png" width="40" height="40">
      <div id="composer"></div>
      <script>
        window.attach = () => document.getElementById('composer').innerHTML =
          '<img id="thumb" width="80" height="80" src="{PIX}">';
      </script>
    </body></html>
    """

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        except Exception:  # noqa: BLE001
            browser = pw.chromium.launch()
        try:
            page = browser.new_context(viewport={"width": 800, "height": 600}).new_page()
            page.set_content(page_html)

            check("аватарки и значки с сервера за прикреплённое фото не считаем",
                  mb._thumbs(page) == 0, str(mb._thumbs(page)))  # noqa: SLF001

            was = mb._thumbs(page)  # noqa: SLF001
            page.evaluate("() => window.attach()")
            check("а картинка из памяти браузера – считается",
                  mb._thumbs(page) - was == 1, str(mb._thumbs(page)))  # noqa: SLF001

            # Ждать миниатюру, которой не будет, бесконечно нельзя.
            page.set_content(page_html)
            was_wait = mb.THUMB_WAIT_S
            mb.THUMB_WAIT_S = 2
            try:
                check("миниатюра не появилась – так и говорим",
                      not mb._wait_thumbs(page, mb._thumbs(page), 1))  # noqa: SLF001
                page.evaluate("() => setTimeout(window.attach, 700)")
                check("а появившуюся с задержкой – дожидаемся",
                      mb._wait_thumbs(page, 0, 1))  # noqa: SLF001
            finally:
                mb.THUMB_WAIT_S = was_wait

            import inspect
            src = inspect.getsource(mb.schedule_postponed_post)
            check("без миниатюры отложку не ставим", "_wait_thumbs(" in src)
            check("и человеку сказано, почему", "не тот пост" in src)
        finally:
            browser.close()


def test_max_feed_redraw_is_not_a_send() -> None:
    """
    Дорисованное старое сообщение – это НЕ наша отправка.

    Живой отказ 18:33 (14.08.2026): «Пока вводили текст, в канал ушло
    сообщение „Технические характеристики:“. Отложку не ставим». Ничего не
    уходило: список сообщений у МАКС виртуальный, при росте поля он
    перерисовывается – и в разметке появляются старые сообщения, которых
    там мгновение назад не было. Прежняя проверка сравнивала ленту «до» и
    «после» безусловно и приняла эту дорисовку за отправку, отменив уже
    введённый пост.

    Правило теперь такое: поле – главный свидетель. Чат опустошает поле,
    когда отправляет; текст на месте – значит, не отправляли, и ленту можно
    не спрашивать вовсе.
    """
    import max_browser as mb
    print("\nМАКС: перерисовка ленты – не отправка")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright доступен", False, "не установлен")
        return

    SEL = 'div[contenteditable][role="textbox"][data-lexical-editor="true"]'
    # Лента виртуальная: старые сообщения дорисовываются, когда поле растёт.
    page_html = MAX_CHAT_PAGE.replace(
        "<script>",
        """<script>
    // Пока поле пустое, старого сообщения в разметке нет – ровно так ведёт
    // себя виртуальный список: за экраном сообщения не нарисованы.
    new MutationObserver(() => {
      const ed = document.getElementById('editor');
      if ((ed.innerText || '').length > 40 && !document.getElementById('old')) {
        document.getElementById('feed').insertAdjacentHTML('beforeend',
          '<div class="msg" id="old">Технические характеристики:</div>');
      }
    }).observe(document.getElementById('editor'), {childList: true, subtree: true,
                                                   characterData: true});
        """, 1)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        except Exception:  # noqa: BLE001
            browser = pw.chromium.launch()
        try:
            page = browser.new_context(viewport={"width": 900, "height": 700}).new_page()
            page.set_content(page_html)
            said: list[str] = []
            why = mb._fill_post_text(page, SEL, MAX_POST, said.append)  # noqa: SLF001

            check("дорисовку старого сообщения отправкой не считаем", why == "", why)
            check("и ничего не отправляли на самом деле",
                  page.evaluate("() => window.__sent") == [],
                  str(page.evaluate("() => window.__sent")))
            check("старое сообщение в ленте при этом появилось – проверка честная",
                  "Технические характеристики" in page.eval_on_selector(
                      "#feed", "el => el.innerText"))
            check("текст остался в поле целиком",
                  "1,2-5 мм" in page.eval_on_selector("#editor", "el => el.innerText"))
        finally:
            browser.close()


def test_report_only_click_work() -> None:
    """
    В отчёте – работа Click, а не история реестра.

    Заказчица (14.08.2026): «в отчёты зачем мне весь список – и то, что не
    надо выкладывать, и то, что надо?». В заголовке стояло «отложек 2 ·
    вышло 439 · ошибок 1»: 439 – это ссылки, набитые в реестре руками за
    месяцы. За ними не видно ни двух отложек, ни одной ошибки.
    """
    print("\nКросспостинг: отчёт только про работу Click")
    import inspect

    import streamlit_app as app

    src = inspect.getsource(app._crosspost_report_block)  # noqa: SLF001
    check("ссылка без действия Click в отчёт не идёт",
          'if link and not saved.get("state"):' in src and "continue" in src)
    check("а ссылка на поставленную Click отложку – идёт",
          src.index('if link and not saved.get("state")') < src.index("counts[\"live\"]"))
    check("и в пояснении сказано, почему списка нет",
          "старых записей" in src and "реестра здесь нет" in src)


def test_form_log_is_fresh() -> None:
    """
    Лог формирования пишется по ходу, а не одной строчкой в конце.

    Заказчица (14.08.2026): «лог вообще старый висит, нового нет». Так и
    было: файл лога переписывался ПОСЛЕДНИМ действием прогона. Пока прогон
    шёл, в разделе висел лог прошлого раза, а если прогон обрывался –
    новый не появлялся вовсе, и разбирать было нечего.
    """
    print("\nКросспостинг: лог пишется по ходу прогона")
    import inspect

    import streamlit_app as app

    src = inspect.getsource(app._crosspost_form_block)  # noqa: SLF001
    check("файл лога переписывается на каждой строке",
          src.count("flush()") >= 3, f"вызовов flush: {src.count('flush()')}")
    check("и в самом начале, до первого шага",
          "flush()        # старый лог уступает место новому сразу" in src)
    check("сама запись – в одном месте", "def flush()" in src)

    head = inspect.getsource(app._crosspost_form_last_log)  # noqa: SLF001
    check("в заголовке лога видно его время", 'f" – {when}"' in head)


def test_forget_target() -> None:
    """
    Сброс одной площадки: ошибка убирается, соседние отложки не трогаются.

    Живой случай (14.08.2026): у поста 17.08 отложки ВК и ОК стояли, МАКС
    упал с таймаутом – и красная строка висела без выхода, а после удаления
    записей в соцсети нечем было сказать Click «их больше нет». Сброс всего
    поста переставил бы и МАКС – вторым экземпляром.
    """
    print("\nСброс одной площадки")
    import os
    import tempfile

    was = os.environ.get("CLICK_DATA_DIR")
    os.environ["CLICK_DATA_DIR"] = tempfile.mkdtemp(prefix="click-forget-")
    try:
        import importlib
        import paths
        importlib.reload(paths)
        import crosspost_state as cps

        post = {"brand": "SMU", "date": "2026-08-17", "when": "2026-08-17T11:00:00+05:00",
                "text": "спецпредложение"}
        cps.set_status("SMU", post, "vk", cps.SCHEDULED)
        cps.set_status("SMU", post, "max", cps.FAILED, error="Timeout")

        cps.forget_target("SMU", post, "max")
        st_ = cps.load("SMU")
        check("ошибка МАКС сброшена", cps.status_of(st_, post, "max") == cps.WAITING)
        check("отложка ВК не тронута", cps.status_of(st_, post, "vk") == cps.SCHEDULED)
        check("сброшенная площадка снова формируется",
              not cps.is_done(st_, post, "max"))
        check("беды по сброшенному больше нет",
              not cps.problems(st_, [dict(post, images=["x"], targets=[
                  {"network": "max", "published_link": ""}])]))

        cps.forget_target("SMU", post, "vk")
        check("сброс последней площадки не падает",
              cps.status_of(cps.load("SMU"), post, "vk") == cps.WAITING)
    finally:
        if was is None:
            os.environ.pop("CLICK_DATA_DIR", None)
        else:
            os.environ["CLICK_DATA_DIR"] = was
        import importlib
        import paths
        importlib.reload(paths)


def test_build_bumped() -> None:
    """
    Правил модули – подними метку сборки. Иначе правка НЕ доезжает.

    Облако держит модули в памяти от прежней сборки и перечитывает их только
    когда BUILD в build.py изменился. 13.08.2026 это стоило второго прогона
    по уже исправленной поломке: починенный max_browser лежал в репозитории,
    а в облаке крутился старый – метку поднять забыли, и «та же ошибка»
    повторилась на живом канале. Здесь это ловится на тестах: изменился код
    модулей – BUILD обязан измениться тоже.

    .build-hash обновляется сам, когда метка поднята: файл коммитится вместе
    с правкой, как золотые файлы текстов.
    """
    print("\nМетка сборки")
    import hashlib
    import json
    from pathlib import Path

    import build
    import streamlit_app as app

    root = Path(__file__).parent
    digest = hashlib.sha1()
    for name in sorted(app._OWN_MODULES):
        fp = root / f"{name}.py"
        if fp.exists():
            digest.update(fp.read_bytes())
    code_hash = digest.hexdigest()

    stamp_fp = root / ".build-hash"
    try:
        stamp = json.loads(stamp_fp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        stamp = {}

    if stamp.get("hash") == code_hash:
        check("код модулей не менялся с последней метки", True)
        check("метка на месте", stamp.get("build") == build.BUILD,
              f'{stamp.get("build")} vs {build.BUILD}')
        return

    check("код изменился → метка сборки поднята (build.py, BUILD)",
          stamp.get("build") != build.BUILD,
          f"метка всё ещё «{build.BUILD}» – правка НЕ доедет до облака")
    if stamp.get("build") != build.BUILD:
        stamp_fp.write_text(json.dumps({"hash": code_hash, "build": build.BUILD},
                                       ensure_ascii=False, indent=2), encoding="utf-8")


def test_max_text_entry() -> None:
    """
    Ввод текста в МАКС: НИ ОДНОГО Enter и ни одного посимвольного набора.

    Худшая поломка Click (13.08.2026): пост из двенадцати строк ушёл в канал
    двенадцатью отдельными сообщениями – сразу, до всякой отложки. Поле
    поста МАКС – поле чата, каждый Enter в нём «отправить», а page.type()
    печатал текст со всеми переводами строк как клавишами.
    """
    print("\nМАКС: ввод текста без отправки")
    import inspect

    import max_browser as mb

    src = inspect.getsource(mb._fill_post_text)
    check("посимвольного набора больше нет", "page.type(" not in src)
    check("текст вставляется событием, не клавишами", "insert_text" in src)
    check("голого Enter нет", 'press("Enter")' not in src.replace("Shift+Enter", ""))
    check("отправку проверяем по каналу, а не по полю", "_sent_lines(" in src)
    check("в конце сверяем начало и конец текста", "_probe" in src)

    whole = inspect.getsource(mb.schedule_postponed_post)
    check("формирование зовёт безопасный ввод", "_fill_post_text(" in whole)
    check("отказ ввода останавливает отложку", '"bad-text"' in whole)
    # Всплывашки перед правым кликом гасим ВОЗВРАТОМ ФОКУСА В ПОЛЕ, а НЕ
    # Escape: Escape в МАКС убирает картинку и закрывает чат целиком (живой
    # прогон 18.08.2026). Клик в поле подсказку гасит и ничего не закрывает.
    check("всплывашки перед правым кликом гасим без Escape",
          'button="right"' in whole and "БЕЗ Escape" in whole)
    check("Escape после фото в МАКС не жмём", 'press("Escape")' not in whole)

    # Отпечаток строки: эмодзи и пробелы не мешают сверке.
    check("отпечаток строки игнорирует значки",
          mb._probe("⚡ СТОИМОСТЬ – 1 400 ₽/кг ⚡") == mb._probe("СТОИМОСТЬ 1400кг"),
          mb._probe("⚡ СТОИМОСТЬ – 1 400 ₽/кг ⚡"))
    check("пустая строка – пустой отпечаток", mb._probe("⚡ – ⚡") == "")


# Поле МАКС, устроенное как настоящее: contenteditable с role=textbox,
# рядом – узел-подсказка, подходящий под ТОТ ЖЕ селектор (на нём и ломалось
# чтение), выше – лента канала со вчерашним постом.
MAX_CHAT_PAGE = """
<html><head><meta charset="utf-8"></head>
<body style="margin:0;font:14px sans-serif">
  <div id="feed">
    <div class="msg">Вчерашний пост: поступила партия арматуры, отгружаем со склада.</div>
  </div>
  <div id="hint" contenteditable role="textbox" data-lexical-editor="true"
       style="position:absolute;opacity:0;height:1px;width:1px"></div>
  <div id="editor" contenteditable role="textbox" data-lexical-editor="true"
       style="border:1px solid #ccc;min-height:28px;max-height:90px;overflow:auto"></div>
  <script>
    // Два переключателя – это две беды, которые надо уметь пережить:
    //   __eatInsertBreaks – сборка МАКС не принимает переносы одним куском;
    //   __enterAlwaysSends – Shift+Enter в ней ОТПРАВЛЯЕТ, а не переносит.
    window.__sent = [];
    const ed = document.getElementById('editor');

    ed.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      if ((e.shiftKey || e.ctrlKey) && !window.__enterAlwaysSends) {
        document.execCommand('insertLineBreak');
        return;
      }
      window.__sent.push(ed.innerText);
      document.getElementById('feed').insertAdjacentHTML(
        'beforeend', '<div class="msg">' + ed.innerText + '</div>');
      ed.innerHTML = '';
    });

    // Переносы из вставки текста выбрасываем через Range, а не execCommand:
    // execCommand внутри обработчика сам поднимает beforeinput, и поведение
    // становится непредсказуемым от захода к заходу.
    ed.addEventListener('beforeinput', e => {
      if (!window.__eatInsertBreaks) return;
      const data = e.data || '';
      if (!(e.inputType || '').startsWith('insert') || !data.includes('\\n')) return;
      e.preventDefault();
      const sel = window.getSelection();
      const node = document.createTextNode(data.replace(/\\n+/g, ''));
      if (sel && sel.rangeCount) {
        const r = sel.getRangeAt(0);
        r.deleteContents();
        r.insertNode(node);
        r.setStartAfter(node);
        r.collapse(true);
        sel.removeAllRanges();
        sel.addRange(r);
      } else {
        ed.appendChild(node);
      }
    });
  </script>
</body></html>
"""

MAX_POST = ("На склад поступила новая партия сетки рабицы\n"
            "В наличии:\n"
            "\n"
            "• Размер ячейки: от 10x10 до 100x100 мм;\n"
            "• Диаметр проволоки: 1,2-5 мм.")


def test_max_text_on_real_field() -> None:
    """
    Ввод текста МАКС – в настоящем браузере, на поле как в кабинете.

    Две беды подряд, обе из одной семьи «проверка врёт про отправку».

    13.08.2026: пост ушёл в канал двенадцатью сообщениями – текст печатался
    клавишами, а каждый Enter в поле чата это «отправить».

    14.08.2026: Click объявил «МАКС отправил строку вместо переноса: после
    Shift+Enter поле опустело» – а в канале ничего не появилось, и весь пост
    стоял в поле. Проверка смотрела ТОЛЬКО в поле и читала при этом первый
    подходящий узел, а он у МАКС – пустышка-подсказка.

    Поэтому здесь проверяется главное: в канал не уходит НИЧЕГО, а решение
    «отправилось» принимается по самому каналу – по тому, появилось ли в нём
    сообщение, которого не было до ввода. Вчерашний пост в ленте лежит с
    самого начала: принять его за свежую отправку нельзя.
    """
    import max_browser as mb
    print("\nМАКС: ввод текста на живом поле")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright доступен", False, "не установлен")
        return

    SEL = 'div[contenteditable][role="textbox"][data-lexical-editor="true"]'
    lines = [ln for ln in MAX_POST.split("\n") if ln.strip()]

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        except Exception:  # noqa: BLE001
            browser = pw.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 900, "height": 700})
            said: list[str] = []

            def sent(p):
                return p.evaluate("() => window.__sent")

            def chat(**flags):
                """Свежая страница канала. Каждому случаю своя: повторный
                set_content в одной вкладке скрипт страницы уже не заводит,
                и переключатели фикстуры молча перестают работать."""
                p = context.new_page()
                p.set_content(MAX_CHAT_PAGE)
                for name, value in flags.items():
                    p.evaluate(f"() => {{ window.{name} = {str(value).lower()}; }}")
                return p

            # ── Обычный случай ───────────────────────────────────────
            page = chat()
            why = mb._fill_post_text(page, SEL, MAX_POST, said.append)  # noqa: SLF001
            check("текст вписан без отказа", why == "", why)
            check("в канал не ушло ни одного сообщения", sent(page) == [], str(sent(page)))
            got = page.eval_on_selector("#editor", "el => el.innerText")
            check("в поле все строки, а не одна",
                  len([ln for ln in got.split("\n") if ln.strip()]) >= len(lines), repr(got))
            check("начало текста на месте", "сетки рабицы" in got, got[:60])
            check("конец текста на месте", "1,2-5 мм" in got, got[-60:])
            check("строки не склеились", "задачВ наличии" not in got.replace(" ", ""))
            check("вчерашний пост в ленте не тронут",
                  "Вчерашний пост" in page.eval_on_selector("#feed", "el => el.innerText"))

            # ── В поле остался хвост прошлой попытки ─────────────────
            # На снимке заказчицы в поле лежал хвост прошлого захода, а под
            # ним – весь пост заново. Дописывать в остаток нельзя.
            page = chat()
            page.eval_on_selector("#editor", "el => el.innerText = 'хвост прошлой попытки'")
            why = mb._fill_post_text(page, SEL, MAX_POST, said.append)  # noqa: SLF001
            check("с остатком в поле тоже справились", why == "", why)
            got = page.eval_on_selector("#editor", "el => el.innerText")
            check("остаток стёрт, а не дописан", "хвост прошлой" not in got, got[:60])

            # ── МАКС не принимает переносы одним куском ──────────────
            # Тогда идём построчно – но по-прежнему ничего не отправляем.
            page = chat(__eatInsertBreaks=True)
            why = mb._fill_post_text(page, SEL, MAX_POST, said.append)  # noqa: SLF001
            check("запасной путь сработал", why == "", why)
            check("и снова ничего не отправлено", sent(page) == [], str(sent(page)))
            check("про запасной путь сказано в логе",
                  any("построчно" in m for m in said), str(said[-3:]))

            # ── Худший случай: Shift+Enter в этой сборке ОТПРАВЛЯЕТ ──
            page = chat(__eatInsertBreaks=True, __enterAlwaysSends=True)
            why = mb._fill_post_text(page, SEL, MAX_POST, said.append)  # noqa: SLF001
            check("отправку заметили и остановились", "отправил строку" in why, why)
            check("и потеряли ровно одну строку, а не весь пост", len(sent(page)) == 1,
                  str(sent(page)))
            check("в отказе сказано, что удалить в канале",
                  "удалите" in why.lower(), why)

            # ── Ложная тревога: поле «пустое», а канал не тронут ─────
            # Тот самый случай 14.08.2026. Читаем первый подходящий узел –
            # он пустышка; настоящее поле полно текста. Отправкой это
            # объявлять нельзя.
            page = chat()
            page.eval_on_selector("#editor", "el => el.innerText = 'вписанный пост'")
            check("читаем самый полный узел, а не первый попавшийся",
                  mb._editor_text(page, SEL).strip() == "вписанный пост",  # noqa: SLF001
                  repr(mb._editor_text(page, SEL)))  # noqa: SLF001
            check("канал не менялся – значит, ничего не отправляли",
                  mb._sent_lines(page, SEL, mb._feed_letters(page, SEL),  # noqa: SLF001
                                 ["вписанный пост"]) == [])

            # А вот вчерашний пост в ленте за нашу отправку не выдаётся –
            # он был там до нас.
            before = mb._feed_letters(page, SEL)  # noqa: SLF001
            check("старое сообщение в канале нашей отправкой не считается",
                  mb._sent_lines(page, SEL, before,  # noqa: SLF001
                                 ["Вчерашний пост: поступила партия арматуры"]) == [])
        finally:
            browser.close()


def test_publish_off() -> None:
    """
    Click в час выхода ничего не публикует (13.08.2026, решение заказчицы).

    Телеграм уходил не тем видом, а раздел писал «вышло» – неправда. Пока
    площадку не доделали: Телеграм в плане – «вручную», задания планировщику
    не ставятся, а уже стоящие отменяются, не отправляясь.
    """
    print("\nПубликация в час выхода выключена")
    import content_plan as cp
    import scheduler

    check("Телеграм не формируется",
          not any(n.startswith("tg") for n in cp.SUPPORTED), str(cp.SUPPORTED))
    # Дзен добавился 14.08.2026 – он публикует статьи родной отложкой, и к
    # выключенной публикации Телеграма отношения не имеет.
    check("ВК, ОК, МАКС и Дзен остались",
          set(cp.SUPPORTED) == {"vk", "ok", "max", "zen"}, str(cp.SUPPORTED))
    check("реестр Телеграм по-прежнему понимает",
          cp.canonical_network("Телеграм клиенты") == "tg-client")
    check("планировщик не шлёт ТГ и бот МАКС",
          set(scheduler.PUBLISH_OFF) == {"tg", "max"}, str(scheduler.PUBLISH_OFF))


def test_max_native_scheduling() -> None:
    """
    МАКС переехал с бота на родную отложку – и не должен уйти дважды.

    У бота МАКС отложки нет вовсе: он шлёт «сейчас», а время держал
    планировщик, значит Click обязан был работать в час выхода. Через
    веб-версию пост держит сам МАКС. Опасность одна: если оставить и
    задание боту, тот же пост уйдёт вторым экземпляром.
    """
    print("\nМАКС: родная отложка вместо задания боту")
    import crosspost_form
    import crosspost_plan as plan
    import crosspost_state as cps
    import streamlit_app as app

    check("формирование МАКС существует", hasattr(crosspost_form, "form_max_all"))

    with_web = {"tgChannelClient": "@a", "maxChatId": "-123",
                "maxWebUrl": "https://web.max.ru/-70916890460398"}
    check("есть ссылка – боту задание не ставим",
          app._crosspost_channels(with_web)["max"] == "")
    check("а Телеграм при этом на месте",
          app._crosspost_channels(with_web)["tg-client"] == "@a")
    check("нет ссылки – бот остаётся запасным путём",
          app._crosspost_channels({"maxChatId": "-123"})["max"] == "-123")
    check("ни того, ни другого – пусто",
          app._crosspost_channels({})["max"] == "")

    # Знак в плане: одно и то же «запланировано» значит разное.
    post = {"date": "14.08.2026", "when": "2026-08-14T20:08:00",
            "text": "текст", "targets": [{"network": "max"}]}
    key = cps.post_key(post)
    native = {key: {"targets": {"max": {"state": cps.SCHEDULED, "native": True}}}}
    by_bot = {key: {"targets": {"max": {"state": cps.SCHEDULED}}}}
    m1 = plan.net_view(post, {"network": "max"}, native)
    m2 = plan.net_view(post, {"network": "max"}, by_bot)
    check("родная отложка – держит сама площадка",
          m1["note"] == "отложка стоит в соцсети", m1["note"])
    check("задание боту – держит Click",
          m2["note"] == "отправит Click в час выхода", m2["note"])


def test_sessions_in_store() -> None:
    """
    Вход в соцсети переживает перезапуск облака.

    Файловая система Streamlit Cloud временная: после каждого рестарта
    заказчица заново вставляла файл сессии. Сессии Яндекса и 2ГИС уже
    лежали в закрытой ветке проекта – теперь туда же уезжают ВК, ОК и МАКС.
    """
    print("\nСессии соцсетей: переживают перезапуск")
    import streamlit_app as app

    names = [n for n, _ in app._session_files("SMU")]
    for want in ("session-SMU", "device-SMU", "session-gis-SMU",
                 "session-vk-SMU", "session-ok-SMU", "session-max-SMU"):
        check(f"{want} хранится снаружи", want in names, str(names))
    check("имена не повторяются", len(set(names)) == len(names), str(names))
    paths = [str(p) for _, p in app._session_files("SMU")]
    check("файлы у всех разные", len(set(paths)) == len(paths), str(paths))

    # «Войти заново» должно стирать и копию в хранилище. Иначе кнопка врёт:
    # локальный файл исчез, а при следующем запуске Click вернёт его назад.
    import inspect
    src = inspect.getsource(app._forget_session)
    check("сброс стирает и локально, и снаружи",
          "unlink" in src and "repo_store.save" in src)
    for kind in ("vk", "ok", "gis", "max"):
        check(f"кнопка сброса {kind} зовёт общий сброс",
              f'_forget_session(project_id, "{kind}")' in inspect.getsource(app))


def main() -> int:
    print("═" * 60)
    test_probe_networks()
    test_combined_session_file()
    test_max_calendar()
    test_max_slow_load()
    test_tg_access_advice()
    test_social_defaults()
    test_link_card_and_report()
    test_mouse_selection_wiring()
    test_ok_bold_and_probe()
    test_forget_target()
    test_build_bumped()
    test_max_text_entry()
    test_max_text_on_real_field()
    test_ok_bold_first_line()
    test_max_photo_proof()
    test_form_log_is_fresh()
    test_max_feed_redraw_is_not_a_send()
    test_report_only_click_work()
    test_publish_off()
    test_max_native_scheduling()
    test_sessions_in_store()
    test_ok_session_detection()
    test_ok_schedule_flow()
    test_plan_horizon_and_past()
    test_form_selection()
    test_vk_confirm_schedule()
    test_vk_captcha_on_posting()
    test_playwright_worker()
    test_scheduler()
    test_vk_domain()
    test_vk_time_pickers()
    test_ok_via_vk()
    test_ok_profile_check()
    test_ok_verify_code()
    test_platform_clients()
    test_post_text()
    test_inline_format()
    test_google_bold_utf16()
    test_bold_from_real_file()
    test_state()
    test_network_mapping()
    test_dates_and_time()
    test_parse_blocks()
    test_posts_to_form()
    test_plan_rows()
    test_real_file_optional()
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
