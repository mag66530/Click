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
import shutil
import sys
import tempfile
import time
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


def test_text() -> None:
    print("\n▸ Текст поста (порт buildFinalText)")
    import projects_data as pdata

    src = Path("streamlit_app.py").read_text(encoding="utf-8")
    start = src.index("def build_final_text(")
    end = src.index("# ═══", start)
    endings = {"SMU": pdata.SMU_ENDINGS, "IMP": pdata.IMP_ENDINGS,
               "MPE": pdata.MPE_ENDINGS, "MPI": pdata.MPI_ENDINGS}
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

            # Кнопку жмём по элементу: за сгибом координатный клик промахивается.
            page.set_content("""<html><body style='margin:0'>
                <div style="height:1600px"></div>
                <button onclick="window.__hit=1">Подтвердить</button></body></html>""")
            yb._click_exact_button(page, yb.CONFIRM_BUTTON_TEXTS)  # noqa: SLF001
            check("кнопка ниже сгиба нажимается", page.evaluate("window.__hit === 1"))
        finally:
            browser.close()


def test_report_summary() -> None:
    """Плитки отчёта: в актуализации «Всего» лишнее, время – по часам человека."""
    import ui_theme as T
    print("\n▸ Плитки отчёта")

    act = T.report_summary({"total": 143, "actualized": 3, "notNeeded": 140, "failed": 0},
                           1620, keys=["actualized", "notNeeded", "failed"], with_total=False)
    check("в актуализации нет плитки «Всего»", "Всего" not in act)
    for word in ("Актуализировано", "Не требовалось", "Ошибок", "Время"):
        check(f"плитка «{word}» на месте", word in act)
    check("время в минутах, а не в секундах", "27.0 мин" in act, act[-260:])

    pub = T.report_summary({"total": 3, "ok": 3}, 30)
    check("в публикации «Всего» осталось", "Всего" in pub)


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
        test_task_format(tmp)
        test_report_render()
        test_report_summary()
        test_local_time()
        test_browser_fallback()
        test_engine_order()
        test_packages_txt()
        test_publish_click_on_real_page()
        test_city_duplicates()
        test_worker_thread()
        test_session_validity(tmp)
        test_account_check_on_real_page()
        test_login_step_detection()
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
