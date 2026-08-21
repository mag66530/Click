"""
Клиент для официального Яндекс Бизнес API (Вариант Б).

Преимущества перед IMAP + Playwright:
  ✅ Нет задержки — polling каждые 5 минут
  ✅ Публикация ответов через POST-запрос (без капчи)
  ✅ Точный статус отзыва (API вернёт ошибку если удалён)

Как получить OAuth-токен:
  1. Войдите в https://yandex.ru/business
  2. Перейдите в Настройки → API
  3. Создайте приложение и получите токен
  4. Добавьте токен в БД: Brand.yandex_oauth_token
  5. Найдите company_id в URL вашей организации

Документация: https://yandex.ru/dev/business-api/doc/
"""
import logging
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

YANDEX_API_BASE = "https://api.business.yandex.net/v1"


class YandexBusinessClient:
    """Клиент для работы с Яндекс Бизнес API."""

    def __init__(self, oauth_token: str, company_id: str):
        self.token      = oauth_token
        self.company_id = company_id
        self.headers    = {
            "Authorization": f"OAuth {oauth_token}",
            "Content-Type":  "application/json",
        }

    async def get_reviews(
        self,
        since: Optional[datetime] = None,
        limit: int = 50,
    ) -> list[dict]:
        """
        Получает список отзывов организации.

        since — если указан, возвращает только отзывы новее этой даты.
                Используется для инкрементального polling-а.
        """
        params: dict = {
            "business_id": self.company_id,
            "limit":       limit,
        }
        if since:
            params["updated_after"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{YANDEX_API_BASE}/reviews",
                    headers=self.headers,
                    params=params,
                )
            resp.raise_for_status()
            return resp.json().get("reviews", [])

        except httpx.HTTPStatusError as e:
            logger.error(f"Яндекс API ошибка {e.response.status_code}: {e.response.text}")
            return []
        except httpx.RequestError as e:
            logger.error(f"Сетевая ошибка Яндекс API: {e}")
            return []

    async def post_reply(self, review_id: str, reply_text: str) -> bool:
        """
        Публикует ответ на отзыв через API.
        Возвращает True при успехе.

        Это заменяет Playwright — никакой капчи!
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{YANDEX_API_BASE}/reviews/{review_id}/reply",
                    headers=self.headers,
                    json={"text": reply_text},
                )

            if resp.status_code in (200, 201, 204):
                logger.info(f"Ответ опубликован через Яндекс API: review_id={review_id}")
                return True
            else:
                logger.error(
                    f"Ошибка публикации через API: {resp.status_code} — {resp.text[:200]}"
                )
                return False

        except httpx.RequestError as e:
            logger.error(f"Сетевая ошибка при публикации через API: {e}")
            return False

    async def get_review_by_id(self, review_id: str) -> Optional[dict]:
        """Получает конкретный отзыв по ID. Вернёт None если удалён."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{YANDEX_API_BASE}/reviews/{review_id}",
                    headers=self.headers,
                )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Сверка "уже отвечен" с данными из API
# ---------------------------------------------------------------------------
#
# Письма из IMAP не содержат ID отзыва — только оценку, автора и текст.
# Поэтому сопоставляем отзыв из письма с отзывом из API по "сигнатуре"
# (оценка + автор + начало текста), а не по ID.
#
# ВАЖНО: точные названия полей в ответе Яндекс Бизнес API (answer/reply/...)
# не были проверены на реальном токене — проверьте на своих данных и при
# необходимости добавьте недостающий вариант ключа в _ANSWER_KEYS.

_ANSWER_KEYS = ("answer", "reply", "companyAnswer", "official_answer", "response")


def _review_is_answered(review: dict) -> bool:
    """Есть ли непустой ответ компании в объекте отзыва из API."""
    for key in _ANSWER_KEYS:
        val = review.get(key)
        if isinstance(val, dict) and (val.get("text") or "").strip():
            return True
        if isinstance(val, str) and val.strip():
            return True
    return False


def review_signature(rating, reviewer_name: str, review_text: str) -> tuple:
    """
    Сигнатура для сопоставления отзыва из письма (IMAP) с отзывом из API.
    Специально грубая (оценка + автор + первые 80 символов текста) — email
    и API могут чуть по-разному экранировать/обрезать текст.
    """
    try:
        rating_int = int(rating)
    except (TypeError, ValueError):
        rating_int = 0
    name = (reviewer_name or "").strip().lower()
    text = (review_text or "").strip().lower()[:80]
    return (rating_int, name, text)


def _api_review_signature(review: dict) -> tuple:
    author = review.get("author")
    name = author.get("name") if isinstance(author, dict) else (
        review.get("author_name") or review.get("reviewer_name") or ""
    )
    text = review.get("text") or review.get("review_text") or ""
    rating = review.get("rating") or review.get("score") or 0
    return review_signature(rating, name, text)


async def fetch_answered_signatures(oauth_token: str, company_id: str) -> set:
    """
    Возвращает множество сигнатур уже отвеченных отзывов организации.
    Используется как более точная замена скрейпинга HTML для проверки
    "не ответили ли на этот отзыв уже вручную на площадке".
    """
    client = YandexBusinessClient(oauth_token, company_id)
    reviews = await client.get_reviews(limit=100)
    return {_api_review_signature(r) for r in reviews if _review_is_answered(r)}


def extract_city_from_yandex_address(address: str) -> str:
    """
    Извлекает город из строки адреса Яндекс API.

    Примеры адресов:
      "г. Казань, ул. Ленина, 10"  → "Казань"
      "Москва, Красная площадь, 1" → "Москва"
    """
    import re
    # Паттерн "г. Казань" или "г Казань"
    match = re.search(r'г\.?\s+([\w\-]+)', address)
    if match:
        return match.group(1).strip()
    # Первая часть до запятой (часто это город)
    parts = address.split(",")
    if parts:
        return parts[0].strip()
    return address.strip()
