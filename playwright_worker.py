"""
playwright_worker.py – постоянный фоновый поток для работы с Playwright (sync API) из Streamlit.

Тот же файл, что и в crosspost/ (см. память по кросспостингу) – Streamlit выполняет
каждую перерисовку страницы в НОВОМ потоке, а Playwright sync API жёстко привязывает
браузер к одному потоку (иначе падает `greenlet.error: cannot switch to a different
thread`). Решение – один постоянный поток-воркер, всё общение с браузером идёт через
очередь команд.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

# Метка сборки: streamlit_app сверяет её и перезагружает модуль при расхождении.
# Метка сборки – одна на всё приложение, лежит в build.py. Держать её
# в каждом модуле своей строкой уже ломалось: у одного файла осталось
# старое значение, и Click принялся перезагружать все модули на каждое
# нажатие, пока не выбило по памяти.
from build import BUILD  # noqa: F401


@dataclass
class _Job:
    func: Callable
    args: tuple
    kwargs: dict
    result_q: "queue.Queue[tuple[bool, Any]]" = field(default_factory=queue.Queue)


class PlaywrightWorker:
    def __init__(self):
        self._jobs: "queue.Queue[_Job | None]" = queue.Queue()
        self._poisoned = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        # Playwright sync API отказывается работать, если в потоке есть
        # ЗАПУЩЕННЫЙ цикл asyncio. Свежий незапущенный цикл снимает вопрос и
        # для тех версий, которые смотрят на get_event_loop().
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
        except Exception:  # noqa: BLE001
            pass
        while True:
            job = self._jobs.get()
            if job is None:
                break
            try:
                result = job.func(*job.args, **job.kwargs)
                job.result_q.put((True, result))
            except Exception as e:  # noqa: BLE001
                if is_poisoned_thread(e):
                    self._poisoned = True
                job.result_q.put((False, e))

    def alive(self) -> bool:
        return self._thread.is_alive() and not self._poisoned

    def call(self, func: Callable, *args, **kwargs) -> Any:
        job = _Job(func, args, kwargs)
        self._jobs.put(job)
        ok, value = job.result_q.get()
        if not ok:
            raise value
        return value

    def stop(self):
        self._jobs.put(None)


# ─── Отравленный поток ──────────────────────────────────────────────
#
# Playwright перед стартом смотрит, не крутится ли в ЭТОМ потоке цикл
# asyncio, и отказывается работать, если крутится. Беда в том, что цикл
# может остаться «запущенным» от прошлого раза: sync-версия Playwright
# работает на гринлетах, а гринлеты делят один поток операционной системы.
# Оборвалась прошлая сессия неудачно (упало посреди работы, не закрылся
# браузер) – диспетчер остаётся припаркованным внутри run_until_complete,
# и для потока цикл числится запущенным НАВСЕГДА.
#
# Снаружи это выглядит так: один раз что-то не получилось, и дальше ВСЕ
# попытки падают с «Sync API inside the asyncio loop» до перезапуска
# приложения – хотя чинить было нечего.
#
# Отсюда два правила. Отравленный поток помечаем мёртвым, чтобы вместо
# него завели новый. А одноразовые задачи (открыл браузер, сделал, закрыл)
# вообще пускаем в СВЕЖЕМ потоке: у нового потока свои локальные данные,
# и отравить его прошлому нечем.
POISON_MARK = "sync api inside the asyncio loop"


def is_poisoned_thread(err: BaseException) -> bool:
    return POISON_MARK in str(err).lower()


def run_once(func: Callable, *args, **kwargs) -> Any:
    """
    Выполнить одноразовую задачу Playwright в отдельном свежем потоке.

    Для задач, которые сами открывают и закрывают браузер (отложка ВК,
    проверка сессии). Постоянный поток-воркер им не нужен – ему нечего
    между вызовами хранить, зато он копит риск отравиться.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
        except Exception:  # noqa: BLE001
            pass
        try:
            box["value"] = func(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 – отдаём наружу как есть
            box["error"] = e

    t = threading.Thread(target=target, daemon=True,
                         name=f"click-pw-once-{getattr(func, '__name__', 'job')}")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")
