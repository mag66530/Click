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
    # будущий пост — часть площадок ещё не опубликована (пустая «Ссылка»)
    ["", "2099-01-10 00:00:00", "Вконтакте", "СМУ", "", "Пост", "Спецпредложение",
     "Скидка на арматуру", "https://i.ibb.co/bbb/2.jpg https://i.ibb.co/ccc/3.jpg", "", ""],
    ["", "", "Однокласники", "", "", "Пост", "", "", "", "", ""],
    ["", "", "Telegram (клиент)", "", "", "Пост", "", "", "", "", ""],
    ["", "", "Дзен", "", "", "Пост", "", "", "", "", ""],   # вне scope
]


def test_network_mapping() -> None:
    print("Соцсеть → код цели")
    check("Вконтакте → vk", cp.canonical_network("Вконтакте") == "vk")
    check("ВК → vk", cp.canonical_network("ВК") == "vk")
    check("Однокласники (с опечаткой) → ok", cp.canonical_network("Однокласники") == "ok")
    check("Telegram (сотрудники) → tg-staff", cp.canonical_network("Telegram (сотрудники)") == "tg-staff")
    check("Telegram (клиент) → tg-client", cp.canonical_network("Telegram (клиент)") == "tg-client")
    check("Max (клиент) → max", cp.canonical_network("Max (клиент)") == "max")
    check("Дзен → zen (вне scope)", cp.canonical_network("Дзен") == "zen")
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
    check("нашли два поста", len(posts) == 2, f"получили {len(posts)}")
    if len(posts) != 2:
        return
    p1, p2 = posts
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
    check("прошлый пост отсеян, остался один будущий", len(to_form) == 1, f"{len(to_form)}")
    if not to_form:
        return
    nets = sorted(t["network"] for t in to_form[0]["targets"])
    check("в будущем формируем vk, ok, tg-client", nets == ["ok", "tg-client", "vk"], str(nets))
    check("дзен (вне scope) не формируем", "zen" not in nets)

    # если у площадки «Ссылка» уже стоит — второй раз не формируем
    already = cp.posts_to_form(
        [{"brand": "SMU", "date": "2099-01-10", "time": "11:00", "when": "x",
          "post_type": "", "format": "Пост", "text": "t", "images": [],
          "targets": [{"network": "vk", "raw": "Вконтакте", "published_link": "https://vk.com/wall-1_1"}],
          "row": 1}],
        today=date(2026, 8, 7))
    check("площадка с готовой ссылкой пропускается", already == [])

    # пустой текст — не формируем
    empty = cp.posts_to_form(
        [{"brand": "SMU", "date": "2099-01-10", "time": "11:00", "when": "x",
          "post_type": "", "format": "Пост", "text": "", "images": [],
          "targets": [{"network": "vk", "raw": "Вконтакте", "published_link": ""}], "row": 1}],
        today=date(2026, 8, 7))
    check("пост без текста пропускается", empty == [])


def test_real_file_optional() -> None:
    """Если рядом лежит выгрузка реестра СМУ — прогнать и на ней (не обязательно)."""
    import glob
    hits = glob.glob(str(Path(__file__).parent / "*.xlsx")) + \
        glob.glob("/root/.claude/uploads/**/*.xlsx", recursive=True)
    real = next((h for h in hits if "sheet" not in h.lower()), None)
    if not real:
        print("Реальный файл реестра рядом не найден — пропускаю (это норма).")
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
    check("по умолчанию — не сформировано", cps.status_of({}, p, "vk") == cps.WAITING)
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
    check("без сайта — без изменений", pt.autolink("на нашем сайте", "") == "на нашем сайте")

    check("html: жирный и ссылка",
          pt.render("**Важно**: [сайт](https://a.ru/?a=1&b=2) <3", "html")
          == '<b>Важно</b>: <a href="https://a.ru/?a=1&amp;b=2">сайт</a> &lt;3')
    check("plain: жирный снят, ссылка раскрыта",
          pt.render("**Важно**: [нашем сайте](https://a.ru)", "plain")
          == "Важно: нашем сайте (https://a.ru)")
    check("plain: текст-адрес не дублируется",
          pt.render("[stalmetural.ru](https://stalmetural.ru)", "plain") == "stalmetural.ru")
    check("strip_markup для превью", pt.strip_markup("**а** [б](https://в.ru)") == "а б (https://в.ru)")


def test_bold_from_real_file() -> None:
    """Жирное из настоящего реестра должно доехать до разметки."""
    import glob
    hits = glob.glob("/root/.claude/uploads/**/*.xlsx", recursive=True)
    if not hits:
        print("Реальный файл реестра не найден — пропускаю проверку жирного (это норма).")
        return
    print("Жирное из реального реестра")
    posts = cp.load_from_xlsx(hits[0], "SMU")
    bold_posts = [p for p in posts if "**" in (p.get("text") or "")]
    check("жирные куски дошли до разметки", len(bold_posts) > 20, f"{len(bold_posts)}")
    dates_ok = all(cp.parse_date(p["date"]) for p in posts)
    check("жирная разметка не сломала даты", dates_ok)
    nets = {t["network"] for p in posts for t in p["targets"]}
    check("и не сломала распознавание сетей", {"vk", "ok", "max"} <= nets, str(sorted(nets)))


def main() -> int:
    print("═" * 60)
    test_post_text()
    test_bold_from_real_file()
    test_state()
    test_network_mapping()
    test_dates_and_time()
    test_parse_blocks()
    test_posts_to_form()
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
