"""
tests_runner_e2e.py – сквозная проверка прогона БЕЗ реального браузера.

Подменяем yb.YbBrowser и yb.publish_to_city заглушками и проверяем то,
из-за чего всё и затевалось:

  1. прогон реально доходит до конца и пишет отчёт после КАЖДОГО города;
  2. повторный запуск не публикует заново то, что уже ушло (реестр);
  3. пока идёт прогон, второй запуск не стартует (лок);
  4. после статуса 'unknown' (клик был, подтверждения нет) повтор НЕ делается;
  5. после падения ДО клика – безопасный повтор делается;
  6. остановка по кнопке действительно останавливает.

Запуск:  python tests_runner_e2e.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import runner  # noqa: E402
import yb_playwright as yb  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED.append(name)
        print(f"  ❌ {name}" + (f" – {detail}" if detail else ""))


def eq(name: str, got, expected) -> None:
    check(name, got == expected, f"получили {got!r}, ждали {expected!r}")

# ─── Заглушки браузера ──────────────────────────────────────────────
class FakePage:
    url = "https://yandex.ru/sprav/1/p/edit/posts/"
    # Вкладка, пережившая перезапуск браузера, живой не бывает: любое действие
    # на ней падает «Event loop is closed! Is Playwright already stopped?».
    dead = False

    def goto(self, *a, **k): return None
    def wait_for_timeout(self, *a, **k): return None
    def evaluate(self, *a, **k): return None
    def is_closed(self): return False
    def close(self): return None


class FakeBrowser:
    def __init__(self, project_id, headless=True):
        self.project_id = project_id
        self.page = FakePage()
        self.started = 0
        self.side_opened = 0
        self.side_closed = 0
        self._side = None

    def start(self): self.started += 1
    def new_page(self): return self.page
    def save_session(self): return None
    def close(self): return None

    def restart(self):
        # Перезапуск уносит с собой ВСЕ вкладки, включая вторую сессию: у
        # настоящего браузера после него нет даже цикла событий. Модель тут
        # честная нарочно – на этом прогон 2ГИС однажды и лёг.
        self.started += 1
        if self._side is not None:
            self._side.dead = True
        self._side = None

    # Вторая сессия (2ГИС) в том же браузере – см. yb.YbBrowser.side_page.
    def side_page(self, session_file):
        if self._side is None:
            self.side_opened += 1
            self._side = FakePage()
        return self._side

    def close_side(self):
        self.side_closed += 1
        self._side = None


CALLS: list[str] = []
SCRIPT: dict[str, list[dict]] = {}
# Сколько городов прогон СЧИТАЛ сделанными в момент начала очередного города.
PROGRESS_AT_START: list[int] = []
_PROGRESS_PID: list[str] = []


def fake_publish(page, task, idx=0, total=1, temp_dir=None, should_stop=None):
    city = task.get("cityName")
    if _PROGRESS_PID:
        PROGRESS_AT_START.append(int(runner.read_state(_PROGRESS_PID[0]).get("current") or 0))
    CALLS.append(city)
    queue = SCRIPT.get(city)
    if queue:
        result = queue.pop(0) if len(queue) > 1 else queue[0]
    else:
        result = {"status": "ok", "reason": "Пост опубликован (API подтвердил)",
                  "steps": {"publish": "api-confirmed"}}
    return {"cityName": city, "companyUrl": task.get("companyUrl"), "durationMs": 100, **result}


def fake_verify_account(page, expected_email):
    return {"matched": True, "emails": [expected_email], "checked": True}


def install_fakes() -> None:
    yb.YbBrowser = FakeBrowser            # type: ignore[assignment]
    yb.publish_to_city = fake_publish     # type: ignore[assignment]
    yb.verify_account = fake_verify_account  # type: ignore[assignment]
    yb.check_post_already_exists = lambda page, text: {"found": False, "fresh": False, "reason": ""}  # type: ignore
    yb.upload_product_photos = lambda *a, **k: {"uploaded": 0, "failed": 0, "errors": []}  # type: ignore
    # Файлы «скачиваются» без сети: пути отдаются как есть.
    yb.fetch_photos = lambda urls, tmp: ([str(u) for u in (urls or [])], [])  # type: ignore


# ─── Хелперы ────────────────────────────────────────────────────────
def write_tasks(pid: str, cities: list[str], text: str = "Текст поста для теста") -> None:
    folder = runner.p_tasks(pid)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "credentials": {"email": "test@yandex.ru", "password": "x"},
        "projectName": "TEST", "country": "Россия",
        "tasks": [{"cityName": c, "companyUrl": f"https://yandex.ru/sprav/{100 + i}/p/edit/posts/",
                   "companyId": str(100 + i), "postText": text}
                  for i, c in enumerate(cities)],
    }
    (folder / f"01-Россия-{int(time.time() * 1000)}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")



def last_log(pid: str) -> str:
    """Текст последнего файла лога прогона."""
    names = runner.list_logs(pid, limit=1)
    return runner.read_log(pid, names[0]) if names else ""


def wait_done(pid: str, timeout: float = 25, kind: str | None = None) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        state = runner.read_state(pid, kind)
        if state.get("status") in ("done", "error", "stopped"):
            return state
        time.sleep(0.1)
    return runner.read_state(pid, kind)


def settle(pid: str, timeout: float = 5, kind: str | None = None) -> None:
    """Дождаться, пока прогон реально отпустит лок. Между статусом «done»
    и снятием лок-файла есть окно – без этой паузы следующий старт получал
    «Уже идёт другой прогон»."""
    end = time.time() + timeout
    while time.time() < end and runner.is_running(pid, kind):
        time.sleep(0.05)
    # Лок снят, но поток ещё доживает свой finally – без join следующий
    # старт получает «Предыдущий прогон ещё не завершился».
    for key, t in list(runner._threads.items()):
        if key[0] == pid and t:
            t.join(timeout=max(0.1, end - time.time()))


def latest_report(pid: str) -> dict:
    reports = runner.list_reports(pid, "publish", limit=1)
    return runner.read_report(pid, "publish", reports[0]["name"]) if reports else {}


# ─── Сценарии ───────────────────────────────────────────────────────
def scenario_happy_path(pid: str) -> None:
    print("\n▸ Обычный прогон: 3 города")
    CALLS.clear(); SCRIPT.clear()
    write_tasks(pid, ["Москва", "Казань", "Пермь"])
    ok, msg = runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
    check("прогон запустился", ok, msg)
    state = wait_done(pid); settle(pid)
    check("прогон завершился статусом done", state.get("status") == "done", str(state.get("status")))

    report = latest_report(pid)
    totals = report.get("totals") or {}
    check("в отчёте 3 города", totals.get("total") == 3, str(totals))
    check("все 3 успешны", totals.get("ok") == 3, str(totals))
    check("отчёт помечен finished", report.get("state") == "finished")
    check("каждый город был опубликован ровно один раз",
          CALLS == ["Москва", "Казань", "Пермь"], str(CALLS))
    check("файл задач уехал в done/", not list(runner.p_tasks(pid).glob("*.json")))
    check("лог прогона записан", "ИТОГИ" in runner.read_live_log(pid, "publish"))
    check("файл лога на диске создан", bool(runner.list_logs(pid)))


def scenario_progress(pid: str) -> None:
    """
    Полоса прогресса не должна забегать вперёд.

    Живой случай: счётчик увеличивался ДО обработки города, и на одном городе
    сразу писалось «1 из 1». Заказчик видела «готово», ждала отчёт и решала,
    что он не формируется, хотя работа ещё шла.
    """
    print("\n▸ Прогресс показывает сделанное, а не начатое")
    CALLS.clear(); SCRIPT.clear()
    PROGRESS_AT_START.clear(); _PROGRESS_PID.clear(); _PROGRESS_PID.append(pid)
    write_tasks(pid, ["Тула", "Орёл"], text="Текст для проверки прогресса")
    runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
    state = wait_done(pid); settle(pid)
    _PROGRESS_PID.clear()

    check("на первом городе прогресс ещё 0",
          PROGRESS_AT_START[:1] == [0], str(PROGRESS_AT_START))
    check("на втором городе прогресс 1 (первый закончен)",
          PROGRESS_AT_START[1:2] == [1], str(PROGRESS_AT_START))
    check("в конце прогресс равен числу городов",
          int(state.get("current") or 0) == 2, str(state.get("current")))


def scenario_dedup(pid: str) -> None:
    print("\n▸ ГЛАВНОЕ: повторный запуск того же поста не публикует дубли")
    CALLS.clear(); SCRIPT.clear()
    write_tasks(pid, ["Москва", "Казань", "Пермь"])          # тот же текст, что и в прошлом прогоне
    ok, _ = runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
    check("второй прогон запустился", ok)
    wait_done(pid); settle(pid)

    check("НИ ОДИН город не отправлен в Яндекс повторно", CALLS == [], str(CALLS))
    totals = (latest_report(pid).get("totals") or {})
    check("все 3 помечены как пропущенные дубли", totals.get("skipped") == 3, str(totals))

    print("\n▸ Другой текст в те же города публикуется нормально")
    CALLS.clear()
    write_tasks(pid, ["Москва", "Казань"], text="Совершенно другой текст поста")
    runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
    wait_done(pid); settle(pid)
    check("новый текст ушёл в оба города", sorted(CALLS) == ["Казань", "Москва"], str(CALLS))


def scenario_unknown_no_retry(pid: str) -> None:
    print("\n▸ Клик был, подтверждения нет → повтора НЕТ (главная причина дублей)")
    CALLS.clear(); SCRIPT.clear()
    SCRIPT["Сочи"] = [{"status": "unknown", "reason": "Публикация не подтверждена",
                       "steps": {"publish": "unknown"}}]
    write_tasks(pid, ["Сочи"], text="Текст для сценария unknown")
    runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru", retry_unknown=False)
    wait_done(pid); settle(pid)

    check("публикация вызвана РОВНО один раз", CALLS.count("Сочи") == 1, str(CALLS))
    totals = (latest_report(pid).get("totals") or {})
    check("город помечен «проверьте вручную»", totals.get("unknown") == 1, str(totals))
    results = latest_report(pid).get("results") or []
    check("в отчёте видна причина", any("не подтверждена" in (r.get("reason") or "") for r in results))


def scenario_safe_retry(pid: str) -> None:
    print("\n▸ Упали ДО клика → безопасный повтор делается")
    CALLS.clear(); SCRIPT.clear()
    SCRIPT["Тверь"] = [
        {"status": "failed", "reason": "Кнопка «Добавить пост» не найдена",
         "steps": {"addButton": "missing"}},
        {"status": "ok", "reason": "Пост опубликован (API подтвердил)",
         "steps": {"publish": "api-confirmed"}},
    ]
    write_tasks(pid, ["Тверь"], text="Текст для сценария retry")
    runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
    wait_done(pid); settle(pid)

    check("была вторая попытка", CALLS.count("Тверь") == 2, str(CALLS))
    totals = (latest_report(pid).get("totals") or {})
    check("итог – успех", totals.get("ok") == 1, str(totals))


def scenario_attempt_budget(pid: str) -> None:
    """
    На город – не больше ТРЁХ попыток.

    Первый проход делал попытку + безопасный ретрай (две), второй проход звал
    ту же функцию, и она снова делала две – итого четыре. Теперь во втором
    проходе внутренний ретрай запрещён.
    """
    print("\n▸ На город не больше трёх попыток")
    CALLS.clear(); SCRIPT.clear()
    # Город падает ДО клика КАЖДЫЙ раз – значит повторяем по максимуму.
    SCRIPT["Псков"] = [
        {"status": "failed", "reason": "Кнопка «Добавить пост» не найдена",
         "steps": {"addButton": "missing"}},
    ]
    write_tasks(pid, ["Псков"], text="Текст для подсчёта попыток")
    runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
    wait_done(pid)

    check("попыток ровно три, а не четыре", CALLS.count("Псков") == 3, str(CALLS))
    totals = (latest_report(pid).get("totals") or {})
    check("город помечен ошибкой", totals.get("failed") == 1, str(totals))


def scenario_double_start(pid: str) -> None:
    print("\n▸ Двойной клик по кнопке «Опубликовать» не запускает второй прогон")
    CALLS.clear(); SCRIPT.clear()
    slow = {"status": "ok", "reason": "ok", "steps": {"publish": "api-confirmed"}}
    original = yb.publish_to_city

    def slow_publish(page, task, idx=0, total=1, temp_dir=None, should_stop=None):
        CALLS.append(task.get("cityName"))
        time.sleep(0.6)
        return {"cityName": task.get("cityName"), "durationMs": 600, **slow}

    yb.publish_to_city = slow_publish  # type: ignore[assignment]
    try:
        write_tasks(pid, ["Омск", "Томск", "Тула"], text="Текст для двойного клика")
        ok1, _ = runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
        time.sleep(0.2)
        ok2, msg2 = runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
        check("первый запуск принят", ok1)
        check("второй запуск ОТКЛОНЁН", not ok2, msg2)
        check("причина отказа понятна пользователю", "уже" in msg2.lower(), msg2)
        wait_done(pid); settle(pid)
        check("каждый город опубликован один раз", sorted(CALLS) == ["Омск", "Томск", "Тула"], str(CALLS))
    finally:
        yb.publish_to_city = original  # type: ignore[assignment]


def scenario_stop(pid: str) -> None:
    print("\n▸ Кнопка «Остановить» действительно останавливает")
    CALLS.clear(); SCRIPT.clear()
    original = yb.publish_to_city

    def slow_publish(page, task, idx=0, total=1, temp_dir=None, should_stop=None):
        CALLS.append(task.get("cityName"))
        time.sleep(0.5)
        return {"cityName": task.get("cityName"), "durationMs": 500,
                "status": "ok", "reason": "ok", "steps": {"publish": "api-confirmed"}}

    yb.publish_to_city = slow_publish  # type: ignore[assignment]
    try:
        write_tasks(pid, [f"Город-{i}" for i in range(10)], text="Текст для остановки")
        runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
        time.sleep(1.2)
        runner.request_stop(pid, "publish")
        state = wait_done(pid, timeout=15)
        check("статус прогона – «остановлен»", state.get("status") == "stopped", str(state.get("status")))
        check("обработали не все 10 городов", len(CALLS) < 10, str(len(CALLS)))
        check("сделанное сохранено в отчёте",
              (latest_report(pid).get("totals") or {}).get("total") == len(CALLS))
        check("файл задач НЕ уехал в done (прогон не закончен)",
              bool(list(runner.p_tasks(pid).glob("*.json"))))
    finally:
        yb.publish_to_city = original  # type: ignore[assignment]


def scenario_wrong_account(pid: str) -> None:
    print("\n▸ Чужой аккаунт в Яндексе → прогон не стартует")
    CALLS.clear(); SCRIPT.clear()
    original = yb.verify_account
    yb.verify_account = lambda page, email: {  # type: ignore
        "state": "other", "matched": False, "emails": ["someone@else.ru"], "checked": True}
    try:
        write_tasks(pid, ["Уфа"], text="Текст для проверки аккаунта")
        runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru",
                             strict_account_check=True)
        state = wait_done(pid); settle(pid)
        check("прогон остановлен с ошибкой", state.get("status") == "error", str(state.get("status")))
        check("ни один пост не отправлен", CALLS == [], str(CALLS))
        check("в ошибке названы оба аккаунта",
              "test@yandex.ru" in (state.get("error") or "") and "someone@else.ru" in (state.get("error") or ""),
              str(state.get("error")))
    finally:
        yb.verify_account = original  # type: ignore[assignment]


def scenario_account_unknown(pid: str) -> None:
    """
    Проверка аккаунта не смогла ничего определить – это НЕ повод стоять.
    Ровно на этом падал живой прогон: «залогинен НЕ ТОТ аккаунт, найдено:
    не определено» при полностью правильном аккаунте.
    """
    print("\n▸ Аккаунт определить не удалось → прогон всё равно идёт")
    CALLS.clear(); SCRIPT.clear()
    original = yb.verify_account
    yb.verify_account = lambda page, email: {  # type: ignore
        "state": "unknown", "matched": True, "emails": [], "checked": True}
    try:
        write_tasks(pid, ["Тверь"], text="Текст при неопределённом аккаунте")
        runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru",
                             strict_account_check=True)
        state = wait_done(pid); settle(pid)
        check("прогон дошёл до конца", state.get("status") == "done", str(state.get("status")))
        # В очереди могли остаться файлы прошлых сценариев – важно, что наш город ушёл.
        check("город опубликован", "Тверь" in CALLS, str(CALLS))
        check("в логе предупреждение, а не остановка",
              "не удалось определить аккаунт" in last_log(pid).lower(), "нет предупреждения")
    finally:
        yb.verify_account = original  # type: ignore[assignment]


def scenario_no_session(pid: str) -> None:
    print("\n▸ Сессии нет вовсе → понятная остановка, а не «чужой аккаунт»")
    CALLS.clear(); SCRIPT.clear()
    original = yb.verify_account
    yb.verify_account = lambda page, email: {  # type: ignore
        "state": "anonymous", "matched": False, "emails": [], "checked": True}
    try:
        write_tasks(pid, ["Курск"], text="Текст без сессии")
        runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru",
                             strict_account_check=True)
        state = wait_done(pid); settle(pid)
        err = (state.get("error") or "").lower()
        check("прогон остановлен", state.get("status") == "error", str(state.get("status")))
        check("сказано про сессию, а не про чужой аккаунт",
              "никто не залогинен" in err and "не тот аккаунт" not in err, err)
    finally:
        yb.verify_account = original  # type: ignore[assignment]


def scenario_actualize_reviews(pid: str) -> None:
    """
    Актуализация с галочкой «отзывы». Браузер и Gemini подменены –
    проверяем именно склейку: что отбор доходит до очереди, что негатив
    туда попадает без черновика, а отзыв с ответом не попадает вовсе.
    """
    print("\n▸ Актуализация с отзывами")
    import reviews as rv

    (runner.p_tasks_actualize(pid)).mkdir(parents=True, exist_ok=True)
    (runner.p_tasks_actualize(pid) / "01-Россия.json").write_text(json.dumps({
        "country": "Россия", "projectName": "TEST",
        "tasks": [{"cityName": "Москва", "companyUrl": "https://yandex.ru/sprav/500/p/edit/",
                   "companyId": "500"}],
    }, ensure_ascii=False), encoding="utf-8")

    rv.save_queue(pid, [])                       # начинаем с пустой очереди
    yb.actualize_city = lambda page, task, i=0, t=1: {
        "cityName": task["cityName"], "companyUrl": task["companyUrl"],
        "status": "actualized", "reason": "клик прошёл", "durationMs": 10}
    yb.read_reviews = lambda page, url: {
        "ok": True, "url": url, "total": 3, "shown": 3, "reason": "",
        "items": [
            {"id": "r1", "rating": 5, "full_text": "всё отлично", "author": {"user": "Егор"},
             "time_created": 1690250836338},
            {"id": "r2", "rating": 2, "full_text": "долго везли", "author": {"user": "Иван"},
             "time_created": 1690250836338},
            {"id": "r3", "rating": 5, "full_text": "спасибо", "author": {"user": "Анна"},
             "time_created": 1690250836338, "owner_comment": {"text": "и вам спасибо"}},
        ]}
    rv.project_prompt = lambda project_id: "Ответь на отзыв. [вставить отзыв] [имя]"
    import llm
    llm.generate = lambda prompt: "Уважаемый Егор! Благодарим за отзыв."

    ok, msg = runner.start_actualize(pid, headless=True, delay_s=0, with_reviews=True)
    check("прогон с отзывами стартовал", ok, msg)
    check("в сообщении видно, что отзывы включены", "отзыв" in msg.lower(), msg)
    wait_done(pid); settle(pid)

    queue = rv.load_queue(pid)
    eq("в очередь попали два отзыва (третий уже с ответом)", len(queue), 2)
    by_id = {it["reviewId"]: it for it in queue}
    eq("пятёрка получила черновик", by_id["r1"]["status"], rv.DRAFTED)
    check("черновик не пустой", bool(by_id["r1"]["draft"]), by_id["r1"]["draft"])
    eq("двойка ушла человеку", by_id["r2"]["status"], rv.NEEDS_HUMAN)
    eq("человеку черновик не писали", by_id["r2"]["draft"], "")
    check("отвеченный отзыв в очередь не попал", "r3" not in by_id)
    check("в очереди есть ссылка на раздел отзывов",
          by_id["r2"]["reviewsUrl"].endswith("/reviews/"), by_id["r2"]["reviewsUrl"])

    reports = runner.list_reports(pid, "actualize", limit=1)
    data = runner.read_report(pid, "actualize", reports[0]["name"]) if reports else {}
    check("отчёт помечен как прогон с отзывами", bool(data.get("withReviews")))
    eq("в отчёте два неотвеченных", (data.get("reviewTotals") or {}).get("found"), 2)
    eq("в отчёте один черновик", (data.get("reviewTotals") or {}).get("drafted"), 1)
    live = runner.read_live_log(pid, "actualize")
    check("итог по отзывам попал в лог", "ОТЗЫВЫ" in live, live[-300:])


def scenario_actualize_reviews_off(pid: str) -> None:
    """Галочка выключена – к отзывам не ходим вообще. Это обратная совместимость."""
    print("\n▸ Актуализация без отзывов не трогает отзывы")
    import reviews as rv

    (runner.p_tasks_actualize(pid) / "01-Россия.json").write_text(json.dumps({
        "country": "Россия", "projectName": "TEST",
        "tasks": [{"cityName": "Казань", "companyUrl": "https://yandex.ru/sprav/501/p/edit/",
                   "companyId": "501"}],
    }, ensure_ascii=False), encoding="utf-8")
    rv.save_queue(pid, [])

    touched = []
    yb.read_reviews = lambda page, url: touched.append(url) or {
        "ok": True, "url": url, "total": 0, "shown": 0, "items": [], "reason": ""}

    ok, msg = runner.start_actualize(pid, headless=True, delay_s=0)
    check("прогон без отзывов стартовал", ok, msg)
    wait_done(pid); settle(pid)
    eq("к разделу отзывов не обращались", touched, [])
    eq("очередь осталась пустой", rv.load_queue(pid), [])


def scenario_reviews_llm_down(pid: str) -> None:
    """Gemini недоступен – отзыв всё равно в очереди, но с ручным вводом."""
    print("\n▸ Отзывы при недоступном Gemini")
    import reviews as rv

    (runner.p_tasks_actualize(pid) / "01-Россия.json").write_text(json.dumps({
        "country": "Россия", "projectName": "TEST",
        "tasks": [{"cityName": "Пермь", "companyUrl": "https://yandex.ru/sprav/502/p/edit/",
                   "companyId": "502"}],
    }, ensure_ascii=False), encoding="utf-8")
    rv.save_queue(pid, [])

    yb.read_reviews = lambda page, url: {
        "ok": True, "url": url, "total": 1, "shown": 1, "reason": "",
        "items": [{"id": "z1", "rating": 5, "full_text": "хорошо", "author": {"user": "Пётр"},
                   "time_created": 1690250836338}]}
    rv.project_prompt = lambda project_id: "Ответь на отзыв. [вставить отзыв] [имя]"
    import llm

    def boom(prompt):
        raise llm.LlmError("Упёрлись в лимит запросов Gemini.")
    llm.generate = boom

    ok, msg = runner.start_actualize(pid, headless=True, delay_s=0, with_reviews=True)
    check("прогон стартовал", ok, msg)
    state = wait_done(pid); settle(pid)
    eq("прогон дошёл до конца, а не упал", state.get("status"), "done")

    queue = rv.load_queue(pid)
    eq("отзыв всё равно в очереди", len(queue), 1)
    eq("но без черновика", queue[0]["status"], rv.NO_DRAFT)
    check("причина видна человеку", "лимит" in queue[0]["note"].lower(), queue[0]["note"])


def scenario_reviews_page_broken(pid: str) -> None:
    """Страница отзывов не открылась – город всё равно актуализирован."""
    print("\n▸ Сбой страницы отзывов не ломает актуализацию")
    import reviews as rv

    (runner.p_tasks_actualize(pid) / "01-Россия.json").write_text(json.dumps({
        "country": "Россия", "projectName": "TEST",
        "tasks": [{"cityName": "Омск", "companyUrl": "https://yandex.ru/sprav/503/p/edit/",
                   "companyId": "503"}],
    }, ensure_ascii=False), encoding="utf-8")
    rv.save_queue(pid, [])

    yb.read_reviews = lambda page, url: {
        "ok": False, "url": url, "total": 0, "shown": 0, "items": [],
        "reason": "Страница отзывов не найдена (404)"}

    ok, msg = runner.start_actualize(pid, headless=True, delay_s=0, with_reviews=True)
    check("прогон стартовал", ok, msg)
    wait_done(pid); settle(pid)

    reports = runner.list_reports(pid, "actualize", limit=1)
    data = runner.read_report(pid, "actualize", reports[0]["name"]) if reports else {}
    row = (data.get("results") or [{}])[0]
    eq("город всё равно актуализирован", row.get("status"), "actualized")
    check("но про отзывы честно написано", "404" in (row.get("reviews") or ""), row.get("reviews"))
    eq("в очередь ничего не добавилось", rv.load_queue(pid), [])


def scenario_gis_actualize(pid: str) -> None:
    """
    Прогон по 2ГИС: свои города, свой лок, своя очередь ответов.

    Браузер подменён – проверяем склейку. Главное здесь: отзыв с чужой
    площадки (Flamp) не уходит в Gemini и не считается своим, а очередь
    2ГИС не смешивается с яндексовой.
    """
    print("\n▸ 2ГИС: прогон с отзывами")
    import gis_playwright as gis
    import reviews as rv

    folder = runner.p_tasks_actualize(pid, runner.GIS)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "01-Россия.json").write_text(json.dumps({
        "country": "Россия", "projectName": "TEST",
        "tasks": [{"cityName": "Краснодар",
                   "companyUrl": "https://account.2gis.com/orgs/70000001060441666/"}],
    }, ensure_ascii=False), encoding="utf-8")

    rv.save_queue(pid, [], platform=rv.GIS)
    rv.save_queue(pid, [{"reviewId": "ya-1", "status": rv.DRAFTED, "platform": rv.YANDEX}])

    gis.browser = lambda project_id, headless=True: FakeBrowser(project_id, headless)
    gis.actualize_city = lambda page, task, i=0, t=1: {
        "cityName": task["cityName"], "companyUrl": task.get("companyUrl"),
        "status": "actualized", "reason": "плашка ответила", "durationMs": 10}
    gis.read_reviews = lambda page, url, navigate=True, org_id=None: {
        "ok": True, "url": url, "shown": 3, "skipped": 0, "reason": "",
        "items": [
            {"id": "g1", "rating": 5, "text": "отличные трубы", "author": "Вера",
             "time_created": 1720137600000, "answered": False, "platform": "2GIS",
             "canAnswer": True, "foreignUrl": "", "branchUrl": "", "branchName": ""},
            {"id": "g2", "rating": 3, "text": "ждал дольше", "author": "Игорь",
             "time_created": 1720137600000, "answered": False, "platform": "2GIS",
             "canAnswer": True, "foreignUrl": "", "branchUrl": "", "branchName": ""},
            {"id": "g3", "rating": 5, "text": "порезали бесплатно", "author": "Марина",
             "time_created": 1720137600000, "answered": False, "platform": "Flamp",
             "canAnswer": False, "foreignUrl": "https://flamp.ru/firm/1/reviews",
             "branchUrl": "", "branchName": ""},
        ]}
    rv.project_prompt = lambda project_id: "Ответь на отзыв. [вставить отзыв] [имя]"
    import llm
    asked = []
    llm.generate = lambda prompt: asked.append(prompt) or "Уважаемая Вера! Благодарим за отзыв."

    ok, msg = runner.start_actualize(pid, headless=True, delay_s=0, with_reviews=True,
                                     platform=runner.GIS)
    check("прогон 2ГИС стартовал", ok, msg)
    check("в сообщении названа площадка", "2ГИС" in msg, msg)
    wait_done(pid, kind="actualize-gis")
    settle(pid, kind="actualize-gis")

    queue = rv.load_queue(pid, rv.GIS)
    by_id = {it["reviewId"]: it for it in queue}
    eq("в очередь 2ГИС попали все три отзыва", len(queue), 3)
    eq("пятёрка получила черновик", by_id["g1"]["status"], rv.DRAFTED)
    eq("тройка ушла человеку", by_id["g2"]["status"], rv.NEEDS_HUMAN)
    eq("отзыв с Flamp – тоже человеку", by_id["g3"]["status"], rv.NEEDS_HUMAN)
    check("и с пояснением, куда идти", "Flamp" in (by_id["g3"]["note"] or ""), by_id["g3"]["note"])
    eq("ссылка ведёт на Flamp", by_id["g3"]["reviewsUrl"], "https://flamp.ru/firm/1/reviews")
    check("на чужую площадку Gemini не звали", len(asked) == 1, f"звали {len(asked)} раз")
    check("у всех проставлена площадка", all(it.get("platform") == rv.GIS for it in queue))

    yandex_queue = rv.load_queue(pid)
    eq("яндексова очередь не тронута", [it["reviewId"] for it in yandex_queue], ["ya-1"])

    reports = runner.list_reports(pid, "actualize-gis", limit=1)
    data = runner.read_report(pid, "actualize-gis", reports[0]["name"]) if reports else {}
    eq("отчёт знает площадку", data.get("platform"), "gis")
    eq("город актуализирован", (data.get("totals") or {}).get("actualized"), 1)
    # Имя отчёта – по времени с точностью до секунды, так что у площадок оно
    # может совпасть. Важно другое: папки разные, и в яндексовой лежит
    # яндексов прогон, а не этот.
    check("отчёт лежит в папке 2ГИС",
          (runner.p_reports_actualize(pid, runner.GIS) / reports[0]["name"]).exists())
    same_name = runner.read_report(pid, "actualize", reports[0]["name"])
    check("в яндексову папку прогон 2ГИС не записался",
          same_name is None or same_name.get("platform") != "gis",
          str((same_name or {}).get("platform")))
    live = runner.read_live_log(pid, "actualize-gis")
    check("в логе прогона названа площадка", "2ГИС" in live, live[:200])


def scenario_collect(pid: str) -> None:
    """
    Сбор организаций для сверки: список читается, результат сохраняется,
    карточки открываются только по просьбе.
    """
    print("\n▸ Сбор организаций для сверки")

    listed = [
        {"id": "1", "type": "ordinal", "publishing": "publish", "name": "Авиапромсталь",
         "address": "Ростовская область, городской округ Шахты, Шахты",
         "site": "https://shahty.aviastal.ru/", "sites": ["https://shahty.aviastal.ru/"],
         "social": {}, "emails": [], "phones": ["+7 (863) 206-68-85"], "rubrics": [], "noAccess": False},
        {"id": "2", "type": "chain", "publishing": "publish", "name": "АвиаПромСталь",
         "address": "Земля", "site": "", "sites": [], "social": {}, "emails": [],
         "phones": [], "rubrics": [], "noAccess": False},
    ]
    opened: list[str] = []
    yb.collect_companies = lambda page, log_every=0, page_limit=50, stats=None: [dict(c) for c in listed]  # type: ignore
    yb.read_company_card = (lambda page, url: opened.append(url) or  # type: ignore
                            {"emails": ["shahty@aviastal.ru"]})

    ok, msg = runner.start_collect(pid, headless=True)
    check("сбор стартовал", ok, msg)
    state = wait_done(pid); settle(pid)
    eq("сбор завершился", state.get("status"), "done")
    eq("карточки без просьбы не открывались", opened, [])

    saved = runner.load_companies(pid)
    eq("организации сохранились", len(saved.get("companies") or []), 2)
    eq("сохранилось то же, что прочитали",
       [c["id"] for c in saved["companies"]], ["1", "2"])
    check("отметка времени есть", bool(saved.get("collectedAt")), str(saved)[:120])
    log = runner.read_live_log(pid, "collect")
    check("в логе видно, сколько собрано", "организаций: 2" in log, log[-200:])

    # С галочкой «открывать карточки» – обходим каждую и дополняем данными.
    ok, msg = runner.start_collect(pid, headless=True, with_cards=True)
    check("сбор с карточками стартовал", ok, msg)
    wait_done(pid); settle(pid)
    eq("открыли обе карточки", len(opened), 2)
    check("адрес карточки – это /p/edit/", all(u.endswith("/p/edit/") for u in opened), str(opened))
    saved = runner.load_companies(pid)
    eq("почта из карточки попала в данные", saved["companies"][0]["emails"], ["shahty@aviastal.ru"])

    # Сессия кончилась – прогон падает понятно, а не молча отдаёт пустой список.
    def boom(page, log_every=0, page_limit=50, stats=None):
        raise RuntimeError("Яндекс открыл страницу входа – сессия закончилась.")

    yb.collect_companies = boom  # type: ignore
    runner.start_collect(pid, headless=True)
    state = wait_done(pid); settle(pid)
    eq("прогон честно сломался", state.get("status"), "error")
    check("в ошибке сказано про вход", "сессия" in (state.get("error") or ""), str(state.get("error")))
    eq("прошлые данные не затёрты", len(runner.load_companies(pid).get("companies") or []), 2)


def scenario_parallel(pid: str) -> None:
    """
    Сверка и актуализация идут ОДНОВРЕМЕННО – ради этого всё и затевалось.

    Раньше лок был один на проект, и второй запуск отбивался «уже идёт другой
    прогон». Проверяем не только что оба стартовали, а что они реально жили
    в одну секунду, что логи не перемешались и что «Остановить» у одного не
    трогает второго.
    """
    print("\n▸ Сверка и актуализация параллельно")
    import threading

    runner.p_stop(pid, "actualize").unlink(missing_ok=True)
    runner.p_stop(pid, "collect").unlink(missing_ok=True)

    (runner.p_tasks_actualize(pid)).mkdir(parents=True, exist_ok=True)
    (runner.p_tasks_actualize(pid) / "01-Россия.json").write_text(json.dumps({
        "country": "Россия", "projectName": "TEST",
        "tasks": [{"cityName": f"Город-{i}", "companyId": str(600 + i),
                   "companyUrl": f"https://yandex.ru/sprav/{600 + i}/p/edit/"} for i in range(4)],
    }, ensure_ascii=False), encoding="utf-8")

    # Оба прогона придерживаем, пока не убедимся, что они живы разом.
    both_live = threading.Event()

    def slow_actualize(page, task, i=0, t=1):
        both_live.wait(timeout=10)
        return {"cityName": task["cityName"], "companyUrl": task["companyUrl"],
                "status": "actualized", "reason": "клик прошёл", "durationMs": 10}

    def slow_collect(page, log_every=0, page_limit=50, stats=None):
        both_live.wait(timeout=10)
        return [{"id": "9", "type": "ordinal", "publishing": "publish", "name": "Тест",
                 "address": "Земля", "site": "", "sites": [], "social": {}, "emails": [],
                 "phones": [], "rubrics": [], "noAccess": False}]

    yb.actualize_city = slow_actualize          # type: ignore[assignment]
    yb.collect_companies = slow_collect         # type: ignore[assignment]

    ok_a, msg_a = runner.start_actualize(pid, headless=True)
    check("актуализация стартовала", ok_a, msg_a)
    ok_c, msg_c = runner.start_collect(pid, headless=True)
    check("сверка стартовала ПРИ ИДУЩЕЙ актуализации", ok_c, msg_c)

    # Оба должны оказаться живыми одновременно – это и есть параллельность.
    live = []
    end = time.time() + 10
    while time.time() < end:
        live = runner.running_kinds(pid)
        if len(live) == 2:
            break
        time.sleep(0.05)
    eq("оба прогона идут в одну секунду", sorted(live), ["actualize", "collect"])

    # Пока оба живы – третий не пускаем и одинаковый второй тоже.
    check("вторая сверка не пускается", not runner.start_collect(pid, headless=True)[0])
    check("публикация третьей не пускается", bool(runner.busy_reason(pid, "publish")))

    both_live.set()
    for kind in ("actualize", "collect"):
        end = time.time() + 25
        while time.time() < end and runner.is_running(pid, kind):
            time.sleep(0.05)
    for key, t in list(runner._threads.items()):
        if key[0] == pid and t:
            t.join(timeout=10)

    eq("актуализация дошла до конца", runner.read_state(pid, "actualize").get("status"), "done")
    eq("сверка дошла до конца", runner.read_state(pid, "collect").get("status"), "done")

    log_a = runner.read_live_log(pid, "actualize")
    log_c = runner.read_live_log(pid, "collect")
    check("в логе актуализации её строки", "АКТУАЛИЗАЦИЯ" in log_a, log_a[-200:])
    check("в логе сверки её итог", "организаций: 1" in log_c, log_c[-200:])
    check("логи не перемешались: сверка не писала в чужой",
          "СБОР ОРГАНИЗАЦИЙ" not in log_a, log_a[-200:])
    check("логи не перемешались: актуализация не писала в чужой",
          "АКТУАЛИЗАЦИЯ" not in log_c, log_c[-200:])

    eq("отчёт актуализации записан", len(runner.list_reports(pid, "actualize", limit=99)) > 0, True)
    eq("после прогонов свободно", runner.busy_reason(pid, "publish"), "")



def scenario_dead_session(pid: str) -> None:
    """
    Сессия слетела – прогон обязан встать, а не обходить все города.

    Из боевого лога: 58 городов, у каждого «Кнопка «Данные актуальны» не
    найдена – актуализация не требуется» и «отзывы не прочитаны – Яндекс увёл
    на страницу входа». Кнопки не было потому, что открывалась страница входа:
    прогон писал в отчёт неправду и тратил на это полчаса.
    """
    print("\n▸ Слетевшая сессия останавливает прогон")
    runner.p_stop(pid, "actualize").unlink(missing_ok=True)

    (runner.p_tasks_actualize(pid)).mkdir(parents=True, exist_ok=True)
    for old in runner.p_tasks_actualize(pid).glob("*.json"):
        old.unlink()
    (runner.p_tasks_actualize(pid) / "01-Россия.json").write_text(json.dumps({
        "country": "Россия", "projectName": "TEST",
        "tasks": [{"cityName": c, "companyId": str(700 + i),
                   "companyUrl": f"https://yandex.ru/sprav/{700 + i}/p/edit/"}
                  for i, c in enumerate(["Москва", "Казань", "Пермь", "Омск"])],
    }, ensure_ascii=False), encoding="utf-8")

    видели = []

    def мёртвая_сессия(page, task, i=0, t=1):
        видели.append(task["cityName"])
        return {"cityName": task["cityName"], "companyUrl": task["companyUrl"],
                "status": "no-session",
                "reason": "Сессия Яндекса не активна: открылась страница входа",
                "durationMs": 10}

    yb.actualize_city = мёртвая_сессия        # type: ignore[assignment]

    ok, msg = runner.start_actualize(pid, headless=True)
    check("прогон стартовал", ok, msg)
    state = wait_done(pid); settle(pid)

    eq("прогон встал с ошибкой, а не «завершено»", state.get("status"), "error")
    eq("дальше первого города не пошли", видели, ["Москва"])
    check("в ошибке сказано, что делать человеку",
          "войдите в Яндекс заново" in (state.get("error") or "").lower()
          or "войдите в яндекс заново" in (state.get("error") or "").lower(),
          state.get("error", ""))
    log = runner.read_live_log(pid, "actualize")
    check("в логе видно остановку", "ОСТАНОВКА" in log, log[-200:])

    reports = runner.list_reports(pid, "actualize", limit=1)
    data = runner.read_report(pid, "actualize", reports[0]["name"]) if reports else {}
    eq("в отчёте один город, а не четыре", (data.get("totals") or {}).get("total"), 1)
    check("и он не помечен как «не требуется»",
          all(r.get("status") != "not-needed" for r in data.get("results") or []),
          str([r.get("status") for r in data.get("results") or []]))

    # Сам детектор: страница входа – это «no-session», а НЕ «кнопки нет».
    # Именно этой проверки в актуализации и не было.
    import yb_playwright as real_yb
    было_404, было_login = real_yb.is_404, real_yb.looks_like_login_page
    try:
        real_yb.is_404 = lambda page: False                      # type: ignore[assignment]
        real_yb.looks_like_login_page = lambda page: True        # type: ignore[assignment]
        res = real_yb.actualize_city(
            FakePage(), {"cityName": "Тест",
                         "companyUrl": "https://yandex.ru/sprav/1/p/edit/"})
        eq("страница входа – это no-session, а не «кнопки нет»", res.get("status"), "no-session")
        check("и причина человеческая", "страница входа" in (res.get("reason") or "").lower(),
              res.get("reason", ""))
    finally:
        real_yb.is_404, real_yb.looks_like_login_page = было_404, было_login

    # Тот же случай, но сессия отвалилась на шаге отзывов.
    yb.actualize_city = lambda page, task, i=0, t=1: {   # type: ignore[assignment]
        "cityName": task["cityName"], "companyUrl": task["companyUrl"],
        "status": "actualized", "reason": "клик прошёл", "durationMs": 10}
    yb.read_reviews = lambda page, url: {                # type: ignore[assignment]
        "ok": False, "url": url, "total": 0, "shown": 0, "items": [],
        "reason": "Яндекс увёл на страницу входа – сессия не действует",
        "noSession": True}
    runner.p_stop(pid, "actualize").unlink(missing_ok=True)
    (runner.p_tasks_actualize(pid) / "02-Россия.json").write_text(json.dumps({
        "country": "Россия", "projectName": "TEST",
        "tasks": [{"cityName": c, "companyId": str(800 + i),
                   "companyUrl": f"https://yandex.ru/sprav/{800 + i}/p/edit/"}
                  for i, c in enumerate(["Тула", "Курск", "Орёл"])],
    }, ensure_ascii=False), encoding="utf-8")
    ok, msg = runner.start_actualize(pid, headless=True, with_reviews=True)
    check("прогон с отзывами стартовал", ok, msg)
    state = wait_done(pid); settle(pid)
    eq("и он тоже встал", state.get("status"), "error")
    check("в ошибке сказано про страницу входа",
          "страница входа" in (state.get("error") or "").lower(), state.get("error", ""))



def scenario_collect_missing(pid: str) -> None:
    """
    Список организаций Яндекса отдаёт НЕ ВСЁ – недостающее добираем по КП.

    У заказчика в разделе «Организации» два Красноярска, а список приносит
    один: второй заведён онлайн-организацией и в выдачу не попадает. Она
    трижды перечитывала организации – карточки всё равно нет, и дубль не
    находился. В КП у города записана прямая ссылка на карточку: по её
    номеру Click открывает карточку сам.
    """
    print("\n▸ Карточка есть в КП, но список её не отдал")

    из_списка = [{"id": "1", "tycoonId": "", "permanentId": "1", "type": "ordinal",
                  "publishing": "publish", "name": "МетПромИнтекс",
                  "address": "Красноярский край, городской округ Красноярск, Красноярск, "
                             "Тамбовская улица, 5",
                  "site": "", "sites": [], "social": {}, "emails": [], "phones": [],
                  "rubrics": [], "noAccess": False}]
    открыты: list[str] = []

    def карточка_по_ссылке(page, url):
        открыты.append(url)
        if "218208924466" not in url:
            return {"error": "Страница карточки не найдена (404)", "url": url}
        return {"id": "218208924466", "type": "ordinal", "publishing": "publish",
                "name": "Метпромитнекс", "address": "Красноярск",
                "site": "https://krasnoyarsk.metprointex.ru/",
                "sites": ["https://krasnoyarsk.metprointex.ru/"], "social": {},
                "emails": [], "phones": [], "rubrics": [], "noAccess": False, "url": url}

    yb.collect_companies = lambda page, log_every=0, page_limit=50, stats=None: [dict(c) for c in из_списка]  # type: ignore
    yb.read_company_card = карточка_по_ссылке    # type: ignore[assignment]

    ok, msg = runner.start_collect(pid, headless=True, must_ids=["218208924466", "1"])
    check("сбор стартовал", ok, msg)
    state = wait_done(pid); settle(pid)
    eq("сбор завершился", state.get("status"), "done")

    saved = runner.load_companies(pid)
    ids = [c["id"] for c in saved.get("companies") or []]
    eq("в списке обе карточки", sorted(ids), sorted(["1", "218208924466"]))
    eq("по ссылке открывали только недостающую", len(открыты), 1)
    check("и именно её", "218208924466" in открыты[0], открыты[0])
    check("карточка помечена как пришедшая по ссылке",
          any(c.get("fromLink") for c in saved["companies"]))
    log = runner.read_live_log(pid, "collect")
    check("в логе видно, что добирали", "ДОБИРАЮ" in log, log[-300:])

    # И главное: теперь сверка видит дубль.
    import kp_audit
    кп = [["Страна", "Город", "Сайт", "Ссылка", "", "", "", ""],
          ["Россия", "Красноярск", "https://krasnoyarsk.metprointex.ru/",
           "https://yandex.ru/sprav/218208924466/", "", "", "", ""]]
    res = kp_audit.build(кп, saved["companies"])
    eq("у Красноярска две карточки", (res["items"][0]["cmp"] or {}).get("status"), "несколько")
    eq("и обе на листе «Дубли»", len(kp_audit.double_rows(res)) - 1, 2)


def scenario_gis_photos(pid: str) -> None:
    """
    Фото отгрузки уходят и в 2ГИС – прогоном публикации, вторым контекстом.

    Проверяем склейку целиком: город с карточкой 2ГИС фото получает, город без
    неё – спокойную пометку, повтор прогона второй раз не заливает, а сессия
    2ГИС закрывается вместе с браузером.
    """
    print("\n▸ Сценарий: фото отгрузки ещё и в 2ГИС")
    CALLS.clear(); SCRIPT.clear()
    runner.clear_ledger(pid)
    import gis_playwright as gis
    seen: list[tuple[str, int]] = []

    def fake_upload(page, gis_url, files, label=""):
        seen.append((gis_url, len(files)))
        return {"requested": len(files), "uploaded": len(files), "skipped": 0, "reason": ""}

    was = gis.upload_media
    gis.upload_media = fake_upload            # type: ignore[assignment]
    try:
        folder = runner.p_tasks(pid)
        folder.mkdir(parents=True, exist_ok=True)
        shot = Path(tempfile.mkdtemp()) / "отгрузка.jpg"
        shot.write_bytes(b"\xff\xd8\xff" + b"x" * 32)
        payload = {
            "credentials": {"email": "test@yandex.ru", "password": "x"},
            "projectName": "TEST", "country": "Россия",
            "tasks": [
                {"cityName": "Самара", "companyUrl": "https://yandex.ru/sprav/501/p/edit/posts/",
                 "companyId": "501", "postText": "Отгрузка в Самару",
                 "productPhotos": [str(shot)], "gisPhotos": True,
                 "gisUrl": "https://account.2gis.com/orgs/70000001081103893/"},
                {"cityName": "Тула", "companyUrl": "https://yandex.ru/sprav/502/p/edit/posts/",
                 "companyId": "502", "postText": "Отгрузка в Тулу",
                 "productPhotos": [str(shot)], "gisPhotos": True, "gisUrl": None},
            ],
        }
        (folder / f"01-Россия-{int(time.time() * 1000)}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        ok, msg = runner.start_publish(pid, delay_between_posts_s=0,
                                       expected_email="test@yandex.ru")
        check("прогон стартовал", ok, msg)
        wait_done(pid); settle(pid)
        report = latest_report(pid)
        rows = {r["cityName"]: r for r in report["results"]}

        eq("в 2ГИС ушёл один город", len(seen), 1)
        eq("и это Самара", seen[0][0], "https://account.2gis.com/orgs/70000001081103893/")
        eq("снимок доехал", (rows["Самара"].get("gisPhotos") or {}).get("uploaded"), 1)
        eq("у города без карточки – спокойная пометка",
           (rows["Тула"].get("gisPhotos") or {}).get("reason"), "Нет карточки в 2ГИС")
        check("и это не поломка публикации", rows["Тула"]["status"] in ("ok", "no-image"),
              rows["Тула"]["status"])
        live = runner.read_live_log(pid, "publish")
        check("в итогах есть строка про 2ГИС", "ФОТО В 2ГИС" in live, live[-300:])

        # Второй прогон тем же постом: пост-то дубль, а вот фото проверяем
        # отдельным реестром – он не должен заливать их второй раз.
        seen.clear()
        eq("реестр помнит снимки",
           bool(runner.recent_gis_photos(pid, payload["tasks"][0]["gisUrl"], [str(shot)])), True)
        (folder / f"02-Россия-{int(time.time() * 1000)}.json").write_text(
            json.dumps({**payload, "tasks": [payload["tasks"][0]]}, ensure_ascii=False),
            encoding="utf-8")
        ok, msg = runner.start_publish(pid, delay_between_posts_s=0,
                                       expected_email="test@yandex.ru", dedup_window_hours=0)
        check("второй прогон стартовал", ok, msg)
        wait_done(pid); settle(pid)
        eq("те же снимки второй раз не заливаются", len(seen), 0)
        again = {r["cityName"]: r for r in latest_report(pid)["results"]}
        check("и в отчёте сказано почему",
              "уже заливали" in ((again["Самара"].get("gisPhotos") or {}).get("reason") or ""),
              str(again["Самара"].get("gisPhotos")))
    finally:
        gis.upload_media = was                # type: ignore[assignment]


def scenario_gis_photos_after_restart(pid: str) -> None:
    """
    Сторож памяти перезапустил браузер – фото в 2ГИС идут дальше.

    Из боевого прогона: на пятом городе занято 1553 МБ, сторож перезапустил
    браузер – и все оставшиеся 55 городов получили «Кабинет 2ГИС не открылся:
    Event loop is closed! Is Playwright already stopped?». Прогон помнил
    вкладку 2ГИС сам, а она умерла вместе с браузером.
    """
    print("\n▸ Сценарий: перезапуск браузера посреди прогона с фото в 2ГИС")
    CALLS.clear(); SCRIPT.clear()
    runner.clear_ledger(pid)
    import gis_playwright as gis
    ушло: list[str] = []

    def fake_upload(page, gis_url, files, label=""):
        if getattr(page, "dead", False):
            return {"requested": len(files), "uploaded": 0, "skipped": 0,
                    "reason": "Кабинет 2ГИС не открылся: Event loop is closed!"}
        ушло.append(gis_url)
        return {"requested": len(files), "uploaded": len(files), "skipped": 0, "reason": ""}

    # Память «кончается» ровно один раз – когда первый город уже отработал,
    # а второй ещё не начался. Считаем по числу опубликованных городов, а не
    # по числу замеров: замер делается и до прогона тоже. Значение – между
    # мягким порогом (перезапуск браузера) и жёстким (остановка прогона):
    # нам нужен именно перезапуск.
    _, мягкий, жёсткий = runner.mem_gates()
    was_upload, was_mem = gis.upload_media, runner.memory_mb
    gis.upload_media = fake_upload             # type: ignore[assignment]
    runner.memory_mb = lambda: ((мягкий + жёсткий) // 2 if len(CALLS) == 1 else 10)  # type: ignore[assignment]
    try:
        folder = runner.p_tasks(pid)
        folder.mkdir(parents=True, exist_ok=True)
        shot = Path(tempfile.mkdtemp()) / "отгрузка.jpg"
        shot.write_bytes(b"\xff\xd8\xff" + b"y" * 32)
        города = [
            ("Самара", "501", "https://account.2gis.com/orgs/70000001081103893/"),
            ("Тула", "502", "https://account.2gis.com/orgs/70000001081103895/"),
            ("Омск", "503", "https://account.2gis.com/orgs/70000001081103897/"),
        ]
        (folder / f"03-Россия-{int(time.time() * 1000)}.json").write_text(json.dumps({
            "credentials": {"email": "test@yandex.ru", "password": "x"},
            "projectName": "TEST", "country": "Россия",
            "tasks": [{"cityName": c, "companyUrl": f"https://yandex.ru/sprav/{i}/p/edit/posts/",
                       "companyId": i, "postText": f"Отгрузка в {c}",
                       "productPhotos": [str(shot)], "gisPhotos": True, "gisUrl": u}
                      for c, i, u in города],
        }, ensure_ascii=False), encoding="utf-8")

        ok, msg = runner.start_publish(pid, delay_between_posts_s=0,
                                       expected_email="test@yandex.ru")
        check("прогон стартовал", ok, msg)
        wait_done(pid); settle(pid)

        live = runner.read_live_log(pid, "publish")
        check("сторож памяти сработал", "перезапускаю браузер" in live, live[-300:])
        eq("фото ушли во ВСЕ три города, а не только в первый", len(ушло), 3)
        rows = {r["cityName"]: r for r in latest_report(pid)["results"]}
        for город, *_ in города:
            eq(f"{город}: снимок доехал",
               (rows[город].get("gisPhotos") or {}).get("uploaded"), 1)
        check("мёртвой вкладки в отчёте нет",
              not any("Event loop" in ((r.get("gisPhotos") or {}).get("reason") or "")
                      for r in rows.values()))
    finally:
        gis.upload_media = was_upload          # type: ignore[assignment]
        runner.memory_mb = was_mem             # type: ignore[assignment]


def scenario_gis_only(pid: str) -> None:
    """
    ВРЕМЕННЫЙ режим: только фото в 2ГИС, без поста.

    Заказчик проверяет заливку отдельно от публикации. Главное здесь – что в
    Яндекс не уходит НИЧЕГО: ни поста, ни фото в «Товары», ни даже проверки
    аккаунта. И что город без карточки 2ГИС не выдаётся за успех.
    """
    print("\n▸ Сценарий: только фото в 2ГИС, без поста")
    CALLS.clear(); SCRIPT.clear()
    runner.clear_ledger(pid)
    import gis_playwright as gis
    ушло: list[tuple[str, int]] = []
    аккаунт: list[str] = []

    def fake_upload(page, gis_url, files, label=""):
        ушло.append((gis_url, len(files)))
        return {"requested": len(files), "uploaded": len(files), "skipped": 0, "reason": ""}

    was_upload, was_verify = gis.upload_media, yb.verify_account
    gis.upload_media = fake_upload             # type: ignore[assignment]
    yb.verify_account = lambda page, email: (   # type: ignore[assignment]
        аккаунт.append(email) or fake_verify_account(page, email))
    try:
        folder = runner.p_tasks_gis(pid)          # своя папка, не общая с постами
        folder.mkdir(parents=True, exist_ok=True)
        shot = Path(tempfile.mkdtemp()) / "витрина.jpg"
        shot.write_bytes(b"\xff\xd8\xff" + b"z" * 32)
        (folder / f"04-Россия-{int(time.time() * 1000)}.json").write_text(json.dumps({
            "credentials": {"email": "test@yandex.ru", "password": "x"},
            "projectName": "TEST", "country": "Россия",
            "tasks": [
                {"cityName": "Самара", "companyUrl": "https://yandex.ru/sprav/601/p/edit/posts/",
                 "companyId": "601", "postText": "", "productPhotos": [str(shot)],
                 "gisPhotos": True, "gisOnly": True,
                 "gisUrl": "https://account.2gis.com/orgs/70000001081103893/"},
                {"cityName": "Тула", "companyUrl": "https://yandex.ru/sprav/602/p/edit/posts/",
                 "companyId": "602", "postText": "", "productPhotos": [str(shot)],
                 "gisPhotos": True, "gisOnly": True, "gisUrl": None},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        eq("для обычной очереди этих задач не существует",
           runner.count_pending(pid, "tasks"), (0, 0))
        eq("а в своей очереди они есть", runner.count_pending(pid, "gis")[1], 2)
        ok, msg = runner.start_publish(pid, delay_between_posts_s=0,
                                       expected_email="test@yandex.ru", source="gis")
        check("прогон стартовал", ok, msg)
        wait_done(pid); settle(pid)

        eq("в Яндекс не публиковали НИЧЕГО", CALLS, [])
        eq("и аккаунт Яндекса не проверяли – он тут ни при чём", аккаунт, [])
        eq("в 2ГИС ушёл город с карточкой", len(ушло), 1)
        eq("и это Самара", ушло[0][0], "https://account.2gis.com/orgs/70000001081103893/")

        rows = {r["cityName"]: r for r in latest_report(pid)["results"]}
        eq("Самара: успех", rows["Самара"]["status"], "ok")
        check("и сказано, сколько снимков", "добавлено 1" in rows["Самара"]["reason"],
              rows["Самара"]["reason"])
        eq("Тула без карточки успехом не считается", rows["Тула"]["status"], "failed")
        eq("и причина названа", (rows["Тула"].get("gisPhotos") or {}).get("reason"),
           "Нет карточки в 2ГИС")
        live = runner.read_live_log(pid, "publish")
        check("в логе видно, что это не публикация",
              "ТОЛЬКО ФОТО В 2ГИС" in live and "ЗАПУСК ПУБЛИКАЦИИ" not in live, live[:400])
        eq("и в отчёте есть заголовок прогона",
           latest_report(pid).get("title"), "Только фото в 2ГИС, без постов")

        # Повтор тех же снимков в тот же город – это не поломка, а реестр.
        (folder / f"05-Россия-{int(time.time() * 1000)}.json").write_text(json.dumps({
            "credentials": {"email": "test@yandex.ru", "password": "x"},
            "projectName": "TEST", "country": "Россия",
            "tasks": [{"cityName": "Самара", "companyUrl": "https://yandex.ru/sprav/601/p/edit/posts/",
                       "companyId": "601", "postText": "", "productPhotos": [str(shot)],
                       "gisPhotos": True, "gisOnly": True,
                       "gisUrl": "https://account.2gis.com/orgs/70000001081103893/"}],
        }, ensure_ascii=False), encoding="utf-8")
        ушло.clear()
        ok, msg = runner.start_publish(pid, delay_between_posts_s=0,
                                       expected_email="test@yandex.ru", source="gis")
        check("второй прогон стартовал", ok, msg)
        wait_done(pid); settle(pid)
        eq("те же снимки второй раз не заливаются", len(ушло), 0)
        снова = {r["cityName"]: r for r in latest_report(pid)["results"]}
        eq("и это «пропущено», а не «ошибка»", снова["Самара"]["status"], "skipped-duplicate")
    finally:
        gis.upload_media = was_upload          # type: ignore[assignment]
        yb.verify_account = was_verify         # type: ignore[assignment]


def scenario_never_empty_post(pid: str) -> None:
    """
    Пустой пост не уходит в Яндекс НИКОГДА.

    Живой случай: экран уже новый и складывает задачи «только фото в 2ГИС», а
    прогон в памяти остался от прежней сборки и про такие задачи не знает. Он
    честно доложил «Текст введён полностью (0 символов)» и опубликовал ПУСТЫЕ
    посты в Москве, Петербурге и Новосибирске. Из карточки такое убирается
    только руками, поэтому проверка стоит и у прогона, и у самой кнопки.
    """
    print("\n▸ Сценарий: пустой пост не публикуется")
    CALLS.clear(); SCRIPT.clear()
    runner.clear_ledger(pid)
    folder = runner.p_tasks(pid)
    folder.mkdir(parents=True, exist_ok=True)
    # Задача СТАРОГО вида: текста нет, про gisOnly в ней не сказано ни слова.
    (folder / f"06-Россия-{int(time.time() * 1000)}.json").write_text(json.dumps({
        "credentials": {"email": "test@yandex.ru", "password": "x"},
        "projectName": "TEST", "country": "Россия",
        "tasks": [{"cityName": "Москва", "companyUrl": "https://yandex.ru/sprav/701/p/edit/posts/",
                   "companyId": "701", "postText": "   "},
                  {"cityName": "Казань", "companyUrl": "https://yandex.ru/sprav/702/p/edit/posts/",
                   "companyId": "702", "postText": "Настоящий текст поста"}],
    }, ensure_ascii=False), encoding="utf-8")

    ok, msg = runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
    check("прогон стартовал", ok, msg)
    wait_done(pid); settle(pid)

    eq("город с пустым текстом до публикации не дошёл", CALLS, ["Казань"])
    rows = {r["cityName"]: r for r in latest_report(pid)["results"]}
    check("и в отчёте сказано почему", "публиковать нечего" in rows["Москва"]["reason"],
          rows["Москва"]["reason"])
    eq("а обычный город опубликован как обычно", rows["Казань"]["status"], "ok")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="click-e2e-"))
    runner.USERS_DATA = tmp
    import reviews as _rv
    _rv.USERS_DATA = tmp
    install_fakes()
    pid = "E2E"
    try:
        scenario_happy_path(pid)
        scenario_progress(pid)
        scenario_dedup(pid)
        scenario_unknown_no_retry(pid)
        scenario_safe_retry(pid)
        scenario_attempt_budget(pid)
        scenario_double_start(pid)
        scenario_stop(pid)
        runner.clear_ledger(pid)
        scenario_wrong_account(pid)
        scenario_account_unknown(pid)
        scenario_no_session(pid)
        scenario_actualize_reviews(pid)
        scenario_actualize_reviews_off(pid)
        scenario_reviews_llm_down(pid)
        scenario_reviews_page_broken(pid)
        scenario_gis_actualize(pid)
        scenario_gis_photos(pid)
        scenario_gis_photos_after_restart(pid)
        scenario_gis_only(pid)
        scenario_never_empty_post(pid)
        scenario_collect(pid)
        scenario_parallel(pid)
        scenario_dead_session(pid)
        scenario_collect_missing(pid)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "═" * 60)
    if FAILED:
        print(f"  ПРОВАЛЕНО {len(FAILED)} из {PASSED + len(FAILED)}:")
        for n in FAILED:
            print(f"    • {n}")
        return 1
    print(f"  ВСЁ ХОРОШО – {PASSED} проверок пройдено")
    print("═" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
