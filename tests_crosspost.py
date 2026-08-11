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


def test_vk_time_pickers() -> None:
    """
    Календарь ВК: значение проверяем НЕ мгновенно, а дождавшись.

    Живой случай (11.08.2026): отложка на 15:14 не встала, в логе «Минута не
    принялась: ждали 14, в поле 00», а на снимке отказа – час 15, минуты 14,
    галочка на 14. То есть время стояло верное, а Click прочитал поле раньше,
    чем ВК успел перерисовать надпись: список на React отмечает выбор сразу,
    подпись в поле догоняет позже. Здесь закреплено, что мы ждём.
    """
    print("\nВК: чтение времени в календаре")
    import vk_social

    class FakePicker:
        """Поле, которое показывает нужное значение не сразу, а с задержкой."""

        def __init__(self, values: list[str]):
            self.values = values
            self.reads = 0

        def inner_text(self) -> str:
            v = self.values[min(self.reads, len(self.values) - 1)]
            self.reads += 1
            return v

    class FakePage:
        def __init__(self):
            self.waited = 0

        def wait_for_timeout(self, ms: int) -> None:
            self.waited += ms

    notes: list[str] = []
    page = FakePage()

    # Поле «отстаёт»: два раза показывает старое «00», потом настоящее «14».
    slow = FakePicker(["00", "00", "14"])
    vk_social._wait_picker_value(page, lambda: slow, 14, "Минута", notes.append)
    check("отставшее поле дожидаемся, а не падаем", slow.reads >= 3)

    # Значение так и не встало – вот это честная ошибка.
    stuck = FakePicker(["00"])
    try:
        vk_social._wait_picker_value(page, lambda: stuck, 14, "Минута",
                                     notes.append, tries=3)
        check("непринятое значение – ошибка", False, "ошибки не было")
    except RuntimeError as e:
        check("непринятое значение – ошибка", "ждали 14" in str(e), str(e))

    # Поле не читается вовсе – это не повод ронять отложку (так было и раньше).
    blind = FakePicker([""])
    notes.clear()
    vk_social._wait_picker_value(page, lambda: blind, 14, "Минута",
                                 notes.append, tries=2)
    check("нечитаемое поле отложку не рушит", any("на слово" in n for n in notes))

    # Значение уже стоит – ждать нечего, уходим с первой попытки.
    quick = FakePicker(["14"])
    vk_social._wait_picker_value(page, lambda: quick, 14, "Минута", notes.append)
    check("готовое значение не ждём", quick.reads == 1)

    # Час пишется как «15», минуты как «05» – ведущий ноль не должен мешать.
    zero = FakePicker(["05"])
    vk_social._wait_picker_value(page, lambda: zero, 5, "Минута", notes.append)
    check("ведущий ноль читается верно", zero.reads == 1)

    # Мгновенная проверка «стоит ли уже нужное» – по ней решаем, нужен ли
    # запасной путь через список. Ведущий ноль тут тоже не должен мешать.
    check("«05» – это пять минут",
          vk_social._picker_shows(page, lambda: FakePicker(["05"]), 5))
    check("«00» – это не четырнадцать",
          not vk_social._picker_shows(page, lambda: FakePicker(["00"]), 14))
    check("пустое поле – не совпадение",
          not vk_social._picker_shows(page, lambda: FakePicker([""]), 14))


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


def main() -> int:
    print("═" * 60)
    test_playwright_worker()
    test_scheduler()
    test_vk_domain()
    test_vk_time_pickers()
    test_platform_clients()
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
