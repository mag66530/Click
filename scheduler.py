"""
scheduler.py – планировщик кросспостинга: отправка в назначенное время.

Зачем. ВК и ОК держат отложку сами – им планировщик не нужен. А у ботов
Телеграма и МАКС отложки нет: пост надо отправить ровно в назначенную
минуту, и делает это Click. Планировщик – фоновый поток, который каждые
полминуты смотрит, не пришло ли время какого-нибудь задания.

Задание рождается при «Сформировать» (crosspost_form): по одному на пару
«пост × мессенджер», с текстом, картинками и временем. Лежат задания в
users-data/<ПРОЕКТ>/crosspost/tasks.json – файл, не память: перезапуск
приложения ничего не теряет.

Правило опоздания (Д-5 постановки). Click был выключен в час Х:
    опоздание меньше окна (настройка, по умолчанию 6 ч) – отправить
    с пометкой «с опозданием»; больше – «пропущено», НЕ отправлять
    (утренний пост в полночь – конфуз), и тревога в личный Телеграм.

Отказ отправки – не приговор: до 3 попыток по одной на тик, потом
«ошибка» с причиной словами и тревога.

Два экземпляра Click на одной машине не шлют дважды: лок-файл с
биением сердца – второй планировщик просто не запускается, пока жив
первый. Внутри процесса тик тоже один за раз (threading.Lock).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import apptime
import crosspost_state as cps
import paths

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

TICK_SECONDS = 30
MAX_ATTEMPTS = 3
LATE_AFTER_S = 60          # позже планового на минуту – уже «с опозданием»
LOCK_FRESH_S = 90          # лок живее этого – второй планировщик не стартует
DONE_KEEP_DAYS = 30        # выполненные задания храним для журнала, потом чистим

_tick_lock = threading.Lock()
_thread: threading.Thread | None = None
_thread_guard = threading.Lock()


# ─── Файлы ──────────────────────────────────────────────────────────
def _root() -> Path:
    d = paths.data_root() / "crosspost"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tasks_path(project_id: str) -> Path:
    d = paths.data_root() / project_id / "crosspost"
    d.mkdir(parents=True, exist_ok=True)
    return d / "tasks.json"


def _config_path() -> Path:
    return _root() / "scheduler.json"


def _lock_path() -> Path:
    return _root() / ".scheduler.lock"


def _log_path() -> Path:
    return _root() / f"scheduler-{apptime.stamp('%Y-%m-%d')}.log"


def log(msg: str) -> None:
    line = f"[{apptime.stamp()}] {msg}\n"
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def tail(lines: int = 40) -> str:
    try:
        text = _log_path().read_text(encoding="utf-8")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


# ─── Настройки ──────────────────────────────────────────────────────
def config() -> dict:
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return {"enabled": bool(data.get("enabled", True)),
            "lateWindowHours": float(data.get("lateWindowHours", 6))}


def set_config(enabled: bool | None = None, late_window_hours: float | None = None) -> None:
    cfg = config()
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if late_window_hours is not None:
        cfg["lateWindowHours"] = float(late_window_hours)
    _config_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── Задания ────────────────────────────────────────────────────────
def load_tasks(project_id: str) -> list[dict]:
    fp = _tasks_path(project_id)
    if not fp.exists():
        return []
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_tasks(project_id: str, tasks: list[dict]) -> None:
    fp = _tasks_path(project_id)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(fp)


def queue_task(project_id: str, task: dict) -> None:
    """
    Поставить задание. Идемпотентно по id: то же задание второй раз не
    множится, а обновляется (правка текста в реестре → переформирование).
    Выполненное задание с тем же id не трогаем – дубль не поедет.
    """
    tasks = load_tasks(project_id)
    for i, t in enumerate(tasks):
        if t.get("id") == task.get("id"):
            if t.get("state") in ("done", "done-late"):
                return
            tasks[i] = {**task, "attempts": 0, "state": "waiting"}
            _save_tasks(project_id, tasks)
            return
    tasks.append({**task, "attempts": 0, "state": "waiting"})
    _save_tasks(project_id, tasks)


def projects_with_tasks() -> list[str]:
    root = paths.data_root()
    out = []
    try:
        for p in root.iterdir():
            if (p / "crosspost" / "tasks.json").exists():
                out.append(p.name)
    except OSError:
        pass
    return sorted(out)


# ─── Решение по заданию (чистая логика, накрыта тестами) ────────────
def decide(task: dict, now: datetime, late_window_s: float) -> str:
    """
    'wait'      – рано;
    'send'      – время пришло (в пределах минуты);
    'send-late' – опоздали, но окно ещё не вышло;
    'missed'    – окно вышло, публиковать поздно.
    """
    when = datetime.fromisoformat(task["when"])
    if now < when:
        return "wait"
    late_by = (now - when).total_seconds()
    if late_by > late_window_s:
        return "missed"
    return "send-late" if late_by > LATE_AFTER_S else "send"


# ─── Отправка ───────────────────────────────────────────────────────
def _default_senders() -> dict[str, Callable[[dict], dict]]:
    """
    Кто умеет отправлять. Ключ – семейство сети ('tg' обслуживает и
    tg-client, и tg-staff). Импорты внутри: планировщик должен подниматься
    и тогда, когда какой-то клиент недоступен.
    """
    def send_tg(task: dict) -> dict:
        import tg_social
        local, err = _fetch_images(task)
        if err:
            return {"ok": False, "error": err}
        return tg_social.publish(task.get("chatId", ""), task.get("markup", ""), local)

    def send_max(task: dict) -> dict:
        import max_social
        local, err = _fetch_images(task)
        if err:
            return {"ok": False, "error": err}
        return max_social.publish(task.get("chatId", ""), task.get("markup", ""), local)

    return {"tg": send_tg, "max": send_max}


def _fetch_images(task: dict) -> tuple[list[str], str]:
    urls = task.get("images") or []
    if not urls:
        return [], ""
    import yb_playwright as yb
    temp = paths.data_root() / task.get("project", "") / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    local, errors = yb.fetch_photos(list(urls), temp)
    if errors:
        return [], "картинка не скачалась: " + "; ".join(errors)
    return local, ""


def _alert(text: str) -> None:
    try:
        import tg_social
        tg_social.send_alert(text)
    except Exception:  # noqa: BLE001 – тревога не должна ронять планировщик
        pass


def _record(project_id: str, task: dict, status: str, link: str = "", error: str = "") -> None:
    """Исход – в память кросспостинга, чтобы сводка раздела говорила правду."""
    post = {"brand": task.get("brand", project_id), "date": task.get("date", ""),
            "when": task.get("when", ""), "text": task.get("sourceText", "")}
    cps.set_status(project_id, post, task.get("network", ""), status, link=link, error=error)


def _default_yb_start(task: dict) -> dict:
    """
    Запуск прогона публикации ЯБ – тем же start_publish, что и кнопка
    «Опубликовать»: все локи и защиты от дублей действуют. «Занято» и
    «очередь пуста» – не провал, а «подождём»: лок освободится, очередь
    соберут, дедлайн всё равно закроет вопрос.
    """
    import runner
    started, msg = runner.start_publish(
        task.get("project", ""),
        headless=bool(task.get("headless", True)),
        expected_email=task.get("expectedEmail", ""),
        strict_account_check=bool(task.get("expectedEmail")))
    if started:
        return {"ok": True}
    return {"ok": False, "retryable": True, "error": msg}


def cancel_task(project_id: str, task_id: str) -> bool:
    """Отменить невыполненное задание. Выполненное не трогаем."""
    tasks = load_tasks(project_id)
    for t in tasks:
        if t.get("id") == task_id and t.get("state") in ("waiting", "retry"):
            t["state"] = "cancelled"
            _save_tasks(project_id, tasks)
            log(f"{project_id} {task_id}: отменено человеком")
            return True
    return False


def tick(now: datetime | None = None,
         senders: dict[str, Callable[[dict], dict]] | None = None,
         yb_start: Callable[[dict], dict] | None = None) -> int:
    """
    Один проход по всем заданиям всех проектов. Возвращает, сколько заданий
    поменяло состояние. senders и yb_start подменяются в тестах.
    """
    if not config()["enabled"]:
        return 0
    if not _tick_lock.acquire(blocking=False):
        return 0
    try:
        now = now or apptime.now()
        senders = senders or _default_senders()
        yb_start = yb_start or _default_yb_start
        late_window_s = config()["lateWindowHours"] * 3600
        changed = 0

        for project_id in projects_with_tasks():
            tasks = load_tasks(project_id)
            dirty = False
            for task in tasks:
                if task.get("state") not in ("waiting", "retry"):
                    continue
                action = decide(task, now, late_window_s)
                if action == "wait":
                    continue

                if action == "missed":
                    task["state"] = "missed"
                    _record(project_id, task, cps.MISSED,
                            error=f"окно опоздания ({config()['lateWindowHours']:g} ч) вышло")
                    log(f"{project_id} {task['id']}: пропущено – окно опоздания вышло")
                    _alert(f"⏭ Click: пост {task.get('date')} в "
                           f"{cps.network_ru(task.get('network', ''))} пропущен – "
                           f"приложение не работало в назначенное время.")
                    dirty = True
                    changed += 1
                    continue

                family = (task.get("network") or "").split("-")[0]

                # ЯБ – не отправка, а запуск прогона. «Занято» и «очередь
                # пуста» – повод подождать следующий тик, а не жечь попытки.
                if family == "yb":
                    res = yb_start(task)
                    if res.get("ok"):
                        task["state"] = "done-late" if action == "send-late" else "done"
                        task["sentAt"] = now.isoformat(timespec="seconds")
                        log(f"{project_id} {task['id']}: прогон ЯБ запущен"
                            + (" (с опозданием)" if action == "send-late" else ""))
                        dirty = True
                        changed += 1
                    elif res.get("retryable"):
                        note = res.get("error", "")
                        if task.get("lastNote") != note:   # без спама в журнал каждый тик
                            task["lastNote"] = note
                            log(f"{project_id} {task['id']}: жду – {note}")
                            dirty = True
                    else:
                        task["state"] = "failed"
                        log(f"{project_id} {task['id']}: ошибка – {res.get('error', '')}")
                        _alert(f"❌ Click: прогон ЯБ {task.get('date')} не запустился: "
                               f"{res.get('error', '')}")
                        dirty = True
                        changed += 1
                    continue

                sender = senders.get(family)
                if sender is None:
                    task["state"] = "failed"
                    _record(project_id, task, cps.FAILED, error=f"нет отправителя для {family}")
                    dirty = True
                    changed += 1
                    continue

                log(f"{project_id} {task['id']}: отправляю"
                    + (" (с опозданием)" if action == "send-late" else ""))
                res = sender(task)
                if res.get("ok"):
                    late = action == "send-late"
                    task["state"] = "done-late" if late else "done"
                    task["sentAt"] = now.isoformat(timespec="seconds")
                    _record(project_id, task, cps.SENT_LATE if late else cps.SENT,
                            link=res.get("link", ""))
                    log(f"{project_id} {task['id']}: вышло"
                        + (" с опозданием" if late else ""))
                else:
                    task["attempts"] = int(task.get("attempts", 0)) + 1
                    err = res.get("error", "неизвестная ошибка")
                    if task["attempts"] >= MAX_ATTEMPTS:
                        task["state"] = "failed"
                        _record(project_id, task, cps.FAILED, error=err)
                        log(f"{project_id} {task['id']}: ошибка окончательно – {err}")
                        _alert(f"❌ Click: пост {task.get('date')} в "
                               f"{cps.network_ru(task.get('network', ''))} не вышел: {err}")
                    else:
                        task["state"] = "retry"
                        log(f"{project_id} {task['id']}: ошибка (попытка "
                            f"{task['attempts']}/{MAX_ATTEMPTS}) – {err}")
                dirty = True
                changed += 1

            if dirty:
                _save_tasks(project_id, _purge_old(tasks, now))
        return changed
    finally:
        _tick_lock.release()


def _purge_old(tasks: list[dict], now: datetime) -> list[dict]:
    """Выполненное старше DONE_KEEP_DAYS убираем – журнал не должен пухнуть вечно."""
    keep: list[dict] = []
    for t in tasks:
        if t.get("state") in ("done", "done-late", "missed", "failed", "cancelled"):
            try:
                when = datetime.fromisoformat(t["when"])
                if (now - when) > timedelta(days=DONE_KEEP_DAYS):
                    continue
            except (KeyError, ValueError):
                pass
        keep.append(t)
    return keep


# ─── Фоновый поток ──────────────────────────────────────────────────
def _lock_is_fresh() -> bool:
    try:
        data = json.loads(_lock_path().read_text(encoding="utf-8"))
        return (time.time() - float(data.get("beat", 0))) < LOCK_FRESH_S \
            and int(data.get("pid", -1)) != os.getpid()
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _heartbeat() -> None:
    try:
        _lock_path().write_text(json.dumps({"pid": os.getpid(), "beat": time.time()}),
                                encoding="utf-8")
    except OSError:
        pass


def _loop() -> None:
    log(f"Планировщик запущен (pid {os.getpid()}, тик {TICK_SECONDS} с)")
    while True:
        try:
            _heartbeat()
            tick()
        except Exception as e:  # noqa: BLE001 – один плохой тик не убивает поток
            log(f"Тик упал: {e}")
        time.sleep(TICK_SECONDS)


def ensure_running() -> bool:
    """
    Поднять фоновый поток, если ещё не поднят. Вторая копия Click на той же
    машине планировщик не заводит (лок с биением сердца) – иначе задания
    уходили бы дважды. Возвращает True, если планировщик работает в ЭТОМ
    процессе.
    """
    global _thread
    with _thread_guard:
        if _thread is not None and _thread.is_alive():
            return True
        if _lock_is_fresh():
            return False
        _heartbeat()
        _thread = threading.Thread(target=_loop, name="crosspost-scheduler", daemon=True)
        _thread.start()
        return True


def is_running_here() -> bool:
    return _thread is not None and _thread.is_alive()
