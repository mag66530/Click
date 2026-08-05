"""
runner.py – прогон публикации/актуализации. Замена main() из publish.js и actualize.js.

Почему это отдельный модуль, а не код внутри Streamlit-страницы:
Streamlit перерисовывает скрипт при КАЖДОМ действии пользователя и при каждом
переподключении вкладки. Если запускать публикацию прямо в теле `if st.button(...)`,
то повторный прогон того же запроса (двойной клик, реконнект, F5 во время работы)
стартует публикацию ВТОРОЙ РАЗ – ровно это и приводило к дублям постов.

Здесь три независимых уровня защиты от дубля:

  1. ЛОК-ФАЙЛ `.run.lock`  – второй прогон по проекту физически не стартует,
     пока идёт первый. Проверяется до создания потока.
  2. РЕЕСТР `.published.jsonl` – на каждый (companyId + хэш текста) пишется отметка.
     Повторная публикация того же текста в тот же город в течение DEDUP_WINDOW_HOURS
     не выполняется, город помечается 'skipped-duplicate'. Это спасает и при
     обрыве прогона на середине: перезапуск не публикует заново то, что уже ушло.
  3. ЗАПРЕТ РЕТРАЯ ПОСЛЕ КЛИКА – если клик «Создать» уже был сделан, повторная
     попытка запрещена, статус 'unknown' («проверьте вручную»).

Состояние прогона лежит в файлах (а не в st.session_state) – поэтому прогресс
виден в любой вкладке и переживает перерисовку страницы.
"""

from __future__ import annotations

import hashlib
import json
import re
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import paths
import yb_playwright as yb

# Метка сборки. Одна на все модули Click: облако умеет обновить главный
# скрипт, оставив этот модуль в памяти прежним, и тогда страница зовёт
# функцию, которой тут ещё нет. streamlit_app сверяет метку и при
# расхождении перезагружает модуль сам.
BUILD = "2026-08-05-reviews-send"

ROOT = Path(__file__).parent
USERS_DATA = paths.data_root()

DEDUP_WINDOW_HOURS = 12      # столько часов один и тот же текст не публикуется в тот же город повторно
LIVE_LOG_MAX_BYTES = 400_000
SLOW_WINDOW = 3              # окно детектора «Яндекс тормозит»
SLOW_THRESHOLD_MS = 60_000
COOLDOWN_PAUSE_S = 30
PROTOCOL_FAIL_LIMIT = 3      # столько подряд протокольных падений = браузер завис

_threads: dict[str, threading.Thread] = {}
_lock = threading.Lock()


# ─── пути ───────────────────────────────────────────────────────────
def base(project_id: str) -> Path:
    d = USERS_DATA / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def p_tasks(project_id: str) -> Path:
    return base(project_id) / "tasks"


def p_tasks_actualize(project_id: str) -> Path:
    return base(project_id) / "tasks-actualize"


def p_reports(project_id: str) -> Path:
    return base(project_id) / "reports"


def p_reports_actualize(project_id: str) -> Path:
    return base(project_id) / "reports-actualize"


def p_logs(project_id: str) -> Path:
    return base(project_id) / "logs"


def p_state(project_id: str) -> Path:
    return base(project_id) / ".run-state.json"


def p_lock(project_id: str) -> Path:
    return base(project_id) / ".run.lock"


def p_live_log(project_id: str) -> Path:
    return base(project_id) / ".run-log.txt"


def p_stop(project_id: str) -> Path:
    return base(project_id) / ".STOP_FLAG"


def p_ledger(project_id: str) -> Path:
    return base(project_id) / ".published.jsonl"


def p_temp(project_id: str) -> Path:
    return base(project_id) / "temp"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_atomic(path: Path, text: str) -> None:
    """Пишем через .tmp + rename: при падении на середине старый файл остаётся целым."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ─── состояние прогона ──────────────────────────────────────────────
def read_state(project_id: str) -> dict:
    fp = p_state(project_id)
    if not fp.exists():
        return {"status": "idle"}
    try:
        state = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"status": "idle"}

    # Прогон помечен running, но процесс, который его вёл, уже не жив
    # (перезапуск Streamlit) – показываем честно, а не «вечно идёт».
    if state.get("status") == "running" and state.get("ownerPid") != os.getpid():
        state = {**state, "status": "interrupted",
                 "error": "Прогон прерван перезапуском приложения. Данные о выполненных городах сохранены."}
    return state


def _write_state(project_id: str, state: dict) -> None:
    _write_atomic(p_state(project_id), json.dumps(state, ensure_ascii=False, indent=2))


def is_running(project_id: str) -> bool:
    return read_state(project_id).get("status") == "running"


def request_stop(project_id: str) -> None:
    p_stop(project_id).write_text(str(int(time.time())), encoding="utf-8")
    _append_log(project_id, "WARN", "⏹  Запрошена остановка – завершу после текущего города")


def run_log_path(project_id: str, kind: str, report_name: str) -> Path:
    """Файл лога рядом с отчётом: имя то же, расширение .log."""
    folder = p_reports(project_id) if kind == "publish" else p_reports_actualize(project_id)
    return folder / (Path(report_name).name.replace(".json", "") + ".log")


def _snapshot_log(project_id: str, report_path: Path) -> None:
    """Сохранить лог этого прогона рядом с его отчётом."""
    try:
        text = p_live_log(project_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        report_path.with_suffix(".log").write_text(text, encoding="utf-8")
    except OSError:
        pass


def read_run_log(project_id: str, kind: str, report_name: str) -> str:
    """Лог конкретного прогона. Пусто – значит прогон был до этой версии."""
    fp = run_log_path(project_id, kind, report_name)
    try:
        return fp.read_text(encoding="utf-8", errors="replace") if fp.exists() else ""
    except OSError:
        return ""


def read_live_log(project_id: str, tail: int = 40_000) -> str:
    fp = p_live_log(project_id)
    if not fp.exists():
        return ""
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-tail:]


# Какой прогон идёт сейчас – нужно только для имени файла лога.
_LOG_KIND: dict[str, str] = {}


def _append_log(project_id: str, level: str, msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {msg}\n"
    _touch_lock(project_id)
    try:
        live = p_live_log(project_id)
        live.parent.mkdir(parents=True, exist_ok=True)
        if live.exists() and live.stat().st_size > LIVE_LOG_MAX_BYTES:
            live.write_text(live.read_text(encoding="utf-8", errors="replace")[-LIVE_LOG_MAX_BYTES // 2:],
                            encoding="utf-8")
        with live.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    try:
        p_logs(project_id).mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        kind = _LOG_KIND.get(project_id, "publish")
        with (p_logs(project_id) / f"{kind}-{day}.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# ─── реестр опубликованного (защита от дублей между прогонами) ──────
def _text_key(company_id: str | None, company_url: str | None, text: str) -> str:
    cid = company_id or yb.extract_company_id(company_url) or (company_url or "")
    digest = hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]
    return f"{cid}:{digest}"


def _read_ledger(project_id: str) -> dict[str, dict]:
    fp = p_ledger(project_id)
    if not fp.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("key"):
                out[rec["key"]] = rec  # последняя запись по ключу побеждает
    except OSError:
        pass
    return out


def _ledger_add(project_id: str, key: str, task: dict, status: str, run_id: str) -> None:
    rec = {
        "key": key, "at": _now_iso(), "ts": time.time(), "status": status,
        "cityName": task.get("cityName"), "companyUrl": task.get("companyUrl"), "runId": run_id,
    }
    try:
        with p_ledger(project_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recent_publication(project_id: str, task: dict, window_hours: float = DEDUP_WINDOW_HOURS) -> dict | None:
    """Был ли этот же текст уже отправлен в этот же город за последние N часов."""
    key = _text_key(task.get("companyId"), task.get("companyUrl"), task.get("postText", ""))
    rec = _read_ledger(project_id).get(key)
    if not rec:
        return None
    if time.time() - float(rec.get("ts") or 0) > window_hours * 3600:
        return None
    return rec


def clear_ledger(project_id: str) -> None:
    p_ledger(project_id).unlink(missing_ok=True)


# ─── лок ────────────────────────────────────────────────────────────
# Лок «дышит»: пока прогон идёт, файл лока регулярно обновляется. Если пульса
# нет дольше этого времени – прогон умер вместе с приложением, лок можно
# забирать. Судить по PID нельзя: чужой процесс проверить переносимо не выйдет,
# а на Windows os.kill(pid, 0) вообще убивает процесс.
LOCK_STALE_S = 180
_LOCK_HELD: set[str] = set()
_LOCK_TOUCHED: dict[str, float] = {}


def _touch_lock(project_id: str) -> None:
    """Отметить, что прогон жив. Зовётся часто, поэтому не чаще раза в 5 секунд."""
    if project_id not in _LOCK_HELD:
        return
    now = time.time()
    if now - _LOCK_TOUCHED.get(project_id, 0) < 5:
        return
    _LOCK_TOUCHED[project_id] = now
    try:
        os.utime(p_lock(project_id), None)
    except OSError:
        pass


def lock_owner(project_id: str) -> dict | None:
    """Кто держит лок прямо сейчас. None – свободен или брошен."""
    fp = p_lock(project_id)
    if not fp.exists():
        return None
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    try:
        age = time.time() - fp.stat().st_mtime
    except OSError:
        return None
    if data.get("ownerPid") == os.getpid():
        thread = _threads.get(project_id)
        return {**data, "age": age, "mine": True} if (thread and thread.is_alive()) else None
    # Чужой процесс: живым считаем только пока лок «дышит».
    return {**data, "age": age, "mine": False} if age < LOCK_STALE_S else None


def _acquire_lock(project_id: str, run_id: str) -> bool:
    if lock_owner(project_id) is not None:
        return False
    fp = p_lock(project_id)
    fp.write_text(json.dumps({"runId": run_id, "ownerPid": os.getpid(), "at": _now_iso()},
                             ensure_ascii=False), encoding="utf-8")
    _LOCK_HELD.add(project_id)
    _LOCK_TOUCHED[project_id] = time.time()
    return True


def _release_lock(project_id: str) -> None:
    _LOCK_HELD.discard(project_id)
    _LOCK_TOUCHED.pop(project_id, None)
    p_lock(project_id).unlink(missing_ok=True)


# ─── чтение задач ───────────────────────────────────────────────────
def _load_task_files(folder: Path) -> list[tuple[Path, dict]]:
    if not folder.exists():
        return []
    out = []
    for fp in sorted(folder.glob("*.json")):
        if fp.name.startswith("."):
            continue
        try:
            out.append((fp, json.loads(fp.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def count_pending(project_id: str, folder: str = "tasks") -> tuple[int, int]:
    """(файлов, городов) в очереди."""
    files = _load_task_files(p_tasks(project_id) if folder == "tasks" else p_tasks_actualize(project_id))
    return len(files), sum(len(cfg.get("tasks") or []) for _, cfg in files)


def _archive(fp: Path) -> None:
    done = fp.parent / "done"
    done.mkdir(parents=True, exist_ok=True)
    try:
        fp.rename(done / fp.name)
    except OSError:
        pass


def _click_happened(result: dict) -> bool:
    """Был ли клик «Создать». Если да – повторять НЕЛЬЗЯ ни при каких условиях."""
    step = (result.get("steps") or {}).get("publish")
    return step in ("clicked", "click-error-no-api", "unknown", "api-confirmed", "dom-confirmed", "api-rejected")


# ════════════════════════════════════════════════════════════════════
#  ПУБЛИКАЦИЯ
# ════════════════════════════════════════════════════════════════════

def start_publish(
    project_id: str,
    headless: bool = True,
    delay_between_posts_s: float = 3.0,
    expected_email: str = "",
    strict_account_check: bool = True,
    retry_unknown: bool = False,
    dedup_window_hours: float = DEDUP_WINDOW_HOURS,
) -> tuple[bool, str]:
    """
    Запускает публикацию в фоновом потоке. Возвращает (запущено?, сообщение).
    Повторный вызов при активном прогоне ничего не делает – это и есть защита
    от «нажал кнопку дважды → опубликовалось дважды».
    """
    with _lock:
        if is_running(project_id):
            return False, "Публикация уже идёт – второй запуск заблокирован."
        thread = _threads.get(project_id)
        if thread and thread.is_alive():
            return False, "Предыдущий прогон ещё не завершился."

        files = _load_task_files(p_tasks(project_id))
        if not files:
            return False, "Очередь задач пуста."

        run_id = f"run-{int(time.time() * 1000)}"
        if not _acquire_lock(project_id, run_id):
            return False, ("Прогон уже идёт – запущен другим окном или другой копией Click. "
                           "Если та копия закрыта, подождите пару минут: лок освободится сам.")

        p_stop(project_id).unlink(missing_ok=True)
        p_live_log(project_id).write_text("", encoding="utf-8")
        _LOG_KIND[project_id] = "publish"

        total = sum(len(cfg.get("tasks") or []) for _, cfg in files)
        _write_state(project_id, {
            "runId": run_id, "action": "publish", "status": "running",
            "ownerPid": os.getpid(), "startedAt": _now_iso(), "finishedAt": None,
            "total": total, "current": 0, "currentCity": "", "reportName": None,
            "totals": {"total": 0, "ok": 0, "noImage": 0, "unknown": 0, "failed": 0,
                       "skipped": 0, "retried": 0, "cooldowns": 0},
            "error": None,
        })

        t = threading.Thread(
            target=_publish_worker,
            args=(project_id, run_id, files, headless, delay_between_posts_s,
                  expected_email, strict_account_check, retry_unknown, dedup_window_hours),
            daemon=True,
            name=f"click-publish-{project_id}",
        )
        _threads[project_id] = t
        t.start()
        return True, f"Публикация запущена: {total} городов."


def _publish_worker(
    project_id: str,
    run_id: str,
    files: list[tuple[Path, dict]],
    headless: bool,
    delay_s: float,
    expected_email: str,
    strict_account_check: bool,
    retry_unknown: bool,
    dedup_window_hours: float,
) -> None:
    yb.set_logger(lambda level, msg: _append_log(project_id, level, msg))

    started_at = _now_iso()
    start_ts = time.time()
    report_name = f"report-{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}.json"
    report_path = p_reports(project_id) / report_name

    results: list[dict] = []
    counters = {"ok": 0, "noImage": 0, "unknown": 0, "failed": 0, "skipped": 0, "retried": 0, "cooldowns": 0}
    total = sum(len(cfg.get("tasks") or []) for _, cfg in files)
    stopped = False
    processed_files: list[Path] = []
    account = (files[0][1].get("credentials") or {}).get("email", "") if files else ""

    def should_stop() -> bool:
        return p_stop(project_id).exists()

    def save_report(state: str) -> None:
        report = {
            "startedAt": started_at,
            "finishedAt": _now_iso(),
            "durationSec": int(time.time() - start_ts),
            "account": expected_email or account,
            "stoppedByUser": stopped,
            "state": state,
            "runId": run_id,
            "totals": {"total": len(results), **counters},
            "results": [{k: v for k, v in r.items() if not k.startswith("_")} for r in results],
        }
        _write_atomic(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        # Лог прогона кладём рядом с отчётом, когда прогон закончился: по
        # дневному файлу не понять, где чей прогон, а «Скачать лог» должен
        # отдавать лог ИМЕННО этого отчёта.
        if state != "in-progress":
            _snapshot_log(project_id, report_path)

    def push_state(status: str, current: int, city: str, error: str | None = None) -> None:
        _write_state(project_id, {
            "runId": run_id, "action": "publish", "status": status,
            "ownerPid": os.getpid(), "startedAt": started_at,
            "finishedAt": None if status == "running" else _now_iso(),
            "total": total, "current": current, "currentCity": city,
            "reportName": report_name,
            "totals": {"total": len(results), **counters},
            "error": error,
        })

    def tally(status: str) -> None:
        key = {"ok": "ok", "no-image": "noImage", "unknown": "unknown",
               "skipped-duplicate": "skipped"}.get(status, "failed")
        counters[key] += 1

    browser = yb.YbBrowser(project_id, headless=headless)
    processed = 0
    try:
        _append_log(project_id, "INFO", "═" * 46)
        _append_log(project_id, "INFO", f"ЗАПУСК ПУБЛИКАЦИИ · {total} городов · аккаунт {expected_email or account or '–'}")
        _append_log(project_id, "INFO", "═" * 46)
        save_report("in-progress")

        browser.start()
        _append_log(project_id, "INFO", "🌐 Браузер запущен")

        # ── Защита от публикации не с того аккаунта ──
        # Останавливаемся ТОЛЬКО если нашли чужой аккаунт или сессии нет вовсе.
        # «Определить не удалось» – это неудача проверки, а не повод не работать:
        # раньше из-за этого прогон падал с «залогинен НЕ ТОТ аккаунт, найдено:
        # не определено» при полностью правильном аккаунте.
        if expected_email:
            check = yb.verify_account(browser.page, expected_email)
            state = check.get("state") or (
                "ok" if check.get("matched") else ("other" if check.get("emails") else "unknown"))
            if state == "other":
                msg = (f"В Яндексе залогинен НЕ ТОТ аккаунт. Проект ожидает {expected_email}, "
                       f"на странице профиля найдено: {', '.join(check.get('emails') or [])}.")
            elif state == "anonymous":
                msg = ("В Яндексе никто не залогинен – сессия истекла или ещё не создавалась. "
                       "Войдите в разделе «Настройки».")
            else:
                msg = ""

            if msg:
                _append_log(project_id, "ERROR", "❌ ОСТАНОВКА: " + msg)
                if strict_account_check:
                    save_report("finished")
                    push_state("error", 0, "", msg)
                    return
                _append_log(project_id, "WARN", "⚠️ Строгая проверка выключена – продолжаю на свой страх и риск")
            elif state == "ok":
                _append_log(project_id, "INFO", f"✓ Аккаунт совпадает ({expected_email})")
            else:
                _append_log(project_id, "WARN",
                            "⚠️ Не удалось определить аккаунт на странице профиля Яндекса – "
                            "продолжаю, публикация пойдёт под текущей сессией")

        recent_durations: list[int] = []
        protocol_fails = 0

        for fp, cfg in files:
            if stopped:
                break
            country = cfg.get("country") or cfg.get("projectName") or "–"
            city_tasks = cfg.get("tasks") or []
            _append_log(project_id, "INFO", f"📦 ПАКЕТ: {country} ({len(city_tasks)} городов)")

            for i, task in enumerate(city_tasks):
                if should_stop():
                    stopped = True
                    _append_log(project_id, "INFO", "⏹  Получен сигнал остановки – завершаю")
                    break

                processed += 1
                city = task.get("cityName", "?")
                # В полосу прогресса идёт число ЗАКОНЧЕННЫХ городов, а не номер
                # текущего: иначе на первом же городе пишется «1 из 1», человек
                # видит «готово» и ждёт отчёт, которого ещё нет.
                push_state("running", len(results), city)

                # ── Уровень 2 защиты от дубля: реестр ──
                dup = recent_publication(project_id, task, dedup_window_hours)
                if dup:
                    when = str(dup.get("at", ""))[:19].replace("T", " ")
                    res = {
                        "status": "skipped-duplicate",
                        "reason": f"Этот же текст уже отправлен в «{city}» {when} UTC – повтор пропущен.",
                        "cityName": city, "companyUrl": task.get("companyUrl"),
                        "steps": {}, "durationMs": 0, "country": country, "package": country,
                    }
                    results.append(res)
                    tally(res["status"])
                    _append_log(project_id, "WARN", f"  ⏭ ИТОГ [{processed}/{total}] {city}: {res['reason']}")
                    save_report("in-progress")
                    continue

                res = _publish_one_city(
                    browser, project_id, task, i, len(city_tasks), run_id,
                    should_stop=should_stop,
                )
                res["country"] = country
                res["package"] = country
                res["_task"] = task
                results.append(res)
                tally(res["status"])

                # Фото в раздел «Товары» – только после успешной публикации, изолированно
                photos = task.get("productPhotos") or []
                if photos and res["status"] in ("ok", "no-image"):
                    try:
                        pr = yb.upload_product_photos(browser.page, task, list(photos), p_temp(project_id))
                        res["productPhotos"] = {"requested": len(photos), **pr}
                    except Exception as e:  # noqa: BLE001
                        _append_log(project_id, "WARN", f"  ⚠️ Фото в «Товары» упали: {e}")
                        res["productPhotos"] = {"requested": len(photos), "uploaded": 0,
                                                "failed": len(photos), "errors": [str(e)]}

                icon = {"ok": "✅", "no-image": "🟡", "unknown": "⚠️"}.get(res["status"], "❌")
                dur = f" ({res.get('durationMs', 0) / 1000:.1f} сек)"
                _append_log(project_id, "INFO",
                            f"  {icon} ИТОГ [{processed}/{total}] {city}: {res.get('reason', '')}{dur}")
                save_report("in-progress")
                push_state("running", len(results), city)

                # Скриншот в момент сбоя – в оригинале это был главный способ
                # понять, ПОЧЕМУ город не опубликовался (25 точек takeScreenshot).
                if res["status"] in ("failed", "unknown", "no-session"):
                    shot = _save_failure_screenshot(project_id, browser, city)
                    if shot:
                        res["screenshot"] = shot.name
                        _append_log(project_id, "INFO", f"  📸 Скриншот сбоя: {shot.name}")

                # Страховка длинного прогона: Яндекс продлевает куки по ходу
                # работы, сохраняем их периодически, а не только в самом конце.
                if processed % 10 == 0:
                    try:
                        browser.save_session()
                    except Exception:  # noqa: BLE001
                        pass

                # Сессия слетела – дальше идти незачем: остальные города упрутся
                # в ту же страницу входа. Останавливаемся сразу и говорим почему.
                if res["status"] == "no-session":
                    _append_log(project_id, "ERROR",
                                "❌ ОСТАНОВКА: сессия Яндекса не активна – вместо кабинета "
                                "открывается страница входа")
                    save_report("finished")
                    push_state("error", len(results), city,
                               "Сессия Яндекса не активна: вместо кабинета открывается страница "
                               "входа. Зайдите в «Настройки» и войдите в Яндекс заново.")
                    return

                # Детектор зависшего браузера
                if res["status"] == "failed" and _is_protocol_fail(res.get("reason", "")):
                    protocol_fails += 1
                    if protocol_fails >= PROTOCOL_FAIL_LIMIT:
                        _append_log(project_id, "WARN",
                                    f"💀 {PROTOCOL_FAIL_LIMIT} города подряд упали по таймауту протокола – перезапускаю браузер")
                        try:
                            browser.restart()
                            protocol_fails = 0
                            _append_log(project_id, "INFO", "✅ Браузер перезапущен, продолжаю")
                        except Exception as e:  # noqa: BLE001
                            _append_log(project_id, "ERROR", f"❌ Не удалось перезапустить браузер: {e}")
                            stopped = True
                            break
                else:
                    protocol_fails = 0

                if i < len(city_tasks) - 1 and delay_s > 0:
                    _append_log(project_id, "INFO", f"⏱️  Пауза {delay_s:.0f} сек...")
                    _sleep_interruptible(delay_s, should_stop)

                # Детектор «Яндекс тормозит» – даём остыть
                if res.get("durationMs"):
                    recent_durations.append(res["durationMs"])
                    if len(recent_durations) > SLOW_WINDOW:
                        recent_durations.pop(0)
                if len(recent_durations) == SLOW_WINDOW and i < len(city_tasks) - 1:
                    median = sorted(recent_durations)[SLOW_WINDOW // 2]
                    if median > SLOW_THRESHOLD_MS:
                        counters["cooldowns"] += 1
                        _append_log(project_id, "WARN",
                                    f"🐢 Яндекс тормозит (медиана {median / 1000:.0f} сек). Пауза {COOLDOWN_PAUSE_S} сек...")
                        _sleep_interruptible(COOLDOWN_PAUSE_S, should_stop)
                        recent_durations.clear()

            processed_files.append(fp)
            if stopped:
                break
            if fp is not files[-1][0]:
                _append_log(project_id, "INFO", "⏱️  Пауза 3 сек между странами...")
                _sleep_interruptible(3, should_stop)

        # ── ВТОРОЙ ПРОХОД: только те, где клика «Создать» ТОЧНО не было ──
        if not stopped:
            _second_pass(browser, project_id, results, counters, run_id, retry_unknown,
                         should_stop, save_report, lambda: push_state("running", len(results), "второй проход"))

        browser.save_session()
        save_report("finished")
        push_state("stopped" if stopped else "done", len(results), "")
        _append_log(project_id, "INFO", "═" * 46)
        _append_log(project_id, "INFO",
                    f"ИТОГИ · ✅ {counters['ok']} · 🟡 {counters['noImage']} · ⚠️ {counters['unknown']} "
                    f"· ❌ {counters['failed']} · ⏭ {counters['skipped']}")
        _append_log(project_id, "INFO", "═" * 46)

    except Exception as e:  # noqa: BLE001
        _append_log(project_id, "ERROR", f"💥 Критическая ошибка прогона: {e}")
        _append_log(project_id, "ERROR", traceback.format_exc(limit=6))
        save_report("crashed")
        push_state("error", len(results), "", str(e))
    finally:
        try:
            browser.close()
        except Exception:
            pass
        # Полностью обработанные файлы уводим в done/ (частично обработанные оставляем –
        # при повторном запуске реестр не даст переопубликовать уже сделанные города).
        if not stopped:
            for fp in processed_files:
                _archive(fp)
        _cleanup_temp(project_id)
        p_stop(project_id).unlink(missing_ok=True)
        _release_lock(project_id)
        yb.set_logger(None)


def p_screenshots(project_id: str) -> Path:
    d = USERS_DATA / project_id / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_failure_screenshot(project_id: str, browser, city: str) -> Path | None:
    try:
        safe = re.sub(r"[^\w\-]+", "-", city or "город")[:30]
        fp = p_screenshots(project_id) / f"{datetime.now().strftime('%H-%M-%S')}-{safe}.png"
        browser.page.screenshot(path=str(fp))
        return fp
    except Exception:  # noqa: BLE001
        return None


def _should_ledger(res: dict) -> bool:
    """Что писать в реестр публикаций: ok и no-image – пост есть; unknown –
    возможно есть, повтор опасен. failed и api-rejected – поста нет."""
    return res.get("status") in ("ok", "no-image", "unknown")


def _publish_one_city(
    browser: yb.YbBrowser,
    project_id: str,
    task: dict,
    idx: int,
    total_in_pkg: int,
    run_id: str,
    should_stop: Callable[[], bool],
    allow_retry: bool = True,
) -> dict:
    """
    Одна попытка + БЕЗОПАСНЫЙ ретрай.

    Ретрай разрешён ТОЛЬКО если первая попытка упала ДО клика «Создать»
    (кнопка «Добавить пост» не найдена, поле текста не найдено, контекст умер).
    Если клик был – статус 'unknown', повтор запрещён: дубль хуже, чем ручная проверка.

    allow_retry=False зовёт второй проход: там это УЖЕ повтор, и внутренний
    ретрай сделал бы четвёртую попытку по одному городу вместо трёх.
    """
    retryable = ("Кнопка «Добавить пост» не найдена", "Execution context was destroyed",
                 "Target closed", "Не найдено поле для текста", "Target page, context or browser has been closed")

    try:
        res = yb.publish_to_city(browser.page, task, idx, total_in_pkg,
                                 temp_dir=p_temp(project_id), should_stop=should_stop)
    except Exception as e:  # noqa: BLE001
        res = {"status": "failed", "reason": f"Критическая ошибка: {e}",
               "cityName": task.get("cityName"), "companyUrl": task.get("companyUrl"),
               "steps": {}, "durationMs": 0}

    # В реестр – только исходы, где пост МОГ появиться. api-rejected сюда не
    # входит: Яндекс отказал, поста нет, а запись блокировала бы город на всё
    # окно дедупликации – после починки причины город «пропускался» бы зря.
    if _should_ledger(res):
        _ledger_add(project_id, _text_key(task.get("companyId"), task.get("companyUrl"), task.get("postText", "")),
                    task, res["status"], run_id)

    if res["status"] in ("ok", "no-image"):
        return res
    if res["status"] == "no-session":
        return res                       # повтор бессмыслен: сессии нет, прогон встанет
    if res["status"] == "unknown":
        yb.warn(f"  ⚠️ [{task.get('cityName')}] результат неопределён – ретрай ЗАПРЕЩЁН во избежание дубля")
        return res
    if _click_happened(res):
        yb.warn(f"  ⚠️ [{task.get('cityName')}] клик «Создать» уже был – ретрай ЗАПРЕЩЁН")
        res["status"] = "unknown"
        res["reason"] = ("Клик «Создать» сделан, но публикация не подтверждена. "
                         "Проверьте Яндекс.Бизнес вручную – возможно пост опубликован.")
        return res
    if not any(x.lower() in (res.get("reason") or "").lower() for x in retryable):
        return res
    if not allow_retry:
        return res                       # это уже второй проход – третья попытка последняя
    if should_stop():
        return res

    yb.info(f"  🔄 [{task.get('cityName')}] первая попытка упала ДО клика – безопасный ретрай")
    if any(x in (res.get("reason") or "") for x in ("Execution context", "Target closed", "has been closed")):
        try:
            browser.new_page()
        except Exception as e:  # noqa: BLE001
            yb.warn(f"  ⚠️ Не удалось пересоздать вкладку: {e}")
    time.sleep(0.5)

    try:
        retry = yb.publish_to_city(browser.page, task, idx, total_in_pkg,
                                   temp_dir=p_temp(project_id), should_stop=should_stop)
    except Exception as e:  # noqa: BLE001
        retry = {"status": "failed", "reason": f"Критическая ошибка при ретрае: {e}",
                 "cityName": task.get("cityName"), "companyUrl": task.get("companyUrl"),
                 "steps": {}, "durationMs": 0}

    if _click_happened(retry) or retry["status"] in ("ok", "no-image", "unknown"):
        _ledger_add(project_id, _text_key(task.get("companyId"), task.get("companyUrl"), task.get("postText", "")),
                    task, retry["status"], run_id)
    if retry["status"] in ("ok", "no-image"):
        retry["retried"] = True
        retry["firstAttemptError"] = res.get("reason")
        yb.info(f"  ✅ [{task.get('cityName')}] удалось со 2-й попытки")
    return retry


def _second_pass(
    browser: yb.YbBrowser,
    project_id: str,
    results: list[dict],
    counters: dict,
    run_id: str,
    retry_unknown: bool,
    should_stop: Callable[[], bool],
    save_report: Callable[[str], None],
    push_state: Callable[[], None],
) -> None:
    """
    Второй проход по проблемным городам.

    Отличие от publish.js: города со статусом 'unknown' по умолчанию НЕ публикуются
    заново – только проверяются на наличие свежего поста. Если свежий пост найден,
    статус повышается до 'ok'. Если нет – так и остаётся 'unknown' («проверьте вручную»),
    потому что именно автоповтор после неподтверждённого клика и давал дубли.
    Включить старое поведение можно галочкой «Повторять неопределённые».
    """
    candidates = [r for r in results if r["status"] in ("failed", "unknown")]
    if not candidates:
        return

    yb.info("═" * 46)
    yb.info(f"🔁 ВТОРОЙ ПРОХОД: {len(candidates)} городов")
    yb.info("═" * 46)

    for n, failed in enumerate(candidates):
        if should_stop():
            yb.info("⏹  Остановка – прерываю второй проход")
            return
        task = failed.get("_task")
        if not task:
            continue
        city = task.get("cityName", "?")
        yb.info(f"🔁 [{n + 1}/{len(candidates)}] {city} – проверяю ленту...")
        push_state()

        fresh = None
        try:
            posts_url = yb.build_posts_url(task.get("companyUrl"), task.get("companyId")) or task.get("companyUrl")
            browser.page.goto(posts_url, wait_until="domcontentloaded", timeout=25_000)
            browser.page.wait_for_timeout(3500)
            for _ in range(3):
                found = yb.check_post_already_exists(browser.page, task.get("postText", ""))
                if found.get("found") and found.get("fresh"):
                    fresh = found
                    break
                browser.page.wait_for_timeout(2500)
        except Exception as e:  # noqa: BLE001
            yb.warn(f"  ⚠️ Не удалось проверить наличие поста: {e}")

        if fresh:
            marker = {"moderation": "плашка «Публикация на модерации»",
                      "fresh-time": "метка «только что / N минут назад»"}.get(fresh.get("reason", ""), "свежий пост")
            yb.info(f"  ✅ {city}: {marker} в ленте – статус обновлён на «опубликован»")
            _downgrade(counters, failed["status"])
            failed.update({"status": "ok", "reason": f"Свежий пост найден в ленте ({marker})", "retried": True})
            counters["ok"] += 1
            counters["retried"] += 1
            save_report("in-progress")
            continue

        if failed["status"] == "unknown" and not retry_unknown:
            yb.warn(f"  ⚠️ {city}: свежий пост не найден, но клик «Создать» уже был – "
                    f"повтор НЕ делаю (риск дубля). Проверьте вручную.")
            continue

        if _click_happened(failed):
            yb.warn(f"  ⚠️ {city}: клик уже был – повтор запрещён")
            continue

        dup = recent_publication(project_id, task)
        if dup:
            yb.warn(f"  ⏭ {city}: в реестре уже есть отправка этого текста – повтор пропущен")
            continue

        try:
            browser.new_page()
        except Exception:
            pass
        # Внутренний ретрай тут запрещён: первый проход уже сделал две попытки,
        # эта – третья и последняя. Иначе на город выходило четыре.
        retry = _publish_one_city(browser, project_id, task, n, len(candidates), run_id,
                                  should_stop, allow_retry=False)

        _downgrade(counters, failed["status"])
        retry["country"] = failed.get("country")
        retry["package"] = failed.get("package")
        retry["_task"] = task
        if retry["status"] in ("ok", "no-image"):
            retry["retried"] = True
            retry["firstAttemptError"] = failed.get("reason")
            counters["retried"] += 1
            yb.info(f"  ✅ {city}: опубликован со 2-го прохода")
        failed.clear()
        failed.update(retry)
        counters[{"ok": "ok", "no-image": "noImage", "unknown": "unknown"}.get(retry["status"], "failed")] += 1
        save_report("in-progress")

    yb.info("🔁 Второй проход завершён")


def _downgrade(counters: dict, status: str) -> None:
    key = {"ok": "ok", "no-image": "noImage", "unknown": "unknown"}.get(status, "failed")
    counters[key] = max(0, counters[key] - 1)


def _is_protocol_fail(reason: str) -> bool:
    return bool(reason) and any(
        s in reason for s in ("Timeout", "timed out", "Target closed", "Session closed", "has been closed")
    )


def _sleep_interruptible(seconds: float, should_stop: Callable[[], bool]) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if should_stop():
            return
        time.sleep(min(0.5, max(0.0, end - time.time())))


def _cleanup_temp(project_id: str) -> None:
    temp = p_temp(project_id)
    if not temp.exists():
        return
    for f in temp.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass


# ════════════════════════════════════════════════════════════════════
#  АКТУАЛИЗАЦИЯ
# ════════════════════════════════════════════════════════════════════

def start_actualize(project_id: str, headless: bool = True, delay_s: float = 2.5,
                    with_reviews: bool = False) -> tuple[bool, str]:
    """with_reviews – заодно собрать неотвеченные отзывы и черновики ответов."""
    with _lock:
        if is_running(project_id):
            return False, "Уже идёт другой прогон."
        thread = _threads.get(project_id)
        if thread and thread.is_alive():
            return False, "Предыдущий прогон ещё не завершился."

        files = _load_task_files(p_tasks_actualize(project_id))
        if not files:
            return False, "Список городов для актуализации пуст."

        run_id = f"act-{int(time.time() * 1000)}"
        if not _acquire_lock(project_id, run_id):
            return False, ("Прогон уже идёт – запущен другим окном или другой копией Click. "
                           "Если та копия закрыта, подождите пару минут: лок освободится сам.")

        p_stop(project_id).unlink(missing_ok=True)
        p_live_log(project_id).write_text("", encoding="utf-8")
        _LOG_KIND[project_id] = "actualize"

        total = sum(len(cfg.get("tasks") or []) for _, cfg in files)
        _write_state(project_id, {
            "runId": run_id, "action": "actualize", "status": "running",
            "ownerPid": os.getpid(), "startedAt": _now_iso(), "finishedAt": None,
            "total": total, "current": 0, "currentCity": "", "reportName": None,
            "totals": {"total": 0, "actualized": 0, "notNeeded": 0, "failed": 0}, "error": None,
        })

        t = threading.Thread(target=_actualize_worker,
                             args=(project_id, run_id, files, headless, delay_s, with_reviews),
                             daemon=True, name=f"click-actualize-{project_id}")
        _threads[project_id] = t
        t.start()
        tail = " + отзывы" if with_reviews else ""
        return True, f"Актуализация запущена: {total} городов{tail}."


def _reviews_for_city(project_id: str, page, task: dict, prompt: str) -> dict:
    """
    Шаг по отзывам для одного города: прочитать раздел отзывов, отобрать
    неотвеченные и написать черновики. Ничего не публикует.

    Возвращает {'items': [...в очередь...], 'summary': 'что вышло словами'}.
    Исключения наружу не пускает: сломавшийся шаг по отзывам не должен
    ронять уже работающую актуализацию этого же города.
    """
    import reviews as rv

    city = task.get("cityName") or "?"
    company_url = task.get("companyUrl")
    url = yb.build_reviews_url(company_url, task.get("companyId"))
    out = {"items": [], "summary": "", "found": 0, "drafted": 0, "needsHuman": 0, "noDraft": 0}

    data = yb.read_reviews(page, url)
    if not data["ok"]:
        out["summary"] = data["reason"]
        _append_log(project_id, "WARN", f"  💬 {city}: отзывы не прочитаны – {data['reason']}")
        return out

    page_url = data["url"]
    box = rv.triage(data["items"])
    out["found"] = len(box["unanswered"])

    if not box["unanswered"]:
        out["summary"] = f"отзывов {data['total']}, все с ответом"
        _append_log(project_id, "INFO", f"  💬 {city}: {out['summary']}")
        return out

    def add(item, status, draft="", note=""):
        out["items"].append(rv.as_queue_item(
            item, project_id=project_id, city=city, company_url=company_url,
            reviews_url=page_url, status=status, draft=draft, note=note))

    for item in box["no_text"]:
        pass  # одни звёзды без текста – в очередь не берём (решение заказчика)

    for item in box["needs_human"]:
        add(item, rv.NEEDS_HUMAN, note=f"оценка {item.get('rating')} – отвечает человек")
        out["needsHuman"] += 1

    for item in box["over_limit"]:
        add(item, rv.NO_DRAFT, note=f"сверх потолка {rv.MAX_DRAFTS_PER_CITY} черновиков на город")
        out["noDraft"] += 1

    for item in box["to_draft"]:
        if not prompt.strip():
            add(item, rv.NO_DRAFT, note="промпт проекта не задан – впишите его в «Настройках»")
            out["noDraft"] += 1
            continue
        try:
            import llm
            draft = llm.generate(rv.build_prompt(prompt, item))
            add(item, rv.DRAFTED, draft=draft)
            out["drafted"] += 1
        except Exception as e:  # noqa: BLE001 – причина уже человеческая
            add(item, rv.NO_DRAFT, note=str(e))
            out["noDraft"] += 1
            _append_log(project_id, "WARN", f"  💬 {city}: черновик не вышел – {e}")

    bits = [f"без ответа {out['found']}"]
    if out["drafted"]:
        bits.append(f"черновиков {out['drafted']}")
    if out["needsHuman"]:
        bits.append(f"человеку {out['needsHuman']}")
    if out["noDraft"]:
        bits.append(f"без черновика {out['noDraft']}")
    if box["no_text"]:
        bits.append(f"без текста пропущено {len(box['no_text'])}")
    if data["total"] > data["shown"]:
        bits.append(f"за первой страницей осталось {data['total'] - data['shown']}")
    out["summary"] = ", ".join(bits)
    _append_log(project_id, "INFO", f"  💬 {city}: {out['summary']}")
    return out


def _actualize_worker(project_id: str, run_id: str, files, headless: bool, delay_s: float,
                      with_reviews: bool = False) -> None:
    yb.set_logger(lambda level, msg: _append_log(project_id, level, msg))
    started_at = _now_iso()
    start_ts = time.time()
    report_name = f"actualize-{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}.json"
    report_path = p_reports_actualize(project_id) / report_name

    results: list[dict] = []
    counters = {"actualized": 0, "notNeeded": 0, "failed": 0}
    review_totals = {"found": 0, "drafted": 0, "needsHuman": 0, "noDraft": 0}
    collected: list[dict] = []
    total = sum(len(cfg.get("tasks") or []) for _, cfg in files)
    stopped = False
    processed = 0

    def should_stop() -> bool:
        return p_stop(project_id).exists()

    def save_report(state: str) -> None:
        payload = {
            "type": "actualize", "startedAt": started_at, "finishedAt": _now_iso(),
            "durationSec": int(time.time() - start_ts), "stoppedByUser": stopped,
            "state": state, "runId": run_id,
            "totals": {"total": len(results), **counters},
            "results": results,
        }
        if with_reviews:
            payload["withReviews"] = True
            payload["reviewTotals"] = dict(review_totals)
        _write_atomic(report_path, json.dumps(payload, ensure_ascii=False, indent=2))
        if state != "in-progress":
            _snapshot_log(project_id, report_path)

    def push_state(status: str, city: str, err: str | None = None) -> None:
        _write_state(project_id, {
            "runId": run_id, "action": "actualize", "status": status, "ownerPid": os.getpid(),
            "startedAt": started_at, "finishedAt": None if status == "running" else _now_iso(),
            # Считаем законченные города, а не текущий номер – см. публикацию.
            "total": total, "current": len(results), "currentCity": city, "reportName": report_name,
            "totals": {"total": len(results), **counters}, "error": err,
        })

    browser = yb.YbBrowser(project_id, headless=headless)
    prompt = ""
    if with_reviews:
        import reviews as rv
        prompt = rv.project_prompt(project_id)

    try:
        head = f"АКТУАЛИЗАЦИЯ · {total} городов"
        if with_reviews:
            head += " · с отзывами"
        _append_log(project_id, "INFO", head)
        if with_reviews:
            import llm
            if not llm.is_configured():
                _append_log(project_id, "WARN",
                            "⚠️  Ключ Gemini не задан – отзывы соберём, но черновиков не будет")
            if not prompt.strip():
                _append_log(project_id, "WARN",
                            "⚠️  Промпт проекта пуст – отзывы соберём, но черновиков не будет")
        save_report("in-progress")
        browser.start()

        for fp, cfg in files:
            if stopped:
                break
            country = cfg.get("country") or "–"
            city_tasks = cfg.get("tasks") or []
            for i, task in enumerate(city_tasks):
                if should_stop():
                    stopped = True
                    _append_log(project_id, "INFO", "⏹  Остановка по запросу")
                    break
                processed += 1
                push_state("running", task.get("cityName", ""))
                try:
                    res = yb.actualize_city(browser.page, task, i, len(city_tasks))
                except Exception as e:  # noqa: BLE001
                    res = {"cityName": task.get("cityName"), "companyUrl": task.get("companyUrl"),
                           "status": "failed", "reason": f"Критическая ошибка: {e}", "durationMs": 0}
                res["country"] = country
                res["package"] = country
                # Отзывы – отдельным шагом ПОСЛЕ актуализации: он опциональный
                # и не должен влиять на её статус, что бы там ни случилось.
                if with_reviews:
                    try:
                        rr = _reviews_for_city(project_id, browser.page, task, prompt)
                    except Exception as e:  # noqa: BLE001
                        rr = {"items": [], "summary": f"сбой шага отзывов: {e}",
                              "found": 0, "drafted": 0, "needsHuman": 0, "noDraft": 0}
                        _append_log(project_id, "WARN",
                                    f"  💬 {task.get('cityName', '?')}: {rr['summary']}")
                    res["reviews"] = rr["summary"]
                    for k in review_totals:
                        review_totals[k] += rr.get(k, 0)
                    if rr["items"]:
                        collected.extend(rr["items"])
                        # Пишем локально после каждого города: оборванный прогон
                        # не должен уносить с собой уже написанные черновики.
                        try:
                            import reviews as rv
                            rv.save_queue(project_id, rv.merge(rv.load_queue(project_id), collected))
                        except Exception as e:  # noqa: BLE001
                            _append_log(project_id, "WARN", f"  💬 очередь не сохранилась: {e}")
                results.append(res)
                counters[{"actualized": "actualized", "not-needed": "notNeeded"}.get(res["status"], "failed")] += 1
                save_report("in-progress")
                push_state("running", task.get("cityName", ""))
                if i < len(city_tasks) - 1:
                    _sleep_interruptible(delay_s, should_stop)
            if not stopped:
                _archive(fp)

        browser.save_session()
        # Итоги пишем ДО сохранения отчёта: отчёт забирает с собой снимок лога,
        # и раньше последние строки (ИТОГИ, ОТЗЫВЫ, ОЧЕРЕДЬ) в скачанный лог
        # не попадали – заказчик открывала файл и не находила там концовки.
        _append_log(project_id, "INFO",
                    f"ИТОГИ · ✅ {counters['actualized']} · ⊝ {counters['notNeeded']} · ❌ {counters['failed']}")
        if with_reviews:
            _append_log(project_id, "INFO",
                        f"ОТЗЫВЫ · без ответа {review_totals['found']} · "
                        f"черновиков {review_totals['drafted']} · "
                        f"человеку {review_totals['needsHuman']} · "
                        f"без черновика {review_totals['noDraft']}")
            # Один раз наружу, в конце: в облаке файлы не переживают
            # перезапуск, а коммитить на каждый город – это 140 коммитов.
            if collected:
                try:
                    import reviews as rv
                    where = rv.save_queue(project_id, rv.merge(rv.load_queue(project_id), collected),
                                          push=True)
                    _append_log(project_id, "INFO", f"ОЧЕРЕДЬ · {where}")
                except Exception as e:  # noqa: BLE001
                    _append_log(project_id, "WARN", f"ОЧЕРЕДЬ · сохранить наружу не вышло: {e}")
                _append_log(project_id, "INFO",
                            "ОЧЕРЕДЬ · ответы ждут вас в разделе «🔄 Актуализация», "
                            "карточка «Ответы на отзывы» наверху страницы")

        save_report("finished")
        push_state("stopped" if stopped else "done", "")
    except Exception as e:  # noqa: BLE001
        _append_log(project_id, "ERROR", f"💥 Критическая ошибка: {e}")
        save_report("crashed")
        push_state("error", "", str(e))
    finally:
        try:
            browser.close()
        except Exception:
            pass
        p_stop(project_id).unlink(missing_ok=True)
        _release_lock(project_id)
        yb.set_logger(None)


# ════════════════════════════════════════════════════════════════════
#  Чтение отчётов для UI
# ════════════════════════════════════════════════════════════════════

def list_reports(project_id: str, kind: str = "publish", limit: int = 30) -> list[dict]:
    folder = p_reports(project_id) if kind == "publish" else p_reports_actualize(project_id)
    if not folder.exists():
        return []
    out = []
    prefix = "report-" if kind == "publish" else "actualize-"
    for fp in sorted(folder.glob(f"{prefix}*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "name": fp.name,
            "startedAt": data.get("startedAt"),
            "finishedAt": data.get("finishedAt"),
            "durationSec": data.get("durationSec"),
            "state": data.get("state"),
            "totals": data.get("totals") or {},
        })
    return out


def read_report(project_id: str, kind: str, name: str) -> dict | None:
    folder = p_reports(project_id) if kind == "publish" else p_reports_actualize(project_id)
    fp = folder / Path(name).name
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_logs(project_id: str, limit: int = 20) -> list[str]:
    folder = p_logs(project_id)
    if not folder.exists():
        return []
    return [f.name for f in sorted(folder.glob("*.log"), reverse=True)[:limit]]


def read_log(project_id: str, name: str, tail: int = 200_000) -> str:
    fp = p_logs(project_id) / Path(name).name
    if not fp.exists():
        return ""
    try:
        return fp.read_text(encoding="utf-8", errors="replace")[-tail:]
    except OSError:
        return ""
