"""
Миграция: добавляет колонку cookie_file_path в таблицу brands.

Запускать ОДИН РАЗ после обновления кода:
  python migrations/add_cookie_path.py

Что делает:
  Безопасно добавляет поле cookie_file_path (TEXT, nullable)
  в уже существующую таблицу brands.
  Если колонка уже есть — просто сообщает об этом и завершается.

После этого:
  1. Запустите save_cookies.py для каждого бренда.
  2. БД будет хранить путь к сессии прямо в записи бренда.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

DATABASE_PATH = os.getenv('DATABASE_URL', 'sqlite:///review_bot.db')

# Убираем префикс sqlite:///
if DATABASE_PATH.startswith('sqlite:///'):
    DATABASE_PATH = DATABASE_PATH[len('sqlite:///'):]


def run_migration() -> None:
    print()
    print('=' * 55)
    print('  МИГРАЦИЯ: добавление cookie_file_path в brands')
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
        # Проверяем, есть ли уже такая колонка
        cursor.execute('PRAGMA table_info(brands)')
        columns = [row[1] for row in cursor.fetchall()]

        if 'cookie_file_path' in columns:
            print('  ✅ Колонка cookie_file_path уже существует.')
            print('  Миграция не нужна.')
            return

        # Добавляем колонку
        cursor.execute(
            'ALTER TABLE brands ADD COLUMN cookie_file_path TEXT'
        )
        conn.commit()

        print('  ✅ Колонка cookie_file_path успешно добавлена!')
        print()

        # Показываем текущее состояние брендов
        cursor.execute('SELECT id, name, cookie_file_path FROM brands')
        brands = cursor.fetchall()

        if brands:
            print('  Текущие бренды в БД:')
            for brand_id, name, cookie_path in brands:
                status = f'→ {cookie_path}' if cookie_path else '→ (сессия не настроена)'
                print(f'    [{brand_id}] {name}  {status}')
            print()
            print('  Следующий шаг: запустите save_cookies.py')
            print('  для каждого бренда, чтобы сохранить сессии.')
        else:
            print('  Брендов в БД нет. Добавьте через /add_brand в боте.')

    except Exception as e:
        print(f'  ❌ Ошибка миграции: {e}')
        conn.rollback()
        raise
    finally:
        conn.close()

    print()


if __name__ == '__main__':
    run_migration()