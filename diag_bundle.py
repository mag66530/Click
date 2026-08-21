"""
diag_bundle.py – собрать ВСЮ диагностику последнего формирования в один .zip.

Зачем. Когда что-то в кросспостинге идёт не так, для точной правки мне нужны
сразу три вещи: пошаговый лог (что Click делал, какими селекторами, куда
кликал, сколько раз), скриншоты КАЖДОГО шага (что было на экране) и кусок
настоящей разметки страницы (по нему доводятся селекторы). Раньше это лежало
разными файлами в служебной папке, и человек присылал их по одному —
несколько кругов переписки. Этот модуль пакует всё рядом с логом в один
архив: одна кнопка → один файл → мне видно всё с первого раза.

Что внутри архива:
  ОПИСАНИЕ.txt   – окружение (сборка, ОС, движок, прокси), список файлов с
                   размером и временем, и хвост лога для быстрого взгляда;
  form-last.log  – пошаговый протокол последнего формирования;
  tg-step*.png   – скриншоты шагов 1–6 (успешный путь);
  tg-debug-*.png – кадр в момент падения (если было);
  tg-*.html / *-editor.html / … – сохранённая разметка страниц.

Модуль offline и без сети: только читает служебную папку проекта и жмёт zip
в память. Ничего не удаляет.
"""

from __future__ import annotations

import io
import platform
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import apptime
import paths

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

# Папка с диагностикой кросспостинга – та же, куда tg_browser кладёт лог,
# скриншоты и разметку (см. tg_browser._diag_dir).
DIAG_SUBDIR = "crosspost"


def diag_dir(project_id: str) -> Path:
    return paths.data_root() / project_id / DIAG_SUBDIR


def _diag_files(project_id: str) -> list[Path]:
    """Файлы диагностики, новые сверху. Пустой список – собирать нечего."""
    d = diag_dir(project_id)
    if not d.exists():
        return []
    files = [p for p in d.rglob("*") if p.is_file()]
    # Сам собранный ранее архив внутрь не кладём – иначе он растёт как снежок.
    files = [p for p in files if p.suffix.lower() != ".zip"]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def has_diagnostics(project_id: str) -> bool:
    """Есть ли что собирать (хоть один файл диагностики)."""
    return bool(_diag_files(project_id))


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("Б", "КБ", "МБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"


def _local_stamp(ts: float) -> str:
    """UNIX-время → строка по Екатеринбургу (как всё видимое время в Click)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(apptime.TZ)
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def _proxy_note() -> str:
    """Настроен ли прокси для Телеграма – без раскрытия пароля."""
    try:
        import tg_browser
        p = tg_browser.proxy_config()
    except Exception:  # noqa: BLE001
        return "не определить"
    if not p:
        return "нет (прямое соединение)"
    server = p.get("server", "")
    return f"да, {server}" + (" (с логином)" if p.get("username") else "")


def _log_tail(project_id: str, lines: int = 60) -> str:
    fp = diag_dir(project_id) / "form-last.log"
    if not fp.exists():
        return "(лог формирования не найден)"
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(лог не прочитать)"
    tail = text.strip().splitlines()[-lines:]
    return "\n".join(tail) if tail else "(лог пуст)"


def manifest(project_id: str, files: list[Path], now: datetime) -> str:
    """Человекочитаемое ОПИСАНИЕ.txt: окружение, список файлов, хвост лога."""
    d = diag_dir(project_id)
    out: list[str] = []
    out.append("ДИАГНОСТИКА CLICK — КРОССПОСТИНГ")
    out.append("=" * 52)
    out.append(f"Проект:   {project_id}")
    out.append(f"Сборка:   {BUILD}")
    out.append(f"Собрано:  {now.astimezone(apptime.TZ).strftime('%d.%m.%Y %H:%M:%S')} (Екатеринбург)")
    out.append(f"Система:  {platform.system()} {platform.release()} · Python {platform.python_version()}")
    out.append(f"Прокси ТГ: {_proxy_note()}")
    out.append("")
    out.append("КАК ЧИТАТЬ. Открой form-last.log — там по шагам: какой канал, какой")
    out.append("текст, сколько знаков, куда и сколько раз кликали (селекторы и")
    out.append("координаты), какие жирный/ссылки легли (N/M), чем закончилось.")
    out.append("Скриншоты tg-step1..step6 — что было на экране на каждом шаге.")
    out.append("Файлы *.html — настоящая разметка страницы для доводки селекторов.")
    out.append("")
    out.append(f"ФАЙЛЫ ({len(files)}), новые сверху:")
    out.append("-" * 52)
    for p in files:
        try:
            st = p.stat()
            rel = p.relative_to(d)
        except (OSError, ValueError):
            continue
        out.append(f"  {str(rel):<34} {_human_size(st.st_size):>9}   {_local_stamp(st.st_mtime)}")
    out.append("")
    out.append("ХВОСТ ЛОГА (последние строки):")
    out.append("-" * 52)
    out.append(_log_tail(project_id))
    out.append("")
    return "\n".join(out)


def build(project_id: str, now: datetime | None = None) -> tuple[str, bytes]:
    """
    Собрать архив диагностики. Возвращает (имя_файла, байты_zip).

    Пустой список файлов не падает: в архиве всё равно будет ОПИСАНИЕ.txt с
    пометкой, что собирать было нечего, — это тоже сигнал (значит, прогон не
    записал ни лога, ни снимков).
    """
    now = now or datetime.now(tz=timezone.utc)
    files = _diag_files(project_id)
    d = diag_dir(project_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ОПИСАНИЕ.txt", manifest(project_id, files, now))
        for p in files:
            try:
                z.write(p, arcname=str(p.relative_to(d)))
            except (OSError, ValueError):
                continue

    stamp = now.astimezone(apptime.TZ).strftime("%Y%m%d-%H%M")
    return f"diagnostika-{project_id}-{stamp}.zip", buf.getvalue()
