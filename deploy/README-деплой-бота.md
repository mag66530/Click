# Деплой бота отзывов на бесплатный сервер (Oracle Cloud Always Free)

Коротко: бот отзывов — это долгоживущий процесс (`APScheduler` тикает каждые 15 минут),
он должен работать 24/7 на каком-то сервере, а не в Streamlit Cloud (там для этого нет
условий) и не на бесплатных serverless-платформах (Render/Railway free — засыпают,
PythonAnywhere free — блокирует внешние запросы, что сломает IMAP и парсинг площадок).

**Oracle Cloud Always Free** — единственный вариант из бесплатных, который даёт
настоящую постоянно работающую линуксовую машину без ограничения по времени
(не 30-дневный триал, а бессрочно бесплатный тариф). Нужна карта для верификации
личности при регистрации — списаний по Always Free тарифу не будет.

## 1. Завести виртуалку

1. Регистрация: https://cloud.oracle.com → Sign Up
2. Compute → Instances → Create Instance
3. Shape → Change Shape → **Ampere (ARM), VM.Standard.A1.Flex** — это Always Free
   (1–4 OCPU, 6–24 GB RAM бесплатно, зависит от региона/лимитов на момент регистрации)
4. Образ (Image) — Ubuntu 24.04 (или 22.04)
5. При создании сохраните приватный SSH-ключ (или загрузите свой публичный) — без
   него потом не зайти
6. Дождаться статуса Running, скопировать публичный IP инстанса

## 2. Подключиться и поставить окружение

```bash
ssh -i путь/к/ключу ubuntu@<публичный_IP>

sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip git

git clone <URL_вашего_репозитория_Click> Click
cd Click

python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Playwright ставит свой браузер + системные зависимости под него
playwright install --with-deps chromium
```

## 3. Настроить `.env`

Бот ищет `.env` в корне репозитория (`load_dotenv()` в `bot/main.py`). Создать файл
и заполнить реальными значениями (токен бота, ID модераторов/админов, при необходимости
`PROXY_URL`, если из региона сервера Telegram API недоступен напрямую):

```bash
nano .env
```

```env
TELEGRAM_BOT_TOKEN=...
MODERATOR_CHAT_ID=123456789,987654321
ADMIN_IDS=123456789
DATABASE_URL=sqlite:///review_bot.db
# PROXY_URL=http://...   # если понадобится
```

⚠️ **Не коммитьте `.env` в git** — он и так в `.gitignore`, но перепроверьте перед пушем
с этого сервера, если вообще будете пушить отсюда.

## 4. Инициализировать базу и накатить миграции

```bash
python migrations/init_db.py
python migrations/add_subscriber_roles.py
python migrations/add_yandex_permalink_id.py
python migrations/add_cookie_path.py
```

Дальше — как обычно: `/add_brand` в самом боте, `migrations/save_cookies.py` для
сессии Яндекса под каждый бренд.

## 5. Запустить как systemd-сервис (чтобы жил вечно)

Файл юнита уже готов — `deploy/review_bot.service`. Поправьте в нём пути под
реальные (`WorkingDirectory`, `ExecStart`, `User`), если клонировали не в
`/home/ubuntu/Click`, затем:

```bash
sudo cp deploy/review_bot.service /etc/systemd/system/review_bot.service
sudo systemctl daemon-reload
sudo systemctl enable review_bot   # автозапуск после перезагрузки сервера
sudo systemctl start review_bot
```

Проверить, что жив:
```bash
sudo systemctl status review_bot
journalctl -u review_bot -f        # логи вживую, Ctrl+C чтобы выйти
```

Если упал и не поднимается — `Restart=always` в юните перезапустит сам через
10 секунд, но если падает систематически (например, неправильный токен) —
после 5 падений за минуту systemd остановится и не будет долбить бесконечно
(`StartLimitBurst=5`) — тогда смотреть `journalctl` и чинить причину.

## 6. Обновление кода на сервере после правок

```bash
cd ~/Click
git pull origin <ветка>
source venv/bin/activate
pip install -r requirements.txt   # если менялись зависимости
sudo systemctl restart review_bot
```

## Что ещё может понадобиться

- **Открыть исходящий трафик** — Oracle по умолчанию режет исходящие соединения
  на нестандартные порты через Security List/NSG виртуальной сети. Для IMAP (обычно
  порт 993) и HTTPS (443, Telegram API, Яндекс/2ГИС/Google) может понадобиться
  добавить правило в Security List облака — если бот не может достучаться до почты,
  первым делом проверить это, а не код
- **Память для Playwright** — на самом слабом Always Free шейпе (1 OCPU / 6 GB) headless
  Chromium должен нормально работать, но если увидите OOM-килы в `journalctl` — взять
  вариант с 4 OCPU / 24 GB (тоже в рамках Always Free лимита)
