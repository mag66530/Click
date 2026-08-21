"""
Валидация отзывов перед обработкой.

Решает Edge Case: Яндекс/2ГИС присылают письма с задержкой в несколько часов.
За это время площадка могла удалить отзыв.

Перед отправкой в GPT делаем HTTP-запрос на URL из письма:
  - 200 OK     → отзыв жив, обрабатываем
  - 404        → удалён, сохраняем в БД со статусом DELETED, в Telegram не пишем
  - 301/302    → редирект на главную, скорее всего удалён
  - сетевая ошибка → статус "unknown", обрабатываем на всякий случай
"""
import logging
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

# User-Agent обычного браузера, чтобы не блокировали сразу
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

ReviewAliveStatus = Literal["alive", "deleted", "unknown"]


async def check_review_alive(url: str) -> ReviewAliveStatus:
    """
    Проверяет, существует ли отзыв по URL.

    Возвращает:
      "alive"   — отзыв доступен
      "deleted" — 404 или редирект на главную страницу
      "unknown" — сетевая ошибка или неожиданный статус-код.
                  В этом случае рекомендуем всё равно обработать отзыв
                  (лучше лишний раз отправить модератору, чем пропустить).
    """
    if not url:
        return "unknown"

    try:
        async with httpx.AsyncClient(
            follow_redirects=False,  # Не следуем редиректам — редирект = удалён
            timeout=httpx.Timeout(10.0),
            headers=_HEADERS,
        ) as client:
            resp = await client.get(url)

        code = resp.status_code

        if code == 200:
            # Дополнительная проверка для Яндекса:
            # если отзыв удалён "мягко" (без 404), в теле может быть фраза об ошибке
            body_lower = resp.text.lower()
            if any(phrase in body_lower for phrase in (
                "отзыв удалён",
                "отзыв не найден",
                "страница не найдена",
                "review not found",
            )):
                logger.info(f"Отзыв удалён (200 + текст ошибки): {url}")
                return "deleted"
            return "alive"

        elif code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            logger.info(f"Редирект {code}: {url} → {location}")
            # Если редиректит на главную страницу площадки — отзыв удалён
            if any(main in location for main in (
                "yandex.ru/maps\n",
                "yandex.ru/maps?",
                "yandex.ru/maps/",
                "2gis.ru/\n",
                "2gis.ru/?",
            )):
                return "deleted"
            # Редирект на другой URL отзыва — возможно переезд, считаем живым
            return "alive"

        elif code == 404:
            logger.info(f"Отзыв удалён (404): {url}")
            return "deleted"

        elif code in (403, 429, 503):
            # Заблокировали или перегружены — не можем определить
            logger.warning(f"Статус {code} при проверке {url}, обрабатываем как unknown")
            return "unknown"

        else:
            logger.warning(f"Неожиданный статус {code} для {url}")
            return "unknown"

    except httpx.TimeoutException:
        logger.warning(f"Таймаут при проверке URL: {url}")
        return "unknown"
    except httpx.RequestError as e:
        logger.error(f"Сетевая ошибка при проверке {url}: {e}")
        return "unknown"


async def check_already_answered(url: str) -> bool:
    """
    Проверяет, есть ли уже ответ компании на отзыв.

    Следует по редиректам (трекинговые ссылки Яндекса/2ГИС),
    затем ищет маркеры ответа в HTML и встроенном JSON.

    Возвращает True  — ответ уже есть, не отправлять.
    Возвращает False — ответа нет или проверить не удалось (лучше отправить).
    """
    if not url:
        return False

    # Маркеры ответа компании в HTML и JSON
    _ANSWERED_MARKERS = [
        # Яндекс Карты (HTML-классы и JSON-ключи)
        "ownerreply",
        "owner-response",
        "business-review-view__owner",
        "businessreplytext",
        # Яндекс — текстовые
        "ответ компании",
        "ответ организации",
        "ответ владельца",
        # 2ГИС
        "official_answer",
        "provider-answer",
        "officialanswer",
        "ответ представителя",
        "ответ филиала",
    ]

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            headers=_HEADERS,
        ) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            return False

        body_lower = resp.text.lower()
        found = any(marker in body_lower for marker in _ANSWERED_MARKERS)

        if found:
            logger.info(f"Отзыв уже имеет ответ компании: {url}")
        return found

    except Exception as e:
        logger.warning(f"Не удалось проверить ответ на отзыв ({url}): {e}")
        return False  # При ошибке — лучше отправить, чем пропустить
