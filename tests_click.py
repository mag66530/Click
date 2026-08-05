"""
tests_click.py – самопроверка логики без запуска браузера.

Запуск:  python tests_click.py

Проверяем именно то, что ломалось в первой версии переноса:
  • нормализация URL карточки (иначе форма поста просто не открывается);
  • сборка текста поста (совпадение с оригинальным buildFinalText);
  • правила ретрая – после клика «Создать» повтор ЗАПРЕЩЁН;
  • реестр публикаций – тот же текст в тот же город второй раз не уходит;
  • лок прогона – второй запуск не стартует;
  • формат файла задач, который читает runner.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import paths  # noqa: E402

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


# ════════════════════════════════════════════════════════════════════
def test_urls() -> None:
    import yb_playwright as yb
    print("\n▸ Нормализация URL карточки")

    eq("ID из /edit/posts/", yb.extract_company_id("https://yandex.ru/sprav/40691746/edit/posts/"), "40691746")
    eq("ID из /p/edit/", yb.extract_company_id("https://yandex.ru/sprav/188702920373/p/edit/"), "188702920373")
    eq("ID из мусора", yb.extract_company_id("https://example.com/x"), None)

    # Главное: любой вид ссылки приводится к разделу «Посты» с сохранением формата
    eq("посты: /edit/posts/ → как есть",
       yb.build_posts_url("https://yandex.ru/sprav/40691746/edit/posts/"),
       "https://yandex.ru/sprav/40691746/edit/posts/")
    eq("посты: /edit/photos/ → /edit/posts/",
       yb.build_posts_url("https://yandex.ru/sprav/40691746/edit/photos/"),
       "https://yandex.ru/sprav/40691746/edit/posts/")
    eq("посты: /edit/main → /edit/posts/",
       yb.build_posts_url("https://yandex.ru/sprav/42876867/edit/main"),
       "https://yandex.ru/sprav/42876867/edit/posts/")
    eq("посты: /p/edit/ → /p/edit/posts/ (формат /p/ сохранён)",
       yb.build_posts_url("https://yandex.ru/sprav/188702920373/p/edit/"),
       "https://yandex.ru/sprav/188702920373/p/edit/posts/")
    eq("посты: голый /sprav/{id} → новый формат",
       yb.build_posts_url("https://yandex.ru/sprav/12345"),
       "https://yandex.ru/sprav/12345/p/edit/posts/")
    eq("посты: только companyId", yb.build_posts_url(None, "999"),
       "https://yandex.ru/sprav/999/p/edit/posts/")
    check("посты: НИКОГДА не /sprav/{id}/posts/ (это 404 у Яндекса)",
          all("/sprav/12345/posts/" not in (yb.build_posts_url(u, None) or "")
              for u in ("https://yandex.ru/sprav/12345", "https://yandex.ru/sprav/12345/edit/")))

    eq("данные: /p/edit/posts/ → /p/edit/",
       yb.build_edit_url("https://yandex.ru/sprav/188702920373/p/edit/posts/"),
       "https://yandex.ru/sprav/188702920373/p/edit/")
    eq("данные: /edit/posts/ → /edit/",
       yb.build_edit_url("https://yandex.ru/sprav/24038969/edit/posts/"),
       "https://yandex.ru/sprav/24038969/edit/")
    eq("фото: /p/edit/photos/",
       yb.build_photos_url("https://yandex.ru/sprav/188702920373/p/edit/posts/"),
       "https://yandex.ru/sprav/188702920373/p/edit/photos/")

    # 404 на одном формате не хоронит город: есть второй формат (PLAN 1.5)
    eq("второй формат: /p/edit/ → /edit/",
       yb.alt_posts_url("https://yandex.ru/sprav/1/p/edit/posts/"),
       "https://yandex.ru/sprav/1/edit/posts/")
    eq("второй формат: /edit/ → /p/edit/",
       yb.alt_posts_url("https://yandex.ru/sprav/1/edit/posts/"),
       "https://yandex.ru/sprav/1/p/edit/posts/")
    eq("второго формата нет – None", yb.alt_posts_url("https://example.com/x"), None)


def test_publish_api_filter() -> None:
    import yb_playwright as yb
    print("\n▸ Распознавание API-ответа о создании поста")
    f = yb._is_publish_api_response
    check("POST к /sprav/api/posts – считаем", f("https://yandex.ru/sprav/api/posts/create", "POST"))
    check("PUT к /api/post – считаем", f("https://yandex.ru/api/v1/post/42", "PUT"))
    check("GET не считаем", not f("https://yandex.ru/sprav/api/posts/list", "GET"))
    check("метрика не считается", not f("https://mc.yandex.ru/sprav/metric/hit", "POST"))
    check("аналитика не считается", not f("https://yandex.ru/sprav/api/analytics", "POST"))
    check("посторонний домен без /sprav/ и /posts/ не считается",
          not f("https://example.com/upload", "POST"))
    # Живой случай из лога заказчика: Метрика передаёт адрес страницы в строке
    # запроса, и «sprav»/«posts» находились в ЧУЖОМ запросе – ложное «опубликовано».
    check("Метрика mc.yandex.ru/watch со sprav в параметрах НЕ считается",
          not f("https://mc.yandex.ru/watch/52122583?browser-info=x&page-url="
                "https%3A%2F%2Fyandex.ru%2Fsprav%2F123%2Fp%2Fedit%2Fposts%2F", "POST"))
    check("настоящий API постов с параметрами – считается",
          f("https://yandex.ru/sprav/api/companies/1/posts?lang=ru", "POST"))
    check("ключевые слова только в параметрах, путь чужой – не считается",
          not f("https://yandex.ru/collections/save?from=%2Fsprav%2Fposts%2F", "POST"))

    # Из нескольких подходящих ответов берём САМЫЙ ТОЧНЫЙ, а не первый.
    score = yb._publish_response_score  # noqa: SLF001
    eq("создание поста – высший приоритет",
       score("https://yandex.ru/sprav/api/71186454150/company-post"), 3)
    check("общий API постов – ниже",
          score("https://yandex.ru/sprav/api/companies/1/posts?lang=ru") < 3)
    check("посторонний путь справочника – ноль",
          score("https://yandex.ru/sprav/api/settings") == 0)
    hits = [
        {"url": "https://yandex.ru/sprav/api/1/posts", "status": 200, "score":
         score("https://yandex.ru/sprav/api/1/posts")},
        {"url": "https://yandex.ru/sprav/api/1/company-post", "status": 403, "score":
         score("https://yandex.ru/sprav/api/1/company-post")},
    ]
    best = max(range(len(hits)), key=lambda i: (hits[i]["score"], i))
    check("отказ на точном адресе не перебивается ранним «успехом»",
          hits[best]["status"] == 403, str(hits[best]))
    same = [{"url": "https://yandex.ru/sprav/api/1/posts", "status": 500, "score": 1},
            {"url": "https://yandex.ru/sprav/api/1/posts", "status": 200, "score": 1}]
    best = max(range(len(same)), key=lambda i: (same[i]["score"], i))
    check("среди одинаковых берём последний – он окончательный",
          same[best]["status"] == 200, str(same[best]))


def test_text() -> None:
    print("\n▸ Текст поста (порт buildFinalText)")
    import projects_data as pdata

    src = Path("streamlit_app.py").read_text(encoding="utf-8")
    start = src.index("def build_final_text(")
    end = src.index("# ═══", start)
    endings = {"SMU": pdata.SMU_ENDINGS, "IMP": pdata.IMP_ENDINGS,
               "MPE": pdata.MPE_ENDINGS, "MPI": pdata.MPI_ENDINGS,
               "APS": pdata.APS_ENDINGS}
    ns = {"pdata": pdata, "project_endings": lambda pid: endings[pid]}
    exec(compile(src[start:end], "bft", "exec"), ns)  # noqa: S102
    build = ns["build_final_text"]

    # Эталон снят с прежней реализации ДО перевода СМУ на общий формат окончаний.
    # Любая правка сборки текста, меняющая хоть один байт, обязана его уронить.
    golden = json.loads(Path("tests_text_golden.json").read_text(encoding="utf-8"))
    bodies = ["", "Короткий текст", "Строка один\nСтрока два\n\nСтрока четыре"]
    diverged = [k for k, want in golden.items()
                if build(*k.split("|")[:3], bodies[int(k.split("|")[3])]) != want]
    check(f"текст поста совпадает с эталоном во всех {len(golden)} сочетаниях",
          not diverged, f"разошлось: {diverged[:3]}")

    smu = build("SMU", "Россия", "arrival", "Поступил швеллер")
    check("СМУ: контакты страны подставлены", "stalmetural.ru" in smu and "+7 (499) 130-36-69" in smu)
    check("СМУ: хэштеги на месте", "#Поступление_СМУ #Стальметурал #СМУ #Металлопрокат" in smu)
    check("СМУ: поздравление – без окончания", build("SMU", "Россия", "greeting", "С Новым годом") ==
          "С Новым годом")

    imp = build("IMP", "Казахстан", "shipment", "Отгрузили трубу")
    check("ИМП: контакты Казахстана", "inmetprom.kz" in imp and "astana@inmetprom.kz" in imp)
    check("ИМП: хэштеги бренда", "#Отгрузка_ИМП" in imp)

    # Армения у ИМП без телефона – строка с телефоном должна ИСЧЕЗНУТЬ целиком
    am = build("IMP", "Армения", "shipment", "Текст")
    check("ИМП/Армения: строки с телефоном нет", "📞" not in am)
    check("ИМП/Армения: нет пустой дырки вместо телефона", "\n\n\n" not in am)
    check("ИМП/Армения: email и сайт на месте", "erevan@inmetprom.am" in am and "inmetprom.am" in am)

    mpe = build("MPE", "Беларусь", "special", "Спецпредложение")
    check("МПЭ: контакты Беларуси", "mepen.by" in mpe)
    check("МПЭ: свой формат телефона", "📱 Телефон: +375 (29) 643-66-60" in mpe)

    check("Страна вне списка контактов → окончание не вставляется",
          build("IMP", "Монголия", "shipment", "Текст") == "Текст")


def test_retry_rules() -> None:
    import runner
    print("\n▸ Правила ретрая (защита от дубля №3)")
    click = runner._click_happened
    check("клик был → повтор запрещён", click({"steps": {"publish": "clicked"}}))
    check("клик + ошибка → повтор запрещён", click({"steps": {"publish": "click-error-no-api"}}))
    check("подтверждено API → повтор запрещён", click({"steps": {"publish": "api-confirmed"}}))
    check("Яндекс отклонил → повтор запрещён", click({"steps": {"publish": "api-rejected"}}))
    check("неопределённо → повтор запрещён", click({"steps": {"publish": "unknown"}}))
    check("кнопка публикации не найдена → повтор разрешён", not click({"steps": {"publish": "missing"}}))
    check("упали на поле текста → повтор разрешён", not click({"steps": {"text": "missing"}}))
    check("упали на кнопке «Добавить пост» → повтор разрешён", not click({"steps": {"addButton": "missing"}}))

    ledger = runner._should_ledger
    check("реестр: ok пишем", ledger({"status": "ok"}))
    check("реестр: no-image пишем", ledger({"status": "no-image"}))
    check("реестр: unknown пишем – повтор опасен", ledger({"status": "unknown"}))
    check("реестр: failed НЕ пишем", not ledger({"status": "failed"}))
    check("реестр: api-rejected НЕ пишем – иначе город блокируется зря",
          not ledger({"status": "failed", "steps": {"publish": "api-rejected"}}))


def test_ledger_and_lock(tmp: Path) -> None:
    import runner
    runner.USERS_DATA = tmp
    print("\n▸ Реестр публикаций (защита от дубля №2)")

    pid = "TEST"
    task = {"cityName": "Москва", "companyUrl": "https://yandex.ru/sprav/111/p/edit/posts/",
            "companyId": "111", "postText": "Один и тот же текст поста"}

    check("до публикации записи нет", runner.recent_publication(pid, task) is None)
    runner._ledger_add(pid, runner._text_key("111", task["companyUrl"], task["postText"]), task, "ok", "run-1")
    rec = runner.recent_publication(pid, task)
    check("после публикации запись есть", rec is not None and rec["status"] == "ok")

    other_text = {**task, "postText": "Совсем другой текст"}
    check("другой текст в тот же город – не блокируется", runner.recent_publication(pid, other_text) is None)
    other_city = {**task, "companyId": "222", "companyUrl": "https://yandex.ru/sprav/222/p/edit/posts/"}
    check("тот же текст в другой город – не блокируется", runner.recent_publication(pid, other_city) is None)
    check("окно 0 часов отключает блокировку",
          runner.recent_publication(pid, task, window_hours=0) is None)

    runner._ledger_add(pid, runner._text_key("111", task["companyUrl"], task["postText"]), task,
                       "unknown", "run-2")
    rec = runner.recent_publication(pid, task)
    check("неподтверждённая публикация тоже блокирует повтор", rec is not None and rec["status"] == "unknown")

    runner.clear_ledger(pid)
    check("очистка реестра работает", runner.recent_publication(pid, task) is None)

    print("\n▸ Лок прогона (защита от дубля №1)")
    check("лок берётся", runner._acquire_lock(pid, "publish", "run-A"))
    runner._threads.pop((pid, "publish"), None)
    check("протухший лок (процесс перезапущен) забирается",
          runner._acquire_lock(pid, "publish", "run-B"))

    import threading
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    runner._threads[(pid, "publish")] = t
    check("лок живого прогона НЕ забирается",
          not runner._acquire_lock(pid, "publish", "run-C"))
    stop.set()
    t.join(timeout=2)
    runner._threads.pop((pid, "publish"), None)
    runner._release_lock(pid, "publish")
    check("лок снимается", not runner.p_lock(pid, "publish").exists())

    # ── Лок между РАЗНЫМИ копиями Click ──
    # Раньше чужой процесс считался протухшим и лок у него отбирался: две
    # копии на одном компьютере запускали прогоны параллельно – со всеми
    # дублями. Живость судим по «пульсу»: пока прогон идёт, файл лока
    # регулярно обновляется. Проверять PID чужого процесса нельзя – на
    # Windows os.kill(pid, 0) убивает процесс, а не проверяет его.
    import json as _json
    import os as _os
    lock = runner.p_lock(pid, "publish")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(_json.dumps({"runId": "run-чужой", "ownerPid": _os.getpid() + 12345}),
                    encoding="utf-8")
    runner._LOCK_HELD.discard((pid, "publish"))
    check("лок ЖИВОЙ чужой копии не забирается",
          not runner._acquire_lock(pid, "publish", "run-D"))
    check("видно, кто держит лок",
          (runner.lock_owner(pid, "publish") or {}).get("runId") == "run-чужой")

    # Тот же лок, но пульса давно нет – копию закрыли, лок можно забрать.
    old_time = time.time() - runner.LOCK_STALE_S - 30
    _os.utime(lock, (old_time, old_time))
    check("брошенный чужой лок забирается", runner._acquire_lock(pid, "publish", "run-E"))
    check("после захвата в локе наш прогон",
          _json.loads(lock.read_text(encoding="utf-8"))["runId"] == "run-E")
    # Свой лок без живого потока – это закончившийся прогон, он свободен.
    check("свой лок без живого прогона считается свободным",
          runner.lock_owner(pid, "publish") is None)

    # Пульс: пока прогон идёт, метка времени обновляется.
    stale = time.time() - 60
    _os.utime(lock, (stale, stale))
    runner._LOCK_TOUCHED.pop((pid, "publish"), None)
    runner._touch_lock(pid, "publish")
    check("прогон отмечает, что жив", lock.stat().st_mtime > stale + 30)
    runner._threads.pop((pid, "publish"), None)
    runner._release_lock(pid, "publish")


def test_run_state(tmp: Path) -> None:
    import os
    import runner
    runner.USERS_DATA = tmp
    print("\n▸ Состояние прогона")
    pid = "TEST2"

    eq("нет прогона → idle", runner.read_state(pid).get("status"), "idle")
    runner._write_state(pid, {"status": "running", "ownerPid": os.getpid(), "action": "publish"})
    eq("свой живой прогон виден как running", runner.read_state(pid).get("status"), "running")
    check("is_running=True", runner.is_running(pid))
    runner._write_state(pid, {"status": "running", "ownerPid": os.getpid() + 99999, "action": "publish"})
    eq("прогон чужого (убитого) процесса → interrupted, а не вечный running",
       runner.read_state(pid).get("status"), "interrupted")
    check("is_running=False для мёртвого прогона", not runner.is_running(pid))

    # Состояние у каждого вида своё: сверка рядом с публикацией не должна
    # ни затирать её прогресс, ни выглядеть на её вкладке как «идёт».
    runner._write_state(pid, {"status": "running", "ownerPid": os.getpid(),
                              "action": "publish", "startedAt": "2026-01-01T10:00:00"})
    runner._write_state(pid, {"status": "done", "ownerPid": os.getpid(),
                              "action": "collect", "startedAt": "2026-01-01T11:00:00"})
    eq("публикация видит своё состояние",
       runner.read_state(pid, "publish").get("status"), "running")
    eq("сверка видит своё", runner.read_state(pid, "collect").get("status"), "done")
    eq("без вида показывается идущий прогон, а не последний начатый",
       runner.read_state(pid).get("action"), "publish")
    eq("список идущих прогонов", runner.running_kinds(pid), ["publish"])

    # Прогон, начатый прошлой версией: файл был один на проект.
    for k in runner.RUN_KINDS:
        runner.p_state(pid, k).unlink(missing_ok=True)
    runner.p_state_legacy(pid).write_text(
        json.dumps({"status": "done", "action": "actualize", "ownerPid": os.getpid()}),
        encoding="utf-8")
    eq("прогон прошлой версии виден на своей вкладке",
       runner.read_state(pid, "actualize").get("status"), "done")
    eq("и не подмешивается к чужой", runner.read_state(pid, "publish").get("status"), "idle")
    runner.p_state_legacy(pid).unlink(missing_ok=True)


def test_parallel_runs(tmp: Path) -> None:
    """
    Сверка идёт рядом с актуализацией, а два прогона одного вида – никогда.

    Заказчик просила гонять «Актуализацию» и «Сверку» одновременно: раньше лок
    был один на проект и любой второй прогон отбивался «уже идёт другой».
    """
    import os
    import runner
    runner.USERS_DATA = tmp
    print("\n▸ Параллельные прогоны")
    pid = "TEST-PAR"

    eq("на пустом месте запускать можно", runner.busy_reason(pid, "collect"), "")

    runner._write_state(pid, {"status": "running", "ownerPid": os.getpid(),
                              "action": "actualize", "startedAt": "2026-01-01T10:00:00"})
    eq("сверка рядом с актуализацией разрешена", runner.busy_reason(pid, "collect"), "")
    check("вторая актуализация НЕ разрешена", bool(runner.busy_reason(pid, "actualize")))
    check("публикация рядом с актуализацией НЕ разрешена (два браузера)",
          bool(runner.busy_reason(pid, "publish")))

    runner._write_state(pid, {"status": "running", "ownerPid": os.getpid(),
                              "action": "collect", "startedAt": "2026-01-01T10:30:00"})
    eq("идут оба прогона", sorted(runner.running_kinds(pid)), ["actualize", "collect"])
    check("третий прогон упирается в потолок", bool(runner.busy_reason(pid, "publish")))

    # Стоп-флаги раздельные: остановили сверку – актуализация идёт дальше.
    runner.request_stop(pid, "collect")
    check("сверке сказали остановиться", runner.p_stop(pid, "collect").exists())
    check("актуализацию не трогали", not runner.p_stop(pid, "actualize").exists())

    # Живые логи тоже раздельные – иначе строки одного прогона летели бы в лог
    # другого, а это ровно то, из-за чего лог раньше «прыгал».
    runner._append_log(pid, "INFO", "строка сверки", kind="collect")
    runner._append_log(pid, "INFO", "строка актуализации", kind="actualize")
    check("лог сверки только свой",
          "строка актуализации" not in runner.read_live_log(pid, "collect"))
    check("лог актуализации только свой",
          "строка сверки" not in runner.read_live_log(pid, "actualize"))

    for k in runner.RUN_KINDS:
        runner.p_state(pid, k).unlink(missing_ok=True)
        runner.p_stop(pid, k).unlink(missing_ok=True)


def test_task_format(tmp: Path) -> None:
    import runner
    runner.USERS_DATA = tmp
    print("\n▸ Формат файла задач")
    pid = "TEST3"
    tasks_dir = runner.p_tasks(pid)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "credentials": {"email": "a@b.ru", "password": "x"},
        "projectName": "ИМП", "country": "Россия",
        "tasks": [{"cityName": "Москва", "companyUrl": "https://yandex.ru/sprav/1/p/edit/posts/",
                   "companyId": "1", "postText": "текст"}],
    }
    (tasks_dir / "01-Россия-1.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    files, cities = runner.count_pending(pid)
    eq("файлов в очереди", files, 1)
    eq("городов в очереди", cities, 1)

    loaded = runner._load_task_files(tasks_dir)
    eq("файл читается", len(loaded), 1)
    eq("credentials на месте", loaded[0][1]["credentials"]["email"], "a@b.ru")

    runner._archive(loaded[0][0])
    check("после прогона файл уезжает в done/", (tasks_dir / "done" / "01-Россия-1.json").exists())
    eq("очередь опустела", runner.count_pending(pid)[1], 0)


def test_report_render() -> None:
    import ui_theme as T
    print("\n▸ Отрисовка отчёта")
    row = T.report_row({"status": "unknown", "cityName": "Казань", "reason": "не подтверждено",
                        "durationMs": 12345})
    check("неопределённый статус рисуется отдельным стилем", "report-row warn" in row and "⚠️" in row)
    check("XSS в названии города экранируется",
          "&lt;script&gt;" in T.report_row({"status": "ok", "cityName": "<script>x</script>"}))
    check("страна в строке показывается по требованию",
          "Казахстан" in T.report_row({"status": "ok", "cityName": "Актау",
                                       "country": "Казахстан"}, with_country=True))
    check("без требования страны в строке нет",
          "Казахстан" not in T.report_row({"status": "ok", "cityName": "Актау",
                                           "country": "Казахстан"}))
    import streamlit_app as app
    check("время прогона в минутах", app._dur_text(130) == "2.2 мин")  # noqa: SLF001
    check("короткое время в секундах", app._dur_text(45) == "45 сек")  # noqa: SLF001
    check("пустой лог даёт заглушку", "log-placeholder" in T.log_box(""))
    check("строки лога раскрашиваются", "log-err" in T.log_box("[ERROR] всё плохо"))


def test_browser_fallback() -> None:
    """
    В облаке (Streamlit Cloud) у Chromium часто нет системных библиотек –
    приложение должно само перейти на Firefox, а не падать простынёй логов.
    Текст ошибки взят из реального падения на Streamlit Cloud.
    """
    import yb_playwright as yb
    print("\n▸ Выбор браузера и запасной вариант")

    real = (
        "BrowserType.launch: Target page, context or browser has been closed\n"
        "Browser logs:\n<launching> /home/appuser/.cache/ms-playwright/chromium_headless_shell-1234/"
        "chrome-headless-shell --disable-field-trial-config\n"
        "[pid=1894][err] /home/appuser/.cache/ms-playwright/chromium_headless_shell-1234/"
        "chrome-headless-shell: error while loading shared libraries: libgbm.so.1: "
        "cannot open shared object file: No such file or directory\n"
        "[pid=1894] <gracefully close start>"
    )

    eq("из простыни логов достаётся суть", yb._short_error(RuntimeError(real)), "libgbm.so.1")
    check("«браузер не скачан» распознаётся",
          yb._is_not_installed(RuntimeError("Executable doesn't exist at /root/.cache/x")))
    check("нехватка библиотеки НЕ считается «не скачан»", not yb._is_not_installed(RuntimeError(real)))

    original_launch, original_engine = yb._launch, yb._ENGINE
    try:
        class _FakeBrowser:
            def close(self):
                pass

        tried: list[str] = []

        def only_firefox_works(pw, engine, headless=True):
            tried.append(engine)
            if engine == "chromium":
                raise RuntimeError(real)
            return _FakeBrowser()

        yb._launch, yb._ENGINE = only_firefox_works, None
        eq("Chromium не запустился → берём Firefox", yb.resolve_engine(), "firefox")
        eq("пробовали именно в этом порядке", tried, ["chromium", "firefox"])

        yb._ENGINE = None
        eq("результат кэшируется (второй раз не перебираем)", yb.resolve_engine(force=None) or "firefox", "firefox")

        def nothing_works(pw, engine, headless=True):
            raise RuntimeError(real)

        yb._launch, yb._ENGINE = nothing_works, None
        try:
            yb.resolve_engine()
            check("при полном отказе бросаем ошибку", False, "исключения не было")
        except RuntimeError as e:
            msg = str(e)
            check("в ошибке сказано, что делать локально", "playwright install" in msg)
            check("в ошибке сказано, что делать в облаке",
                  "Reboot" in msg and "CLICK_BROWSER=firefox" in msg)
            check("сообщение короткое, а не простыня", len(msg) < 800, f"{len(msg)} символов")

        yb._launch, yb._ENGINE = only_firefox_works, None
        eq("CLICK_BROWSER=firefox уважается", yb.resolve_engine(force="firefox"), "firefox")
    finally:
        yb._launch, yb._ENGINE = original_launch, original_engine


def test_engine_order() -> None:
    """
    Корень проблемы с облаком: там Chromium не поднимается (нет системных
    библиотек, мало памяти), и доставить их нельзя. Значит в облаке Firefox
    должен идти ПЕРВЫМ – иначе мы качаем 150 МБ Chromium впустую и только
    потом 90 МБ Firefox. Локально наоборот: селекторы писались под Chrome.
    """
    import os
    import yb_playwright as yb
    print("\n▸ Порядок движков по окружению")

    saved_home = os.environ.get("HOME")
    saved_env = os.environ.get("CLICK_ENV")
    saved_root = yb.ROOT
    try:
        os.environ.pop("CLICK_ENV", None)
        os.environ["HOME"] = "/home/someuser"
        yb.ROOT = Path("/home/user/Click")
        check("локально мы НЕ в облаке", not yb.in_cloud())
        eq("локально первым пробуем Chromium", yb._default_order(), ["chromium", "firefox"])

        os.environ["HOME"] = "/home/appuser"
        check("Streamlit Cloud определяется по домашней папке appuser", yb.in_cloud())
        eq("в облаке первым пробуем Firefox", yb._default_order(), ["firefox", "chromium"])

        os.environ["HOME"] = "/home/someuser"
        yb.ROOT = Path("/mount/src/click")
        check("Streamlit Cloud определяется по пути /mount/src", yb.in_cloud())

        yb.ROOT = Path("/home/user/Click")
        os.environ["CLICK_ENV"] = "cloud"
        check("CLICK_ENV=cloud принудительно включает облачный режим", yb.in_cloud())
    finally:
        yb.ROOT = saved_root
        os.environ.pop("CLICK_ENV", None)
        if saved_env is not None:
            os.environ["CLICK_ENV"] = saved_env
        if saved_home is not None:
            os.environ["HOME"] = saved_home


def test_packages_txt() -> None:
    """
    packages.txt на Streamlit Cloud ставится одной командой apt-get: ЛЮБОЕ имя,
    которое не резолвится, обрывает установку целиком, и приложение не поднимается
    вообще («Error installing requirements»).

    Проверено на живой Ubuntu 24.04 (тот же образ, что у Streamlit Cloud):
    `libasound2` там стал ВИРТУАЛЬНЫМ – его предоставляют сразу два пакета
    (libasound2t64 и liboss4-salsa-asound2), apt отказывается выбирать и падает
    с «has no installation candidate». Сама libasound.so.2 при этом в образе есть.

    Поэтому здесь запрещены: имена, ставшие виртуальными после перехода Ubuntu
    на 64-битный time_t (суффикс t64), и сами t64-имена – их нет на старых образах.
    """
    print("\n▸ packages.txt")
    listed = [ln.strip() for ln in Path("packages.txt").read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.strip().startswith("#")]

    # Образ Streamlit Cloud – Ubuntu 24.04. Доказано ошибкой apt на libasound2:
    # там это имя стало виртуальным и его дают сразу два пакета, поэтому установка
    # падала целиком. Значит t64-имена не только допустимы, но и обязательны.
    virtual_on_noble = {"libasound2", "libcups2", "libatk1.0-0",
                        "libatk-bridge2.0-0", "libatspi2.0-0"}
    bad = set(listed) & virtual_on_noble
    check("нет виртуальных имён – от них apt падает целиком", not bad, f"опасные: {sorted(bad)}")
    check("список не пустой", len(listed) >= 10, f"всего {len(listed)}")
    check("нет дублей", len(listed) == len(set(listed)))
    check("библиотеки Firefox на месте",
          {"libdbus-glib-1-2", "libxt6", "libgtk-3-0", "libasound2t64"} <= set(listed))
    # Ровно то, чего не хватало браузерам в облаке: libgbm.so.1 у Chromium и звук
    # у обоих. Без них Playwright падает «Host system is missing dependencies».
    check("библиотеки Chromium на месте",
          {"libgbm1", "libnss3", "libnspr4", "libdrm2", "libxkbcommon0",
           "libcups2t64", "libatk1.0-0t64", "libatk-bridge2.0-0t64"} <= set(listed))


def test_requirements_txt() -> None:
    """
    requirements.txt: закреплены не только наши библиотеки, но и чужие, которые
    тянутся следом и способны положить сервер ДО запуска нашего кода.

    Живой случай. Streamlit требует «starlette<2,>=0.40.0», облако при пересборке
    поставило самую свежую – 1.4.0. В ней у GZipResponder появился обязательный
    аргумент thread_minimum_size, а Streamlit 1.60.0 его не передаёт. Сервер падал
    с TypeError на КАЖДОМ запросе и отвечал 500 даже на проверку живости: заказчик
    видел «Oh no. Error running app», а перезапуск не помогал – окружение
    собиралось тем же составом.

    Поэтому здесь две проверки: версия записана в файле И установленная starlette
    правда стыкуется со Streamlit. Вторая ловит ту же поломку на будущих версиях,
    как бы её ни назвали.
    """
    print("\n▸ requirements.txt")
    lines = [ln.strip() for ln in Path("requirements.txt").read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    pinned = {ln.split("==")[0].strip().lower() for ln in lines if "==" in ln}

    # Мотор – всё, из чего собран сам веб-сервер. Его поломка = белый экран без
    # диагноза, поэтому держим на точных версиях. Список закрыт: новую строку
    # сюда добавляем только вместе с записью в requirements.txt.
    motor = ("streamlit", "starlette", "uvicorn", "h11", "httptools", "anyio",
             "websockets", "python-multipart", "itsdangerous", "watchdog",
             "protobuf", "playwright")
    missing = [n for n in motor if n not in pinned]
    check("мотор закреплён точными версиями", not missing, f"без версии: {missing}")

    # Закреплённое должно совпадать с тем, на чём прогнаны тесты, – иначе
    # проверки говорят про один состав, а в облако уезжает другой.
    from importlib.metadata import version as installed_version

    for line in lines:
        if "==" not in line:
            continue
        name, want = (x.strip() for x in line.split("#")[0].split("=="))
        if name.lower() == "playwright":
            continue  # ставится отдельно вместе с браузерами
        try:
            got = installed_version(name)
        except Exception:
            continue  # локально не стоит – проверить нечем
        eq(f"{name}: закреплена та версия, на которой тестируем", got, want)

    # Главная проверка: подпись, по которой Streamlit создаёт свой сжиматель
    # ответов, совпадает с тем, что умеет установленная starlette.
    import inspect

    from starlette.middleware.gzip import GZipResponder

    from streamlit.web.server.starlette.starlette_gzip_middleware import (
        _MediaAwareGZipResponder,
    )

    required = [p.name for p in inspect.signature(GZipResponder.__init__).parameters.values()
                if p.name != "self" and p.default is inspect.Parameter.empty
                and p.kind is not inspect.Parameter.VAR_KEYWORD]
    check("у starlette нет новых обязательных аргументов сжатия",
          required == ["app", "minimum_size"], f"требуются: {required}")

    async def nothing(scope, receive, send) -> None:  # ASGI-заглушка
        return None

    try:
        _MediaAwareGZipResponder(nothing, 500, compresslevel=9)
        built = True
        why = ""
    except TypeError as exc:  # ровно то, на чём падало облако
        built, why = False, str(exc)
    check("сжиматель ответов Streamlit создаётся на этой starlette", built, why)


def test_publish_click_on_real_page() -> None:
    """
    Клик по кнопке «Создать» в НАСТОЯЩЕМ браузере.

    Живой случай: кнопка публикации у Яндекса оказывается далеко ниже сгиба, а
    страница дорисовывается уже после того, как мы сняли координаты. Клик по
    точке экрана уходит в пустоту – в логе «Клик сделан», а поста нет.
    Оригинал жал по элементу (elementHandle.click), поэтому и работал.
    """
    import yb_playwright as yb
    print("\n▸ Клик по кнопке публикации (живая страница)")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright доступен", False, "не установлен")
        return

    html = """
    <html><body style="margin:0">
    <div id="pad" style="height:1400px"></div>
    <div class="PostFormRoot" style="width:600px;height:400px">
      <button id="cancel">Отменить</button>
      <button id="draft">Сохранить черновик</button>
      <button id="create">Создать</button>
    </div>
    <script>
      window.clicked = null;
      for (const id of ['cancel', 'draft', 'create'])
        document.getElementById(id).onclick = () => { window.clicked = id; };
      setTimeout(() => { document.getElementById('pad').style.height = '2200px'; }, 300);
    </script></body></html>
    """
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
            page = browser.new_context(viewport={"width": 900, "height": 600}).new_page()

            # 1. Так было раньше: снимаем координаты, страница уезжает, клик мимо.
            page.set_content(html)
            coords = page.evaluate("""() => { const r = document.getElementById('create')
                .getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2}; }""")
            page.wait_for_timeout(600)
            page.mouse.click(coords["x"], coords["y"])
            check("клик по протухшим координатам НЕ попадает (так и ломалось)",
                  page.evaluate("() => window.clicked") is None,
                  str(page.evaluate("() => window.clicked")))

            # 2. Как сейчас: ищем элемент и жмём по нему.
            page.set_content(html)
            page.wait_for_timeout(600)
            el = page.evaluate_handle(yb._FIND_PUBLISH_BTN_JS).as_element()
            check("кнопка найдена", el is not None)
            if el is None:
                browser.close()
                return
            got = el.evaluate(yb._BTN_INFO_JS)
            eq("найдена именно «Создать», а не «Отменить»/«Черновик»", got["text"], "Создать")
            check("кнопка действительно ниже сгиба", got["y"] > 600, f"y={got['y']}")
            el.scroll_into_view_if_needed()
            el.click()
            eq("клик по элементу попадает в «Создать»",
               page.evaluate("() => window.clicked"), "create")
            browser.close()
    except Exception as e:  # noqa: BLE001
        check("браузер запустился", False, str(e)[:120])


def test_city_duplicates() -> None:
    """Один и тот же город (та же карточка или то же имя) нельзя добавить дважды."""
    import re
    import yb_playwright as yb
    print("\n▸ Защита от дублей городов")
    src = Path("streamlit_app.py").read_text(encoding="utf-8")
    start = src.index("def _city_duplicate(")
    end = src.index("def country_picker(", start)
    ns = {"yb": yb, "re": re}
    exec(compile(src[start:end], "dup", "exec"), ns)  # noqa: S102
    dup = ns["_city_duplicate"]

    config = {"countries": [
        {"id": "c-ru", "name": "Россия", "cities": [
            {"id": "1", "name": "Москва", "url": "https://yandex.ru/sprav/111/edit/posts/"},
        ]},
        {"id": "c-kz", "name": "Казахстан", "cities": []},
    ]}
    check("та же карточка в той же стране – дубль",
          dup(config, "https://yandex.ru/sprav/111/p/edit/", "Другое имя", "c-ru") is not None)
    check("та же карточка в ДРУГОЙ стране – тоже дубль",
          dup(config, "https://yandex.ru/sprav/111/edit/main", "Алматы", "c-kz") is not None)
    check("то же имя в той же стране – дубль",
          dup(config, "https://yandex.ru/sprav/222/edit/posts/", "  москва ", "c-ru") is not None)
    check("то же имя в другой стране – НЕ дубль (Москва и в Казахстане бывает)",
          dup(config, "https://yandex.ru/sprav/333/edit/posts/", "Москва", "c-kz") is None)
    check("новый город – не дубль",
          dup(config, "https://yandex.ru/sprav/444/edit/posts/", "Тверь", "c-ru") is None)


def test_worker_thread() -> None:
    """
    Playwright sync API отказывается работать в потоке с запущенным циклом
    asyncio – «It looks like you are using Playwright Sync API inside the
    asyncio loop». Проверяем, что поток воркера чистый и что мёртвый воркер
    заметен, а не висит молча.
    """
    import asyncio

    from playwright_worker import PlaywrightWorker
    print("\n▸ Поток для Playwright")

    w = PlaywrightWorker()
    check("поток живой", w.alive())

    def probe() -> dict:
        try:
            running = asyncio.get_running_loop().is_running()
        except RuntimeError:
            running = False
        return {"running": running, "has_loop": asyncio.get_event_loop() is not None}

    got = w.call(probe)
    check("в потоке НЕТ запущенного цикла asyncio", not got["running"])
    check("цикл в потоке всё же задан – старые версии смотрят на него", got["has_loop"])
    eq("значения возвращаются", w.call(lambda a, b: a + b, 2, 3), 5)

    try:
        w.call(lambda: (_ for _ in ()).throw(ValueError("тест")))
        check("исключение пробрасывается вызывающему", False, "не бросилось")
    except ValueError:
        check("исключение пробрасывается вызывающему", True)

    w.stop()
    for _ in range(50):
        if not w.alive():
            break
        time.sleep(0.02)
    check("после stop поток завершается", not w.alive())


def test_session_validity(tmp: Path) -> None:
    """
    «Сессия сохранена» должна означать НАСТОЯЩУЮ сессию. Файл с анонимными
    куками раньше считался входом, приложение писало «сохранена», а публикация
    упиралась в форму входа.
    """
    import yb_playwright as yb
    print("\n▸ Что считается сохранённой сессией")
    yb.USERS_DATA = tmp
    fp = yb.session_path("SES")

    fp.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    check("пустой список кук – не сессия", not yb.has_saved_session("SES"))

    fp.write_text(json.dumps({"cookies": [
        {"name": "yandexuid", "domain": ".yandex.ru"},
        {"name": "spravka", "domain": ".yandex.ru"},
    ]}), encoding="utf-8")
    check("анонимные куки – не сессия", not yb.has_saved_session("SES"))

    fp.write_text(json.dumps({"cookies": [
        {"name": "yandexuid", "domain": ".yandex.ru"},
        {"name": "Session_id", "domain": ".yandex.ru"},
    ]}), encoding="utf-8")
    check("кука авторизации – сессия есть", yb.has_saved_session("SES"))

    fp.write_text(json.dumps({"cookies": [
        {"name": "Session_id", "domain": ".example.com"},
    ]}), encoding="utf-8")
    check("та же кука на чужом домене – не сессия", not yb.has_saved_session("SES"))

    fp.write_text("{не json", encoding="utf-8")
    check("битый файл – не сессия и не падение", not yb.has_saved_session("SES"))

    # Адрес формы входа содержит retpath со словом profile – именно из-за этого
    # приложение считало «мы уже в кабинете» прямо на форме входа.
    pages_url = {
        "форма входа с retpath на профиль":
            ("https://passport.yandex.ru/auth?retpath=https%3A%2F%2Fpassport.yandex.ru%2Fprofile", True),
        "настоящий профиль": ("https://passport.yandex.ru/profile", False),
        "кабинет Бизнеса": ("https://yandex.ru/sprav/123/p/edit/posts/", False),
    }
    class _FakePage:
        def __init__(self, url): self.url = url
        def evaluate(self, *a, **k): return False
    for name, (url, expect) in pages_url.items():
        eq(f"{name} → страница входа: {expect}", yb.looks_like_login_page(_FakePage(url)), expect)



def test_account_check_on_real_page() -> None:
    """
    Проверка аккаунта на НАСТОЯЩЕЙ странице в браузере.

    Нужна потому, что разбор раньше жил в JS внутри page.evaluate: одна лишняя
    обратная косая – и в облаке всё падало с «unterminated regular expression
    literal», а проверка молча считала, что аккаунт определить нельзя. Тест
    гоняет её на трёх страницах и ловит любую такую поломку.
    """
    import yb_playwright as yb
    print("\n▸ Проверка аккаунта на живой странице")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright доступен", False, "не установлен")
        return

    pages = {
        "нужный аккаунт": ("<h1>Профиль</h1><p>metpromintex@yandex.com</p>", "ok"),
        "чужой аккаунт": ("<h1>Профиль</h1><p>someone.else@yandex.ru</p>", "other"),
        "страница входа": ("<form action='/auth/welcome'><input type='password'>"
                           "<button>Create ID</button></form>", "anonymous"),
        "пустая страница": ("<div></div>", "unknown"),
    }
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
            page = browser.new_page()
            for name, (body, expect) in pages.items():
                page.set_content(f"<html><body>{body}</body></html>")
                saved_goto = page.goto
                page.goto = lambda *a, **k: None          # страницу уже задали руками
                try:
                    got = yb.verify_account(page, "metpromintex@yandex.com")
                finally:
                    page.goto = saved_goto
                eq(f"{name} → {expect}", got.get("state"), expect)
                check(f"{name}: проверка не сломалась", got.get("checked") is not False,
                      "checked=False – значит упало исключение")
            browser.close()
    except Exception as e:  # noqa: BLE001
        check("браузер запустился", False, str(e)[:120])


def test_kp_sheet_choice() -> None:
    """
    Из какого ЛИСТА берём города.

    Живой случай: в таблице рядом с рабочим листом «кп» (101 город) лежал
    старый «карта присутсвия Аэросталь (OLD» с четырьмя. Click выбирал по
    названию и брал старый – заказчик видела 4 города вместо 101. Плюс
    ссылка с gid прямо указывала нужный лист, а мы его игнорировали.
    """
    import kp_sheet
    print("\n▸ Выбор листа в таблице КП")

    eq("gid из ссылки читается",
       kp_sheet.sheet_gid("https://docs.google.com/spreadsheets/d/X/edit?gid=1223548196#gid=1223548196"),
       "1223548196")
    eq("без gid – пусто", kp_sheet.sheet_gid("https://docs.google.com/spreadsheets/d/X/edit"), "")

    titles = ["карта присутсвия Аэросталь (OLD", "Чек-лист доб городов", "кп",
              "НЕ ТРОГАТЬ ОРИГ", "Выгрузка", "Оплаты"]
    order = kp_sheet._sheet_order(titles)  # noqa: SLF001
    eq("рабочий лист «кп» идёт первым", order[0], "кп")
    check("старая копия ушла в конец",
          order.index("карта присутсвия Аэросталь (OLD") > order.index("кп"), str(order))
    check("«НЕ ТРОГАТЬ ОРИГ» тоже в конце",
          order.index("НЕ ТРОГАТЬ ОРИГ") > order.index("кп"), str(order))

    picked = kp_sheet._sheet_order(titles, prefer="Выгрузка")  # noqa: SLF001
    eq("лист, указанный ссылкой, идёт первым", picked[0], "Выгрузка")

    # Обычная таблица без старых копий – порядок как раньше.
    plain = ["Оплаты", "Карта присутствия", "Сводка"]
    eq("карта присутствия по-прежнему в приоритете",
       kp_sheet._sheet_order(plain)[0], "Карта присутствия")  # noqa: SLF001

    # Берём первый лист, на котором ЕСТЬ города.
    sheets = {
        "карта присутсвия Аэросталь (OLD": [
            ["Страна", "Город", "Аккаунт", "Статус"],
            ["Россия", "Москва", "https://yandex.ru/sprav/1/edit", "активные"],
        ],
        "кп": [
            ["Страна", "Город", "Аккаунт", "Статус"],
            ["Россия", "Москва", "https://yandex.ru/sprav/1/edit", "активные"],
            ["Россия", "Казань", "https://yandex.ru/sprav/2/edit", "активные"],
            ["Казахстан", "Алматы", "https://yandex.ru/sprav/3/edit", "активные"],
        ],
    }
    rows = kp_sheet._pick_sheet(list(sheets), lambda t: sheets[t])  # noqa: SLF001
    eq("взят «кп», а не старая копия", len(kp_sheet.parse_rows(rows)[0]), 3)
    rows = kp_sheet._pick_sheet(list(sheets), lambda t: sheets[t],  # noqa: SLF001
                                prefer="карта присутсвия Аэросталь (OLD")
    eq("если ссылка указывает на старый лист – берём его",
       len(kp_sheet.parse_rows(rows)[0]), 1)

    # Старая копия – последняя очередь: сначала ВСЕ рабочие листы, и только
    # если ни на одном городов нет, берём её. Так таблица не остаётся без
    # городов, но при живом «кп» старый лист не подхватывается никогда.
    with_empty = {
        "карта присутсвия Аэросталь (OLD": sheets["карта присутсвия Аэросталь (OLD"],
        "кп": [["Страна", "Город", "Аккаунт"], ["", "", ""]],
    }
    rows = kp_sheet._pick_sheet(list(with_empty), lambda t: with_empty[t])  # noqa: SLF001
    eq("рабочий лист пуст – старая копия идёт запасным вариантом",
       len(kp_sheet.parse_rows(rows)[0]), 1)

    # Если рабочих листов нет вовсе – старый читаем, иначе таблица с
    # единственным листом «…OLD» перестала бы работать.
    only_old = {"карта присутсвия (OLD": sheets["карта присутсвия Аэросталь (OLD"]}
    rows = kp_sheet._pick_sheet(list(only_old), lambda t: only_old[t])  # noqa: SLF001
    eq("единственный лист читается, даже если назван OLD",
       len(kp_sheet.parse_rows(rows)[0]), 1)


def test_kp_sheet() -> None:
    """
    Разбор КП-таблицы. Ключевая сложность: шапка объединённая и колонка
    «Аккаунт» встречается дважды – у Яндекс.Бизнеса и у 2ГИС. Поэтому колонку
    со ссылками ищем по СОДЕРЖИМОМУ, а не по названию.
    """
    import kp_sheet
    print("\n▸ Google-таблица КП")

    rows = [
        ["", "", "", "", "", "Мессенджеры", "", "Яндекс Бизнес", "", "", "2ГИС", "", ""],
        ["Страна", "Город", "Численность", "url", "Telegram", "WhatsApp",
         "Посты", "Аккаунт", "Карта", "Статус", "Аккаунт", "Карта", "Статус"],
        ["Россия", "Москва", "13274285", "https://metpromintex.ru", "", "",
         "", "https://yandex.ru/sprav/365594/p/edit/posts/", "https://yandex.ru/maps/1", "Активная",
         "https://account.2gis.com/1", "https://2gis.ru/1", "Активная"],
        ["Россия", "Санкт-Петербург", "5652922", "https://spb.metpromintex.ru", "", "",
         "", "https://yandex.ru/sprav/234955/p/edit/posts/", "https://yandex.ru/maps/2", "Онлайн",
         "https://account.2gis.com/2", "https://2gis.ru/2", "Удалена"],
        ["Казахстан", "Алматы", "2000000", "https://kz.metpromintex.ru", "", "",
         "", "https://yandex.ru/sprav/777777/edit/posts/", "", "Активная", "", "", "Активная"],
        ["Россия", "Омск", "1104000", "https://omsk.metpromintex.ru", "", "",
         "", "https://yandex.ru/sprav/299334/p/edit/posts/", "", "Удалена", "", "", "Удалена"],
        ["Россия", "БезСсылки", "1000", "https://x.ru", "", "", "", "", "", "Активная", "", "", ""],
    ]
    cities, diag = kp_sheet.parse_rows(rows)

    check("шапка найдена, ошибок нет", not diag.get("error"), str(diag.get("error")))
    eq("колонка со ссылками определена по содержимому (Яндекс, а не 2ГИС)",
       diag.get("urlHeader"), "Аккаунт")
    eq("взята именно колонка Яндекса", diag.get("urlColumn"), 7)
    eq("статус берётся справа от неё (Яндекса, не 2ГИС)", diag.get("statusColumn"), 9)
    eq("городов после фильтра", len(cities), 3)
    check("Омск со статусом «Удалена» отброшен", all(c["name"] != "Омск" for c in cities))
    check("город без ссылки на ЯБ отброшен", all(c["name"] != "БезСсылки" for c in cities))
    check("Питер оставлен: у него удалён только 2ГИС, а Яндекс «Онлайн»",
          any(c["name"] == "Санкт-Петербург" for c in cities))
    eq("ссылка сохранена как есть", cities[0]["url"], "https://yandex.ru/sprav/365594/p/edit/posts/")

    countries = kp_sheet.to_countries(cities, "IMP")
    eq("стран получилось", len(countries), 2)
    eq("первая страна", countries[0]["name"], "Россия")
    ids = [ct["id"] for c in countries for ct in c["cities"]]
    check("id городов уникальны", len(ids) == len(set(ids)))
    check("из ссылок извлекается ID компании",
          all(yb_extract(ct["url"]) for c in countries for ct in c["cities"]))

    # Пустая и битая таблица не должны валить приложение
    empty, d2 = kp_sheet.parse_rows([])
    check("пустая таблица – пустой результат без исключения", empty == [])
    broken, d3 = kp_sheet.parse_rows([["что-то", "не то"], ["1", "2"]])
    check("таблица без шапки – понятная ошибка, а не падение",
          broken == [] and "шапк" in (d3.get("error") or "").lower())

    eq("ID таблицы из ссылки",
       kp_sheet.sheet_id("https://docs.google.com/spreadsheets/d/1AbC_-xyz00000000000/edit#gid=0"),
       "1AbC_-xyz00000000000")
    eq("ID из голого ID", kp_sheet.sheet_id("1AbC_-xyz00000000000"), "1AbC_-xyz00000000000")
    eq("мусор – пусто", kp_sheet.sheet_id("не ссылка"), "")

    # ── Настоящая раскладка КП МетПромИнтекс ──
    # Шапка на ВТОРОЙ строке, над ней объединённые заголовки блоков; «Аккаунт»
    # встречается трижды (Яндекс, 2ГИС, Google); контакты города – слева.
    real = [
        ["", "", "", "", "", "", "Телефония/Почта", "", "", "", "", "Мессенджеры", "",
         "Яндекс Бизнес", "", "", "", "", "2ГИС", "", "", "Google", "", ""],
        ["Страна", "Сортировка", "Город", "Численность", "url", "Адрес", "Почта",
         "Общий\nГород", "Общий\nСотовый", "Реклама\nГород", "SEO\nГород", "Telegram",
         "WhatsApp", "Посты", "Отгрузки", "Аккаунт", "Карта", "Статус",
         "Аккаунт", "Карта", "Статус", "Аккаунт", "Карта", "Статус"],
        ["Россия", "", "Москва", "13274285", "https://metpromintex.ru", "ул. Потаповская Роща",
         "moscow@metpromintex.ru", "+7 (495) 729-83-58", "", "", "", "", "", "", "",
         "https://yandex.ru/sprav/36559471/edit/main", "https://yandex.ru/maps/1", "Активная",
         "https://account.2gis.com/1", "https://2gis.ru/1", "Активная", "", "", ""],
        ["Россия", "", "Казань", "1319000", "https://kazan.metpromintex.ru", "ул. Гафури",
         "kazan@metpromintex.ru", "+7 (843) 216-39-54", "", "", "", "", "", "", "",
         "https://yandex.ru/sprav/363476/edit/main", "", "Онлайн", "", "", "", "", "", ""],
        ["Россия", "", "СТехПроблемой", "1000", "https://x.ru", "ул. Тест",
         "x@metpromintex.ru", "+7 (000) 000-00-00", "", "", "", "", "", "", "",
         "https://yandex.ru/sprav/111111/edit/main", "", "Тех. проблемы", "", "", "", "", "", ""],
        ["Россия", "", "Удалённый", "1000", "https://y.ru", "ул. Тест", "y@metpromintex.ru",
         "+7 (000) 000-00-01", "", "", "", "", "", "", "",
         "https://yandex.ru/sprav/222222/edit/main", "", "Удалена", "", "", "", "", "", ""],
        ["Казахстан", "", "Ещё Не Завели", "1000", "https://z.ru", "ул. Тест", "z@metpromintex.ru",
         "+7 (000) 000-00-02", "", "", "", "", "", "", "",
         "https://yandex.ru/sprav/333333/edit/main", "", "Добавить", "", "", "", "", "", ""],
    ]
    rc, rd = kp_sheet.parse_rows(real)
    check("КП МПИ: шапка на второй строке найдена", not rd.get("error"), str(rd.get("error")))
    eq("КП МПИ: взята колонка Яндекса, а не 2ГИС и не Google", rd.get("urlColumn"), 15)
    eq("КП МПИ: статус Яндекса", rd.get("statusColumn"), 17)
    eq("КП МПИ: городов после фильтра", len(rc), 3)
    check("«Удалена» отброшена", all(c["name"] != "Удалённый" for c in rc))
    check("«Добавить» отброшено – карточки ещё нет", all(c["name"] != "Ещё Не Завели" for c in rc))
    check("«Тех. проблемы» остаётся, но помечена",
          any(c["name"] == "СТехПроблемой" for c in rc) and rd.get("withProblems") == 1)
    eq("контакты города подтянулись: сайт", rc[0]["site"], "https://metpromintex.ru")
    eq("контакты города подтянулись: почта", rc[0]["email"], "moscow@metpromintex.ru")
    eq("контакты города подтянулись: телефон", rc[0]["phone"], "+7 (495) 729-83-58")

    order = kp_sheet._sheet_order(
        ["Сводка", "Карта присутсвия", "НЕ ТРОГАТЬ", "2ГИС", "Приоритеты"])
    eq("лист «Карта присутсвия» (с опечаткой) идёт первым", order[0], "Карта присутсвия")
    check("служебные листы уходят в конец", order[-1] in {"Сводка", "НЕ ТРОГАТЬ", "Приоритеты"})


def yb_extract(url: str):
    import yb_playwright as yb
    return yb.extract_company_id(url)


def test_login_step_detection() -> None:
    """
    Какой экран паспорта перед нами – читаем со страницы, а не помним.

    Живой случай у заказчика: после авто-входа шаг угадали один раз («код»),
    Яндекс тем временем вернулся на «Введите логин», а приложение продолжало
    показывать графы «Пароль» и «Код». Логин вводить было некуда, а пароль,
    введённый в единственное поле, улетал в графу кода.
    """
    import yb_playwright as yb
    print("\n▸ Распознавание экранов входа")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright доступен", False, "не установлен")
        return

    # Разметка повторяет настоящие экраны Яндекс ID (см. скриншоты заказчика).
    screens = {
        "phone": """<h1>Введите номер телефона</h1><p>Чтобы войти или зарегистрироваться</p>
                    <input type="tel" placeholder="+7 (000) 000-00-00">
                    <button>По лицу или отпечатку</button><button>QR-код</button><button>Ещё</button>""",
        "login": """<h1>Введите логин</h1><p>Который вы указали при регистрации</p>
                    <input type="text" placeholder="Логин или email"><button>Войти</button>""",
        "password": """<h1>Введите пароль</h1><p>Чтобы войти в аккаунт metpromintex@yandex.com</p>
                    <input type="password" placeholder="Пароль"><button>Далее</button>
                    <button>Войти с помощью смс</button><button>Отправить письмо для входа</button>""",
        "challenge": """<h1>Безопасный вход</h1>
                    <p>Пожалуйста, подтвердите номер телефона, который привязан к вашему аккаунту.</p>
                    <p>Ваш номер телефона: +7 965 ***-**-77</p><button>Подтвердить</button>""",
        "code": """<h1>Введите код из смс</h1><p>Отправили на +7 965 ***-**-77</p>
                    <input type="text" inputmode="numeric" maxlength="6">
                    <button disabled>Отправить ещё код 00:58</button>""",
        "captcha": """<h1>Введите символы с картинки</h1>
                    <img src="/captcha.png"><input type="text"><button>Далее</button>""",
    }

    with sync_playwright() as pw:
        # На сервере браузер лежит по фиксированному пути, на своём компьютере –
        # там, где его поставил Playwright. Пробуем оба, иначе тест «падает»
        # только из-за окружения.
        try:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        except Exception:  # noqa: BLE001
            try:
                browser = pw.chromium.launch()
            except Exception as e:  # noqa: BLE001
                check("браузер запустился", False, str(e)[:120])
                return
        try:
            context = browser.new_context(viewport={"width": 1000, "height": 700})
            page = context.new_page()
            flow = yb.YbLoginFlow.__new__(yb.YbLoginFlow)
            flow.page, flow.context = page, context

            # Английский паспорт – это НЕ перевод, а другой экран. Заказчику
            # он и достался: вместо «Введите номер телефона» и «Ещё» там
            # «Log in with ID», а вместо звонка «Enter the last 6 digits».
            screens_en = {
                "login": """<h1>Log in with ID</h1><button>Email</button><button>Phone number</button>
                        <input type="text" placeholder="Username or email"><button>Next</button>""",
                "password": """<h1>Enter password</h1><p>To log in to metpromintex@yandex.com account</p>
                        <input type="password" placeholder="Password"><button>Next</button>
                        <button>Log in with SMS code</button><button>Send email for log in</button>""",
                "code": """<h1>Enter the last 6 digits of the calling number</h1>
                        <p>Calling +7 965 ***-**-77 - you don't need to answer</p>
                        <input type="text" inputmode="numeric"><button disabled>There was no call 00:57</button>""",
            }

            for expected, body in screens.items():
                page.set_content(f"<html><body style='margin:40px'>{body}</body></html>")
                eq(f"экран «{expected}» распознан", flow.page_state()["step"], expected)
            for expected, body in screens_en.items():
                page.set_content(f"<html><body style='margin:40px'>{body}</body></html>")
                eq(f"английский экран «{expected}» распознан", flow.page_state()["step"], expected)

            # Экран ожидания письма: полей нет, только «Другой способ входа».
            page.set_content("""<html><body style='margin:40px'>
                <p>Письмо отправлено на metpromintex@yandex.ru</p>
                <p>Когда вы нажмёте на кнопку в письме, увидите на экране три символа</p>
                <button>Другой способ входа</button></body></html>""")
            eq("экран «письмо отправлено» распознан", flow.page_state()["step"], "mail-wait")
            check("кнопка «Другой способ входа» нажимается",
                  yb._click_exact_button(page, yb.ANOTHER_WAY_TEXTS))  # noqa: SLF001

            # Кнопка входа по почте должна находиться на обоих языках.
            for label in ("Отправить письмо для входа", "Send email for log in"):
                page.set_content(f"""<html><body style='margin:40px'>
                    <input type="password"><button>Далее</button>
                    <button onclick="window.__mail=1">{label}</button></body></html>""")
                check(f"кнопка «{label}» нажимается",
                      yb._click_exact_button(page, yb.MAIL_LOGIN_TEXTS)  # noqa: SLF001
                      and page.evaluate("window.__mail === 1"))

            # Защита от «не в ту графу»: на экране логина пароль вводить нельзя.
            page.set_content(f"<html><body>{screens['login']}</body></html>")
            try:
                flow._require_step("password")  # noqa: SLF001
                check("пароль не уходит в чужое поле", False, "проверка не сработала")
            except RuntimeError as e:
                check("пароль не уходит в чужое поле", "логин" in str(e).lower(), str(e))

            # «Ещё» → «Войти по логину»: пункт появляется только после «Ещё».
            page.set_content("""<html><body style='margin:40px'>
                <h1>Введите номер телефона</h1><input type="tel">
                <button onclick="document.getElementById('m').style.display='block'">Ещё</button>
                <div id="m" style="display:none">
                  <button onclick="document.body.innerHTML='<h1>Введите логин</h1>'
                          + '<input type=text placeholder=\\'Логин или email\\'>'">Войти по логину</button>
                </div></body></html>""")
            check("с телефона переходим на вход по логину",
                  flow._switch_to_login_by_password() and flow.page_state()["step"] == "login")  # noqa: SLF001

            # Идём за письмом – «Подтвердить» на телефонном экране жать нельзя:
            # это и есть отправка SMS. Раньше Click жал её сам и своей же
            # рукой уводил вход на телефон, хотя шли за письмом.
            page.set_content("""<html><body style='margin:40px'>
                <h1>Безопасный вход</h1>
                <p>Пожалуйста, подтвердите номер телефона, который привязан к вашему аккаунту.</p>
                <p>Ваш номер телефона: +7 965 ***-**-77</p>
                <button onclick="window.__confirmed=1">Подтвердить</button></body></html>""")
            res = flow.auto_login("a@yandex.ru", "secret", max_steps=4, by_mail=True)
            check("по письму «Подтвердить» НЕ нажимается",
                  page.evaluate("window.__confirmed === undefined"))
            check("объяснено, что делать дальше",
                  "Назад" in (res.get("reason") or ""), res.get("reason"))

            # Обычный вход по паролю эту кнопку жать обязан – иначе тупик:
            # полей на экране нет, нажимать в приложении нечего.
            page.set_content("""<html><body style='margin:40px'>
                <h1>Безопасный вход</h1><p>Подтвердите номер телефона</p>
                <button onclick="window.__confirmed=1">Подтвердить</button></body></html>""")
            flow.auto_login("a@yandex.ru", "secret", max_steps=2, by_mail=False)
            check("без письма «Подтвердить» нажимается",
                  page.evaluate("window.__confirmed === 1"))

            # Пароль: вписать и нажать «Далее» ОДИН раз. Раньше был каскад
            # «Enter + кнопка + запасной клик» – лишние нажатия улетали в
            # кнопки следующего экрана, и заказчик просил так не делать.
            page.set_content("""<html><body style='margin:40px'>
                <h1>Введите пароль</h1><input type="password" placeholder="Пароль">
                <button onclick="window.__next=(window.__next||0)+1">Далее</button>
                <script>document.addEventListener('keydown',
                    e => { if (e.key === 'Enter') window.__enter = 1; });</script>
                </body></html>""")
            flow.submit_password("secret-123")
            eq("«Далее» нажата ровно один раз", page.evaluate("window.__next"), 1)
            check("Enter не нажимался вовсе", page.evaluate("window.__enter === undefined"))
            eq("пароль действительно вписан",
               page.evaluate("document.querySelector('input').value"), "secret-123")

            # Куки устройства помним, куки авторизации – НЕТ. Иначе «сбросить
            # сессию» переставало работать: сессию стёрли, а вход всё равно
            # под старым аккаунтом.
            flow.project_id = "TEST-DEVICE"
            context.add_cookies([
                {"name": "yandexuid", "value": "1", "domain": ".yandex.ru", "path": "/"},
                {"name": "Session_id", "value": "secret", "domain": ".yandex.ru", "path": "/"},
            ])
            flow.save_device()
            saved = json.loads(yb.device_path("TEST-DEVICE").read_text(encoding="utf-8"))
            names = {(c.get("name") or "").lower() for c in saved.get("cookies") or []}
            check("кука устройства сохранена", "yandexuid" in names, str(names))
            check("кука авторизации НЕ сохранена", "session_id" not in names, str(names))
            shutil.rmtree(yb.device_path("TEST-DEVICE").parent.parent, ignore_errors=True)

            # Кнопку жмём по элементу: за сгибом координатный клик промахивается.
            page.set_content("""<html><body style='margin:0'>
                <div style="height:1600px"></div>
                <button onclick="window.__hit=1">Подтвердить</button></body></html>""")
            yb._click_exact_button(page, yb.CONFIRM_BUTTON_TEXTS)  # noqa: SLF001
            check("кнопка ниже сгиба нажимается", page.evaluate("window.__hit === 1"))
        finally:
            browser.close()


def test_actualize_selection() -> None:
    """
    Выбор городов для актуализации переживает обновление списка.

    Живой случай: после загрузки городов из КП у них НОВЫЕ id, а в наборе
    выбранных лежали старые. Новые города оказывались невыбранными молча –
    «проверяет не все города».
    """
    import streamlit as st
    import streamlit_app as app
    print("\n▸ Выбор городов для актуализации")

    st.session_state.clear()
    sel = app._act_selected(["a", "b", "c"])  # noqa: SLF001
    eq("по умолчанию выбраны все", sel, {"a", "b", "c"})

    sel.discard("b")                                    # человек снял галочку
    sel = app._act_selected(["a", "b", "c"])  # noqa: SLF001
    eq("снятая галочка сохраняется", sel, {"a", "c"})

    sel = app._act_selected(["a", "b", "c", "d"])  # noqa: SLF001
    eq("новый город выбран сразу, снятый остался снятым", sel, {"a", "c", "d"})

    sel = app._act_selected(["a", "d"])  # noqa: SLF001
    eq("исчезнувшие города уходят из набора", sel, {"a", "d"})

    # Полная замена списка (загрузка из КП: у всех городов новые id).
    st.session_state.clear()
    app._act_selected(["old-1", "old-2"])  # noqa: SLF001
    fresh = app._act_selected(["new-1", "new-2", "new-3"])  # noqa: SLF001
    eq("после загрузки из КП выбраны ВСЕ новые города",
       fresh, {"new-1", "new-2", "new-3"})
    st.session_state.clear()


def test_add_post_click_on_real_page() -> None:
    """
    Клик «Добавить пост» в НАСТОЯЩЕМ браузере.

    Четвёртое место с той же поломкой: кнопка искалась по координатам и
    жалась по точке экрана. Если она ниже сгиба – промах, и город падал с
    «Кнопка "Добавить пост" не найдена», хотя кнопка была на месте.
    """
    import yb_playwright as yb
    print("\n▸ Клик «Добавить пост» (живая страница)")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright доступен", False, "не установлен")
        return

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        except Exception:  # noqa: BLE001
            browser = pw.chromium.launch()
        try:
            page = browser.new_context(viewport={"width": 900, "height": 600}).new_page()

            page.set_content("""<html><body style="margin:0">
              <div style="height:1500px">лента постов</div>
              <button style="width:200px;height:40px" onclick="window.__add=1">Добавить пост</button>
              <button style="width:200px;height:40px" onclick="window.__wrong=1">Удалить пост</button>
            </body></html>""")
            el = yb.wait_for_element(page, yb._FIND_ADD_POST_JS, 2000)  # noqa: SLF001
            check("кнопка найдена", el is not None)
            box = el.bounding_box() if el else None
            check("кнопка действительно ниже сгиба", box and box["y"] > 600, str(box))
            page.mouse.click(box["x"] + box["width"] / 2, min(box["y"] + 10, 599))
            check("клик по протухшим координатам НЕ попадает",
                  page.evaluate("window.__add === undefined"))
            eq("клик по элементу сработал", yb.click_element(page, el, "Добавить пост"), "по элементу")
            check("нажата именно «Добавить пост»", page.evaluate("window.__add === 1"))
            check("соседняя кнопка не задета", page.evaluate("window.__wrong === undefined"))

            # Кнопка только с иконкой – ищем по подписи для скринридера.
            page.set_content("""<html><body>
              <button aria-label="Добавить публикацию" style="width:44px;height:44px"
                      onclick="window.__add=1"></button></body></html>""")
            check("кнопка-иконка находится по aria-label",
                  yb.wait_for_element(page, yb._FIND_ADD_POST_JS, 1500) is not None)  # noqa: SLF001

            page.set_content("""<html><body>
              <button style="width:44px;height:44px" onclick="window.__add=1">+</button></body></html>""")
            check("голый плюсик тоже находится",
                  yb.wait_for_element(page, yb._FIND_ADD_POST_JS, 1500) is not None)  # noqa: SLF001

            page.set_content("<html><body><button>Отзывы</button></body></html>")
            check("на чужой странице ничего не выдумываем",
                  yb.wait_for_element(page, yb._FIND_ADD_POST_JS, 800) is None)  # noqa: SLF001
        finally:
            browser.close()


def test_toast_and_access_on_real_page() -> None:
    """
    Уведомление «Данные актуализированы» и заглушка «нет доступа».

    Живой случай: заказчик показала, что уведомление всплывает, а Click его
    не видел – искал только внутри элементов с «правильными» классами.
    И отдельно: недоступная карточка выдавала «Кнопка "Добавить пост" не
    найдена», хотя искать там нечего – доступа нет.
    """
    import yb_playwright as yb
    print("\n▸ Уведомление Яндекса и доступ к карточке")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright доступен", False, "не установлен")
        return

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        except Exception:  # noqa: BLE001
            browser = pw.chromium.launch()
        try:
            page = browser.new_context(viewport={"width": 1200, "height": 800}).new_page()

            # Уведомление в контейнере с ПОСТОРОННИМ классом – как у Яндекса.
            page.set_content("""<html><body style="margin:0">
              <div style="height:400px">страница</div>
              <div class="Popup2 Popup2_visible" style="position:fixed;top:20px;right:20px;
                   width:320px;height:56px;background:#fff">
                <span style="display:block">Данные актуализированы</span>
              </div></body></html>""")
            check("уведомление найдено, хотя класс посторонний",
                  page.evaluate(yb._ACTUALIZE_TOAST_JS))  # noqa: SLF001

            page.set_content("""<html><body>
              <div>Режим работы</div><button>Данные актуальны</button></body></html>""")
            check("без уведомления ничего не выдумываем",
                  not page.evaluate(yb._ACTUALIZE_TOAST_JS))  # noqa: SLF001

            # Заглушка недоступной карточки: одна кнопка «Перейти…».
            page.set_content("""<html><body>
              <button aria-label="Ваш профиль"></button>
              <button>Перейти на страницу организаций</button></body></html>""")
            check("недоступная карточка распознана", yb._looks_like_no_access(page))  # noqa: SLF001

            page.set_content("""<html><body>
              <button>Добавить пост</button><button>Товары и услуги</button>
              <button>Публикации</button><button>Отзывы</button>
              <button>Перейти на страницу организаций</button></body></html>""")
            check("рабочая карточка НЕ считается недоступной",
                  not yb._looks_like_no_access(page))  # noqa: SLF001
        finally:
            browser.close()


def test_preflight_duplicate_guard() -> None:
    """
    Перед публикацией смотрим ленту: такой пост уже есть – не отправляем.

    Живой случай: в облаке файлы стираются при перезапуске, вместе с ними
    пропадал реестр публикаций – и тот же прогон создавал ВТОРОЙ пост.
    Лента Яндекса не теряется никогда, поэтому проверка идёт по ней.
    """
    src = Path("yb_playwright.py").read_text(encoding="utf-8")
    print("\n▸ Защита от повторной публикации")
    body = src[src.index("def publish_to_city("):src.index("def actualize_city(")]
    pre = body.index("check_post_already_exists")
    click = body.index("_FIND_PUBLISH_BTN_JS")
    check("лента проверяется ДО клика «Создать»", pre < click)
    check("повтор помечается пропуском, а не ошибкой",
          "skipped-duplicate" in body[pre:pre + 900])

    import yb_playwright as yb
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright доступен", False, "не установлен")
        return
    text = "Отгрузили партию сотового поликарбоната на склад заказчика в этом городе"
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        except Exception:  # noqa: BLE001
            browser = pw.chromium.launch()
        try:
            page = browser.new_context(viewport={"width": 1200, "height": 800}).new_page()
            card = ('<div class="PostCard" style="width:600px;height:200px">'
                    '<span>Публикация на модерации</span><p>{t}</p></div>')

            page.set_content(f"<html><body>{card.format(t=text)}</body></html>")
            got = yb.check_post_already_exists(page, text)
            check("свежий пост в ленте найден", got.get("found") and got.get("fresh"), str(got))

            page.set_content("<html><body>"
                             + f'<div class="PostCard" style="width:600px;height:200px">'
                               f'<span>3 дня назад</span><p>{text}</p></div>' + "</body></html>")
            got = yb.check_post_already_exists(page, text)
            check("старый пост найден, но НЕ свежий",
                  got.get("found") and not got.get("fresh"), str(got))

            page.set_content("<html><body>"
                             + card.format(t="совершенно другой текст поста для города")
                             + "</body></html>")
            got = yb.check_post_already_exists(page, text)
            check("чужой пост за свой не считаем", not got.get("found"), str(got))
        finally:
            browser.close()


def test_bulk_city_duplicates() -> None:
    """
    Добавление городов СПИСКОМ тоже обязано ловить дубли.

    Проверка стояла только на добавлении по одному, а «Списком» складывало
    строки внутрь как есть – именно так в проект попадали одинаковые карточки.
    """
    src = Path("streamlit_app.py").read_text(encoding="utf-8")
    print("\n▸ Дубли при добавлении списком")

    start = src.index("with tab_bulk:")
    end = src.index("st.divider()", start)
    bulk_code = src[start:end]
    check("список проверяется на дубли", "_city_duplicate(" in bulk_code)
    check("про пропущенные говорим человеку", "дубли" in bulk_code.lower())

    # Сама проверка: одна карточка в двух записях – второй раз не проходит.
    import streamlit_app as app
    cfg = {"countries": [
        {"id": "ru", "name": "Россия", "cities": [
            {"id": "1", "name": "Барнаул", "url": "https://yandex.ru/sprav/21461411/p/edit/posts/"}]},
        {"id": "kz", "name": "Казахстан", "cities": []},
    ]}
    dup = app._city_duplicate  # noqa: SLF001
    check("та же карточка – дубль",
          dup(cfg, "https://yandex.ru/sprav/21461411/edit/", "Барнаул-2", "ru"))
    check("та же карточка в ДРУГОЙ стране – тоже дубль",
          dup(cfg, "https://yandex.ru/sprav/21461411/edit/", "Барнаул", "kz"))
    check("новый адрес той же карточки – дубль",
          dup(cfg, "https://yandex.ru/business/companies/company/21461411/", "Барнаул", "ru"))
    check("то же имя в той же стране – дубль",
          dup(cfg, "https://yandex.ru/sprav/99999/edit/", "барнаул", "ru"))
    check("то же имя в другой стране – НЕ дубль",
          not dup(cfg, "https://yandex.ru/sprav/99999/edit/", "Барнаул", "kz"))
    check("новый город – не дубль",
          not dup(cfg, "https://yandex.ru/sprav/99999/edit/", "Томск", "ru"))


def test_actualize_click_on_real_page() -> None:
    """
    Клик «Данные актуальны» в НАСТОЯЩЕМ браузере.

    Живой случай: 11 городов из 19 получили жёлтое «клик прошёл, тост не
    появился». Дело было не в тосте – кнопка у Яндекса прилипшая и уезжает
    при дорисовке страницы, а клик шёл по СНЯТЫМ РАНЕЕ координатам и уходил
    в пустоту. Та же поломка, что была с «Создать».
    """
    import yb_playwright as yb
    print("\n▸ Клик «Данные актуальны» (живая страница)")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright доступен", False, "не установлен")
        return

    page_html = """
    <html><body style="margin:0">
      <div style="height:1500px">много контента</div>
      <button style="width:200px;height:40px"
              onclick="window.__hit=1">Данные актуальны</button>
      <button style="width:200px;height:40px" onclick="window.__wrong=1">Отменить</button>
    </body></html>
    """
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium",
                                         args=["--no-sandbox"])
        except Exception:  # noqa: BLE001
            browser = pw.chromium.launch()
        try:
            page = browser.new_context(viewport={"width": 900, "height": 600}).new_page()
            page.set_content(page_html)

            handle = page.evaluate_handle(yb._ACTUALIZE_BTN_JS)  # noqa: SLF001
            el = handle.as_element()
            check("кнопка найдена", el is not None)
            if el is None:
                browser.close()
                return
            box = el.bounding_box()
            check("кнопка действительно ниже сгиба", box and box["y"] > 600, str(box))

            # Так было раньше: клик по координатам мимо экрана – промах.
            page.mouse.click(box["x"] + box["width"] / 2, min(box["y"] + 20, 599))
            check("клик по протухшим координатам НЕ попадает",
                  page.evaluate("window.__hit === undefined"))

            el.scroll_into_view_if_needed(timeout=3000)
            el.click(timeout=5000)
            check("клик по элементу попадает в «Данные актуальны»",
                  page.evaluate("window.__hit === 1"))
            check("соседняя кнопка не задета", page.evaluate("window.__wrong === undefined"))

            # Кнопки нет вовсе – это «актуализация не требуется», а не ошибка.
            page.set_content("<html><body><button>Отменить</button></body></html>")
            check("без кнопки ничего не находим",
                  page.evaluate_handle(yb._ACTUALIZE_BTN_JS).as_element() is None)  # noqa: SLF001
        finally:
            browser.close()


def test_run_logs(tmp: Path) -> None:
    """
    Лог каждого прогона лежит рядом со своим отчётом.

    Дневной файл мешает все прогоны в кучу, и «Скачать лог» отдавал бы не то.
    Заодно имя дневного файла врало: актуализация писалась в «publish-…».
    """
    import runner
    print("\n▸ Логи прогонов")
    pid = "TEST-LOGS"
    runner.USERS_DATA = tmp
    try:
        runner._append_log(pid, "INFO", "строка актуализации", kind="actualize")  # noqa: SLF001
        names = runner.list_logs(pid)
        check("дневной лог назван по типу прогона",
              any(n.startswith("actualize-") for n in names), str(names))

        runner._append_log(pid, "INFO", "строка публикации", kind="publish")  # noqa: SLF001
        names = runner.list_logs(pid)
        check("публикация пишется в свой файл",
              any(n.startswith("publish-") for n in names), str(names))

        report = runner.p_reports_actualize(pid) / "actualize-2026-08-04T10-00-00.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}", encoding="utf-8")
        runner._snapshot_log(pid, report, kind="actualize")  # noqa: SLF001
        saved = runner.read_run_log(pid, "actualize", report.name)
        check("лог прогона сохранён рядом с отчётом", "строка актуализации" in saved, saved[:80])
        eq("путь лога совпадает с именем отчёта",
           runner.run_log_path(pid, "actualize", report.name).name,
           "actualize-2026-08-04T10-00-00.log")
        eq("лога нет – пустая строка, а не падение",
           runner.read_run_log(pid, "publish", "report-нет-такого.json"), "")
    finally:
        runner.USERS_DATA = paths.data_root()


def test_aps_project() -> None:
    """
    АПС (Авиапромсталь) – пятый проект, города из КП «карта присутствия».

    Её таблица отличается от МПИ: статус называется «Яндекс_Статус» (а не
    «Статус»), значения «активные / не активные» (а не «Удалена»), и одна и
    та же страна записана и «РФ», и «Россия». Без разбора всего этого Click
    брал бы неактивные карточки и рисовал две плитки одной страны.
    """
    import kp_sheet
    import projects_data as pdata
    import streamlit_app as app
    import yb_playwright as yb
    print("\n▸ Проект АПС и его КП")

    check("проект есть в списке", "APS" in app.PROJECTS)

    # Города зашиты списком: КП в таблице ещё не доделана.
    check("города загружены", len(pdata.APS_CITIES) == 62, str(len(pdata.APS_CITIES)))
    check("у каждого города есть страна, имя и ссылка",
          all(c.get("country") and c.get("name") and c.get("url") for c in pdata.APS_CITIES))
    check("нет городов-дублей",
          len({(c["country"], c["name"]) for c in pdata.APS_CITIES}) == len(pdata.APS_CITIES))
    check("все ссылки ведут в раздел «Посты»",
          all(c["url"].endswith("/edit/posts/") for c in pdata.APS_CITIES),
          str([c["url"] for c in pdata.APS_CITIES if not c["url"].endswith("/edit/posts/")][:2]))
    check("для каждого города вытаскивается номер карточки",
          all(yb.extract_company_id(c["url"]) for c in pdata.APS_CITIES))
    check("страны АПС покрыты окончаниями",
          {c["country"] for c in pdata.APS_CITIES} <= set(pdata.APS_ENDINGS["contacts"]),
          str({c["country"] for c in pdata.APS_CITIES} - set(pdata.APS_ENDINGS["contacts"])))

    # Новый адрес карточки Яндекса – в КП АПС он у трёх городов. Раньше такая
    # ссылка не опознавалась вовсе, и город молча выпадал из прогона.
    new_url = "https://yandex.ru/business/companies/company/70210624498/"
    eq("номер из нового адреса", yb.extract_company_id(new_url), "70210624498")
    eq("новый адрес → раздел «Посты»", yb.build_posts_url(new_url),
       "https://yandex.ru/sprav/70210624498/p/edit/posts/")
    eq("новый адрес → раздел «Данные»", yb.build_edit_url(new_url),
       "https://yandex.ru/sprav/70210624498/p/edit/")
    aps = app.PROJECTS.get("APS") or {}
    eq("почта аккаунта", aps.get("yandexEmail"), "aviastalru@yandex.ru")
    eq("название", aps.get("fullName"), "Авиапромсталь")
    check("окончания подключены", aps.get("endings") is pdata.APS_ENDINGS)
    check("таблица КП прописана", "APS" in kp_sheet.DEFAULT_SHEET_URLS)
    check("id таблицы разбирается",
          kp_sheet.sheet_id(kp_sheet.DEFAULT_SHEET_URLS["APS"]) == "1VCwHQRtxWzNv7yAK0pO4OHmxGOjrmI1f")
    check("у всех проектов свой цвет",
          len({p["color"] for p in app.PROJECTS.values()}) == len(app.PROJECTS))
    check("у всех проектов свой пароль",
          len({p["passwordHash"] for p in app.PROJECTS.values()}) == len(app.PROJECTS))

    # ── Разбор строк ровно того вида, что в КП Авиапромстали ──
    header = ["Страна", "Город", "Численность", "url", "Адрес", "Почта", "Общий\nГород"] \
        + [""] * 8 + ["ЯндексБизнес", "ЯндексКарта", "Яндекс_Статус", "Яндекс дата статуса",
                      "2ГИС.Бизнес", "2ГИС.Карта", "2ГИС_статус"]

    def row(country, city, status, sprav="https://yandex.ru/sprav/111/edit", gis="активные"):
        r = [country, city, "", f"https://{city}.aviastal.ru", "", f"{city}@aviastal.ru", ""] \
            + [""] * 8 + [sprav, "", status, "", "", "", gis]
        return r

    rows = [["", "", "", "", "", "Телефония/Почта"], header,
            row("РФ", "abakan", "активные"),
            row("Россия", "moscow", "активные"),
            row("Казахстан", "aktau", "не активные"),
            row("Беларусь", "minsk", "удалён из справочника"),
            row("Киргизия", "bishkek", "")]
    cities, diag = kp_sheet.parse_rows(rows)

    eq("колонка статуса найдена по «Яндекс_Статус»", diag.get("statusHeader"), "Яндекс_Статус")
    check("взят статус Яндекса, а не 2ГИС", diag.get("statusColumn") == 17, str(diag))
    names = [c["name"] for c in cities]
    check("«не активные» отброшены", "aktau" not in names, str(names))
    check("«удалён из справочника» отброшен", "minsk" not in names, str(names))
    check("пустой статус берём", "bishkek" in names, str(names))
    eq("отброшено ровно два города", diag.get("skippedDeleted"), 2)
    eq("«РФ» и «Россия» слились в одну страну",
       sorted({c["country"] for c in cities}), ["Киргизия", "Россия"])

    eq("РФ → Россия", kp_sheet.normalize_country("РФ"), "Россия")
    eq("Белоруссия → Беларусь", kp_sheet.normalize_country("Белоруссия"), "Беларусь")
    eq("незнакомую страну не трогаем", kp_sheet.normalize_country("Армения"), "Армения")
    check("«активные» НЕ считается неактивным",
          not kp_sheet.SKIP_STATUS_RX.search("активные"))
    check("«не активные» считается неактивным",
          bool(kp_sheet.SKIP_STATUS_RX.search("не активные")))


# ════════════════════════════════════════════════════════════════════
def test_reviews() -> None:
    """
    Отзывы. Фикстура – настоящая страница карточки АвиаПромСталь
    (tests_reviews_fixture.json): 13 отзывов, у 12 есть ответ компании,
    один без него. Проверяем ровно то, на чём фича может тихо сломаться:
    адрес раздела, признак «без ответа», отбор и правило имени.
    """
    import json as _json

    import reviews as rv
    import yb_playwright as yb
    print("\n▸ Отзывы")

    # ─── адрес раздела: подтверждён живой карточкой ───
    eq("отзывы: /p/edit/reviews/ – как на живой карточке",
       yb.build_reviews_url("https://yandex.ru/sprav/20761435635/p/edit/reviews/"),
       "https://yandex.ru/sprav/20761435635/p/edit/reviews/")
    eq("отзывы: из раздела «Посты» старого формата",
       yb.build_reviews_url("https://yandex.ru/sprav/40691746/edit/posts/"),
       "https://yandex.ru/sprav/40691746/edit/reviews/")
    eq("отзывы: формат со /p/ сохраняется",
       yb.build_reviews_url("https://yandex.ru/sprav/188702920373/p/edit/"),
       "https://yandex.ru/sprav/188702920373/p/edit/reviews/")
    eq("отзывы: новый адрес карточки приводится к рабочему виду",
       yb.build_reviews_url("https://yandex.ru/business/companies/company/70210624498/"),
       "https://yandex.ru/sprav/70210624498/p/edit/reviews/")
    eq("отзывы: без ссылки – None", yb.build_reviews_url(None), None)

    # ─── разбор настоящей страницы ───
    fx = _json.loads((Path(__file__).parent / "tests_reviews_fixture.json").read_text(encoding="utf-8"))
    preload = {"initialState": {"edit": {"reviews": fx["reviews"]}}}
    block = rv.extract_list(preload)
    eq("фикстура: отзывов на странице", block["shown"], 13)
    eq("фикстура: всего по пейджеру", block["total"], 13)
    eq("фикстура: страница вмещает", block["limit"], 20)

    box = rv.triage(block["items"])
    eq("фикстура: без ответа ровно один", len(box["unanswered"]), 1)
    eq("фикстура: он же уходит в генерацию", len(box["to_draft"]), 1)
    eq("фикстура: это отзыв Егора Севастьянова",
       rv.review_author(box["to_draft"][0]), "Егор Севастьянов")

    # Пустой __PRELOAD_DATA не должен ронять прогон – просто нет отзывов.
    eq("пустое состояние страницы – ноль отзывов", rv.extract_list(None)["shown"], 0)
    eq("чужое состояние страницы – ноль отзывов", rv.extract_list({"initialState": {}})["shown"], 0)

    # ─── признак «есть ответ» ───
    check("отзыв с owner_comment считается отвеченным",
          not rv.is_unanswered({"owner_comment": {"text": "Спасибо за отзыв"}}))
    check("отзыв без owner_comment считается неотвеченным",
          rv.is_unanswered({"full_text": "текст"}))
    check("пустой owner_comment – всё ещё без ответа",
          rv.is_unanswered({"owner_comment": {"text": "   "}}))

    # ─── отбор: пятёрки машине, остальное человеку ───
    made = [
        {"id": "a", "rating": 5, "full_text": "всё отлично", "author": {"user": "Егор"}},
        {"id": "b", "rating": 4, "full_text": "неплохо, но", "author": {"user": "Иван"}},
        {"id": "c", "rating": 1, "full_text": "ужасно",      "author": {"user": "Пётр"}},
        {"id": "d", "rating": 5, "full_text": "",            "author": {"user": "Анна"}},
        {"id": "e", "rating": 5, "full_text": "спасибо",     "author": {"user": "Ольга"},
         "owner_comment": {"text": "уже ответили"}},
    ]
    box = rv.triage(made)
    eq("отбор: без ответа четыре из пяти", len(box["unanswered"]), 4)
    eq("отбор: черновик пишем только на пятёрку с текстом", [i["id"] for i in box["to_draft"]], ["a"])
    eq("отбор: четвёрка и единица – человеку", [i["id"] for i in box["needs_human"]], ["b", "c"])
    eq("отбор: отзыв без текста пропускаем", [i["id"] for i in box["no_text"]], ["d"])

    over = rv.MAX_DRAFTS_PER_CITY + 3
    many = [{"id": f"x{n}", "rating": 5, "full_text": "текст", "author": {"user": "Егор"}}
            for n in range(over)]
    box = rv.triage(many)
    eq("потолок черновиков на город", len(box["to_draft"]), rv.MAX_DRAFTS_PER_CITY)
    eq("остальные помечены как сверх потолка", len(box["over_limit"]), 3)

    # ─── имя автора ───
    eq("имя: из «Имя Фамилия» берём только имя", rv.nice_name("Егор Севастьянов"), "Егор")
    eq("имя: «Павел Филиппов» → «Павел»", rv.nice_name("Павел Филиппов"), "Павел")
    eq("имя: инициал отбрасываем", rv.nice_name("Надежда Х."), "Надежда")
    eq("имя: одно слово", rv.nice_name("Денис"), "Денис")
    eq("имя: двойное через дефис", rv.nice_name("Анна-Мария"), "Анна-Мария")
    eq("имя: с цифрами не берём", rv.nice_name("Пользователь 123"), None)
    # Транслит не берём: пол по нему не прочитать, а в русском ответе он
    # выглядит чужеродно – заказчик просила в таких случаях «клиент».
    eq("имя: латиницей не берём", rv.nice_name("Michail Derugin"), None)
    eq("имя: латиницей – «Клиент»", rv.name_for_prompt("Felix Ostrovitov"), rv.FALLBACK_NAME)
    eq("имя: никнейм не берём", rv.nice_name("xxx_killer"), None)
    eq("имя: грубость не берём", rv.nice_name("сука"), None)
    eq("имя: почту не берём", rv.nice_name("ivan@mail.ru"), None)
    eq("имя: пустое – «Клиент»", rv.name_for_prompt(""), rv.FALLBACK_NAME)
    eq("имя: странное – «Клиент»", rv.name_for_prompt("Пользователь 123"), rv.FALLBACK_NAME)

    # ─── промпты ───
    import projects_data as pdata
    eq("промпты есть на все пять проектов", sorted(pdata.REVIEW_PROMPTS), ["APS", "IMP", "MPE", "MPI", "SMU"])
    for pid in pdata.REVIEW_PROMPTS:
        check(f"маркеры на месте: {pid}",
              pdata.REVIEW_TEXT_MARK in rv.default_prompt(pid)
              and pdata.REVIEW_NAME_MARK in rv.default_prompt(pid))

    item = {"full_text": "Хороший ассортимент", "author": {"user": "Надежда Х."}, "rating": 5}
    built = rv.build_prompt(rv.default_prompt("APS"), item)
    check("промпт: текст отзыва подставлен", "Хороший ассортимент" in built)
    check("промпт: имя подставлено без инициала", "Надежда" in built and "Надежда Х." not in built)
    only_name = rv.build_prompt(rv.default_prompt("APS"),
                                {"full_text": "хорошо", "author": {"user": "Павел Филиппов"}, "rating": 5})
    check("промпт: фамилия в обращение не идёт",
          "Павел" in only_name and "Филиппов" not in only_name)
    check("промпт: маркеров не осталось",
          pdata.REVIEW_TEXT_MARK not in built and pdata.REVIEW_NAME_MARK not in built)

    anon = rv.build_prompt(rv.default_prompt("APS"),
                           {"full_text": "текст", "author": {"user": "vasya2000"}, "rating": 5})
    check("промпт: вместо никнейма «Клиент»", rv.FALLBACK_NAME in anon)

    # МПИ по промпту заказчика не обращается по имени вовсе.
    check("МПИ: промпт запрещает обращение по имени",
          "без личного обращения по имени" in rv.default_prompt("MPI"))
    check("МПИ есть в списке проектов без имени", "MPI" in pdata.REVIEW_NO_NAME_PROJECTS)

    # ─── запрещённые слова ───
    eq("запрет: «склад» и «магазин» ловятся",
       sorted(rv.banned_words("Приезжайте на склад или в магазин")), ["магазин", "склад"])
    eq("запрет: чистый ответ проходит", rv.banned_words("Благодарим за отзыв!"), [])

    # ─── очередь ───
    made_items = [rv.as_queue_item(box["to_draft"][0], project_id="APS", city="Москва",
                                   company_url="https://yandex.ru/sprav/1/p/edit/",
                                   reviews_url="https://yandex.ru/sprav/1/p/edit/reviews/",
                                   status=rv.DRAFTED, draft="Спасибо!")]
    eq("очередь: запись содержит id отзыва", made_items[0]["reviewId"], "x0")
    eq("очередь: город записан", made_items[0]["city"], "Москва")
    merged = rv.merge(made_items, made_items)
    eq("очередь: повторный прогон не плодит дубли", len(merged), 1)


    check("обрыв на середине фразы виден", rv.looks_cut_off("Благодарим за отзыв и"))
    check("целая фраза обрывом не считается", not rv.looks_cut_off("Благодарим за отзыв."))
    check("пустой текст обрывом не считается", not rv.looks_cut_off(""))

    # Общие правила по всем брендам: тире и обращение по полу.
    for pid in pdata.REVIEW_PROMPTS:
        eq(f"{pid}: длинных тире в промпте нет", rv.default_prompt(pid).count("\u2014"), 0)
    item5 = {"full_text": "Заказано 12 тонн", "author": {"user": "Михаил"}, "rating": 5}
    full = rv.build_prompt(rv.default_prompt("MPE"), item5)
    check("общие правила приклеиваются к любому промпту",
          "Уважаемый клиент!" in full and "по полу" in full.lower())

    eq("чистка: длинное тире меняется на короткое",
       rv.clean_draft("Трубы \u2014 это хорошо."), "Трубы \u2013 это хорошо.")
    eq("чистка: markdown-звёздочки убираются",
       rv.clean_draft("Спасибо за **отзыв**."), "Спасибо за отзыв.")
    eq("чистка: кавычки вокруг всего ответа снимаются",
       rv.clean_draft("«Уважаемый Иван! Спасибо.»"), "Уважаемый Иван! Спасибо.")
    eq("чистка: лишние пустые строки схлопываются",
       rv.clean_draft("А.\n\n\n\nБ."), "А.\n\nБ.")
    check("«Уважаемый(ая)» – неудачный черновик",
          rv.looks_broken("Уважаемый(ая) Michail! Благодарим за отзыв о поставке."))

    # Промпт АПС переписан по эталонам заказчика: конкретика из отзыва
    # и статичная ассортиментная фраза дословно, отдельным абзацем.
    aps = rv.default_prompt("APS")
    frase = ("Полный набор металлопродукции: цветной прокат, нержавейка, сортовые и "
             "листовые виды, трубы, арматура \u2013 собираем и отгружаем без задержек.")
    check("АПС: ассортиментная фраза в промпте дословно", frase in aps)
    check("АПС: требуем конкретику из отзыва", "12 тонн арматуры" in aps)
    check("АПС: есть эталонные примеры заказчика", aps.count("Ответ:") >= 3)
    check("АПС: замечание в отзыве отрабатывается",
          "обратную связь" in aps and "оправдываться" in aps)
    check("АПС: закрытие на месте", "С уважением, команда Авиапромсталь." in aps)

    # Отчёт по отправке: после падения приложения это единственный способ
    # понять, что уже ушло в Яндекс.
    import streamlit_app as _app2
    csv = _app2._sent_csv([{"city": "Гродно", "author": "Иван", "rating": 5,
                            "status": rv.ANSWERED, "sentAt": "2026-08-05T10:00:00+00:00",
                            "text": "трубы\nхорошие", "finalText": "Спасибо!", "note": ""}])
    text = csv.decode("utf-8-sig")
    check("в отчёте есть город и автор", "Гродно" in text and "Иван" in text)
    check("в отчёте видно, что отправлено", "отправлен" in text)
    check("переносы строк не ломают CSV", text.count("\n") == 2, repr(text))

    # Раскладка исходов отправки. Отдельно проверяем «не подтвердилось»:
    # раньше такой ответ считался успехом и уходил из списка, а в Яндексе
    # его не было.
    import streamlit_app as _app
    for status, want in (("answered", rv.ANSWERED), ("already", rv.ALREADY),
                         ("unknown", rv.FAILED), ("failed", rv.FAILED)):
        it = {"status": rv.DRAFTED}
        _app._apply_send_result(it, status, "почему")
        eq(f"исход отправки «{status}»", it["status"], want)
    it = {"status": rv.DRAFTED}
    _app._apply_send_result(it, "unknown", "Яндекс пока не показывает ответ")
    check("не подтверждённый ответ остаётся в списке", it["status"] in rv.OPEN_STATUSES)

    # Плоский вид отзыва: со страницы теперь приходит он, из фикстуры –
    # исходный. Понимать надо оба, иначе отбор молча пропустит всё.
    flat = {"id": "f1", "author": "Иван", "rating": 5, "text": "трубы",
            "time_created": 1, "answered": False}
    check("плоский отзыв опознаётся как без ответа", rv.is_unanswered(flat))
    eq("плоский: текст", rv.review_text(flat), "трубы")
    eq("плоский: автор", rv.review_author(flat), "Иван")
    eq("исходный: автор из вложенного поля",
       rv.review_author({"author": {"user": "Пётр"}}), "Пётр")
    check("плоский отвеченный не берётся",
          not rv.is_unanswered({**flat, "answered": True}))
    eq("отбор понимает плоский вид", len(rv.triage([flat])["to_draft"]), 1)
    norm = rv.normalize({"id": "x", "author": {"user": "Анна"}, "rating": 4,
                         "full_text": "лист", "owner_comment": {"text": "ответ"}})
    eq("приведение к плоскому виду",
       (norm["author"], norm["text"], norm["answered"]), ("Анна", "лист", True))

    # Правила, о которых заказчик просила отдельно.
    rules = pdata.REVIEW_COMMON_RULES
    check("правило: конкретика из отзыва", "Назови то, что клиент купил" in rules)
    check("правило: ассортиментная фраза без добавок", "НИЧЕГО КРОМЕ НЕЁ" in rules)
    check("правило: «вы» со строчной", "со строчной буквы" in rules)
    check("правило: «Уважаемый клиент» без имени", "Уважаемый клиент!" in rules)
    eq("в общих правилах нет длинных тире", rules.count("\u2014"), 0)

    # Длина абзацев: заказчик просила, чтобы первый абзац не был вдвое
    # длиннее ассортиментной фразы.
    check("АПС: задана длина абзаца благодарности", "150-230 знаков" in aps)
    check("АПС: ровно три предложения в благодарности", "РОВНО ТРИ" in aps)
    import re as _re
    for body in _re.findall(r"Ответ:\n(.+?)(?=\n\nОтзыв:|$)", aps, _re.S):
        paras = [x.strip() for x in body.strip().split("\n\n") if x.strip()]
        if len(paras) >= 3:
            check(f"эталон: абзацы соразмерны ({len(paras[1])} и {len(paras[2])})",
                  len(paras[1]) <= len(paras[2]) * 1.9,
                  f"{len(paras[1])} против {len(paras[2])}")

    # Негодный черновик – не только оборванный. Один из боевых заканчивался
    # точкой и потому считался целым, хотя целиком состоял из размышлений
    # модели о том, как трактовать промпт.
    check("неудачный: пустой", rv.looks_broken(""))
    check("неудачный: оборван", rv.looks_broken("Благодарим за отзыв и"))
    check("неудачный: заметки модели",
          rv.looks_broken('" is proper, but let\'s check if the prompt specifies exact string.'))
    check("неудачный: рассуждение про абзацы",
          rv.looks_broken("Salutation a sentence? If every paragraph MUST be 2 sentences."))
    check("неудачный: сплошная латиница", rv.looks_broken("Thank you for your kind feedback!"))
    good = ("Уважаемый Святослав!\n\nБлагодарим за оставленный отзыв и обратную связь. "
            "Мы оптимизируем логистику.\n\nС уважением, команда Авиапромсталь.")
    check("годный черновик проходит", not rv.looks_broken(good))
    check("латинское имя внутри русского ответа не мешает",
          not rv.looks_broken("Уважаемый Felix!\n\nБлагодарим за отзыв о поставке. "
                              "Рады сотрудничеству.\n\nС уважением, команда Авиапромсталь."))

    # ─── разбор ответа Gemini ───
    # Всё это – разбор первого боевого прогона: там обрезало ВСЕ 13 черновиков,
    # а в пару из них просочились размышления модели по-английски.
    import llm
    text, finish = llm._text_from({"candidates": [{"content": {"parts": [
        {"text": "Разбираю структуру...", "thought": True},
        {"text": "Уважаемый Егор!\nБлагодарим за отзыв."}]}, "finishReason": "STOP"}]})
    eq("ответ Gemini: размышления не попадают в текст",
       text, "Уважаемый Егор!\nБлагодарим за отзыв.")
    eq("ответ Gemini: причина завершения видна", finish, "STOP")

    text, finish = llm._text_from({"candidates": [{"content": {"parts": [
        {"text": "думаю", "thought": True}]}, "finishReason": "STOP"}]})
    eq("ответ Gemini: одни размышления – пустой текст", text, "")

    _, finish = llm._text_from({"candidates": [{"content": {"parts": [
        {"text": "Уважаемый Егор! Благодарим за"}]}, "finishReason": "MAX_TOKENS"}]})
    eq("ответ Gemini: обрыв распознаётся", finish, "MAX_TOKENS")

    eq("Gemini 3.x – thinkingLevel", llm._thinking_config("gemini-3.6-flash"),
       {"thinkingLevel": "minimal"})
    eq("Gemini 2.5 – thinkingBudget", llm._thinking_config("gemini-2.5-flash"),
       {"thinkingBudget": 0})
    eq("Gemini: слушаем подсказку retryDelay",
       llm._retry_after({"error": {"details": [{"retryDelay": "27s"}]}}), 27.0)
    eq("Gemini: без подсказки – ноль", llm._retry_after({"error": {}}), 0.0)
    # Несколько ключей: лимит Gemini считается на каждый отдельно, поэтому
    # два-три ключа – самый дешёвый способ ускорить генерацию.
    import os as _os
    saved = {k: _os.environ.get(k) for k in
             ("gemini_api_key", "gemini_api_key_2", "gemini_api_keys")}
    try:
        _os.environ["gemini_api_key"] = "k1"
        _os.environ["gemini_api_key_2"] = "k2"
        _os.environ["gemini_api_keys"] = "k3, k2"      # повтор не должен задваиваться
        eq("ключи собираются из всех секретов", llm.api_keys(), ["k1", "k2", "k3"])
        check("несколько ключей видно в описании", "3" in llm.where(), llm.where())

        llm._hits_by = {}; llm._next_key = 0; llm._rpm_cap = llm.START_RPM
        got = [llm._pick_key(llm.api_keys(), "gemini-3.6-flash")[0] for _ in range(3)]
        eq("собранные из секретов ключи чередуются по кругу", got, ["k1", "k2", "k3"])
        llm._hits_by = {}; llm._next_key = 0

        _os.environ.pop("gemini_api_key_2"); _os.environ.pop("gemini_api_keys")
        eq("один ключ – тоже рабочий случай", llm.api_keys(), ["k1"])
        check("при одном ключе подсказываем добавить второй",
              "gemini_api_key_2" in llm.where(), llm.where())
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v

    check("общее время на один черновик ограничено", llm.TOTAL_BUDGET_S <= 120, llm.TOTAL_BUDGET_S)
    check("кругов по моделям немного", llm.ROUNDS <= 3, llm.ROUNDS)

    # Потолок должен вмещать реальную карточку: в Ереване было 12 неотвеченных.
    many12 = [{"id": f"y{n}", "rating": 5, "full_text": "текст", "author": {"user": "Егор"}}
              for n in range(12)]
    eq("потолок вмещает 12 неотвеченных с одной карточки",
       len(rv.triage(many12)["to_draft"]), 12)

    answered = dict(made_items[0], status=rv.ANSWERED)
    eq("очередь: разобранное уходит из открытых", len(rv.open_items([answered])), 0)
    c = rv.counters([answered])
    eq("очередь: счётчик отвеченных", c["answered"], 1)
# ════════════════════════════════════════════════════════════════════
def test_gemini_keys() -> None:
    """
    Ключи Gemini: чередование по кругу и темп вместо гадания паузами.

    Два живых наблюдения заказчика, оба верные.

    Первое: «мы один ключик до конца выжимаем и идём выжимать второй». Так и
    было. Раньше брался первый ключ, которым можно слать ПРЯМО СЕЙЧАС, а
    первым в списке всегда идёт первый ключ – пока пауза мала, он успевал
    освободиться к следующему отзыву, и до второго очередь не доходила.

    Второе: «если падаем по лимиту, нет смысла дальше просить». Тоже так.
    Отвергнутый запрос черновика не даёт, а время съедает. Поэтому теперь
    считаем запросы за минуту и ждём заранее, а планку темпа узнаём по
    первому же отказу и дальше держим сами.

    И общее для обоих: лимит Google считается НА ПРОЕКТ, а не на ключ
    («Rate limits are applied per project, not per API key»). Ключи из одного
    проекта делят одну квоту – это Click распознаёт и говорит вслух.
    """
    import llm
    print("\n▸ Ключи Gemini: круг и темп")

    def reset() -> None:
        llm._shared_quota = False
        llm._hits_by = {}
        llm._next_key = 0
        llm._rpm_cap = llm.START_RPM
        llm._calm = 0

    keys, model = ["k1", "k2", "k3"], "gemini-3.6-flash"

    # ── Чередование ──────────────────────────────────────────────────
    # Все ключи свободны – значит идём строго по кругу, а не липнем к первому.
    reset()
    picked = []
    for _ in range(6):
        k, wait = llm._pick_key(keys, model)
        picked.append(k)
        eq("свободный ключ берётся без ожидания", round(wait), 0)
    eq("ключи идут по кругу, а не один за всех", picked, ["k1", "k2", "k3"] * 2)

    # Именно этого не было раньше: ключ, которым только что слали, не должен
    # получать следующий запрос, пока остальные простаивают.
    reset()
    first, _ = llm._pick_key(keys, model)
    llm._note_use(first, model)
    second, _ = llm._pick_key(keys, model)
    check("подряд один и тот же ключ не берётся", first != second, f"{first} → {second}")

    # ── Темп ─────────────────────────────────────────────────────────
    # Пока за минуту запросов меньше планки – ждать нечего.
    reset()
    llm._rpm_cap = 5
    now = time.time()
    llm._hits_by = {("k1", model): [now - 1, now - 2]}
    eq("не добрали до планки – шлём сразу", round(llm._wait_for("k1", model)), 0)

    # Добрали – ждём, пока самый старый запрос выпадет из минутного окна.
    llm._hits_by = {("k1", model): [now - 50, now - 40, now - 30, now - 20, now - 10]}
    wait = llm._wait_for("k1", model)
    check("добрали планку – ждём освобождения окна", 8 < wait < 12, f"ждём {wait:.1f} сек")

    # Квота у Google считается и на модель тоже: занятость одной модели не
    # мешает другой. На этом и держится перебор моделей при отказе.
    eq("у другой модели своя квота", round(llm._wait_for("k1", "gemini-2.5-flash")), 0)

    # Отказ по лимиту опускает планку ниже темпа, на котором нас остановили,
    # – и дальше держим её сами, не дожидаясь новых отказов.
    reset()
    llm._hits_by = {("k1", model): [now - i for i in range(9)]}
    llm._slower("k1", model)
    check("после отказа планка темпа опускается", llm.current_pace() < llm.START_RPM,
          llm.current_pace())
    check("но не в ноль – иначе прогон встанет", llm.current_pace() >= llm.MIN_RPM,
          llm.current_pace())

    was = llm.current_pace()
    for _ in range(llm.CALM_STREAK):
        llm._faster()
    check("после серии спокойных ответов планка растёт обратно",
          llm.current_pace() > was, llm.current_pace())
    check("выше потолка не разгоняемся", llm.current_pace() <= llm.MAX_RPM)

    # ── Общая квота ──────────────────────────────────────────────────
    # Ключ отказал, наработав запросы сам – всё честно, квота своя.
    reset()
    llm._hits_by = {("k1", model): [now - i for i in range(8)]}
    llm._note_limit("k1", keys)
    check("свой лимит выработал сам ключ – квоту общей не считаем", not llm.shared_quota())

    # А тут отказ получил ключ, которым почти не пользовались: свою минутную
    # квоту он выбрать не мог – значит проект у ключей один.
    reset()
    llm._hits_by = {("k1", model): [now - i for i in range(8)], ("k2", model): [now]}
    llm._note_limit("k2", keys)
    check("отказ отдохнувшему ключу – квота общая", llm.shared_quota())

    # С одним ключом делить квоту не с кем – выдумывать нечего.
    reset()
    llm._hits_by = {("k1", model): [now]}
    llm._note_limit("k1", ["k1"])
    check("с одним ключом общая квота не выдумывается", not llm.shared_quota())

    # При общей квоте запросы всех ключей считаются вместе – иначе тремя
    # ключами мы выбираем одну и ту же квоту втрое быстрее.
    reset()
    llm._shared_quota = True
    llm._rpm_cap = 5
    llm._hits_by = {("k1", model): [now - 50, now - 40, now - 30],
                    ("k2", model): [now - 20, now - 10]}
    wait = llm._wait_for("k3", model)
    check("общая квота: чужие запросы тоже в счёт", wait > 5, f"ждём {wait:.1f} сек")

    # Человеку это надо сказать словами, а не оставить в жёлтых строках лога.
    reset()
    llm._shared_quota = True
    saved = llm.api_keys
    llm.api_keys = lambda: keys
    try:
        text = llm.where()
    finally:
        llm.api_keys = saved
    check("в «Настройках» сказано про общий лимит", "ОБЩИЙ" in text, text)
    check("и сказано, что делать – новый проект", "НОВОМ проекте" in text, text)
    reset()


# ════════════════════════════════════════════════════════════════════
def test_review_batch() -> None:
    """
    Пачка «Переписать все» / «Отправить все» идёт по одной штуке за
    перерисовку – только так работает кнопка «Остановить». Блокирующий
    цикл её не давал: страница замирала, и прервать было нечем.
    """
    import streamlit as st
    import streamlit_app as app
    import reviews as rv
    print("\n▸ Пачка ответов с остановкой")

    class Rerun(Exception):
        pass

    keep = (st.rerun, st.progress, st.button, st.caption, st.success,
            app._review_queue_save, app._review_regenerate)
    st.rerun = lambda *a, **k: (_ for _ in ()).throw(Rerun())
    st.progress = st.caption = st.success = lambda *a, **k: None
    st.button = lambda *a, **k: False
    app._review_queue_save = lambda *a, **k: None
    app._review_regenerate = lambda pid, it: it.update(draft="переписано.", status=rv.DRAFTED)

    def step(items):
        try:
            app._batch_block("APS", items)
        except Rerun:
            pass

    try:
        st.session_state.clear()
        items = [{"reviewId": f"r{n}", "status": rv.DRAFTED, "draft": "старое",
                  "city": "Гродно", "author": "Иван", "text": "отзыв"} for n in range(4)]
        app._batch_start("redo", items, "APS")
        eq("пачка знает свой объём", st.session_state["rv-batch"]["total"], 4)

        step(items)
        eq("за перерисовку обрабатывается ровно одна штука",
           st.session_state["rv-batch"]["done"], 1)
        eq("первый черновик переписан", items[0]["draft"], "переписано.")
        eq("второй пока не тронут", items[1]["draft"], "старое")

        app._batch_stop()
        check("остановка запомнена", st.session_state["rv-batch"]["stop"])
        step(items)
        check("после остановки пачки нет", "rv-batch" not in st.session_state)
        note = st.session_state.get("rv-batch-note") or ""
        check("в итоге сказано, сколько осталось", "осталось 3" in note, note)
        eq("остальные черновики не тронуты", items[3]["draft"], "старое")

        # Пачка доходит до конца сама, если не останавливать.
        st.session_state.clear()
        items = [{"reviewId": f"x{n}", "status": rv.DRAFTED, "draft": "старое",
                  "city": "Баку", "author": "Пётр", "text": "отзыв"} for n in range(3)]
        app._batch_start("redo", items, "APS")
        for _ in range(4):
            step(items)
        check("пачка сама завершилась", "rv-batch" not in st.session_state)
        eq("переписаны все", [i["draft"] for i in items], ["переписано."] * 3)
    finally:
        (st.rerun, st.progress, st.button, st.caption, st.success,
         app._review_queue_save, app._review_regenerate) = keep
        st.session_state.clear()



# ════════════════════════════════════════════════════════════════════
def test_city_checkbox_survives_collapse() -> None:
    """
    Снятая галочка города не теряется, если тут же свернуть страну.

    Заказчик: «снимаю галочку и сворачиваю страну – она всё равно остаётся
    включённой, снять можно только кнопкой».

    Механика. Снятие запоминается в on_change, а он зовётся только если
    галочку в этот проход рисуют. Браузер отправляет снятие; ответа ещё нет,
    человек уже жмёт по стране. Второй запрос приходит с обоими изменениями,
    страна сворачивается – галочки не рисуются, on_change по ним не зовётся,
    снятие пропадает. Кнопка «Снять все» пишет напрямую, поэтому работала.

    Лечится чтением значения НАПРЯМУЮ до отрисовки: невидимые виджеты
    Streamlit вычищает только ПОСЛЕ прогона скрипта (on_script_finished →
    _remove_stale_widgets), так что присланное значение в этот момент ещё
    доступно.
    """
    import inspect
    import streamlit as st
    import streamlit_app as app
    print("\n▸ Галочка города и сворачивание страны")

    st.session_state.clear()
    all_ids = ["ct-1", "ct-2", "ct-3"]
    chosen = app._act_selected(all_ids)
    eq("сначала выбраны все", len(chosen), 3)

    # Браузер прислал снятие, но страну свернули – on_change не позвали.
    st.session_state["act-cb-ct-2"] = False
    check("без подбора значение бы потерялось", "ct-2" in chosen)
    app._act_sync_widgets(all_ids)
    check("снятая галочка подобрана", "ct-2" not in chosen, sorted(chosen))
    eq("остальные не тронуты", sorted(chosen), ["ct-1", "ct-3"])

    # И обратно: галочку вернули – тоже подхватывается.
    st.session_state["act-cb-ct-2"] = True
    app._act_sync_widgets(all_ids)
    check("возврат галочки подхватывается", "ct-2" in chosen, sorted(chosen))

    # Города, про которые браузер ничего не присылал, не трогаем.
    st.session_state.clear()
    st.session_state["act-selected"] = {"ct-1"}
    app._act_sync_widgets(all_ids)
    eq("про что не спрашивали – то не меняем", sorted(st.session_state["act-selected"]), ["ct-1"])

    # Подбор обязан идти ДО отрисовки галочек, иначе смысла в нём нет.
    src = inspect.getsource(app.tab_actualize)
    sync = src.find("_act_sync_widgets")
    draw = src.find("act-cb-")
    check("подбор идёт до отрисовки галочек", 0 < sync < draw, f"подбор {sync}, отрисовка {draw}")

    # Галочка отзывов включена по умолчанию.
    check("«заодно проверить отзывы» включена сразу",
          'setdefault("act-reviews", True)' in src, "")
    st.session_state.clear()


# ════════════════════════════════════════════════════════════════════
def test_reviews_limit_and_stop() -> None:
    """
    Упёрлись в лимит Gemini – хватит пробовать. И «Остановить» слышно сразу.

    Два живых наблюдения заказчика. Первое: в логе по одному городу подряд
    четыре одинаковых отказа «Gemini придерживает запросы по лимиту», шесть
    минут впустую. Вопрос был прямой: «какой смысл?». Смысла нет – квота за
    минуту сама не вернётся, а каждая попытка стоит до полутора минут.

    Второе: «я остановила прогон, но ничего не произошло, как будто надо
    перезагрузить страницу». Остановка проверялась только на границе города,
    а карточка с десятком отзывов обрабатывается минутами.

    Важно: отзывы при этом собираются ВСЕ – теряется только черновик, и он
    дописывается потом кнопкой «Переписать все».
    """
    import llm
    import runner as r
    import reviews as rv
    import yb_playwright as yb
    print("\n▸ Лимит Gemini и остановка прогона")

    items = [{"id": f"r{n}", "rating": 5, "full_text": "Хороший поставщик, спасибо.",
              "author": {"user": "Иван"}} for n in range(8)]
    keep = (llm.generate, yb.read_reviews, yb.build_reviews_url)
    yb.read_reviews = lambda page, url, navigate=True: {
        "ok": True, "items": items, "total": len(items), "shown": len(items), "url": url}
    yb.build_reviews_url = lambda a, b: "https://yandex.ru/sprav/1/p/edit/reviews/"
    task = {"cityName": "Шымкент", "companyId": "1"}

    try:
        # ── Лимит: после двух отказов подряд к Gemini больше не ходим ──
        calls = {"n": 0}

        def limited(prompt):
            calls["n"] += 1
            raise llm.LlmError("Gemini придерживает запросы по лимиту.", is_limit=True)

        llm.generate = limited
        budget = {"limitStreak": 0, "giveUp": False}
        out = r._reviews_for_city("APS", object(), task, "промпт", None, budget)
        eq("к Gemini сходили только дважды, а не восемь раз", calls["n"], r.LIMIT_GIVE_UP)
        check("прогон понял, что дальше бесполезно", budget["giveUp"])
        eq("но отзывы собраны ВСЕ", out["found"], 8)
        check("остальным честно сказано, почему черновика нет",
              "Переписать все" in (out["items"][-1].get("note") or ""),
              out["items"][-1].get("note"))

        # Сдались на одном городе – на следующем даже не пробуем.
        calls["n"] = 0
        out2 = r._reviews_for_city("APS", object(), {"cityName": "Тараз", "companyId": "2"},
                                   "промпт", None, budget)
        eq("на следующем городе запросов уже ноль", calls["n"], 0)
        eq("и его отзывы тоже собраны", out2["found"], 8)

        # Обычная поломка (не лимит) сдаваться не заставляет: мало ли, сеть моргнула.
        def broken(prompt):
            calls["n"] += 1
            raise llm.LlmError("Gemini вернул пустой ответ.")

        llm.generate = broken
        budget2 = {"limitStreak": 0, "giveUp": False}
        calls["n"] = 0
        r._reviews_for_city("APS", object(), task, "промпт", None, budget2)
        check("не лимит – пробуем каждый отзыв", not budget2["giveUp"])
        eq("то есть все восемь", calls["n"], 8)

        # ── Остановка слышна между черновиками, а не только между городами ──
        calls["n"] = 0
        llm.generate = lambda prompt: "Уважаемый Иван!\n\nСпасибо."
        stop_after = {"n": 0}

        def should_stop():
            stop_after["n"] += 1
            return stop_after["n"] > 3      # на четвёртом отзыве жмут «Остановить»

        out3 = r._reviews_for_city("APS", object(), task, "промпт", should_stop,
                                   {"limitStreak": 0, "giveUp": False})
        check("после остановки черновики не пишутся", calls["n"] <= 3, calls["n"])
        eq("а отзывы всё равно все в очереди", out3["found"], 8)
        check("и видно, что это была остановка",
              any("останов" in (i.get("note") or "") for i in out3["items"]))
    finally:
        llm.generate, yb.read_reviews, yb.build_reviews_url = keep


# ════════════════════════════════════════════════════════════════════
def test_look_and_order() -> None:
    """
    Вид «Актуализации»: сверху прогон, снизу ответы, тема светлая.

    Заказчик: «подвинем эту штуку наверх, а то не видно сколько и что»,
    «короче всё что на скриншоте 2 подвинуть вниз», «пусть при заходе светлая
    тема будет, а не тёмная автоматом».
    """
    import inspect
    import streamlit_app as app
    import ui_theme as T
    print("\n▸ Порядок блоков и тема")

    src = inspect.getsource(app.tab_actualize)
    launch = src.find("btn-actualize")
    queue = src.rfind("reviews_queue_block")
    check("очередь ответов идёт ПОСЛЕ запуска прогона", 0 < launch < queue,
          f"запуск {launch}, очередь {queue}")

    # Без входа в Яндекс раздел выходит раньше времени – очередь обязана
    # показаться и там, иначе готовые черновики просто исчезают с экрана.
    head = src[:src.find("Сначала войдите в Яндекс")]
    tail = src[src.find("Сначала войдите в Яндекс"):]
    check("без входа в Яндекс очередь тоже показывается",
          "reviews_queue_block" in tail.split("return")[0], tail[:200])
    check("и это не единственное её место", "reviews_queue_block" in src[len(head):])

    eq("при заходе тема светлая", app.theme(), "light")
    cfg = Path(".streamlit/config.toml").read_text(encoding="utf-8")
    check("и Streamlit до нашего CSS рисует светлым", 'base = "light"' in cfg, cfg[:200])
    check("цвет фона совпадает с палитрой LIGHT",
          T.LIGHT["bg"] in cfg, f'{T.LIGHT["bg"]} не найден')

    # Плашки сводки – тем же видом, что в отчёте.
    row = T.stat_row([("Черновик готов", 2, "ok"), ("Вам на ответ", 1, "warn")])
    check("плашки собраны классами отчёта",
          'class="report-summary"' in row and 'class="report-stat ok"' in row, row[:120])
    eq("пустой набор не рисует пустую рамку", T.stat_row([]), "")

    # Лог сворачивается всегда, в том числе пока прогон идёт.
    live = inspect.getsource(app._render_live_panel)
    check("лог прогона убирается в раскрывашку",
          "expander" in live and "expanded=running" in live, "")


# ════════════════════════════════════════════════════════════════════
def test_assortment_verbatim() -> None:
    """
    Ассортиментная фраза уходит в Яндекс ДОСЛОВНО.

    Живой случай: один опубликованный ответ ушёл со строкой «трубы, armatura
    – собираем и отгружаем без задержек». Слово внезапно оказалось латиницей,
    и в трёх десятках ответов заказчик этого не увидела.

    Сплошной запрет латиницы поставить нельзя – в металлоторговле законно
    встречаются AISI 304, DIN, A2. Зато фраза известна заранее, поэтому
    чиним именно её.
    """
    import projects_data as pdata
    import reviews as rv
    print("\n▸ Ассортиментная фраза")

    for pid, want in pdata.REVIEW_ASSORTMENT.items():
        check(f"{pid}: фраза есть и в промпте проекта",
              want[:40] in pdata.REVIEW_PROMPTS.get(pid, ""), want[:40])

    want = pdata.REVIEW_ASSORTMENT["APS"]
    head, tail = "Уважаемый Бобров!\n\nБлагодарим за отзыв.", "С уважением, команда Авиапромсталь."

    spoiled = want.replace("арматура", "armatura")
    out = rv.clean_draft(f"{head}\n\n{spoiled}\n\n{tail}", "APS")
    check("латиница внутри фразы исправлена", want in out and "armatura" not in out)

    # Модель может слегка переписать фразу своими словами – тоже возвращаем эталон.
    reworded = want.replace("собираем и отгружаем без задержек", "отгружаем без задержек")
    out = rv.clean_draft(f"{head}\n\n{reworded}\n\n{tail}", "APS")
    check("слегка переписанная фраза возвращена к эталону", want in out)

    # А посторонние абзацы не трогаем: в них законно бывает латиница.
    keep = "Уважаемый Иван!\n\nВозим AISI 304 и DIN 933, всё по ГОСТу.\n\nС уважением."
    eq("посторонний текст не подменяется", rv.clean_draft(keep, "APS"), keep)

    # Абзац, ничем не похожий на фразу, эталоном не становится.
    other = f"{head}\n\nДоставка по городу бесплатная.\n\n{tail}"
    check("непохожий абзац остался как был", want not in rv.clean_draft(other, "APS"))

    # Без проекта поведение прежнее – чинить нечего, подменять нечем.
    eq("без проекта фраза не трогается",
       rv.clean_draft(f"{head}\n\n{spoiled}\n\n{tail}"), f"{head}\n\n{spoiled}\n\n{tail}")


# ════════════════════════════════════════════════════════════════════
def test_report_with_reviews() -> None:
    """
    Общий отчёт об актуализации: города, отзывы и лог одним местом.

    Заказчик просила, чтобы отчёт собирался вместе с отзывами и скачивался.
    Тонкость в том, что отправка идёт ПОСЛЕ прогона и вручную: отчёт знает,
    что нашлось, а что с ответом стало – знает очередь. Поэтому выгрузка
    склеивает одно с другим.
    """
    import streamlit_app as app
    import reviews as rv
    print("\n▸ Отчёт вместе с отзывами")

    data = {"type": "actualize", "withReviews": True,
            "reviewTotals": {"found": 2, "drafted": 2, "needsHuman": 0, "noDraft": 0},
            "reviews": [
                {"reviewId": "r1", "city": "Ереван", "author": "Захар", "rating": 5,
                 "text": "Отличные цены.", "draft": "Уважаемый Захар!", "status": rv.DRAFTED},
                {"reviewId": "r2", "city": "Баку", "author": "Амина", "rating": 5,
                 "text": "Помогли нам.", "draft": "Уважаемая Амина!", "status": rv.DRAFTED},
            ]}

    keep = rv.load_queue
    # Отзыв r1 успели отправить уже после прогона, r2 ещё ждёт.
    rv.load_queue = lambda pid: [
        {"reviewId": "r1", "status": rv.ANSWERED, "sentAt": "2026-08-05T10:00:00+00:00",
         "finalText": "Уважаемый Захар! Спасибо.", "note": "Ответ опубликован"},
    ]
    try:
        rows = app._report_reviews("APS", data)
        eq("в отчёт попали оба отзыва прогона", len(rows), 2)
        by_id = {r["reviewId"]: r for r in rows}
        eq("отправленный показан отправленным", by_id["r1"]["status"], rv.ANSWERED)
        eq("и с итоговым текстом", by_id["r1"]["finalText"], "Уважаемый Захар! Спасибо.")
        eq("неотправленный остался ждущим", by_id["r2"]["status"], rv.DRAFTED)
        check("город из отчёта не потерялся", by_id["r1"]["city"] == "Ереван")

        csv = app._sent_csv(rows).decode("utf-8-sig")
        head = csv.splitlines()[0]
        eq("колонки те же, что в выгрузке отправки", head,
           "Город;Автор;Оценка;Статус;Когда;Отзыв;Ответ;Причина")
        check("ждущий отзыв назван по-человечески",
              "черновик ждёт подтверждения" in csv, csv[:400])
        check("отправленный тоже", "отправлен" in csv)

        # Прогон без отзывов ничего лишнего не показывает.
        eq("без отзывов выгрузки нет", app._report_reviews("APS", {"type": "actualize"}), [])
    finally:
        rv.load_queue = keep


# ════════════════════════════════════════════════════════════════════
def test_queue_resets_between_runs() -> None:
    """
    На странице видно разобранное ТОЛЬКО текущего прогона.

    Живой случай: заказчик собрала новые отзывы, а в «Отчёте по отправке»
    увидела «3 отправленных» с какого-то прошлого раза – «чтобы не было
    путаницы, на странице сбрасывается». История при этом не теряется: она
    уходит во вкладку «Отчёт», у каждого прогона свой список и своя выгрузка.

    Ждущие ответа при этом показываются ВСЕ, даже со старых прогонов:
    черновик написан, никуда не делся, потерять его из виду нельзя.
    """
    import streamlit_app as app
    import reviews as rv
    import runner as r
    print("\n▸ Сброс списка между прогонами")

    items = [
        {"reviewId": "old1", "runId": "run-1", "status": rv.ANSWERED, "city": "Баку"},
        {"reviewId": "old2", "runId": "run-1", "status": rv.ANSWERED, "city": "Баку"},
        {"reviewId": "old3", "runId": "run-1", "status": rv.DRAFTED, "city": "Баку",
         "draft": "старый черновик."},
        {"reviewId": "new1", "runId": "run-2", "status": rv.ANSWERED, "city": "Тараз"},
        {"reviewId": "new2", "runId": "run-2", "status": rv.DRAFTED, "city": "Тараз",
         "draft": "новый черновик."},
        {"reviewId": "ancient", "status": rv.ANSWERED, "city": "Ереван"},   # до меток
    ]

    def done_for(run_id: str) -> list[dict]:
        return [it for it in items
                if it.get("status") not in rv.OPEN_STATUSES
                and it.get("runId") == run_id and run_id]

    eq("после нового прогона в списке только его отправленные",
       [it["reviewId"] for it in done_for("run-2")], ["new1"])
    eq("прошлый прогон со страницы ушёл", len(done_for("run-1")), 2)
    check("отзывы без метки (до этой версии) на страницу не лезут",
          all(it.get("runId") for it in done_for("run-2")))

    # Ждущие – все, независимо от прогона.
    pending = rv.open_items(items)
    eq("ждущие показываются со всех прогонов",
       sorted(it["reviewId"] for it in pending), ["new2", "old3"])

    # А в отчёте прогона – его отзывы, даже если в самом отчёте их не сохраняли.
    keep = rv.load_queue
    rv.load_queue = lambda pid: items
    try:
        rows = app._report_reviews("APS", {"type": "actualize", "runId": "run-1"})
        eq("старый отчёт собирается по метке прогона",
           sorted(r["reviewId"] for r in rows), ["old1", "old2", "old3"])
        eq("чужой прогон в него не попадает",
           [r["reviewId"] for r in app._report_reviews("APS", {"runId": "run-2"})
            if r["reviewId"].startswith("old")], [])
    finally:
        rv.load_queue = keep

    # Метку ставит сам прогон – иначе всё вышеописанное не сработает.
    import inspect
    src = inspect.getsource(r._reviews_for_city)
    check("прогон помечает свои отзывы", '"runId"' in src and "run_id" in src)


# ════════════════════════════════════════════════════════════════════
def test_module_attrs_exist() -> None:
    """
    Каждое `модуль.имя`, написанное в коде, обязано существовать.

    Живой случай, из-за которого вкладка «Актуализация» падала на глазах у
    заказчика: в llm.py убрали константу MIN_GAP_S (пауза уступила место
    подсчёту запросов в минуту), а в интерфейсе на неё осталась ссылка в
    оценке «переписать все – примерно N мин». Обычным поиском промах не
    поймался: искали `_gap`, а имя написано прописными.

    Питон такие вещи не проверяет заранее – ошибка вылезает только когда до
    строки дойдёт выполнение. Здесь проверяем разом и заранее.
    """
    import ast
    import importlib
    print("\n▸ Обращения к модулям")

    watched = ("llm", "reviews", "yb_playwright", "runner", "repo_store",
               "projects_data", "ui_theme", "paths", "build", "kp_audit",
               "kp_sheet", "playwright_worker")
    mods = {}
    for name in watched:
        try:
            mods[name] = importlib.import_module(name)
        except Exception:  # noqa: BLE001 – чего нет, того не проверяем
            pass

    # Разбираем именно КОД, а не текст: иначе «build.py» из комментария
    # выглядит как обращение build.py и даёт ложную тревогу.
    bad: list[str] = []
    for src in sorted(Path(".").glob("*.py")):
        if src.name.startswith("tests_"):
            continue
        tree = ast.parse(src.read_text(encoding="utf-8"), filename=src.name)

        # Под какими именами модули зовут ИМЕННО В ЭТОМ файле: `import llm`,
        # `import reviews as rv`. Гадать по общему списку нельзя – в
        # yb_playwright есть своя переменная paths, и это не модуль.
        alias_of: dict[str, str] = {}
        assigned: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in mods:
                        alias_of[a.asname or a.name] = a.name
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                assigned.add(node.id)
            elif isinstance(node, ast.arg):
                assigned.add(node.arg)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            name = node.value.id
            if name in assigned:          # это своя переменная, а не модуль
                continue
            target = mods.get(alias_of.get(name, ""))
            if target is None or hasattr(target, node.attr):
                continue
            bad.append(f"{src.name}:{node.lineno} – {name}.{node.attr}")

    check("в коде нет обращений к несуществующим именам модулей",
          not bad, "; ".join(sorted(set(bad))[:8]))


# ════════════════════════════════════════════════════════════════════
def test_batch_browser_survives_rerun() -> None:
    """
    Один браузер на всю пачку отправки, а не по браузеру на ответ.

    Так приложение падало во второй раз. Заказчик: «попросила массово
    опубликовать, он еле как 6 штук сделал и сломался», в логе облака –
    тишина и мёртвый сервер, без единой питоновской ошибки.

    Причина в том, как устроен сам Streamlit: на каждую перерисовку он
    создаёт для главного скрипта НОВЫЙ модуль (`self._new_module("__main__")`)
    и выполняет файл заново. Значит любое `_X = {}` на верхнем уровне после
    каждого шага снова пустое. Ручка открытого браузера лежала именно там, а
    пачка обрабатывает по одному ответу за перерисовку – и ссылка терялась
    ровно между ответами. Старый Chromium никто не закрывал, на следующий
    ответ поднимался ещё один. Шесть ответов – шесть браузеров, и облако
    убивало приложение по памяти. Счётчик «перезапускать каждые пять» не
    спасал: он обнулялся тем же способом.

    Симптомов было три, причина одна. Не срабатывало «карточка этого города уже
    открыта» – каждый ответ грузил её заново, хотя пять отзывов Еревану должны
    уходить на одну страницу. Не доживал до пяти счётчик перезапуска. И старый
    браузер никто не закрывал.

    Поэтому проверяем не заплатку, а нужное поведение целиком: один браузер на
    пачку, одна загрузка карточки на город, новая вкладка на смене города – и
    ручка живёт в session_state, а не в переменной модуля.
    """
    import streamlit as st
    import streamlit_app as app
    import reviews as rv
    import yb_playwright as yb
    print("\n▸ Браузер пачки переживает перерисовку")

    made = {"browsers": 0, "closed": 0, "workers": 0, "tabs": 0, "loads": []}

    class FakeBrowser:
        def __init__(self, project_id, headless=True):
            self.page = object()
            made["browsers"] += 1

        def start(self):
            return None

        def new_page(self):
            made["tabs"] += 1
            self.page = object()
            return self.page

        def save_session(self):
            return None

        def close(self):
            made["closed"] += 1

    class FakeWorker:
        def __init__(self):
            made["workers"] += 1
            self._alive = True

        def alive(self):
            return self._alive

        def call(self, func, *a, **k):
            return func(*a, **k)

        def stop(self):
            self._alive = False

    def fake_publish(page, url, review_id, text, original, navigate):
        if navigate:
            made["loads"].append(url)      # сколько раз реально грузили карточку
        return {"status": "answered", "reason": ""}

    keep = (app.PlaywrightWorker, yb.YbBrowser, yb.publish_review_answer,
            app.get_settings, app._review_queue_save)
    app.PlaywrightWorker = FakeWorker
    yb.YbBrowser = FakeBrowser
    yb.publish_review_answer = fake_publish
    app.get_settings = lambda pid: {"headless": True}
    app._review_queue_save = lambda *a, **k: None

    try:
        st.session_state.clear()
        # Пять отзывов Еревану и два Баку – ровно тот расклад, про который
        # спросила заказчик: «зачем каждый раз открывать одну и ту же карточку».
        plan = [("Ереван", "https://yandex.ru/sprav/1/p/edit/reviews/")] * 5 \
             + [("Баку", "https://yandex.ru/sprav/2/p/edit/reviews/")] * 2
        items = [{"reviewId": f"s{n}", "status": rv.DRAFTED, "draft": "готовый ответ.",
                  "city": city, "author": "Иван", "text": "отзыв", "reviewsUrl": url}
                 for n, (city, url) in enumerate(plan)]

        def in_module_globals() -> list[str]:
            """
            Где ещё, кроме session_state, лежат браузер и его поток.

            Это и есть проверка на ту самую поломку: всё, что найдётся здесь,
            в облаке обнулится на следующей же перерисовке, а живой Chromium
            останется висеть в памяти.
            """
            found = []
            for name, val in list(vars(app).items()):
                if name.startswith("__"):
                    continue
                seen = [val] if not isinstance(val, dict) else list(val.values())
                for v in seen:
                    parts = v if isinstance(v, tuple) else (v,)
                    if any(isinstance(x, (FakeBrowser, FakeWorker)) for x in parts):
                        found.append(name)
                        break
            return found

        # Шесть ответов подряд – ровно тот случай, на котором всё легло.
        for n, it in enumerate(items, 1):
            app._send_one("APS", it, it["draft"])
            stale = in_module_globals()
            check(f"ответ {n}: браузер не осел в переменных модуля",
                  not stale, f"нашёлся в {stale}")

        eq("на всю пачку один браузер", made["browsers"], 1)
        eq("карточка грузится раз на город, а не на каждый ответ",
           made["loads"], ["https://yandex.ru/sprav/1/p/edit/reviews/",
                           "https://yandex.ru/sprav/2/p/edit/reviews/"])
        eq("вкладка пересоздаётся на смене города", made["tabs"], 2)
        eq("все семь ответов ушли",
           [i.get("status") for i in items], [rv.ANSWERED] * 7)

        # Ручка обязана лежать в session_state: только оно переживает
        # перерисовку. Ровно этого и не было.
        box = st.session_state.get(app._BROWSER_BOX) or {}
        check("ручка браузера живёт в session_state", "APS" in box, list(box))

        app._batch_browser_close("APS")
        eq("после пачки браузер закрыт", made["closed"], 1)
        check("и ручка убрана из session_state",
              not (st.session_state.get(app._BROWSER_BOX) or {}).get("APS"))
    finally:
        (app.PlaywrightWorker, yb.YbBrowser, yb.publish_review_answer,
         app.get_settings, app._review_queue_save) = keep
        st.session_state.clear()


# ════════════════════════════════════════════════════════════════════
def test_one_build() -> None:
    """
    Метка сборки обязана быть одна на все модули.

    Проверка не косметическая. Если хоть у одного модуля метка своя,
    условие «все ли из одной сборки» становится ложным НАВСЕГДА, и Click
    перезагружает все десять модулей на каждое нажатие. Так приложение и
    выбило по памяти в облаке, причём перезапуск не помогал: следующее же
    действие всё повторяло.
    """
    import build
    import kp_audit
    import kp_sheet
    import llm
    import paths as _paths
    import playwright_worker
    import projects_data
    import repo_store
    import reviews
    import runner
    import streamlit_app as app
    import ui_theme
    import yb_playwright
    print("\n▸ Одна метка сборки на всё приложение")

    mods = {
        "kp_audit": kp_audit, "kp_sheet": kp_sheet, "llm": llm, "paths": _paths,
        "playwright_worker": playwright_worker, "projects_data": projects_data,
        "repo_store": repo_store, "reviews": reviews, "runner": runner,
        "ui_theme": ui_theme, "yb_playwright": yb_playwright,
    }
    for name, mod in mods.items():
        eq(f"метка модуля {name}", getattr(mod, "BUILD", None), build.BUILD)
    eq("метка главного экрана", app.UI_BUILD, build.BUILD)

    # Своей строки BUILD в модулях быть не должно – только импорт из build.py,
    # иначе рано или поздно её опять забудут поменять.
    root = Path(__file__).parent
    own = [f.name for f in root.glob("*.py")
           if f.name not in ("build.py",)
           and re.search(r'^BUILD = "', f.read_text(encoding="utf-8"), re.M)]
    eq("метка задана только в build.py", own, [])

    check("список перезагрузки включает сам build", "build" in app._MODULES)
    for name in mods:
        check(f"{name} в списке перезагрузки", name in app._MODULES, app._MODULES)



def test_yandex_domain() -> None:
    """
    Один ящик – два паспорта.

    Живой случай: у трёх проектов стоял @yandex.ru и вход шёл письмом, у
    четвёртого @yandex.com – и Яндекс уводил в международный паспорт, где
    вход подтверждается звонком на телефон. Разница была ровно в домене.
    """
    import streamlit_app as app
    print("\n▸ Домен аккаунта Яндекса")
    eq("com → ru", app._ru_domain("metpromintex@yandex.com"), "metpromintex@yandex.ru")  # noqa: SLF001
    eq("ya.ru → ru", app._ru_domain("vika@ya.ru"), "vika@yandex.ru")  # noqa: SLF001
    eq("com.tr → ru", app._ru_domain("a@yandex.com.tr"), "a@yandex.ru")  # noqa: SLF001
    eq("ru не трогаем", app._ru_domain("mepen88@yandex.ru"), "mepen88@yandex.ru")  # noqa: SLF001
    eq("чужой домен не трогаем", app._ru_domain("v@gmail.com"), "v@gmail.com")  # noqa: SLF001
    eq("пусто не ломает", app._ru_domain(""), "")  # noqa: SLF001
    check("у всех проектов российский паспорт",
          all(p["yandexEmail"].endswith("@yandex.ru") for p in app.PROJECTS.values()),
          str({k: v["yandexEmail"] for k, v in app.PROJECTS.items()}))


def test_local_time() -> None:
    """
    Время отчёта показываем по часам человека.

    В файл оно пишется в UTC, и в шапке стояло «07:33», когда в логе рядом
    было «12:33» – выглядело как посторонний старый отчёт.
    """
    from datetime import datetime, timezone
    import streamlit_app as app
    print("\n▸ Время отчёта")

    iso = "2026-08-04T07:33:44.123456+00:00"
    got = app.local_time(iso)
    expect = datetime.fromisoformat(iso).astimezone().strftime("%d.%m.%Y, %H:%M:%S")
    eq("время переведено в местное", got, expect)
    check("формат как в оригинале (дд.мм.гггг)", got.count(".") >= 2 and ", " in got, got)
    eq("пустое значение не ломает", app.local_time(None), "")
    eq("мусор отдаётся как есть", app.local_time("не дата"), "не дата")
    check("время без часового пояса считаем UTC",
          app.local_time("2026-08-04T07:33:44")
          == datetime(2026, 8, 4, 7, 33, 44, tzinfo=timezone.utc).astimezone()
             .strftime("%d.%m.%Y, %H:%M:%S"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="click-tests-"))
    try:
        test_urls()
        test_publish_api_filter()
        test_text()
        test_retry_rules()
        test_ledger_and_lock(tmp)
        test_run_state(tmp)
        test_parallel_runs(tmp)
        test_task_format(tmp)
        test_report_render()
        test_yandex_domain()
        test_aps_project()
        test_reviews()
        test_gemini_keys()
        test_review_batch()
        test_city_checkbox_survives_collapse()
        test_reviews_limit_and_stop()
        test_look_and_order()
        test_assortment_verbatim()
        test_report_with_reviews()
        test_queue_resets_between_runs()
        test_module_attrs_exist()
        test_batch_browser_survives_rerun()
        test_one_build()
        test_actualize_selection()
        test_add_post_click_on_real_page()
        test_toast_and_access_on_real_page()
        test_preflight_duplicate_guard()
        test_bulk_city_duplicates()
        test_actualize_click_on_real_page()
        test_run_logs(tmp)
        test_local_time()
        test_browser_fallback()
        test_engine_order()
        test_packages_txt()
        test_requirements_txt()
        test_publish_click_on_real_page()
        test_city_duplicates()
        test_worker_thread()
        test_session_validity(tmp)
        test_account_check_on_real_page()
        test_login_step_detection()
        test_kp_sheet_choice()
        test_kp_sheet()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

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
