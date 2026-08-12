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

import apptime
import paths
import yb_playwright as yb

# Метка сборки – одна на всё приложение, лежит в build.py. Держать её
# в каждом модуле своей строкой уже ломалось: у одного файла осталось
# старое значение, и Click принялся перезагружать все модули на каждое
# нажатие, пока не выбило по памяти.
from build import BUILD  # noqa: F401

ROOT = Path(__file__).parent
USERS_DATA = paths.data_root()

DEDUP_WINDOW_HOURS = 12      # столько часов один и тот же текст не публикуется в тот же город повторно
LIVE_LOG_MAX_BYTES = 400_000
SLOW_WINDOW = 3              # окно детектора «Яндекс тормозит»
SLOW_THRESHOLD_MS = 60_000
COOLDOWN_PAUSE_S = 30
PROTOCOL_FAIL_LIMIT = 3      # столько подряд протокольных падений = браузер завис
LIMIT_GIVE_UP = 2            # столько отказов Gemini по лимиту подряд = хватит пробовать

# ─── виды прогонов ──────────────────────────────────────────────────
# Прогоны разных видов идут параллельно, поэтому у каждого свои файлы:
# состояние, живой лог, лок и флаг остановки. Раньше файл был один на проект,
# и второй прогон затирал бы первому и лог, и прогресс.
RUN_KINDS = ("publish", "actualize", "actualize-gis", "collect")

KIND_RU = {"publish": "Публикация", "actualize": "Актуализация",
           "actualize-gis": "Актуализация 2ГИС", "collect": "Чтение организаций"}

# ─── площадки ───────────────────────────────────────────────────────
# Яндекс.Бизнес и 2ГИС отличаются браузерной частью и папками, а всё
# остальное – очередь городов, отчёт, лог, память, остановка – у них общее.
# Поэтому не второй воркер, а параметр: две копии одного прогона разъехались
# бы на первой же правке.
YANDEX, GIS = "yandex", "gis"

# «ru» – как называется площадка в заголовках, «short» – как её зовут в
# обычной речи («войдите в Яндекс»), «gen» – родительный падеж («сессия
# Яндекса»). Без падежей выходило «Сессия Яндекс.Бизнес не активна».
PLATFORMS = {
    YANDEX: {"kind": "actualize", "ru": "Яндекс.Бизнес", "short": "Яндекс", "gen": "Яндекса",
             "tasks": "tasks-actualize", "reports": "reports-actualize"},
    GIS:    {"kind": "actualize-gis", "ru": "2ГИС", "short": "2ГИС", "gen": "2ГИС",
             "tasks": "tasks-actualize-gis", "reports": "reports-actualize-gis"},
}

# Что с чем НЕ уживается. Сверка только читает раздел «Организации» и ничего
# в Яндексе не меняет – её можно гонять рядом с чем угодно. Два прогона
# одного вида не пускаются никогда: это дубли постов и двойные клики по
# одному городу, ради чего лок и заводился. Публикация с актуализацией
# разведены по другой причине – это два тяжёлых браузера разом, а бесплатному
# облаку хватает и одного (приложение уже выбивало по памяти).
CONFLICTS: dict[str, tuple[str, ...]] = {
    "publish":       ("publish", "actualize", "actualize-gis"),
    "actualize":     ("actualize", "publish", "actualize-gis"),
    # 2ГИС – такой же тяжёлый браузер, как Яндекс. Рядом с яндексовым прогоном
    # его не пускаем по той же причине: облако не тянет два разом.
    "actualize-gis": ("actualize-gis", "actualize", "publish"),
    "collect":       ("collect",),
}

# Потолок на всякий случай: сколько браузеров живёт разом. Из таблицы выше
# больше двух и не выходит, но если видов прогонов станет больше – потолок
# останется.
MAX_PARALLEL_RUNS = 2

_threads: dict[tuple[str, str], threading.Thread] = {}
_lock = threading.Lock()

# Какой прогон ведёт этот поток. Ставится один раз в начале воркера, дальше
# все пути (лог, стоп-флаг, состояние) сами попадают в нужные файлы – иначе
# пришлось бы протаскивать вид прогона через полсотни вызовов, и один
# забытый писал бы соседу в стоп-флаг.
_CUR = threading.local()


def _kind(kind: str | None = None) -> str:
    """Вид прогона: явно переданный или тот, что ведёт этот поток."""
    return kind or getattr(_CUR, "kind", "") or "publish"


# ─── пути ───────────────────────────────────────────────────────────
def base(project_id: str) -> Path:
    d = USERS_DATA / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def p_tasks(project_id: str) -> Path:
    return base(project_id) / "tasks"


# Задания «только фото в 2ГИС» лежат ОТДЕЛЬНО от постов, и это не про порядок
# в папках. Прогон прежней сборки про такие задания не знает: он видит пустой
# текст поста и публикует пустой пост – так у заказчика и вышло, пять пустых
# постов в живых карточках. В свою папку он не заглядывает вовсе, потому что
# в его коде её нет: очередь для него просто пуста, и он ничего не делает.
def p_tasks_gis(project_id: str) -> Path:
    return base(project_id) / "tasks-gis-photos"


def tasks_folder(project_id: str, source: str = "tasks") -> Path:
    return p_tasks_gis(project_id) if source == "gis" else p_tasks(project_id)


def p_tasks_actualize(project_id: str, platform: str = YANDEX) -> Path:
    return base(project_id) / PLATFORMS[platform]["tasks"]


def p_reports(project_id: str) -> Path:
    return base(project_id) / "reports"


def p_reports_actualize(project_id: str, platform: str = YANDEX) -> Path:
    return base(project_id) / PLATFORMS[platform]["reports"]


def p_logs(project_id: str) -> Path:
    return base(project_id) / "logs"


def p_state(project_id: str, kind: str | None = None) -> Path:
    return base(project_id) / f".run-state-{_kind(kind)}.json"


def p_lock(project_id: str, kind: str | None = None) -> Path:
    return base(project_id) / f".run-{_kind(kind)}.lock"


def p_live_log(project_id: str, kind: str | None = None) -> Path:
    return base(project_id) / f".run-log-{_kind(kind)}.txt"


def p_stop(project_id: str, kind: str | None = None) -> Path:
    return base(project_id) / f".STOP_FLAG-{_kind(kind)}"


# Файлы прошлой версии – один комплект на проект. Читаем их, чтобы после
# обновления на экране остался итог последнего прогона; писать в них больше
# некому.
def p_state_legacy(project_id: str) -> Path:
    return base(project_id) / ".run-state.json"


def p_live_log_legacy(project_id: str) -> Path:
    return base(project_id) / ".run-log.txt"


def _legacy_action(project_id: str) -> str:
    """Какого вида был прогон, записанный файлами прошлой версии."""
    fp = p_state_legacy(project_id)
    if not fp.exists():
        return ""
    try:
        return json.loads(fp.read_text(encoding="utf-8")).get("action") or "publish"
    except (json.JSONDecodeError, OSError):
        return ""


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
def _read_state_file(project_id: str, kind: str) -> dict | None:
    fp = p_state(project_id, kind)
    if not fp.exists():
        # Прогон, начатый прошлой версией: комплект файлов был один на проект.
        if _legacy_action(project_id) != kind:
            return None
        try:
            fp_state = json.loads(p_state_legacy(project_id).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    else:
        try:
            fp_state = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    # Прогон помечен running, но процесс, который его вёл, уже не жив
    # (перезапуск Streamlit) – показываем честно, а не «вечно идёт».
    if fp_state.get("status") == "running" and fp_state.get("ownerPid") != os.getpid():
        fp_state = {**fp_state, "status": "interrupted",
                    "error": "Прогон прерван перезапуском приложения. Данные о выполненных городах сохранены."}
    return fp_state


def read_state(project_id: str, kind: str | None = None) -> dict:
    """
    Состояние прогона. С видом – именно этого вида; без вида – того, что идёт
    сейчас, а если не идёт ни один – последнего начатого.
    """
    if kind is not None:
        return _read_state_file(project_id, kind) or {"status": "idle"}

    states = [s for s in (_read_state_file(project_id, k) for k in RUN_KINDS) if s]
    if not states:
        return {"status": "idle"}
    live = [s for s in states if s.get("status") == "running"]
    return max(live or states, key=lambda s: s.get("startedAt") or "")


def _write_state(project_id: str, state: dict) -> None:
    _write_atomic(p_state(project_id, state.get("action")),
                  json.dumps(state, ensure_ascii=False, indent=2))


def running_kinds(project_id: str) -> list[str]:
    """Какие прогоны идут прямо сейчас – их может быть несколько."""
    return [k for k in RUN_KINDS
            if (_read_state_file(project_id, k) or {}).get("status") == "running"]


def is_running(project_id: str, kind: str | None = None) -> bool:
    """Без вида – идёт ли хоть какой-нибудь прогон."""
    if kind is None:
        return bool(running_kinds(project_id))
    return read_state(project_id, kind).get("status") == "running"


# ─── Память ─────────────────────────────────────────────────────────
#
# Самая частая смерть Click в облаке – не ошибка в коде, а нехватка памяти.
# Выглядит она страшно и непонятно: приложение просто исчезает, в логе ни
# одной питоновской строки, на экране «Oh no». Так и было: заказчик запустила
# актуализацию в одном браузере, во втором зашла в другой проект и запустила
# там актуализацию со сверкой – три тяжёлых прогона разом, и всё легло.
#
# Считать прогоны штуками мало: один прогон на сотне городов ест куда больше,
# чем на десяти. Поэтому смотрим на ФАКТИЧЕСКУЮ память процесса и ведём себя
# по ней:
#   • перед стартом заглядываем – если и так много, новый прогон не пускаем;
#   • во время прогона проверяем на каждом городе. Перевалило за мягкий
#     порог – перезапускаем браузер, это возвращает сотни мегабайт. Перевалило
#     за жёсткий – аккуратно останавливаемся: отчёт сохранён, приложение живо.
#
# Смысл в том, чтобы вместо «облако убило всех» получалось «один прогон
# закончился раньше времени, и об этом написано словами».
MEM_START_MB = 1200      # выше – новый прогон не начинаем
MEM_SOFT_MB = 1500       # выше – перезапускаем браузер прямо посреди прогона
MEM_HARD_MB = 1900       # выше – останавливаем прогон, сохранив сделанное

CGROUP_ROOT = Path("/sys/fs/cgroup")


def _stat_key(path: Path, key: str) -> int:
    """Значение по ключу из файла вида «ключ число» построчно. 0 – не вышло."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition(" ")
            if name == key:
                return int(value.strip())
    except (OSError, ValueError):
        pass
    return 0


def container_memory_mb(root: Path | None = None) -> int:
    """
    Память ВСЕГО контейнера в мегабайтах. 0 – спросить не у кого.

    Берём именно «живую» память (anon / total_rss), а не memory.current:
    в последнюю входит файловый кэш, а его в этой машине бывает гигабайт.
    По кэшу сторож бил бы тревогу на ровном месте.
    """
    root = root or CGROUP_ROOT
    for path, key in ((root / "memory.stat", "anon"),                  # cgroup v2
                      (root / "memory" / "memory.stat", "total_rss")):  # cgroup v1
        got = _stat_key(path, key)
        if got:
            return got // (1024 * 1024)
    return 0


def _tree_memory_mb() -> int:
    """
    Своё дерево процессов по /proc. Запасной путь, если cgroup не видно.

    Считаем себя и потомков – браузер запускается отдельным процессом и
    сидит именно там. Чужие процессы не трогаем: на общей машине они не
    наши, и мешать из-за них работе нельзя.
    """
    try:
        pids = [int(d.name) for d in Path("/proc").iterdir() if d.name.isdigit()]
    except OSError:
        return 0
    parent, rss = {}, {}
    for pid in pids:
        try:
            text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            if line.startswith("PPid:"):
                parent[pid] = int(line.split()[1])
            elif line.startswith("VmRSS:"):
                rss[pid] = int(line.split()[1])
    mine = {os.getpid()}
    # Несколько проходов: дети могут перечисляться раньше родителей.
    for _ in range(4):
        for pid, ppid in parent.items():
            if ppid in mine:
                mine.add(pid)
    return sum(rss.get(pid, 0) for pid in mine) // 1024


def memory_mb() -> int:
    """
    Сколько памяти занято ПРЯМО СЕЙЧАС, в мегабайтах.

    Здесь была слепота, из-за которой сторож памяти не срабатывал почти
    никогда. Замер брался из /proc/self/status – это память интерпретатора
    Python. А браузер Playwright живёт ОТДЕЛЬНЫМ процессом, и его 400–500 МБ
    в замер не попадали вовсе. Приложение видело «занято 300 МБ», спокойно
    пускало второй прогон, облако считало полтора гигабайта и убивало всё
    целиком – со страницей «Oh no» и без единой строчки в логе.

    Поэтому спрашиваем у контейнера: он считает всех своих. Не вышло –
    складываем своё дерево процессов. И только в последнюю очередь
    возвращаемся к собственной памяти.
    """
    for measure in (container_memory_mb, _tree_memory_mb):
        try:
            got = measure()
        except Exception:  # noqa: BLE001 – замер не должен мешать работе
            got = 0
        if got:
            return got
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    try:
        import resource
        # На Linux ru_maxrss в килобайтах; это пик, но как оценка сойдёт.
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    except Exception:  # noqa: BLE001 – без замера просто не мешаем работать
        return 0


def memory_limit_mb(root: Path | None = None) -> int:
    """Потолок памяти контейнера в мегабайтах. 0 – не задан или не виден."""
    root = root or CGROUP_ROOT
    for path in (root / "memory.max",                       # cgroup v2
                 root / "memory" / "memory.limit_in_bytes"):  # cgroup v1
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        # «Без ограничения» пишут гигантским числом – это не потолок.
        if 0 < value < (1 << 62):
            return value // (1024 * 1024)
    return 0


def mem_gates() -> tuple[int, int, int]:
    """
    Пороги сторожа: (не начинать, перезапустить браузер, остановиться).

    Если контейнер сам называет свой потолок – считаем от него и берём то,
    что строже. Числа в константах проверены на нынешнем тарифе; окажется
    тариф скромнее – пороги опустятся сами, а не будут ждать, пока облако
    убьёт приложение.
    """
    cap = memory_limit_mb()
    if not cap:
        return MEM_START_MB, MEM_SOFT_MB, MEM_HARD_MB
    return (min(MEM_START_MB, int(cap * 0.55)),
            min(MEM_SOFT_MB, int(cap * 0.68)),
            min(MEM_HARD_MB, int(cap * 0.82)))


def live_runs_everywhere() -> list[tuple[str, str]]:
    """
    Все идущие прогоны по ВСЕМ проектам: [(проект, вид)].

    Память у облака одна на всё приложение, а не на проект. Пока потолок
    считался по проекту, двое за разными компами на разных проектах могли
    поднять по два браузера каждый – четыре разом, и это верная смерть по
    памяти (один браузер весит около 450 МБ). Считаем поэтому глобально.
    """
    out: list[tuple[str, str]] = []
    try:
        projects = [d.name for d in USERS_DATA.iterdir() if d.is_dir()]
    except OSError:
        return out
    for pid in projects:
        for k in RUN_KINDS:
            if is_running(pid, k) or lock_owner(pid, k) is not None:
                out.append((pid, k))
    return out


def _raw_state(project_id: str, kind: str) -> dict:
    """
    Состояние КАК ЗАПИСАНО, без правки «чужой процесс – значит прервано».

    Для рассказа о том, кто занял память, важно именно записанное: прогон
    из другого окна для нас «прерван», а на деле живёхонек и жуёт память.
    """
    try:
        return json.loads(p_state(project_id, kind).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _how_long(started_at: str) -> str:
    """«12 мин» / «1 ч 5 мин». Пусто – времени старта нет или оно битое."""
    if not started_at:
        return ""
    try:
        began = datetime.fromisoformat(started_at)
    except ValueError:
        return ""
    if began.tzinfo is None:
        began = began.replace(tzinfo=timezone.utc)
    minutes = int((datetime.now(timezone.utc) - began).total_seconds() // 60)
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60} мин"


def running_now() -> list[dict]:
    """
    Кто прямо сейчас работает – по всем проектам и видам, с подробностями.

    Нужно для честного отказа. «Занято 2034 МБ» без имени виновника –
    тупик: заказчица видит цифру, а какой проект что делает и как давно,
    понять неоткуда, и остаётся только ждать вслепую.
    """
    out: list[dict] = []
    for pid, kind in live_runs_everywhere():
        st_ = _raw_state(pid, kind)
        out.append({
            "project": pid,
            "kind": kind,
            "ru": KIND_RU.get(kind, kind),
            "for": _how_long(st_.get("startedAt") or ""),
            "current": st_.get("current") or 0,
            "total": st_.get("total") or 0,
            "city": st_.get("currentCity") or "",
        })
    return out


def busy_details_ru() -> str:
    """Одной строкой: кто, что, как давно и на чём. Пусто – никто ничего."""
    parts = []
    for run in running_now():
        line = f"{run['project']} – {run['ru'].lower()}"
        if run["for"]:
            line += f", идёт {run['for']}"
        if run["total"]:
            line += f", город {run['current']} из {run['total']}"
        if run["city"]:
            line += f" ({run['city']})"
        parts.append(line)
    return "; ".join(parts)


def busy_reason(project_id: str, kind: str) -> str:
    """
    Почему прогон этого вида сейчас не запустить. Пустая строка – можно.

    Смотрим и на состояние (свой процесс), и на лок (чужое окно или другая
    копия Click: там состояние читается как «прервано», а прогон живой).
    """
    live = [k for k in RUN_KINDS
            if is_running(project_id, k) or lock_owner(project_id, k) is not None]
    for other in CONFLICTS.get(kind, (kind,)):
        if other in live:
            if other == kind:
                return f"{KIND_RU[kind]} уже идёт – второй запуск заблокирован."
            return (f"Сейчас идёт «{KIND_RU[other]}». Вместе с ней «{KIND_RU[kind]}» "
                    "не запускается: это два браузера разом, приложению не хватит памяти.")
    if len(live) >= MAX_PARALLEL_RUNS:
        return (f"Уже идут два прогона ({', '.join(KIND_RU[k] for k in live)}). "
                "Дождитесь окончания одного из них.")
    # Потолок общий на всё приложение: браузеры делят одну память облака,
    # и неважно, из какого проекта и с чьего компьютера их запустили.
    # Потолок общий на ВСЁ приложение, без поблажек своему проекту. Поблажка
    # тут и стояла – «если в этом проекте уже что-то идёт, глобальный потолок
    # пропускаем», – и через неё пролезал третий прогон: актуализация в одном
    # проекте, актуализация и сверка в другом. Три тяжёлых браузера разом,
    # и облако убивало приложение целиком.
    everywhere = live_runs_everywhere()
    if len(everywhere) >= MAX_PARALLEL_RUNS:
        return (f"Уже идут {len(everywhere)} прогона – это потолок на всё приложение, "
                "включая другие проекты и другие компьютеры. Браузеры тяжёлые, а "
                f"память у облака одна на всех. Сейчас работают: {busy_details_ru()}. "
                "Дождитесь окончания.")

    # И отдельно – по факту занятой памяти. Прогонов может быть мало, а память
    # уже на исходе: сотня городов ест куда больше десятка.
    used = memory_mb()
    start_at, _, _ = mem_gates()
    if used and used > start_at:
        # Обязательно говорим, КТО занял. «Занято 2034 МБ» – тупик: цифру
        # видно, а какой проект что делает и как давно, понять неоткуда, и
        # остаётся ждать вслепую. Заказчица на этом застряла с тестами.
        details = busy_details_ru()
        if details:
            return (f"Сейчас занято {used} МБ памяти – для нового прогона это много. "
                    f"Работают: {details}. Дождитесь окончания.")
        # Прогонов нет, а память занята – значит её держит не работа, а
        # брошенный браузер от упавшего прогона. Ждать тут бесполезно, и
        # честнее сказать это прямо, чем советовать «дождитесь окончания»
        # того, что уже кончилось.
        return (f"Сейчас занято {used} МБ памяти, но НИ ОДНОГО прогона не идёт – "
                "похоже, память держит браузер от упавшего прогона. Ждать смысла "
                "нет: обновите страницу, а если цифра не падает – перезапустите "
                "приложение (в облаке: Manage app → Reboot).")
    return ""


def request_stop(project_id: str, kind: str | None = None) -> None:
    """Остановить прогон этого вида. Без вида – все, что идут."""
    kinds = [kind] if kind else (running_kinds(project_id) or [_kind()])
    for k in kinds:
        p_stop(project_id, k).write_text(str(int(time.time())), encoding="utf-8")
        _append_log(project_id, "WARN",
                    "⏹  Запрошена остановка – завершу после текущего города", kind=k)


def run_log_path(project_id: str, kind: str, report_name: str) -> Path:
    """
    Файл лога рядом с отчётом: имя то же, расширение .log.

    Папку берём той же функцией, что и сам отчёт. Здесь стояла своя строчка
    с двумя вариантами – и про 2ГИС она не знала: лог сохранялся рядом с
    отчётом 2ГИС, а искался в папке Яндекса. Кнопка «Лог (.txt)» получалась
    вечно серой, с перечёркнутым курсором.
    """
    return _report_folder(project_id, kind) / (Path(report_name).name.replace(".json", "") + ".log")


def _snapshot_log(project_id: str, report_path: Path, kind: str | None = None) -> None:
    """Сохранить лог этого прогона рядом с его отчётом."""
    try:
        text = p_live_log(project_id, kind).read_text(encoding="utf-8", errors="replace")
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


def read_live_log(project_id: str, kind: str | None = None, tail: int = 40_000) -> str:
    kind = _kind(kind)
    fp = p_live_log(project_id, kind)
    if not fp.exists():
        # Лог прогона, начатого прошлой версией: файл был один на проект.
        # Без этого сразу после обновления лог последнего прогона исчезал бы
        # с экрана, хотя его итог рядом по-прежнему показан.
        legacy = p_live_log_legacy(project_id)
        if _legacy_action(project_id) != kind or not legacy.exists():
            return ""
        fp = legacy
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-tail:]


def _append_log(project_id: str, level: str, msg: str, kind: str | None = None) -> None:
    kind = _kind(kind)
    line = f"[{apptime.stamp()}] [{level}] {msg}\n"
    _touch_lock(project_id, kind)
    try:
        live = p_live_log(project_id, kind)
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
        day = apptime.stamp("%Y-%m-%d")
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


def _photo_key(city_url: str | None, files: list[str]) -> str:
    """
    Ключ «эти снимки → эта карточка». Считаем по СОДЕРЖИМОМУ файлов, а не по
    именам: одна и та же фотография, скачанная дважды, ложится во временную
    папку под разными именами, и по именам защита от дублей не сработала бы.
    """
    h = hashlib.sha1()
    for fp in sorted(files):
        try:
            h.update(hashlib.sha1(Path(fp).read_bytes()).digest())
        except OSError:
            h.update(str(fp).encode("utf-8"))
    return f"gisphoto:{gis_org_id(city_url)}:{h.hexdigest()[:16]}"


def gis_org_id(url: str | None) -> str:
    import gis_playwright as gis
    return gis.extract_org_id(url) or (url or "")


def recent_gis_photos(project_id: str, city_url: str | None, files: list[str],
                      window_hours: float = 24 * 30) -> dict | None:
    """Заливали ли уже ЭТИ снимки в ЭТУ карточку 2ГИС. Окно – месяц."""
    rec = _read_ledger(project_id).get(_photo_key(city_url, files))
    if not rec:
        return None
    if time.time() - float(rec.get("ts") or 0) > window_hours * 3600:
        return None
    return rec


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
_LOCK_HELD: set[tuple[str, str]] = set()
_LOCK_TOUCHED: dict[tuple[str, str], float] = {}


def _touch_lock(project_id: str, kind: str | None = None) -> None:
    """Отметить, что прогон жив. Зовётся часто, поэтому не чаще раза в 5 секунд."""
    key = (project_id, _kind(kind))
    if key not in _LOCK_HELD:
        return
    now = time.time()
    if now - _LOCK_TOUCHED.get(key, 0) < 5:
        return
    _LOCK_TOUCHED[key] = now
    try:
        os.utime(p_lock(project_id, key[1]), None)
    except OSError:
        pass


def lock_owner(project_id: str, kind: str | None = None) -> dict | None:
    """Кто держит лок этого вида прямо сейчас. None – свободен или брошен."""
    kind = _kind(kind)
    fp = p_lock(project_id, kind)
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
        thread = _threads.get((project_id, kind))
        return {**data, "age": age, "mine": True} if (thread and thread.is_alive()) else None
    # Чужой процесс: живым считаем только пока лок «дышит».
    return {**data, "age": age, "mine": False} if age < LOCK_STALE_S else None


def _acquire_lock(project_id: str, kind: str, run_id: str) -> bool:
    if lock_owner(project_id, kind) is not None:
        return False
    fp = p_lock(project_id, kind)
    fp.write_text(json.dumps({"runId": run_id, "kind": kind, "ownerPid": os.getpid(),
                              "at": _now_iso()}, ensure_ascii=False), encoding="utf-8")
    _LOCK_HELD.add((project_id, kind))
    _LOCK_TOUCHED[(project_id, kind)] = time.time()
    return True


def _release_lock(project_id: str, kind: str | None = None) -> None:
    key = (project_id, _kind(kind))
    _LOCK_HELD.discard(key)
    _LOCK_TOUCHED.pop(key, None)
    p_lock(project_id, key[1]).unlink(missing_ok=True)


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
    """(файлов, городов) в очереди. folder: tasks | gis | actualize."""
    where = {"tasks": p_tasks(project_id),
             "gis": p_tasks_gis(project_id)}.get(folder) or p_tasks_actualize(project_id)
    files = _load_task_files(where)
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
    source: str = "tasks",
) -> tuple[bool, str]:
    """
    Запускает публикацию в фоновом потоке. Возвращает (запущено?, сообщение).
    Повторный вызов при активном прогоне ничего не делает – это и есть защита
    от «нажал кнопку дважды → опубликовалось дважды».

    source='gis' – очередь «только фото в 2ГИС», она лежит в своей папке.
    """
    with _lock:
        busy = busy_reason(project_id, "publish")
        if busy:
            return False, busy
        thread = _threads.get((project_id, "publish"))
        if thread and thread.is_alive():
            return False, "Предыдущая публикация ещё не завершилась."

        files = _load_task_files(tasks_folder(project_id, source))
        if not files:
            return False, "Очередь задач пуста."

        run_id = f"run-{int(time.time() * 1000)}"
        if not _acquire_lock(project_id, "publish", run_id):
            return False, ("Публикация уже идёт – запущена другим окном или другой копией Click. "
                           "Если та копия закрыта, подождите пару минут: лок освободится сам.")

        p_stop(project_id, "publish").unlink(missing_ok=True)
        p_live_log(project_id, "publish").write_text("", encoding="utf-8")

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
        _threads[(project_id, "publish")] = t
        t.start()
        return True, f"{publish_title(files)}: {total} городов."


def _gis_photos_for_city(project_id: str, browser, task: dict, local: list[str],
                         run_id: str, box: dict) -> dict:
    """
    Те же снимки отгрузки – ещё и в «Фото и видео» 2ГИС.

    Шаг полностью отдельный: что бы здесь ни случилось, статус публикации
    поста не меняется. Так же устроены «Товары» Яндекса – пост уже стоит в
    карточке, и объявлять его неудачным из-за фотографии было бы неправдой.
    """
    import gis_playwright as gis
    city = task.get("cityName", "?")
    out = {"requested": len(local), "uploaded": 0, "skipped": 0, "reason": ""}

    gis_url = task.get("gisUrl")
    if not gis_url:
        out["reason"] = "Нет карточки в 2ГИС"
        _append_log(project_id, "INFO", f"  📸 {city}: нет карточки в 2ГИС – фото не отправляли")
        return out

    # Сессия слетела на прошлом городе – остальные упрутся в ту же страницу
    # входа. Один раз сказали, дальше молча пропускаем.
    if box.get("off"):
        out["reason"] = box["off"]
        return out

    was = recent_gis_photos(project_id, gis_url, local)
    if was:
        when = str(was.get("at", ""))[:19].replace("T", " ")
        out["reason"] = f"Эти снимки уже заливали в 2ГИС {when} UTC – повтор пропущен"
        out["dup"] = True
        _append_log(project_id, "INFO", f"  ⏭ {city}: {out['reason']}")
        return out

    try:
        # Вкладку 2ГИС спрашиваем у браузера КАЖДЫЙ РАЗ, а не помним её сами.
        # Своя память тут уже стоила прогона: сторож памяти перезапустил
        # браузер на пятом городе, вкладка вместе с ним умерла – и все
        # оставшиеся 55 городов получили «Кабинет 2ГИС не открылся: Event loop
        # is closed». Браузер помнит вкладку сам и после перезапуска заводит
        # новую; здесь достаточно её попросить.
        res = gis.upload_media(browser.side_page(gis.session_path(project_id)),
                               gis_url, local, label=f"{city}: ")
    except Exception as e:  # noqa: BLE001 – шаг не должен ронять публикацию
        out["reason"] = f"Сбой шага 2ГИС: {e}"
        _append_log(project_id, "WARN", f"  📸 {city}: {out['reason']}")
        return out

    out.update(uploaded=res.get("uploaded", 0), skipped=res.get("skipped", 0),
               reason=res.get("reason", ""))
    if res.get("noSession"):
        box["off"] = "Сессия 2ГИС не действует – войдите в «Настройках»"
        _append_log(project_id, "WARN",
                    "  📸 Сессия 2ГИС не действует – фото в 2ГИС до конца прогона "
                    "не отправляем. Публикация в Яндекс идёт как обычно.")
    elif out["uploaded"]:
        _ledger_add(project_id, _photo_key(gis_url, local), task, "gis-photos", run_id)
    return out


def publish_title(files) -> str:
    """
    Как назвать прогон публикации человеку – одной строкой.

    Заказчик: «в отчёте пусть будет заголовок: типа публикация с добавлением
    фото в товары и 2ГИС, или публикация информационного поста». По одним
    цифрам отчёты и правда не различить – три подряд выглядят одинаково, а
    делали они разное.
    """
    import projects_data as pdata

    tasks = [t for _, cfg in files for t in (cfg.get("tasks") or [])]
    if not tasks:
        return "Публикация"
    if all(t.get("gisOnly") for t in tasks):
        return "Только фото в 2ГИС, без постов"

    types = {cfg.get("postType") for _, cfg in files if cfg.get("postType")}
    name = ""
    if len(types) == 1:
        one = next(iter(types))
        name = next((t["title"].lower() for t in pdata.POST_TYPES if t["id"] == one), "")
    title = f"Публикация · {name}" if name else "Публикация постов"

    extras = []
    if any(t.get("productPhotos") for t in tasks):
        extras.append("фото в «Товары»")
    if any(t.get("gisPhotos") for t in tasks):
        extras.append("фото в 2ГИС")
    return title + (" + " + " + ".join(extras) if extras else "")


def actualize_title(platform: str, with_reviews: bool) -> str:
    """Заголовок прогона актуализации – по тем же соображениям, что и у публикации."""
    where = PLATFORMS.get(platform, PLATFORMS[YANDEX])["short"]
    return f"Актуализация {where}" + (" + проверка отзывов" if with_reviews else "")


def _gis_only_city(project_id: str, browser, task: dict, run_id: str, box: dict) -> dict:
    """
    Только фото в «Фото и видео» 2ГИС – без поста в Яндекс.

    ВРЕМЕННЫЙ режим, заведённый для проверки: убедиться, что заливка в 2ГИС
    работает, не публикуя ради этого шестьдесят постов. Потом он сольётся
    обратно с фото Яндекса и отсюда уйдёт.

    Результат выглядит как обычная строка отчёта, чтобы её понимали и отчёт,
    и выгрузка CSV: статус города здесь – это судьба самих снимков, а не
    поста, которого не было.
    """
    city = task.get("cityName", "?")
    photos = task.get("productPhotos") or []
    started = time.time()
    base = {"cityName": city, "companyUrl": task.get("gisUrl") or task.get("companyUrl"),
            "steps": {}, "gisOnly": True}

    def done(status: str, reason: str, gis: dict | None = None) -> dict:
        return {**base, "status": status, "reason": reason,
                "durationMs": int((time.time() - started) * 1000),
                **({"gisPhotos": gis} if gis else {})}

    # Сюда попадают и задачи БЕЗ текста поста, которые в 2ГИС никто не звал:
    # такое бывает у файлов, слепленных прежней сборкой. Публиковать пустой
    # пост нельзя, поэтому честно говорим, что делать нечего.
    if not photos:
        return done("failed", "Пустой текст поста и нет снимков – публиковать нечего")

    _append_log(project_id, "INFO", f"  📸 {city}: только фото в 2ГИС, пост не публикуем")
    local, errors = yb.fetch_photos(list(photos), p_temp(project_id))
    if not local:
        return done("failed", "; ".join(errors) or "Нечего заливать: файлы не получены")

    gis = _gis_photos_for_city(project_id, browser, task, local, run_id, box)
    if gis.get("uploaded"):
        return done("ok", f"В 2ГИС добавлено {gis['uploaded']} из {gis['requested']}", gis)
    # Повтор тех же снимков в тот же город – это не поломка, а сработавший
    # реестр: в отчёте он должен читаться как «пропущено», а не «ошибка».
    if gis.get("dup"):
        return done("skipped-duplicate", gis.get("reason", ""), gis)
    return done("failed", gis.get("reason") or "Фото в 2ГИС не добавились", gis)


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
    _CUR.kind = "publish"
    yb.set_logger(lambda level, msg: _append_log(project_id, level, msg, kind="publish"))

    started_at = _now_iso()
    start_ts = time.time()
    report_name = f"report-{apptime.stamp('%Y-%m-%dT%H-%M-%S')}.json"
    report_path = p_reports(project_id) / report_name

    results: list[dict] = []
    counters = {"ok": 0, "noImage": 0, "unknown": 0, "failed": 0, "skipped": 0, "retried": 0, "cooldowns": 0}
    total = sum(len(cfg.get("tasks") or []) for _, cfg in files)
    stopped = False
    processed_files: list[Path] = []
    account = (files[0][1].get("credentials") or {}).get("email", "") if files else ""

    # Чем этот прогон занят – одной строкой, в лог и в отчёт. По цифрам отчёты
    # не различаются, а делают они разное.
    title = publish_title(files)
    # Прогон целиком про 2ГИС – в Яндекс не уйдёт ни один пост. Тогда и
    # аккаунт Яндекса проверять незачем: сессия нужна другая, 2ГИС.
    gis_only_run = bool(total) and all(t.get("gisOnly")
                                       for _, cfg in files for t in (cfg.get("tasks") or []))

    def should_stop() -> bool:
        return p_stop(project_id).exists()

    # Вторая вкладка с сессией 2ГИС открывается лениво – только если в задачах
    # есть фото и галочка «ещё в 2ГИС». Пустой прогон лишней памяти не берёт.
    gis_box: dict = {"page": None, "off": ""}

    def save_report(state: str) -> None:
        report = {
            "startedAt": started_at,
            "finishedAt": _now_iso(),
            "durationSec": int(time.time() - start_ts),
            "account": expected_email or account,
            "stoppedByUser": stopped,
            "state": state,
            "runId": run_id,
            "title": title,
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
        if gis_only_run:
            _append_log(project_id, "INFO",
                        f"{title.upper()} · {total} городов · посты не публикуются")
        else:
            _append_log(project_id, "INFO",
                        f"{title.upper()} · {total} городов · аккаунт {expected_email or account or '–'}")
        _append_log(project_id, "INFO", "═" * 46)
        save_report("in-progress")

        browser.start()
        _append_log(project_id, "INFO", "🌐 Браузер запущен")

        # ── Защита от публикации не с того аккаунта ──
        # Останавливаемся ТОЛЬКО если нашли чужой аккаунт или сессии нет вовсе.
        # «Определить не удалось» – это неудача проверки, а не повод не работать:
        # раньше из-за этого прогон падал с «залогинен НЕ ТОТ аккаунт, найдено:
        # не определено» при полностью правильном аккаунте.
        if expected_email and not gis_only_run:
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

                # Сторож памяти. У актуализации он стоял с самого начала, а у
                # публикации его не было вовсе – и падала она молча: облако
                # убивало приложение целиком, в логе ни строчки, на экране
                # «Oh no». Сторож один и тот же: перевалило за мягкий порог –
                # перезапускаем браузер, за жёсткий – аккуратно встаём.
                used = memory_mb()
                _, soft_at, hard_at = mem_gates()
                if used > hard_at:
                    stopped = True
                    _append_log(project_id, "WARN",
                                f"⏹  ПАМЯТЬ · занято {used} МБ – останавливаюсь, чтобы не "
                                "уронить приложение. Отправленное сохранено в отчёте, "
                                "оставшиеся города можно догнать следующим прогоном.")
                    break
                if used > soft_at:
                    _append_log(project_id, "WARN",
                                f"  ♻️ ПАМЯТЬ · занято {used} МБ – перезапускаю браузер, "
                                "это вернёт несколько сотен мегабайт")
                    try:
                        browser.restart()
                    except Exception as e:  # noqa: BLE001 – перезапуск не должен ронять прогон
                        _append_log(project_id, "WARN", f"  ♻️ перезапуск не вышел: {e}")

                processed += 1
                city = task.get("cityName", "?")
                # В полосу прогресса идёт число ЗАКОНЧЕННЫХ городов, а не номер
                # текущего: иначе на первом же городе пишется «1 из 1», человек
                # видит «готово» и ждёт отчёт, которого ещё нет.
                push_state("running", len(results), city)

                # Временный режим «только фото в 2ГИС»: поста нет, значит нет
                # ни публикации, ни «Товаров» Яндекса – только заливка. Стоит
                # ДО защиты от дубля: та стережёт повтор ПОСТА, а поста тут
                # нет вовсе. Свой сторож у снимков есть – реестр 2ГИС.
                #
                # Пустой текст сюда же и по той же причине: публиковать нечего.
                # Это не придирка. Прогон, доставшийся от прежней сборки, не
                # знал про gisOnly – и отправил в Москву, Петербург и
                # Новосибирск ПУСТЫЕ посты по задачам «только фото в 2ГИС».
                # Пустой пост в карточке заказчика хуже любой строки в отчёте,
                # и чинить это надо здесь, а не только там, откуда задача
                # приехала.
                if task.get("gisOnly") or not (task.get("postText") or "").strip():
                    res = _gis_only_city(project_id, browser, task, run_id, gis_box)
                    res["country"] = country
                    res["package"] = country
                    res["_task"] = task
                    results.append(res)
                    tally(res["status"])
                    icon = {"ok": "✅", "skipped-duplicate": "⏭"}.get(res["status"], "❌")
                    _append_log(project_id, "INFO",
                                f"  {icon} ИТОГ [{processed}/{total}] {city}: {res['reason']}")
                    save_report("in-progress")
                    push_state("running", len(results), city)
                    continue

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
                    # Качаем ОДИН раз: те же файлы уходят и в «Товары» Яндекса,
                    # и в «Фото и видео» 2ГИС.
                    local, dl_errors = yb.fetch_photos(list(photos), p_temp(project_id))
                    try:
                        pr = yb.upload_product_photos(browser.page, task, list(photos),
                                                      p_temp(project_id), local_files=local)
                        pr["failed"] = pr.get("failed", 0) + len(dl_errors)
                        pr.setdefault("errors", []).extend(dl_errors)
                        res["productPhotos"] = {"requested": len(photos), **pr}
                    except Exception as e:  # noqa: BLE001
                        _append_log(project_id, "WARN", f"  ⚠️ Фото в «Товары» упали: {e}")
                        res["productPhotos"] = {"requested": len(photos), "uploaded": 0,
                                                "failed": len(photos), "errors": [str(e)]}
                    if task.get("gisPhotos") and local:
                        res["gisPhotos"] = _gis_photos_for_city(
                            project_id, browser, task, local, run_id, gis_box)

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
        # Вторая сессия (2ГИС) закрывается со своими куками: 2ГИС продлевает
        # их по ходу работы, и терять продление незачем.
        browser.close_side()
        save_report("finished")
        push_state("stopped" if stopped else "done", len(results), "")
        _append_log(project_id, "INFO", "═" * 46)
        _append_log(project_id, "INFO",
                    f"ИТОГИ · ✅ {counters['ok']} · 🟡 {counters['noImage']} · ⚠️ {counters['unknown']} "
                    f"· ❌ {counters['failed']} · ⏭ {counters['skipped']}")
        gis_done = sum((r.get("gisPhotos") or {}).get("uploaded", 0) for r in results)
        gis_asked = sum(1 for r in results if r.get("gisPhotos"))
        if gis_asked:
            _append_log(project_id, "INFO",
                        f"ФОТО В 2ГИС · снимков добавлено {gis_done} · городов {gis_asked}")
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
        fp = p_screenshots(project_id) / f"{apptime.stamp('%H-%M-%S')}-{safe}.png"
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
    # Задания «только фото в 2ГИС» второй проход не касается вовсе: он про
    # ленту постов Яндекса, а поста тут не было. Иначе город без карточки
    # 2ГИС уезжал на публикацию – ровно то, чего этот режим и не должен
    # делать.
    candidates = [r for r in results
                  if r["status"] in ("failed", "unknown") and not r.get("gisOnly")]
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
                    with_reviews: bool = False, platform: str = YANDEX) -> tuple[bool, str]:
    """
    with_reviews – заодно собрать неотвеченные отзывы и черновики ответов.
    platform – 'yandex' (по умолчанию) или 'gis'. У площадок свои города, свои
    файлы прогона и свой лок: прогон 2ГИС не мешает яндексовой очереди.
    """
    kind = PLATFORMS[platform]["kind"]
    with _lock:
        busy = busy_reason(project_id, kind)
        if busy:
            return False, busy
        thread = _threads.get((project_id, kind))
        if thread and thread.is_alive():
            return False, "Предыдущая актуализация ещё не завершилась."

        files = _load_task_files(p_tasks_actualize(project_id, platform))
        if not files:
            return False, "Список городов для актуализации пуст."

        run_id = f"act-{int(time.time() * 1000)}"
        if not _acquire_lock(project_id, kind, run_id):
            return False, ("Актуализация уже идёт – запущена другим окном или другой копией Click. "
                           "Если та копия закрыта, подождите пару минут: лок освободится сам.")

        p_stop(project_id, kind).unlink(missing_ok=True)
        p_live_log(project_id, kind).write_text("", encoding="utf-8")

        total = sum(len(cfg.get("tasks") or []) for _, cfg in files)
        _write_state(project_id, {
            "runId": run_id, "action": kind, "status": "running",
            "ownerPid": os.getpid(), "startedAt": _now_iso(), "finishedAt": None,
            "total": total, "current": 0, "currentCity": "", "reportName": None,
            "totals": {"total": 0, "actualized": 0, "notNeeded": 0, "statusMismatch": 0, "failed": 0},
            "error": None,
        })

        t = threading.Thread(target=_actualize_worker,
                             args=(project_id, run_id, files, headless, delay_s, with_reviews,
                                   platform),
                             daemon=True, name=f"click-{kind}-{project_id}")
        _threads[(project_id, kind)] = t
        t.start()
        tail = " + отзывы" if with_reviews else ""
        where = PLATFORMS[platform]["ru"]
        return True, f"Актуализация {where} запущена: {total} городов{tail}."


def _reviews_for_city(project_id: str, page, task: dict, prompt: str,
                      should_stop=None, budget: dict | None = None,
                      run_id: str = "", platform: str = YANDEX) -> dict:
    """
    Шаг по отзывам для одного города: прочитать раздел отзывов, отобрать
    неотвеченные и написать черновики. Ничего не публикует.

    Возвращает {'items': [...в очередь...], 'summary': 'что вышло словами'}.
    Исключения наружу не пускает: сломавшийся шаг по отзывам не должен
    ронять уже работающую актуализацию этого же города.

    Площадки отличаются только чтением: правила отбора (пять звёзд – пишем,
    ниже – человеку, без текста – молча мимо) и промпт у них общие.
    """
    import reviews as rv

    city = task.get("cityName") or "?"
    company_url = task.get("companyUrl")
    out = {"items": [], "summary": "", "found": 0, "drafted": 0, "needsHuman": 0,
           "noDraft": 0, "foreign": 0}

    if platform == GIS:
        import gis_playwright as gis
        url = gis.build_reviews_url(company_url)
        data = gis.read_reviews(page, url, org_id=gis.extract_org_id(company_url))
        total = data.get("shown", 0)
    else:
        url = yb.build_reviews_url(company_url, task.get("companyId"))
        data = yb.read_reviews(page, url)
        total = data.get("total", 0)

    if not data["ok"]:
        out["summary"] = data["reason"]
        out["noSession"] = bool(data.get("noSession"))   # прогону – знак остановиться
        _append_log(project_id, "WARN", f"  💬 {city}: отзывы не прочитаны – {data['reason']}")
        return out

    page_url = data.get("url") or url
    items = data["items"]

    # 2ГИС показывает отзывы ещё и с Flamp, Otello, Booking и прочих площадок.
    # Ответить на них из кабинета нельзя – там вместо кнопки ссылка «Посмотреть
    # на …». Такие отзывы собираем и отдаём человеку: молча их потерять хуже,
    # чем показать со ссылкой.
    foreign = []
    if platform == GIS:
        import gis_playwright as gis
        # Отвеченные сюда попадать не должны вовсе – ни свои, ни чужие.
        items = [it for it in items if not it.get("answered")]
        foreign = [it for it in items if not gis.is_own(it)]
        items = [it for it in items if gis.is_own(it)]

    box = rv.triage(items)
    out["found"] = len(box["unanswered"]) + len(foreign)

    if not box["unanswered"] and not foreign:
        out["summary"] = ("отзывов пока нет" if not total
                          else f"отзывов {total}, все с ответом")
        _append_log(project_id, "INFO", f"  💬 {city}: {out['summary']}")
        return out

    def add(item, status, draft="", note=""):
        # runId помечает, ЧЬИ это отзывы. Без метки страница показывала
        # разобранное вперемешку со всеми прошлыми прогонами: заказчик только
        # собрала новые, а видела «3 отправленных» с какого-то прошлого раза.
        row = rv.as_queue_item(
            item, project_id=project_id, city=city, company_url=company_url,
            reviews_url=page_url, status=status, draft=draft, note=note,
            platform=platform)
        if run_id:
            row["runId"] = run_id
        out["items"].append(row)

    for item in box["no_text"]:
        pass  # одни звёзды без текста – в очередь не берём (решение заказчика)

    for item in foreign:
        where = item.get("platform") or "другой площадке"
        row_note = f"отзыв с {where} – ответить можно только там"
        add(item, rv.NEEDS_HUMAN, note=row_note)
        out["foreign"] += 1
        out["needsHuman"] += 1
        if item.get("foreignUrl"):
            out["items"][-1]["reviewsUrl"] = item["foreignUrl"]

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
        # Остановку слушаем МЕЖДУ ЧЕРНОВИКАМИ, а не только между городами.
        # Раньше проверка стояла лишь на границе города, и на карточке с
        # десятком отзывов «Остановить» отзывалось минут через пять: каждый
        # черновик – до полутора минут. Заказчик так и сказала: «нажала, но
        # ничего не произошло, как будто надо перезагрузить страницу».
        if should_stop and should_stop():
            add(item, rv.NO_DRAFT, note="прогон остановлен – черновик не писали")
            out["noDraft"] += 1
            continue
        # Лимит Gemini уже исчерпан – новую попытку не делаем вовсе. Смысла в
        # ней нет: ответ будет тот же, а полторы минуты потеряны. Отзыв
        # остаётся в очереди, черновик допишется кнопкой «Переписать все».
        if budget and budget.get("giveUp"):
            add(item, rv.NO_DRAFT, note="лимит Gemini исчерпан – нажмите «Переписать все» позже")
            out["noDraft"] += 1
            continue
        try:
            import llm
            t0 = time.time()
            draft = rv.clean_draft(llm.generate(rv.build_prompt(prompt, item, project_id)),
                                   project_id, rv.review_text(item))
            add(item, rv.DRAFTED, draft=draft)
            out["drafted"] += 1
            # Замер в лог: без него «долго генерирует» не отличить от
            # «упёрлись в лимит и ждём» – а лечится это по-разному.
            st = getattr(llm, "last_stats", {}) or {}
            slow = time.time() - t0
            if slow > 8:
                # Сколько ключей в деле – без этого «несколько ключей не помогают»
                # не проверить: в логе видно только паузу, а не причину.
                keys_note = f"ключей {st.get('keys', '?')}"
                if st.get("shared"):
                    keys_note += " с ОБЩИМ лимитом (один проект Google)"
                _append_log(project_id, "WARN",
                            f"  💬 {city}: черновик за {slow:.0f} сек "
                            f"(модель {st.get('model') or '?'}, запросов {st.get('calls', 1)}, "
                            f"{keys_note}, темп {st.get('pace', '?')} запросов/мин) – "
                            "ждём своей очереди у Gemini")
            if budget is not None:
                budget["limitStreak"] = 0
        except Exception as e:  # noqa: BLE001 – причина уже человеческая
            add(item, rv.NO_DRAFT, note=str(e))
            out["noDraft"] += 1
            _append_log(project_id, "WARN", f"  💬 {city}: черновик не вышел – {e}")
            # Подряд упёрлись в лимит – дальше не пробуем до конца прогона.
            # В логе это выглядело так: четыре одинаковых отказа по одному
            # городу, шесть минут впустую. Заказчик спросила прямо: «какой
            # смысл?». Смысла нет – квота за минуту сама не появится.
            if budget is not None and getattr(e, "is_limit", False):
                budget["limitStreak"] = budget.get("limitStreak", 0) + 1
                if budget["limitStreak"] >= LIMIT_GIVE_UP and not budget.get("giveUp"):
                    budget["giveUp"] = True
                    _append_log(project_id, "WARN",
                                "  💬 ЛИМИТ · Gemini больше не отвечает по квоте – черновики "
                                "до конца прогона не пишем, отзывы всё равно собираем. "
                                "Допишутся кнопкой «Переписать все», когда квота вернётся.")

    bits = [f"без ответа {out['found']}"]
    if out["drafted"]:
        bits.append(f"черновиков {out['drafted']}")
    if out["needsHuman"]:
        bits.append(f"человеку {out['needsHuman']}")
    if out["noDraft"]:
        bits.append(f"без черновика {out['noDraft']}")
    if box["no_text"]:
        bits.append(f"без текста пропущено {len(box['no_text'])}")
    if out["foreign"]:
        bits.append(f"на чужих площадках {out['foreign']}")
    if data.get("total", 0) > data.get("shown", 0):
        bits.append(f"за первой страницей осталось {data['total'] - data['shown']}")
    out["summary"] = ", ".join(bits)
    _append_log(project_id, "INFO", f"  💬 {city}: {out['summary']}")
    return out


# Статус actualize_city → ключ в counters. Один словарь на оба места, где
# он нужен, – иначе при добавлении нового статуса легко забыть про второе.
_ACT_COUNTER_KEY = {"actualized": "actualized", "not-needed": "notNeeded",
                    "status-mismatch": "statusMismatch"}


def _actualize_worker(project_id: str, run_id: str, files, headless: bool, delay_s: float,
                      with_reviews: bool = False, platform: str = YANDEX) -> None:
    kind = PLATFORMS[platform]["kind"]
    _CUR.kind = kind
    # Логгер общий: 2ГИС пишет в лог через те же yb.info/warn, и строки
    # попадают в живой лог своего прогона.
    yb.set_logger(lambda level, msg: _append_log(project_id, level, msg, kind=kind))
    started_at = _now_iso()
    start_ts = time.time()
    report_name = f"actualize-{apptime.stamp('%Y-%m-%dT%H-%M-%S')}.json"
    report_path = p_reports_actualize(project_id, platform) / report_name

    results: list[dict] = []
    # statusMismatch – КП и площадка расходятся (см. actualize_city в
    # yb_playwright/gis_playwright): не сбой прогона, а неверный статус
    # в таблице. Врозь с failed, иначе «Проверьте таблицу» тонет среди
    # настоящих поломок.
    counters = {"actualized": 0, "notNeeded": 0, "statusMismatch": 0, "failed": 0}
    review_totals = {"found": 0, "drafted": 0, "needsHuman": 0, "noDraft": 0}
    collected: list[dict] = []
    # Общий на весь прогон счётчик отказов Gemini по лимиту. Упёрлись дважды
    # подряд – черновики до конца прогона не пишем: квота за минуту сама не
    # вернётся, а каждая попытка стоит до полутора минут.
    review_budget: dict = {"limitStreak": 0, "giveUp": False}
    total = sum(len(cfg.get("tasks") or []) for _, cfg in files)
    stopped = False
    processed = 0

    def should_stop() -> bool:
        return p_stop(project_id).exists()

    def save_report(state: str) -> None:
        payload = {
            "type": "actualize", "platform": platform,
            "title": actualize_title(platform, with_reviews),
            "startedAt": started_at, "finishedAt": _now_iso(),
            "durationSec": int(time.time() - start_ts), "stoppedByUser": stopped,
            "state": state, "runId": run_id,
            "totals": {"total": len(results), **counters},
            "results": results,
        }
        if with_reviews:
            payload["withReviews"] = True
            payload["reviewTotals"] = dict(review_totals)
            # Сами отзывы кладём в отчёт, а не только их количество. Иначе
            # выгрузить прогон целиком нельзя: отправка идёт ПОСЛЕ прогона,
            # и что с каждым ответом стало, знает только очередь. Отчёт
            # хранит найденное, очередь – случившееся; при выгрузке одно
            # дополняется другим, и получается общий отчёт с отзывами.
            payload["reviews"] = [
                {k: it.get(k) for k in
                 ("reviewId", "city", "author", "rating", "text", "draft",
                  "status", "note", "reviewsUrl")}
                for it in collected
            ]
        _write_atomic(report_path, json.dumps(payload, ensure_ascii=False, indent=2))
        if state != "in-progress":
            _snapshot_log(project_id, report_path)

    def push_state(status: str, city: str, err: str | None = None) -> None:
        _write_state(project_id, {
            "runId": run_id, "action": kind, "status": status, "ownerPid": os.getpid(),
            "startedAt": started_at, "finishedAt": None if status == "running" else _now_iso(),
            # Считаем законченные города, а не текущий номер – см. публикацию.
            "total": total, "current": len(results), "currentCity": city, "reportName": report_name,
            "totals": {"total": len(results), **counters}, "error": err,
        })

    if platform == GIS:
        import gis_playwright as gis
        browser = gis.browser(project_id, headless=headless)
        actualize_one = gis.actualize_city
    else:
        browser = yb.YbBrowser(project_id, headless=headless)
        actualize_one = yb.actualize_city
    prompt = ""
    if with_reviews:
        import reviews as rv
        prompt = rv.project_prompt(project_id)

    try:
        head = f"АКТУАЛИЗАЦИЯ {PLATFORMS[platform]['ru'].upper()} · {total} городов"
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
                # Сторож памяти. Раньше прогон шёл вслепую до тех пор, пока
                # облако не убивало приложение целиком – без единой строки в
                # логе, со всеми чужими вкладками заодно. Теперь беда видна
                # заранее и лечится по-хорошему.
                used = memory_mb()
                _, soft_at, hard_at = mem_gates()
                if used > hard_at:
                    stopped = True
                    _append_log(project_id, "WARN",
                                f"⏹  ПАМЯТЬ · занято {used} МБ – останавливаюсь, чтобы не "
                                "уронить приложение. Сделанное сохранено в отчёте, "
                                "оставшиеся города можно догнать следующим прогоном.")
                    break
                if used > soft_at:
                    _append_log(project_id, "WARN",
                                f"  ♻️ ПАМЯТЬ · занято {used} МБ – перезапускаю браузер, "
                                "это вернёт несколько сотен мегабайт")
                    try:
                        browser.restart()
                    except Exception as e:  # noqa: BLE001 – перезапуск не должен ронять прогон
                        _append_log(project_id, "WARN", f"  ♻️ перезапуск не вышел: {e}")
                processed += 1
                push_state("running", task.get("cityName", ""))
                try:
                    res = actualize_one(browser.page, task, i, len(city_tasks))
                except Exception as e:  # noqa: BLE001
                    res = {"cityName": task.get("cityName"), "companyUrl": task.get("companyUrl"),
                           "status": "failed", "reason": f"Критическая ошибка: {e}", "durationMs": 0}
                res["country"] = country
                res["package"] = country

                # Снимок экрана на неудачу. Разбирать «плашка была или нет» по
                # одной строчке в отчёте невозможно: отчёт говорит «нет», глаза
                # говорят «есть», и проверить нечем. По картинке – минута.
                if res.get("status") == "failed":
                    shot = _save_failure_screenshot(project_id, browser, task.get("cityName", ""))
                    if shot:
                        res["screenshot"] = shot.name
                        _append_log(project_id, "INFO", f"  📸 Скриншот сбоя: {shot.name}")

                # Сессия слетела – дальше идти незачем: остальные города упрутся
                # в ту же страницу входа. Раньше прогон честно обходил все 58
                # городов, писал каждому «актуализация не требуется» (кнопки на
                # странице входа нет) и складывал эту неправду в отчёт.
                if res["status"] == "no-session":
                    results.append(res)
                    counters["failed"] += 1
                    gen, short = PLATFORMS[platform]["gen"], PLATFORMS[platform]["short"]
                    _append_log(project_id, "ERROR",
                                f"❌ ОСТАНОВКА: сессия {gen} не активна – вместо кабинета "
                                "открывается страница входа")
                    save_report("finished")
                    push_state("error", task.get("cityName", ""),
                               f"Сессия {gen} не активна: вместо кабинета открывается страница "
                               f"входа. Зайдите в «Настройки» и войдите в {short} заново.")
                    return

                # Отзывы – отдельным шагом ПОСЛЕ актуализации: он опциональный
                # и не должен влиять на её статус, что бы там ни случилось.
                if with_reviews:
                    try:
                        rr = _reviews_for_city(project_id, browser.page, task, prompt,
                                               should_stop, review_budget, run_id, platform)
                    except Exception as e:  # noqa: BLE001
                        rr = {"items": [], "summary": f"сбой шага отзывов: {e}",
                              "found": 0, "drafted": 0, "needsHuman": 0, "noDraft": 0}
                        _append_log(project_id, "WARN",
                                    f"  💬 {task.get('cityName', '?')}: {rr['summary']}")
                    res["reviews"] = rr["summary"]
                    for k in review_totals:
                        review_totals[k] += rr.get(k, 0)
                    # Тот же случай, но обнаружился на шаге отзывов: страница
                    # «Данные» ещё открывалась, а отзывы уже уводят на вход.
                    if rr.get("noSession"):
                        results.append(res)
                        counters[_ACT_COUNTER_KEY.get(res["status"], "failed")] += 1
                        gen = PLATFORMS[platform]["gen"]
                        _append_log(project_id, "ERROR",
                                    f"❌ ОСТАНОВКА: сессия {gen} не действует – раздел отзывов "
                                    "открывает страницу входа")
                        save_report("finished")
                        push_state("error", task.get("cityName", ""),
                                   f"Сессия {gen} не действует: вместо отзывов открывается "
                                   "страница входа. Зайдите в «Настройки» и войдите заново.")
                        return
                    if rr["items"]:
                        collected.extend(rr["items"])
                        # Пишем локально после каждого города: оборванный прогон
                        # не должен уносить с собой уже написанные черновики.
                        try:
                            import reviews as rv
                            rv.save_queue(project_id,
                                          rv.merge(rv.load_queue(project_id, platform), collected),
                                          platform=platform)
                        except Exception as e:  # noqa: BLE001
                            _append_log(project_id, "WARN", f"  💬 очередь не сохранилась: {e}")
                results.append(res)
                counters[_ACT_COUNTER_KEY.get(res["status"], "failed")] += 1
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
                    f"ИТОГИ · ✅ {counters['actualized']} · ⊝ {counters['notNeeded']} · "
                    f"🚦 {counters['statusMismatch']} · ❌ {counters['failed']} · "
                    f"память {memory_mb()} МБ")
        if with_reviews:
            _append_log(project_id, "INFO",
                        f"ОТЗЫВЫ · без ответа {review_totals['found']} · "
                        f"черновиков {review_totals['drafted']} · "
                        f"человеку {review_totals['needsHuman']} · "
                        f"без черновика {review_totals['noDraft']}")
            # Отдельной строкой, крупно: если ключи сидят на одной квоте, все
            # жёлтые предупреждения выше лечатся не паузами в коде, а новым
            # ключом в НОВОМ проекте Google. Без этой строки не догадаться.
            try:
                import llm
                if llm.shared_quota():
                    _append_log(project_id, "WARN",
                                "КЛЮЧИ · лимит у ключей Gemini общий – они созданы в одном "
                                "проекте Google, а лимит считается на проект, а не на ключ. "
                                "Скорости такие ключи не добавляют: новый ключ нужно "
                                "создавать в НОВОМ проекте.")
            except Exception:  # noqa: BLE001 – диагностика не должна ронять прогон
                pass
            # Один раз наружу, в конце: в облаке файлы не переживают
            # перезапуск, а коммитить на каждый город – это 140 коммитов.
            if collected:
                try:
                    import reviews as rv
                    where = rv.save_queue(project_id,
                                          rv.merge(rv.load_queue(project_id, platform), collected),
                                          push=True, platform=platform)
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
#  СБОР ОРГАНИЗАЦИЙ (для сверки с КП)
# ════════════════════════════════════════════════════════════════════

def _store_key_companies(project_id: str) -> str:
    return f"companies-{project_id}"


def load_companies(project_id: str) -> dict:
    """
    Последний собранный список организаций аккаунта.

    Хранится снаружи (репозиторий данных), поэтому переживает перезапуск
    облака: собирать 200 карточек заново из-за перезагрузки контейнера –
    впустую потраченные десять минут.
    """
    try:
        import repo_store
        data = repo_store.load(_store_key_companies(project_id))
    except Exception:  # noqa: BLE001
        data = None
    return data or {}


def _save_companies(project_id: str, payload: dict) -> str:
    try:
        import repo_store
        return repo_store.save(_store_key_companies(project_id), payload,
                               f"Click: организации {project_id}")
    except Exception as e:  # noqa: BLE001
        return f"сохранить наружу не вышло: {e}"


def start_collect(project_id: str, headless: bool = True, with_cards: bool = False,
                  card_ids: list[str] | None = None,
                  must_ids: list[str] | None = None) -> tuple[bool, str]:
    """
    Собрать организации аккаунта из раздела «Организации».

    with_cards – заодно открыть карточки: список отдаёт те же поля, но если
    Яндекс что-то в нём придержит, карточка покажет всё наверняка.
    card_ids – открыть только эти карточки (например, только спорные).
    """
    with _lock:
        busy = busy_reason(project_id, "collect")
        if busy:
            return False, busy
        thread = _threads.get((project_id, "collect"))
        if thread and thread.is_alive():
            return False, "Предыдущее чтение организаций ещё не завершилось."

        run_id = f"col-{int(time.time() * 1000)}"
        if not _acquire_lock(project_id, "collect", run_id):
            return False, ("Организации уже читаются – запущено другим окном или другой копией "
                           "Click. Если та копия закрыта, подождите пару минут: лок освободится сам.")

        p_stop(project_id, "collect").unlink(missing_ok=True)
        p_live_log(project_id, "collect").write_text("", encoding="utf-8")
        _write_state(project_id, {
            "runId": run_id, "action": "collect", "status": "running",
            "ownerPid": os.getpid(), "startedAt": _now_iso(), "finishedAt": None,
            "total": 0, "current": 0, "currentCity": "", "reportName": None,
            "totals": {}, "error": None,
        })
        t = threading.Thread(target=_collect_worker,
                             args=(project_id, run_id, headless, with_cards,
                                   list(card_ids or []), list(must_ids or [])),
                             daemon=True, name=f"click-collect-{project_id}")
        _threads[(project_id, "collect")] = t
        t.start()
        return True, "Читаю организации в Яндексе…"


def _collect_worker(project_id: str, run_id: str, headless: bool,
                    with_cards: bool, card_ids: list[str],
                    must_ids: list[str] | None = None) -> None:
    _CUR.kind = "collect"
    yb.set_logger(lambda level, msg: _append_log(project_id, level, msg, kind="collect"))
    started_at = _now_iso()
    start_ts = time.time()
    companies: list[dict] = []

    def push_state(status: str, current: int = 0, total: int = 0,
                   what: str = "", err: str | None = None) -> None:
        _write_state(project_id, {
            "runId": run_id, "action": "collect", "status": status, "ownerPid": os.getpid(),
            "startedAt": started_at, "finishedAt": None if status == "running" else _now_iso(),
            "total": total, "current": current, "currentCity": what, "reportName": None,
            "totals": {"companies": len(companies)}, "error": err,
        })

    browser = yb.YbBrowser(project_id, headless=headless)
    try:
        _append_log(project_id, "INFO", "СБОР ОРГАНИЗАЦИЙ · читаю раздел «Организации»")
        browser.start()
        page = browser.page
        сколько_обещал = {}
        companies = yb.collect_companies(page, log_every=1, stats=сколько_обещал)
        _append_log(project_id, "INFO", f"СОБРАНО · организаций: {len(companies)}")
        push_state("running", len(companies), len(companies), "")

        # Список организаций Яндекса отдаёт НЕ ВСЁ. У заказчика в разделе
        # «Организации» два Красноярска, а список приносит один: второй
        # заведён как онлайн-организация и в выдачу не попадает. Сколько
        # ни перечитывай – его там не будет, и дубль не найдётся никогда.
        #
        # Поэтому добираем поимённо: в КП у города записана ссылка на
        # карточку, её номер и берём. Нет такого номера среди собранных –
        # открываем карточку напрямую и дописываем в список.
        известные: set[str] = set()
        for co in companies:
            известные |= {str(co.get(k) or "") for k in ("id", "permanentId", "tycoonId")}
        известные.discard("")
        добрать = [i for i in dict.fromkeys(must_ids or []) if i and i not in известные]
        if добрать:
            _append_log(project_id, "INFO",
                        f"ДОБИРАЮ · в списке нет {len(добрать)} карточек из КП – "
                        "открываю их по ссылкам")
        for n, cid in enumerate(добрать, 1):
            if p_stop(project_id).exists():
                break
            card = yb.read_company_card(page, f"https://yandex.ru/sprav/{cid}/p/edit/")
            if card.get("error"):
                _append_log(project_id, "WARN", f"  🟡 карточка {cid} · {card['error']}")
                continue
            card.setdefault("id", cid)
            card["fromCard"] = True
            card["fromLink"] = True          # пришла по ссылке, а не из списка
            companies.append(card)
            _append_log(project_id, "INFO",
                        f"  ➕ {n}/{len(добрать)} · {card.get('name', '')} · "
                        f"{card.get('address', '')[:60]}")
            _sleep_interruptible(0.4, lambda: p_stop(project_id).exists())
        if добрать:
            _append_log(project_id, "INFO", f"ВСЕГО · организаций: {len(companies)}")
            push_state("running", len(companies), len(companies), "")

        # Карточки открываем по просьбе – это долго, по 3-4 секунды на каждую.
        wanted = [c for c in companies
                  if not card_ids or str(c.get("id")) in set(card_ids)] if (with_cards or card_ids) else []
        for n, co in enumerate(wanted, 1):
            if p_stop(project_id).exists():
                _append_log(project_id, "WARN", "⏹ Остановлено человеком")
                break
            card = yb.read_company_card(page, f"https://yandex.ru/sprav/{co.get('id')}/p/edit/")
            if card.get("error"):
                _append_log(project_id, "WARN", f"  🟡 {co.get('name')} · {card['error']}")
            else:
                co.update({k: v for k, v in card.items() if v not in ("", [], {}, None)})
                co["fromCard"] = True
            _append_log(project_id, "INFO",
                        f"  📇 {n}/{len(wanted)} · {co.get('name', '')} · {co.get('address', '')[:60]}")
            push_state("running", n, len(wanted), co.get("name", ""))
            _sleep_interruptible(0.4, lambda: p_stop(project_id).exists())

        payload = {"collectedAt": _now_iso(), "count": len(companies),
                   "withCards": bool(wanted), "companies": companies,
                   # Сколько организаций насчитал сам Яндекс. Если меньше, чем
                   # видно в кабинете, – список отдал не всё, и это надо
                   # показать в отчёте, а не гадать.
                   "yandexTotal": int(сколько_обещал.get("total") or 0),
                   "build": BUILD}
        where = _save_companies(project_id, payload)
        _append_log(project_id, "INFO", f"ИТОГИ · организаций {len(companies)} · {where}")
        browser.save_session()
        push_state("done", len(companies), len(companies), "")
    except Exception as e:  # noqa: BLE001
        _append_log(project_id, "ERROR", f"💥 Не собралось: {e}")
        push_state("error", 0, 0, "", str(e))
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

def _report_folder(project_id: str, kind: str) -> Path:
    if kind == "publish":
        return p_reports(project_id)
    return p_reports_actualize(project_id, GIS if kind == "actualize-gis" else YANDEX)


def list_reports(project_id: str, kind: str = "publish", limit: int = 30) -> list[dict]:
    folder = _report_folder(project_id, kind)
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
    folder = _report_folder(project_id, kind)
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
