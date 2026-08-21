"""
Миграция: добавляет роли и привязку к бренду в таблицу subscribers.

Запускать ОДИН РАЗ после обновления кода:
  python migrations/add_subscriber_roles.py

Что делает:
  Безопасно добавляет поля display_name, role, brand_id
  в уже существующую таблицу subscribers.
  Все существующие подписчики получают роль MANAGER без привязки
  к бренду (brand_id=NULL) — то есть продолжают видеть все отзывы,
  как и раньше, пока администратор не назначит роли в веб-панели Click.

  Если колонки уже есть — просто сообщает об этом и завершается.
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
    print('  МИГРАЦИЯ: роли подписчиков (subscribers)')
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
        cursor.execute('PRAGMA table_info(subscribers)')
        columns = [row[1] for row in cursor.fetchall()]

        added = []

        if 'display_name' not in columns:
            cursor.execute('ALTER TABLE subscribers ADD COLUMN display_name TEXT')
            added.append('display_name')

        if 'role' not in columns:
            cursor.execute(
                "ALTER TABLE subscribers ADD COLUMN role TEXT NOT NULL DEFAULT 'manager'"
            )
            added.append('role')

        if 'brand_id' not in columns:
            cursor.execute('ALTER TABLE subscribers ADD COLUMN brand_id INTEGER')
            added.append('brand_id')

        conn.commit()

        if not added:
            print('  ✅ Все колонки уже существуют. Миграция не нужна.')
            return

        print(f'  ✅ Добавлены колонки: {", ".join(added)}')
        print('  Все текущие подписчики получили роль MANAGER без привязки к бренду')
        print('  (видят всё, как раньше). Настройте роли в веб-панели Click → «🔔 Отзывы».')
        print()

        cursor.execute('SELECT id, chat_id, role, brand_id FROM subscribers')
        rows = cursor.fetchall()
        if rows:
            print('  Текущие подписчики:')
            for sub_id, chat_id, role, brand_id in rows:
                print(f'    [{sub_id}] chat_id={chat_id} role={role} brand_id={brand_id}')

    except Exception as e:
        print(f'  ❌ Ошибка миграции: {e}')
        conn.rollback()
        raise
    finally:
        conn.close()

    print()


if __name__ == '__main__':
    run_migration()
