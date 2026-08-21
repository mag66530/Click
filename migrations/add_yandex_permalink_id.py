"""
Миграция: добавляет yandex_permalink_id в таблицу brands.

Запускать ОДИН РАЗ после обновления кода:
  python migrations/add_yandex_permalink_id.py

Что делает:
  Добавляет колонку yandex_permalink_id (TEXT, nullable) в brands.
  Это числовой ID организации из ссылки раздела редактирования отзывов:
    https://yandex.ru/sprav/<ID>/p/edit/reviews/
  Нужен для точной проверки "уже отвечен на площадке" через
  window.__PRELOAD_DATA (core/yandex_preload.py) — без него бот
  использует старую, менее надёжную проверку по HTML.

  Как узнать ID: откройте свою карточку в Яндекс.Бизнесе, перейдите
  в раздел «Отзывы» — число в адресной строке после /sprav/ и есть ID.

  После миграции заполните вручную для каждого бренда, например:
    sqlite3 review_bot.db "UPDATE brands SET yandex_permalink_id='123456' WHERE id=1;"
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

DATABASE_PATH = os.getenv('DATABASE_URL', 'sqlite:///review_bot.db')

if DATABASE_PATH.startswith('sqlite:///'):
    DATABASE_PATH = DATABASE_PATH[len('sqlite:///'):]


def run_migration() -> None:
    print()
    print('=' * 55)
    print('  МИГРАЦИЯ: yandex_permalink_id в brands')
    print('=' * 55)
    print()
    print(f'  База данных: {DATABASE_PATH}')
    print()

    if not os.path.exists(DATABASE_PATH):
        print(f'  ❌ Файл БД не найден: {DATABASE_PATH}')
        print('  Сначала запустите: python migrations/init_db.py')
        return

    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute('PRAGMA table_info(brands)')
        columns = [row[1] for row in cursor.fetchall()]

        if 'yandex_permalink_id' in columns:
            print('  ✅ Колонка yandex_permalink_id уже существует. Миграция не нужна.')
            return

        cursor.execute('ALTER TABLE brands ADD COLUMN yandex_permalink_id TEXT')
        conn.commit()

        print('  ✅ Колонка yandex_permalink_id добавлена.')
        print()
        print('  Заполните её для каждого бренда числовым ID организации из ссылки')
        print('  раздела «Отзывы» в Яндекс.Бизнесе: yandex.ru/sprav/<ID>/p/edit/reviews/')
        print()

        cursor.execute('SELECT id, name, cookie_file_path FROM brands')
        brands = cursor.fetchall()
        if brands:
            print('  Текущие бренды:')
            for brand_id, name, cookie_path in brands:
                session_status = 'сессия есть' if cookie_path else 'сессии НЕТ (нужна для этой проверки тоже)'
                print(f'    [{brand_id}] {name} — {session_status}')

    except Exception as e:
        print(f'  ❌ Ошибка миграции: {e}')
        conn.rollback()
        raise
    finally:
        conn.close()

    print()


if __name__ == '__main__':
    run_migration()
