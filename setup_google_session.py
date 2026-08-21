"""
Скрипт для первоначальной авторизации в Google Business.
Запускается ОДИН РАЗ для каждого бренда.

Использование:
    python setup_google_session.py

Откроется браузер — войдите в нужный Google аккаунт вручную.
Сессия сохранится и бот будет использовать её автоматически.
"""
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

SESSIONS_DIR = Path(__file__).parent / "google_sessions"
SESSIONS_DIR.mkdir(exist_ok=True)


def _load_config(brand_name: str) -> dict:
    import json
    config_file = SESSIONS_DIR / f"{brand_name}_config.json"
    if config_file.exists():
        return json.loads(config_file.read_text(encoding="utf-8"))
    return {"email": "", "reviews_url": "https://business.google.com/reviews?hl=ru"}


async def setup_session(brand_name: str) -> None:
    session_file = SESSIONS_DIR / f"{brand_name}.json"
    cfg = _load_config(brand_name)
    reviews_url = cfg.get("reviews_url", "https://business.google.com/reviews?hl=ru")
    email = cfg.get("email", "")

    print(f"\n{'='*50}")
    print(f"Настройка Google аккаунта для бренда: {brand_name}")
    if email:
        print(f"Аккаунт: {email}")
    print(f"URL отзывов: {reviews_url}")
    print(f"{'='*50}")
    print("Откроется браузер. Войдите в нужный Google аккаунт.")
    print("Когда увидите список отзывов — нажмите Enter здесь.")
    print("\nНажмите Enter чтобы открыть браузер...")
    input()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            channel="chrome",  # использует реальный Chrome, не Chromium
            args=["--start-maximized"],
        )
        context = await browser.new_context(
            viewport=None,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Открываем Google — там пользователь входит в аккаунт
        await page.goto("https://accounts.google.com", timeout=60_000)

        print(f"\n{'!'*50}")
        print(f"Войдите в аккаунт: {email}")
        print(f"После входа перейдите по ссылке:")
        print(f"  {reviews_url}")
        print(f"Когда увидите страницу с отзывами — вернитесь сюда.")
        print(f"{'!'*50}")
        input("\nНажмите Enter когда будете на странице с отзывами...")

        await context.storage_state(path=str(session_file))
        print(f"\n✅ Сессия сохранена: {session_file}")

        await browser.close()


async def main() -> None:
    sys.path.insert(0, str(Path(__file__).parent))
    from dotenv import load_dotenv
    load_dotenv()
    from core.database import Brand, get_session

    db = get_session()
    brands = db.query(Brand).all()
    db.close()

    print("Настройка Google Business сессий")
    print("Бренды в системе:")
    for i, b in enumerate(brands, 1):
        session_file = SESSIONS_DIR / f"{b.name}.json"
        status = "✅ сессия есть" if session_file.exists() else "❌ нет сессии"
        print(f"  {i}. {b.name} — {status}")

    print("\nДля какого бренда настроить? Введите номер (или 'все'):")
    choice = input().strip()

    if choice.lower() == "все":
        for brand in brands:
            await setup_session(brand.name)
    elif choice.isdigit() and 1 <= int(choice) <= len(brands):
        await setup_session(brands[int(choice) - 1].name)
    else:
        print("Неверный выбор")


if __name__ == "__main__":
    asyncio.run(main())
