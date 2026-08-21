"""
Точка входа. Запускает Telegram-бота и планировщик задач.
"""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession  # ДОБАВЛЕНО
from dotenv import load_dotenv

from bot.handlers import admin, reviews
from core.database import init_db
from core.scheduler import run_imap_check, setup_scheduler

load_dotenv()

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

if not os.path.exists("logs"):
    os.makedirs("logs")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

class Config:
    TELEGRAM_BOT_TOKEN: str  = os.getenv("TELEGRAM_BOT_TOKEN", "")
    MODERATOR_CHAT_IDS: list[int] = [
        int(x.strip()) for x in os.getenv("MODERATOR_CHAT_ID", "").split(",") if x.strip().isdigit()
    ]
    ADMIN_IDS: list[int] = [
        int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
    ]

    # Обратная совместимость — первый модератор как основной чат
    @property
    def MODERATOR_CHAT_ID(self) -> int:
        return self.MODERATOR_CHAT_IDS[0] if self.MODERATOR_CHAT_IDS else 0

    def validate(self):
        if not self.TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env")
        if not self.MODERATOR_CHAT_IDS:
            raise ValueError("MODERATOR_CHAT_ID не задан в .env")
        if not self.ADMIN_IDS:
            logger.warning("ADMIN_IDS не задан — никто не сможет управлять ботом!")


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------

async def main():
    config = Config()
    config.validate()

    # Инициализация БД
    init_db()
    logger.info("База данных инициализирована")

    # Настройка прокси
    proxy_url = os.getenv("PROXY_URL")
    session = None
    if proxy_url:
        logger.info(f"Используем прокси: {proxy_url}")
        session = AiohttpSession(proxy=proxy_url)

    # ВАЖНО: передаем session=session в Bot
    bot = Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    # Регистрируем роутеры
    dp.include_router(admin.router)
    dp.include_router(reviews.router)

    dp["config"] = config

    # Запускаем планировщик
    scheduler = setup_scheduler(bot, config)
    scheduler.start()
    logger.info("Планировщик запущен")

    # Авто-подписка всех модераторов на уведомления
    from core.database import Subscriber
    db_startup = None
    try:
        from core.database import get_session as _gs
        db_startup = _gs()
        for chat_id in config.MODERATOR_CHAT_IDS:
            if not db_startup.query(Subscriber).filter_by(chat_id=chat_id).first():
                db_startup.add(Subscriber(chat_id=chat_id))
        db_startup.commit()
    except Exception as e:
        logger.warning(f"Не удалось авто-подписать модераторов: {e}")
    finally:
        if db_startup:
            db_startup.close()

    # Сообщаем о старте всем модераторам
    startup_text = (
        "🚀 <b>Бот успешно запущен и готов к работе!</b>\n\n"
        "📬 <b>Почта:</b> мониторинг включен\n\n"
        "Нажмите /start, чтобы вывести полную инструкцию по командам."
    )
    for chat_id in config.MODERATOR_CHAT_IDS:
        try:
            await bot.send_message(chat_id, startup_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Не удалось отправить стартовое сообщение {chat_id}: {e}")

    logger.info("Бот запущен. Ожидаем команды...")

    # Запускаем первую проверку немедленно (не ждём 15 мин до первого тика)
    asyncio.create_task(run_imap_check(bot, config))

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())