# 🤖 Review Bot — Telegram-бот для автоматизации отзывов

Внутренний сервис для мониторинга отзывов (Яндекс Бизнес, 2ГИС, Google)
и генерации ответов через ChatGPT без платных агрегаторов.

---

## Что умеет бот

- 📬 Читает новые отзывы с почты (IMAP) от Яндекс Бизнеса и 2ГИС
- ✅ Проверяет, не удалён ли отзыв площадкой до обработки
- 🤖 Генерирует ответ через GPT с учётом промта бренда и города
- 🚨 Сигнализирует о негативных отзывах (1-3★) без генерации ответа
- ✏️ Позволяет модератору отредактировать ответ перед публикацией
- 🚀 Публикует ответ автоматически через браузер (Playwright)
- ➕ Позволяет администратору добавлять бренды, города, промты через Telegram

---

## Структура проекта

```
review_bot/
├── bot/
│   ├── main.py              ← Точка входа
│   ├── keyboards.py         ← Inline-кнопки
│   ├── states.py            ← FSM-состояния
│   └── handlers/
│       ├── admin.py         ← /add_brand, /add_city и др.
│       └── reviews.py       ← Одобрение / редактирование
├── core/
│   ├── database.py          ← Модели SQLAlchemy
│   ├── imap_parser.py       ← Чтение писем
│   ├── review_validator.py  ← Проверка URL отзыва
│   ├── gpt_client.py        ← Генерация ответов
│   ├── publisher.py         ← Публикация через Playwright
│   ├── yandex_api_client.py ← Опционально: Яндекс Бизнес API
│   └── scheduler.py         ← Планировщик задач
├── migrations/
│   ├── init_db.py           ← Начальное заполнение БД
│   └── save_cookies.py      ← Сохранение сессии Яндекса
├── cookies/                 ← Здесь хранятся сессии браузера
├── logs/                    ← Логи бота
├── .env.example             ← Шаблон переменных окружения
└── requirements.txt
```

---

## Установка

### 1. Клонируйте / распакуйте проект

```bash
cd review_bot
```

### 2. Создайте виртуальное окружение

```bash
python3 -m venv venv
source venv/bin/activate      # Linux/Mac
# или
venv\Scripts\activate.bat     # Windows
```

### 3. Установите зависимости

```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. Создайте файл `.env`

```bash
cp .env.example .env
```

Откройте `.env` и заполните:

```env
TELEGRAM_BOT_TOKEN=токен_от_BotFather
MODERATOR_CHAT_ID=-100xxxxxxxxxx   # ID чата/группы для уведомлений
ADMIN_IDS=123456789                # Ваш Telegram user_id
OPENAI_API_KEY=sk-...
```

> Как узнать свой Telegram user_id: напишите @userinfobot

### 5. Инициализируйте базу данных

```bash
python migrations/init_db.py
```

Это создаст файл `review_bot.db` и добавит три начальных бренда (СМУ, ИМП, МПЭ).

**Важно:** Откройте файл `migrations/init_db.py` и замените `ЗАМЕНИТЕ_НА_РЕАЛЬНЫЙ_ПАРОЛЬ`
на реальные пароли от почтовых ящиков, или добавьте бренды через команду `/add_brand` в боте.

### 6. Сохраните сессию Яндекса (для автопубликации)

```bash
python migrations/save_cookies.py
```

Откроется браузер. Войдите в кабинет Яндекс Бизнеса вручную,
затем нажмите Enter в консоли. Сессия сохранится в `cookies/yandex_session.json`.

> Повторяйте этот шаг раз в 2-3 недели, когда сессия устаревает.

### 7. Запустите бота

```bash
python -m bot.main
```

---

## Команды бота

| Команда | Описание | Кто может |
|---------|----------|-----------|
| `/add_brand` | Добавить новый бренд | Администратор |
| `/add_city` | Добавить город к бренду | Администратор |
| `/list_brands` | Список всех брендов | Администратор |
| `/edit_prompt` | Изменить промт бренда | Администратор |
| `/check_now` | Проверить почту прямо сейчас | Администратор |
| `/cancel` | Отменить текущее действие | Все |

---

## Как работает pipeline отзыва

```
Яндекс/2ГИС → письмо на почту
      ↓
  Парсинг письма (imap_parser.py)
  Бренд, город, автор, оценка, текст, URL
      ↓
  Проверка URL (review_validator.py)
  ├── Удалён (404) → сохранить как DELETED, не беспокоить модератора
  └── Жив → продолжаем
      ↓
  Оценка 1-3★?
  ├── Да → Уведомление 🚨 НЕГАТИВ в Telegram (без GPT)
  └── Нет → Генерация ответа (gpt_client.py)
                ↓
           Сообщение в Telegram:
           [текст отзыва] + [ответ GPT]
           [✅ Одобрить] [✏️ Редактировать]
                ↓
           Модератор нажимает ✅
                ↓
           Playwright (publisher.py)
           ├── Успех → ✅ Ответ опубликован
           ├── Капча → ⚠️ Скопируйте ответ вручную
           └── Удалён → ❌ Отзыв удалён площадкой
```

---

## Настройка почты Яндекс

1. Войдите в почту → Настройки → Все настройки → Почтовые клиенты
2. Включите **IMAP**
3. Создайте **пароль приложения** (не основной пароль!) для бота
4. Сервер: `imap.yandex.ru`, порт: `993`, SSL: Да

---

## Вариант Б: Яндекс Бизнес API (без задержки писем)

Если хотите получать отзывы без задержки 2-3 часа от почты:

1. Получите OAuth-токен в кабинете Яндекс Бизнес → Настройки → API
2. Найдите `company_id` в URL: `https://business.yandex.ru/company/XXXXXXXX`
3. Добавьте токен в БД для каждого бренда:
   ```python
   brand.yandex_oauth_token = "ваш_токен"
   brand.yandex_company_id  = "12345678"
   ```
4. В `scheduler.py` раскомментируйте вызов `poll_yandex_reviews()` вместо IMAP

---

## Запуск через systemd (Linux, для продакшена)

Создайте файл `/etc/systemd/system/review_bot.service`:

```ini
[Unit]
Description=Review Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/review_bot
ExecStart=/home/ubuntu/review_bot/venv/bin/python -m bot.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable review_bot
sudo systemctl start review_bot
sudo systemctl status review_bot
```

---

## Частые вопросы

**Q: Бот не находит кнопку "Ответить" на Яндексе**
A: Яндекс обновил вёрстку. Откройте `core/publisher.py`, раздел `reply_selectors`,
и обновите CSS-селекторы. Скриншот для отладки сохраняется в `logs/publish_debug.png`.

**Q: Сессия устарела, бот не может авторизоваться**
A: Запустите `python migrations/save_cookies.py` и войдите заново.

**Q: Отзыв помечен как DELETED, но на самом деле существует**
A: Яндекс иногда отдаёт редиректы на живые отзывы. Проверьте логику редиректов
в `core/review_validator.py` и скорректируйте паттерны.

**Q: GPT генерирует ответ с запрещёнными словами**
A: Добавьте слово в список `GLOBAL_RULES` в `core/gpt_client.py`.
