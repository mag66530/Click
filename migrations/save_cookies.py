import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright
from sqlalchemy import select
from core.database import get_session, Brand

URLS = {
    "yandex": "https://yandex.ru/sprav/companies/?no_redirect=1",
    "2gis": "https://account.2gis.com/orgs/70000001077855513/reviews"
}

async def save_cookies_main():
    if not os.path.exists('cookies'): os.makedirs('cookies')

    brands_list = []
    with get_session() as session:
        result = session.execute(select(Brand))
        db_brands = result.scalars().all()
        for b in db_brands:
            brands_list.append({"id": b.id, "name": b.name})

    if not brands_list:
        print("❌ Брендов нет в базе."); return

    print("\n=== УПРАВЛЕНИЕ СЕССИЯМИ ===")
    print("1. Яндекс Бизнес")
    print("2. 2ГИС")
    platform_choice = input("\nВыберите платформу (1 или 2): ")
    platform = "yandex" if platform_choice == "1" else "2gis"

    for i, b in enumerate(brands_list):
        print(f"[{i+1}] {b['name']}")
    
    brand_idx = input(f"\nВыберите бренд для {platform}: ")
    try:
        selected_brand = brands_list[int(brand_idx)-1]
    except:
        print("❌ Ошибка выбора"); return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print(f"\nОткрываю {platform} для {selected_brand['name']}...")
        await page.goto(URLS[platform])

        print("\n" + "!"*50)
        print(f"ДЕЙСТВИЕ: Войдите в аккаунт {platform} ({selected_brand['name']})")
        print("Когда увидите личный кабинет — вернитесь сюда.")
        print("!"*50)
        
        input("\nНажмите ENTER после входа...")

        file_path = f"cookies/{platform}_{selected_brand['id']}.json"
        await context.storage_state(path=file_path)
        
        with get_session() as session:
            brand_in_db = session.get(Brand, selected_brand['id'])
            if platform == "yandex":
                brand_in_db.cookie_file_path = file_path
            else:
                brand_in_db.cookie_2gis_path = file_path
            session.commit()

        print(f"\n✅ Сессия {platform} сохранена!")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(save_cookies_main())