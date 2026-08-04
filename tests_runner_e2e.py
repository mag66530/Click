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
    state = wait_done(pid)
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
    state = wait_done(pid)
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
    wait_done(pid)

    check("НИ ОДИН город не отправлен в Яндекс повторно", CALLS == [], str(CALLS))
    totals = (latest_report(pid).get("totals") or {})
    check("все 3 помечены как пропущенные дубли", totals.get("skipped") == 3, str(totals))

    print("\n▸ Другой текст в те же города публикуется нормально")
    CALLS.clear()
    write_tasks(pid, ["Москва", "Казань"], text="Совершенно другой текст поста")
    runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru")
    wait_done(pid)
    check("новый текст ушёл в оба города", sorted(CALLS) == ["Казань", "Москва"], str(CALLS))


def scenario_unknown_no_retry(pid: str) -> None:
    print("\n▸ Клик был, подтверждения нет → повтора НЕТ (главная причина дублей)")
    CALLS.clear(); SCRIPT.clear()
    SCRIPT["Сочи"] = [{"status": "unknown", "reason": "Публикация не подтверждена",
                       "steps": {"publish": "unknown"}}]
    write_tasks(pid, ["Сочи"], text="Текст для сценария unknown")
    runner.start_publish(pid, delay_between_posts_s=0, expected_email="test@yandex.ru", retry_unknown=False)
    wait_done(pid)

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
    wait_done(pid)

    check("была вторая попытка", CALLS.count("Тверь") == 2, str(CALLS))
    totals = (latest_report(pid).get("totals") or {})
    check("итог – успех", totals.get("ok") == 1, str(totals))


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
        wait_done(pid)
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
        state = wait_done(pid)
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
        state = wait_done(pid)
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
        state = wait_done(pid)
        err = (state.get("error") or "").lower()
        check("прогон остановлен", state.get("status") == "error", str(state.get("status")))
        check("сказано про сессию, а не про чужой аккаунт",
              "никто не залогинен" in err and "не тот аккаунт" not in err, err)
    finally:
        yb.verify_account = original  # type: ignore[assignment]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="click-e2e-"))
    runner.USERS_DATA = tmp
    install_fakes()
    pid = "E2E"
    try:
        scenario_happy_path(pid)
        scenario_progress(pid)
        scenario_dedup(pid)
        scenario_unknown_no_retry(pid)
        scenario_safe_retry(pid)
        scenario_double_start(pid)
        scenario_stop(pid)
        runner.clear_ledger(pid)
        scenario_wrong_account(pid)
        scenario_account_unknown(pid)
        scenario_no_session(pid)
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
