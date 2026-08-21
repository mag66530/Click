"""
Тест парсера Google Business отзывов.
Запуск: python test_google.py
"""
import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from core.database import Brand, get_session
from core.google_scraper import fetch_unanswered_google_reviews, SESSIONS_DIR


async def main():
    db = get_session()
    brands = db.query(Brand).all()
    db.close()

    if not brands:
        print("Нет брендов в БД")
        return

    print("Бренды:")
    for i, b in enumerate(brands, 1):
        session_file = SESSIONS_DIR / f"{b.name}.json"
        status = "[OK] сессия есть" if session_file.exists() else "[нет] нет сессии"
        print(f"  {i}. {b.name} — {status}")

    print("\nКакой бренд тестировать? Введите номер:")
    choice = input().strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(brands)):
        print("Неверный выбор")
        return

    brand = brands[int(choice) - 1]
    session_file = SESSIONS_DIR / f"{brand.name}.json"

    if not session_file.exists():
        print(f"Нет сессии для {brand.name}. Сначала запусти: python setup_google_session.py")
        return

    print(f"\nПарсим отзывы для: {brand.name}")
    print("=" * 50)

    reviews = await fetch_unanswered_google_reviews(brand.name)

    print(f"\nНайдено неотвеченных отзывов: {len(reviews)}")
    print("=" * 50)

    for i, r in enumerate(reviews, 1):
        print(f"\n--- Отзыв #{i} ---")
        print(f"Автор:   {r.get('reviewer_name')}")
        print(f"Рейтинг: {r.get('rating')} ★")
        print(f"Текст:   {r.get('review_text', '')[:200]}")
        print(f"URL:     {r.get('review_url')}")


asyncio.run(main())
