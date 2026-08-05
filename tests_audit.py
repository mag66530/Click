"""
tests_audit.py – самопроверка сверки КП с организациями Яндекса.

Запуск:  python tests_audit.py

Браузер не нужен: сюда приходят уже прочитанные строки таблицы и уже
собранный список организаций, а модуль kp_audit – чистая логика. Проверяем то,
что ломается в жизни:

  • «городской округ Шахты, Шахты» и город «Шахты» – это один город;
  • «Ростовская область» городом не считается, иначе она перетянет карточку;
  • телефон «7 (385) 225-26-58» и «+7 (3852) 25-26-58» – один номер;
  • два города с похожими названиями не растаскивают карточки друг друга;
  • дубли в одном городе видны отдельной пометкой;
  • выгрузка отдаёт исходную таблицу без изменений плюс новые колонки.

Плюс разбор настоящих страниц Яндекса из tests_fixtures: если Яндекс поменяет
формат, тест это покажет раньше заказчика.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import kp_audit as A  # noqa: E402

FIXTURES = Path(__file__).parent / "tests_fixtures"
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


def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"получили {got!r}, ждали {expected!r}")


# ─── общая заготовка КП ─────────────────────────────────────────────
KP_HEADER = ["Страна", "Город", "URL", "Адрес", "Почта", "Общий Город", "Аккаунт", "Статус"]


def kp(*rows: list[str]) -> list[list[str]]:
    return [["", "", "", "", "", "", "Яндекс Бизнес", ""], list(KP_HEADER), *[list(r) for r in rows]]


def company(cid: str, address: str, site: str = "", phones=(), emails=(), **kw) -> dict:
    return {"id": cid, "type": kw.get("type", "ordinal"),
            "publishing": kw.get("publishing", "publish"),
            "name": kw.get("name", "Авиапромсталь"), "names": [kw.get("name", "Авиапромсталь")],
            "address": address, "site": site, "sites": [site] if site else [],
            "social": kw.get("social", {}), "emails": list(emails), "phones": list(phones),
            "rubrics": kw.get("rubrics", []), "noAccess": kw.get("noAccess", False)}


# ════════════════════════════════════════════════════════════════════
def test_normalize() -> None:
    print("\n▸ Названия, адреса, телефоны")

    eq("городской округ снимается", A.norm_city("городской округ Шахты"), "шахты")
    eq("«г.» снимается", A.norm_city("г. Барнаул"), "барнаул")
    eq("ё сводится к е", A.norm_city("Артём"), "артем")
    eq("скобки отбрасываются", A.norm_city("Шахты (склад)"), "шахты")
    eq("дефис единый", A.norm_city("Ростов‑на‑Дону"), "ростов-на-дону")

    places = A.address_places("Южный федеральный округ, Ростовская область, "
                              "городской округ Шахты, Шахты")
    eq("из адреса остаётся только город", places, {"шахты"})
    check("область городом не считается", "ростовская область" not in places)
    check("улица городом не считается",
          "улица апатова" not in A.address_places("городской округ Мариуполь, Мариуполь, улица Апатова, 116А"))
    eq("город из короткого адреса", A.address_places("Баку, улица Башира Сафароглы, 123"), {"баку"})

    eq("хост без схемы и слэша", A.site_host("https://Shahty.Aviastal.RU/"), "shahty.aviastal.ru")
    eq("хост без www", A.site_host("www.aviastal.ru/page"), "aviastal.ru")
    eq("пустая ссылка", A.site_host(""), "")

    eq("телефон: код страны не мешает",
       A.phone_key("7 (391) 271-75-19"), A.phone_key("+7 (391) 271-75-19"))
    eq("телефон: 8 и 7 – один номер",
       A.phone_key("8 (800) 700-36-89"), A.phone_key("+7 (800) 700-36-89"))
    eq("телефон: скобки в разных местах",
       A.phone_key("7 (385) 225-26-58"), A.phone_key("+7 (3852) 25-26-58"))
    eq("не телефон", A.phone_key("нет"), "")


def test_parse_sheet() -> None:
    print("\n▸ Разбор листа КП")

    rows = kp(["Россия", "Абакан", "https://abakan.aviastal.ru", "ул. Щетинкина, 24",
               "abakan@aviastal.ru", "7 (391) 271-75-19", "", ""],
              ["Россия", "Барнаул", "barnaul.aviastal.ru", "ул. Пушкина, 29",
               "barnaul@aviastal.ru", "7 (385) 225-26-58",
               "https://yandex.ru/sprav/21461411/edit/main", "Активная"])
    got = A.parse_sheet(rows)
    eq("шапка найдена", got["headerIdx"], 1)
    eq("городов", len(got["items"]), 2)
    eq("колонка города", got["columns"]["city"], 1)
    eq("колонка ссылки – та, где ссылки на sprav", got["columns"]["link"], 6)
    eq("номер строки сохранён", got["items"][1]["rowIdx"], 3)
    eq("почта прочитана", got["items"][0]["email"], "abakan@aviastal.ru")
    eq("телефон прочитан", got["items"][1]["phones"], ["7 (385) 225-26-58"])

    # Колонка сайта без заголовка – ровно как в КП АПС.
    blind = [["", "", "", ""], ["Страна", "Город", "", "Почта"],
             ["Россия", "Абакан", "https://abakan.aviastal.ru", "abakan@aviastal.ru"],
             ["Россия", "Бийск", "https://bijsk.aviastal.ru", "bijsk@aviastal.ru"],
             ["Россия", "Псков", "https://pskov.aviastal.ru", "pskov@aviastal.ru"]]
    eq("сайт найден по содержимому", A.parse_sheet(blind)["columns"]["site"], 2)

    eq("без шапки – понятная ошибка",
       A.parse_sheet([["что-то"], ["ещё"]])["error"],
       "Не нашли шапку: нужна строка с колонкой «Город»")


def test_match() -> None:
    print("\n▸ Сопоставление городов и карточек")

    rows = kp(["Россия", "Шахты", "https://shahty.aviastal.ru", "", "", "", "", ""],
              ["Россия", "Ростов-на-Дону", "https://rostov.aviastal.ru", "", "", "", "", ""],
              ["Россия", "Бердск", "https://berdsk.aviastal.ru", "", "", "", "", ""])
    cos = [
        company("1", "Южный федеральный округ, Ростовская область, городской округ Шахты, Шахты",
                "https://shahty.aviastal.ru/"),
        company("2", "Южный федеральный округ, Ростовская область, Ростов-на-Дону, улица Ленина, 1",
                "https://rostov.aviastal.ru/"),
        company("3", "Сибирский федеральный округ, Новосибирская область, городской округ Бердск, Бердск",
                "https://berdsk.aviastal.ru/"),
    ]
    res = A.build(rows, cos)
    by_city = {i["city"]: [c["id"] for c in i["companies"]] for i in res["items"]}
    eq("Шахты – своя карточка", by_city["Шахты"], ["1"])
    eq("Ростов-на-Дону – своя", by_city["Ростов-на-Дону"], ["2"])
    eq("Бердск – своя", by_city["Бердск"], ["3"])
    eq("лишних нет", res["extra"], [])
    eq("область не перетянула карточку", res["totals"]["missing"], 0)

    # Карточка без сайта, адрес называет город – всё равно находится.
    res2 = A.build(kp(["Россия", "Псков", "", "", "", "", "", ""]),
                   [company("9", "Северо-Западный федеральный округ, Псковская область, "
                                 "городской округ Псков, Псков")])
    eq("нашли по одному адресу", [c["id"] for c in res2["items"][0]["companies"]], ["9"])

    # Ничего похожего – организация уходит в «лишние», город остаётся пустым.
    res3 = A.build(kp(["Россия", "Псков", "https://pskov.aviastal.ru", "", "", "", "", ""]),
                   [company("7", "Марс, кратер Гусева", "https://mars.aviastal.ru/")])
    eq("чужая карточка – в лишние", [c["id"] for c in res3["extra"]], ["7"])
    eq("город остался без карточки", res3["items"][0]["cmp"]["status"], "нет")

    # Сетевая карточка города не имеет – её в города не пихаем.
    res4 = A.build(kp(["Россия", "Псков", "", "", "", "", "", ""]),
                   [company("5", "Земля", "https://aviastal.kz/", type="chain")])
    eq("сеть отдельно", [c["id"] for c in res4["chains"]], ["5"])
    eq("сеть не села в город", res4["items"][0]["cmp"]["status"], "нет")

    # Ссылка в КП сильнее всего: адрес пустой, а карточка та самая.
    res5 = A.build(kp(["Россия", "Неведомск", "", "", "", "",
                       "https://yandex.ru/sprav/4242/edit/main", ""]),
                   [company("4242", "Где-то там")])
    eq("нашли по ссылке из КП", [c["id"] for c in res5["items"][0]["companies"]], ["4242"])
    eq("сопоставили по ссылке", res5["items"][0]["companies"][0]["matchedBy"], "ссылка из КП")


def test_duplicates() -> None:
    print("\n▸ Несколько карточек в одном городе")

    rows = kp(["Россия", "Шахты", "https://shahty.aviastal.ru", "", "", "", "", ""])
    cos = [company("1", "Ростовская область, городской округ Шахты, Шахты", "https://shahty.aviastal.ru/"),
           company("2", "Ростовская область, городской округ Шахты, Шахты, улица Ленина, 5")]
    res = A.build(rows, cos)
    item = res["items"][0]
    eq("обе карточки в городе", sorted(c["id"] for c in item["companies"]), ["1", "2"])
    eq("город помечен", item["cmp"]["status"], "несколько")
    check("в пометке сказано про дубли",
          any("дубли" in n for n in item["cmp"]["problems"]),
          str(item["cmp"]["problems"]))
    eq("счётчик дублей", res["totals"]["several"], 1)


def test_compare() -> None:
    print("\n▸ Сравнение полей")

    rows = kp(["Россия", "Шахты", "https://shahty.aviastal.ru", "", "shahty@aviastal.ru",
               "7 (863) 206-68-85", "https://yandex.ru/sprav/1/edit/", ""])
    same = A.build(rows, [company("1", "Ростовская область, Шахты", "https://shahty.aviastal.ru/",
                                  phones=["+7 (863) 206-68-85"], emails=["shahty@aviastal.ru"])])
    cmp = same["items"][0]["cmp"]
    eq("сайт совпал", cmp["site"], "совпадает")
    eq("телефон совпал", cmp["phone"], "совпадает")
    eq("почта совпала", cmp["email"], "совпадает")
    eq("разногласий нет", cmp["problems"], [])
    check("строка считается чистой", cmp["ok"])
    eq("счётчик чистых", same["totals"]["clean"], 1)

    other = A.build(rows, [company("1", "Ростовская область, Шахты", "https://old-shahty.ru/",
                                   phones=["+7 (999) 000-00-00"], emails=[])])
    cmp2 = other["items"][0]["cmp"]
    eq("сайт разошёлся", cmp2["site"], "расходится")
    eq("телефон разошёлся", cmp2["phone"], "расходится")
    eq("почты нет в Яндексе", cmp2["email"], "нет в Яндексе")
    check("строка попала в расхождения", not cmp2["ok"])
    eq("счётчик расхождений", other["totals"]["mismatch"], 1)

    # Телефонов у Яндекса больше – номер из КП среди них, это совпадение.
    more = A.build(rows, [company("1", "Ростовская область, Шахты", "https://shahty.aviastal.ru/",
                                  phones=["+7 (863) 206-68-85", "+7 (999) 111-22-33"],
                                  emails=["shahty@aviastal.ru"])])
    eq("лишний номер не ломает совпадение", more["items"][0]["cmp"]["phone"], "совпадает")

    # Ссылка в КП ведёт не туда – это разногласие, а не «нашли».
    wrong = A.build(kp(["Россия", "Шахты", "https://shahty.aviastal.ru", "", "", "",
                        "https://yandex.ru/sprav/999/edit/", ""]),
                    [company("1", "Ростовская область, Шахты", "https://shahty.aviastal.ru/")])
    check("чужая ссылка замечена",
          any("другую карточку" in n for n in wrong["items"][0]["cmp"]["problems"]),
          str(wrong["items"][0]["cmp"]["problems"]))

    # Пустая ссылка в КП – это подсказка, а не ошибка: её сверка и заполняет.
    empty = A.build(kp(["Россия", "Шахты", "https://shahty.aviastal.ru", "", "", "", "", ""]),
                    [company("1", "Ростовская область, Шахты", "https://shahty.aviastal.ru/")])
    eq("пустая ссылка – не ошибка", empty["items"][0]["cmp"]["problems"], [])
    check("но подсказка есть", bool(empty["items"][0]["cmp"]["hints"]))
    eq("счётчик «без ссылки»", empty["totals"]["noLink"], 1)


def test_export() -> None:
    print("\n▸ Выгрузка")

    rows = kp(["Россия", "Шахты", "https://shahty.aviastal.ru", "ул. Ленина, 1",
               "shahty@aviastal.ru", "7 (863) 206-68-85", "", ""],
              ["Россия", "Мытищи", "https://mytishchi.aviastal.ru", "", "", "", "", ""])
    res = A.build(rows, [company("1", "Ростовская область, Шахты", "https://shahty.aviastal.ru/",
                                 phones=["+7 (863) 206-68-85"], emails=["shahty@aviastal.ru"])])
    out = A.to_rows(rows, res)

    eq("строк столько же", len(out), len(rows))
    for i, src in enumerate(rows):
        eq(f"строка {i} не тронута", out[i][:len(src)], [str(c) for c in src])
    eq("шапка получила новые колонки", out[1][len(KP_HEADER):], A.EXTRA_HEADERS)
    eq("статус найденного города", out[2][len(KP_HEADER)], "✅ найдена")
    eq("статус ненайденного", out[3][len(KP_HEADER)], "❌ нет")
    check("ссылка на карточку подставлена",
          "yandex.ru/sprav/1/p/edit/" in out[2][len(KP_HEADER) + 9], out[2][len(KP_HEADER) + 9])

    csv = A.to_csv(out)
    eq("в CSV столько же строк", len(csv.strip().splitlines()), len(rows))
    check("перенос строки внутри ячейки не рвёт CSV", "\n" not in csv.strip().splitlines()[2])

    blob = A.to_xlsx(rows, res, "Лист20")
    check("xlsx собрался", len(blob) > 2000, str(len(blob)))
    import io

    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(blob))
    eq("листы книги", wb.sheetnames, ["Лист20", "Лишние в Яндексе"])
    ws = wb["Лист20"]
    eq("колонок стало больше", ws.max_column, len(KP_HEADER) + len(A.EXTRA_HEADERS))
    eq("город на месте", ws.cell(row=3, column=2).value, "Шахты")
    eq("плашка статуса", ws.cell(row=3, column=len(KP_HEADER) + 1).value, "✅ найдена")
    check("ненайденный город закрашен красным",
          (ws.cell(row=4, column=len(KP_HEADER) + 1).fill.fgColor.rgb or "").endswith(A.FILL_BAD),
          str(ws.cell(row=4, column=len(KP_HEADER) + 1).fill.fgColor.rgb))
    wb.close()


# ─── настоящие страницы Яндекса ─────────────────────────────────────
def _preload(html: str) -> dict:
    """window.__PRELOAD_DATA из сохранённой страницы – без браузера, скобками."""
    i = html.index("window.__PRELOAD_DATA = ") + len("window.__PRELOAD_DATA = ")
    depth, instr, esc, end = 0, False, False, len(html)
    for j in range(i, len(html)):
        c = html[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
            continue
        if c == '"':
            instr = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    return json.loads(html[i:end])


def test_yandex_pages() -> None:
    print("\n▸ Настоящие страницы Яндекса")

    card_fp = FIXTURES / "yb-company-card.html"
    list_fp = FIXTURES / "yb-companies-list.html"
    if not card_fp.exists() or not list_fp.exists():
        check("фикстуры страниц Яндекса на месте", False, "нет файлов в tests_fixtures")
        return

    import yb_playwright as yb

    # Читалка живёт в браузере, поэтому здесь проверяем ДАННЫЕ, из которых она
    # берёт всё: если Яндекс уберёт __PRELOAD_DATA, сверка останется без списка.
    card = _preload(card_fp.read_text(encoding="utf-8", errors="replace"))
    co = card["initialState"]["edit"]["info"]["company"]
    eq("карточка: адрес", co["address"]["formatted"]["value"], "Баку, улица Башира Сафароглы, 123")
    eq("карточка: почта", co["emails"], ["info@aviastal.az"])
    eq("карточка: телефонов", len(co["phones"]), 2)
    eq("карточка: сайт", [u["value"] for u in co["urls"] if u["type"] == "main"], ["https://aviastal.az/"])

    lst = _preload(list_fp.read_text(encoding="utf-8", errors="replace"))["initialState"]["companiesList"]
    eq("список: страница", lst["page"], 1)
    check("список: сказано, сколько всего", lst["total"] > len(lst["listCompanies"]),
          f'{lst["total"]} vs {len(lst["listCompanies"])}')
    check("список: строки со всеми полями",
          all(k in lst["listCompanies"][0] for k in ("id", "address", "urls", "phones", "emails")))

    # Читалка списка обязана уметь дочитывать страницы: одной страницы мало.
    src = yb._COMPANIES_JS
    check("читалка списка берёт данные из preload", "__PRELOAD_DATA" in src)
    check("читалка списка отдаёт total и limit", "total:" in src and "limit:" in src)
    import inspect
    check("страницы запрашиваются параметрами page и limit",
          "page={num}" in inspect.getsource(yb._companies_page)
          and "limit={limit}" in inspect.getsource(yb._companies_page))
    check("почта в запасном разборе ищется по-русски",
          "/почт/i" in yb._CARD_DOM_JS,
          "в JS \\w не ловит кириллицу – регулярка по «электронн\\w* почт» не сработает")
    check("адрес в запасном разборе ищется по своему блоку", ".InfoAddress input" in yb._CARD_DOM_JS)

    # Сверка на настоящих данных: девять городов КП против десяти карточек.
    companies = []
    for raw in lst["listCompanies"]:
        urls = raw.get("urls") or []
        companies.append(company(
            str(raw["id"]),
            (raw.get("address") or {}).get("formatted", {}).get("value", ""),
            next((u["value"] for u in urls if u.get("type") == "main"), ""),
            phones=[p.get("formatted", "") for p in (raw.get("phones") or [])],
            emails=raw.get("emails") or [],
            type=raw.get("type", "ordinal"), name=raw.get("displayName", "")))
    real_kp = kp(*[["Россия", city, f"https://{host}.aviastal.ru", "", "", phone, "", ""]
                   for city, host, phone in [
                       ("Шахты", "shahty", "7 (863) 206-68-85"),
                       ("Бердск", "berdsk", "7 (383) 255-73-36"),
                       ("Батайск", "bataysk", "7 (863) 206-68-85"),
                       ("Саранск", "saransk", "7 (831) 414-40-86"),
                       ("Мариуполь", "mariupol", "8 (800) 700-36-89"),
                       ("Бийск", "bijsk", "7 (385) 225-26-58"),
                       ("Псков", "pskov", "7 (812) 986-43-38"),
                       ("Балашиха", "balashiha", "7 (495) 755-36-18"),
                       ("Нальчик", "nalchik", "7 (903) 411-64-68"),
                       ("Сургут", "surgut", "7 (346) 200-00-00")]])
    res = A.build(real_kp, companies)
    eq("девять городов нашлись", res["totals"]["found"], 9)
    eq("Сургута в Яндексе нет", res["totals"]["missing"], 1)
    eq("дублей нет", res["totals"]["several"], 0)
    eq("сетевая карточка отдельно", len(res["chains"]), 1)
    eq("лишних нет", len(res["extra"]), 0)
    # У Сургута карточки нет вовсе, телефон сравнивать не с чем – он не в счёт.
    bad = [i["city"] for i in res["items"]
           if i["companies"] and i["cmp"]["phone"] not in ("совпадает", "–")]
    eq("телефоны сошлись у всех найденных", bad, [])


def main() -> int:
    print("═" * 60)
    print("  ПРОВЕРКА СВЕРКИ КП С ЯНДЕКСОМ")
    print("═" * 60)
    test_normalize()
    test_parse_sheet()
    test_match()
    test_duplicates()
    test_compare()
    test_export()
    test_yandex_pages()

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
