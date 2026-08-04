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

    def start(self): self.started += 1
    def new_page(self): return self.page
    def save_session(self): return None
    def restart(self): self.started += 1
    def close(self): return None


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


def wait_done(pid: str, timeout: float = 25) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        state = runner.read_state(pid)
        if state.get("status") in ("done", "error", "stopped"):
            return state
        time.sleep(0.1)
    return runner.read_state(pid)


def settle(pid: str, timeout: float = 5) -> None:
    """Дождаться, пока прогон реально отпустит лок. Между статусом «done»
    и снятием лок-файла есть окно – без этой паузы следующий старт получал
    «Уже идёт другой прогон»."""
    end = time.time() + timeout
    while time.time() < end and runner.is_running(pid):
        time.sleep(0.05)
    # Лок снят, но поток ещё доживает свой finally – без join следующий
    # старт получает «Предыдущий прогон ещё не завершился».
    t = runner._threads.get(pid)
    if t:
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
    check("лог прогона записан", "ИТОГИ" in runner.read_live_log(pid))
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
        runner.request_stop(pid)
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
    live = runner.read_live_log(pid)
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
