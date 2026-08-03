"""
tests_click.py — самопроверка логики без запуска браузера.

Запуск:  python tests_click.py

Проверяем именно то, что ломалось в первой версии переноса:
  • нормализация URL карточки (иначе форма поста просто не открывается);
  • сборка текста поста (совпадение с оригинальным buildFinalText);
  • правила ретрая — после клика «Создать» повтор ЗАПРЕЩЁН;
  • реестр публикаций — тот же текст в тот же город второй раз не уходит;
  • лок прогона — второй запуск не стартует;
  • формат файла задач, который читает runner.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

FAILED: list[str] = []
PASSED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED.append(name)
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


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


def test_publish_api_filter() -> None:
    import yb_playwright as yb
    print("\n▸ Распознавание API-ответа о создании поста")
    f = yb._is_publish_api_response
    check("POST к /sprav/api/posts — считаем", f("https://yandex.ru/sprav/api/posts/create", "POST"))
    check("PUT к /api/post — считаем", f("https://yandex.ru/api/v1/post/42", "PUT"))
    check("GET не считаем", not f("https://yandex.ru/sprav/api/posts/list", "GET"))
    check("метрика не считается", not f("https://mc.yandex.ru/sprav/metric/hit", "POST"))
    check("аналитика не считается", not f("https://yandex.ru/sprav/api/analytics", "POST"))
    check("посторонний домен без /sprav/ и /posts/ не считается",
          not f("https://example.com/upload", "POST"))


def test_text() -> None:
    print("\n▸ Текст поста (порт buildFinalText)")
    import projects_data as pdata

    src = Path("streamlit_app.py").read_text(encoding="utf-8")
    start = src.index("def build_final_text(")
    end = src.index("# ═══", start)
    ns = {"pdata": pdata, "PROJECTS": {
        "SMU": {"endings": None},
        "IMP": {"endings": pdata.IMP_ENDINGS},
        "MPE": {"endings": pdata.MPE_ENDINGS},
    }}
    exec(compile(src[start:end], "bft", "exec"), ns)  # noqa: S102
    build = ns["build_final_text"]

    smu = build("SMU", "Россия", "arrival", "Поступил швеллер")
    check("СМУ: контакты страны подставлены", "stalmetural.ru" in smu and "+7 (499) 130-36-69" in smu)
    check("СМУ: хэштеги на месте", "#Поступление_СМУ #Стальметурал #СМУ #Металлопрокат" in smu)
    check("СМУ: поздравление — без окончания", build("SMU", "Россия", "greeting", "С Новым годом") ==
          "С Новым годом")

    imp = build("IMP", "Казахстан", "shipment", "Отгрузили трубу")
    check("ИМП: контакты Казахстана", "inmetprom.kz" in imp and "astana@inmetprom.kz" in imp)
    check("ИМП: хэштеги бренда", "#Отгрузка_ИМП" in imp)

    # Армения у ИМП без телефона — строка с телефоном должна ИСЧЕЗНУТЬ целиком
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
    check("другой текст в тот же город — не блокируется", runner.recent_publication(pid, other_text) is None)
    other_city = {**task, "companyId": "222", "companyUrl": "https://yandex.ru/sprav/222/p/edit/posts/"}
    check("тот же текст в другой город — не блокируется", runner.recent_publication(pid, other_city) is None)
    check("окно 0 часов отключает блокировку",
          runner.recent_publication(pid, task, window_hours=0) is None)

    runner._ledger_add(pid, runner._text_key("111", task["companyUrl"], task["postText"]), task,
                       "unknown", "run-2")
    rec = runner.recent_publication(pid, task)
    check("неподтверждённая публикация тоже блокирует повтор", rec is not None and rec["status"] == "unknown")

    runner.clear_ledger(pid)
    check("очистка реестра работает", runner.recent_publication(pid, task) is None)

    print("\n▸ Лок прогона (защита от дубля №1)")
    check("лок берётся", runner._acquire_lock(pid, "run-A"))
    runner._threads.pop(pid, None)
    check("протухший лок (процесс перезапущен) забирается", runner._acquire_lock(pid, "run-B"))

    import threading
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    runner._threads[pid] = t
    check("лок живого прогона НЕ забирается", not runner._acquire_lock(pid, "run-C"))
    stop.set()
    t.join(timeout=2)
    runner._threads.pop(pid, None)
    runner._release_lock(pid)
    check("лок снимается", not runner.p_lock(pid).exists())


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
    summary = T.report_summary({"total": 10, "ok": 7, "unknown": 2, "failed": 1}, 130)
    check("сводка показывает «проверьте»", "Проверьте" in summary and ">2<" in summary)
    check("сводка показывает время в минутах", "2.2 мин" in summary)
    check("пустой лог даёт заглушку", "log-placeholder" in T.log_box(""))
    check("строки лога раскрашиваются", "log-err" in T.log_box("[ERROR] всё плохо"))


def test_browser_fallback() -> None:
    """
    В облаке (Streamlit Cloud) у Chromium часто нет системных библиотек —
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
    должен идти ПЕРВЫМ — иначе мы качаем 150 МБ Chromium впустую и только
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
    `libasound2` там стал ВИРТУАЛЬНЫМ — его предоставляют сразу два пакета
    (libasound2t64 и liboss4-salsa-asound2), apt отказывается выбирать и падает
    с «has no installation candidate». Сама libasound.so.2 при этом в образе есть.

    Поэтому здесь запрещены: имена, ставшие виртуальными после перехода Ubuntu
    на 64-битный time_t (суффикс t64), и сами t64-имена — их нет на старых образах.
    """
    print("\n▸ packages.txt")
    listed = [ln.strip() for ln in Path("packages.txt").read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.strip().startswith("#")]

    # Имена, которые на Ubuntu 24.04 предоставляют НЕСКОЛЬКО пакетов → apt падает.
    ambiguous = {"libasound2"}
    # t64-имена не существуют на Debian 12 / Ubuntu 22.04 → apt падает там.
    t64 = {n for n in listed if n.endswith("t64")}

    bad = (set(listed) & ambiguous) | t64
    check("нет имён, ломающих установку на каком-либо образе", not bad, f"опасные: {sorted(bad)}")
    check("список не пустой", len(listed) >= 10, f"всего {len(listed)}")
    check("нет дублей", len(listed) == len(set(listed)))
    check("библиотеки Firefox на месте (в облаке работает он)",
          {"libdbus-glib-1-2", "libxt6", "libgtk-3-0"} <= set(listed))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="click-tests-"))
    try:
        test_urls()
        test_publish_api_filter()
        test_text()
        test_retry_rules()
        test_ledger_and_lock(tmp)
        test_run_state(tmp)
        test_task_format(tmp)
        test_report_render()
        test_browser_fallback()
        test_engine_order()
        test_packages_txt()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "═" * 60)
    if FAILED:
        print(f"  ПРОВАЛЕНО {len(FAILED)} из {PASSED + len(FAILED)}:")
        for name in FAILED:
            print(f"    • {name}")
        return 1
    print(f"  ВСЁ ХОРОШО — {PASSED} проверок пройдено")
    print("═" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
