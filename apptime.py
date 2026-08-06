"""
apptime.py – время, которое видит человек.

Приложение работает в облаке, а облако живёт по UTC. Отсюда и жалоба: в логе
прогона стояло 11:56, когда в Екатеринбурге было почти 17:00. Дата при этом
совпадала, и выглядело так, будто показывают какой-то посторонний прогон.

Поэтому всё, что показывается людям – строки живого лога, время в отчётах,
имена файлов отчётов и логов, отметка о загрузке городов, – считается по
Екатеринбургу. Внутри и в файлах время по-прежнему хранится в UTC: так его
можно сравнивать между собой и не терять при переезде.

Смещение задано ЧИСЛОМ, а не именем зоны. Имена (`Asia/Yekaterinburg`)
берутся из базы tzdata, а на Windows её может не оказаться – и приложение
упало бы на ровном месте у заказчика, а не у нас. Екатеринбург с 2011 года
живёт на UTC+5 круглый год, часы не переводит, так что число здесь ничем не
хуже имени и надёжнее.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Метка сборки – одна на всё приложение, лежит в build.py.
from build import BUILD  # noqa: F401

TZ_NAME = "Екатеринбург"
TZ = timezone(timedelta(hours=5), TZ_NAME)

FULL = "%d.%m.%Y, %H:%M:%S"
CLOCK = "%H:%M:%S"


def now() -> datetime:
    """Сейчас по Екатеринбургу."""
    return datetime.now(TZ)


def stamp(fmt: str = CLOCK) -> str:
    """Отметка времени для лога или имени файла."""
    return now().strftime(fmt)


def to_local(value: str | datetime | None) -> datetime | None:
    """
    ISO-строка или datetime → время Екатеринбурга.
    None – значения нет или оно не разобралось.

    Время без пояса считаем UTC: именно так его пишут прогоны.
    """
    if not value:
        return None
    dt = value
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def human(value: str | datetime | None, fmt: str = FULL) -> str:
    """Время для показа человеку. Не разобрали – отдаём как есть, не роняя страницу."""
    if not value:
        return ""
    dt = to_local(value)
    if dt is None:
        return str(value)[:19].replace("T", " ")
    return dt.strftime(fmt)
