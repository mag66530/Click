"""
Модели базы данных и утилиты для работы с сессиями.
"""
import enum
import os
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import (
    Column, DateTime, Enum, ForeignKey,
    Integer, String, Text, create_engine,
)
from sqlalchemy.orm import Session, declarative_base, relationship

load_dotenv()

Base = declarative_base()


# ---------------------------------------------------------------------------
# Перечисления
# ---------------------------------------------------------------------------

class ReviewStatus(enum.Enum):
    PENDING  = "pending"   # Ждёт решения модератора
    APPROVED = "approved"  # Одобрен и опубликован
    EDITED   = "edited"    # Отредактирован вручную и опубликован
    NEGATIVE = "negative"  # Негатив — отправлен алерт, ответ не генерировался
    DELETED  = "deleted"   # Отзыв удалён площадкой до обработки
    FAILED   = "failed"    # Ошибка публикации (капча, таймаут и т.д.)


class SubscriberRole(enum.Enum):
    """
    Роль получателя уведомлений в Telegram.

    OWNER   — руководство: только негатив (1-3★), коротким алертом без деталей модерации.
    MANAGER — ответственный за бренд: все отзывы этого бренда (позитив + негатив),
              полные карточки с кнопками модерации. Привязан к конкретному brand_id.
    VIEWER  — просто «в курсе»: однострочное уведомление о факте нового отзыва,
              без текста и без кнопок.
    """
    OWNER   = "owner"
    MANAGER = "manager"
    VIEWER  = "viewer"


# ---------------------------------------------------------------------------
# Таблицы
# ---------------------------------------------------------------------------

class Brand(Base):
    """
    Бренд компании (СМУ, ИМП, МПЭ и любые будущие).
    Добавляется через Telegram-команду /add_brand.
    """
    __tablename__ = 'brands'

    id               = Column(Integer, primary_key=True, autoincrement=True)
    name             = Column(String(100), unique=True, nullable=False)
    prompt_template  = Column(Text, nullable=False)       # Промт для GPT
    imap_login       = Column(String(200), nullable=False)
    imap_password    = Column(String(200), nullable=False)
    imap_server      = Column(String(200), nullable=False, default="imap.yandex.ru")

    # Опционально: Яндекс Бизнес API (Вариант Б)
    yandex_oauth_token = Column(String(500), nullable=True)
    yandex_company_id  = Column(String(100), nullable=True)
    yandex_last_check  = Column(DateTime, nullable=True)

    # Числовой ID организации из ссылки раздела редактирования отзывов
    # (https://yandex.ru/sprav/<ID>/p/edit/reviews/) — нужен для точной
    # проверки "уже отвечен" через window.__PRELOAD_DATA (core/yandex_preload.py),
    # не требует OAuth-токена, только уже существующую cookie_file_path.
    yandex_permalink_id = Column(String(100), nullable=True)

    cities  = relationship("City",   back_populates="brand", cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="brand")

    def __repr__(self):
        return f"<Brand id={self.id} name={self.name!r}>"
    
    cookie_file_path = Column(String, nullable=True)



class City(Base):
    """
    Город/филиал бренда.
    Добавляется через /add_city.
    """
    __tablename__ = "cities"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    brand_id  = Column(Integer, ForeignKey("brands.id"), nullable=False)
    city_name = Column(String(200), nullable=False)

    brand = relationship("Brand", back_populates="cities")

    def __repr__(self):
        return f"<City id={self.id} city={self.city_name!r} brand_id={self.brand_id}>"


class Subscriber(Base):
    """
    Пользователи, подписавшиеся на уведомления через /start.

    Роль и привязка к бренду назначаются в веб-панели Click (не в самом боте):
    пока администратор не настроит подписчика вручную, он попадает как
    MANAGER без привязки к бренду (brand_id=None) — то есть видит всё,
    как было раньше, пока роли не разведены.
    """
    __tablename__ = 'subscribers'

    id            = Column(Integer, primary_key=True, autoincrement=True)
    chat_id       = Column(Integer, unique=True, nullable=False)
    subscribed_at = Column(DateTime, default=datetime.utcnow)

    display_name  = Column(String(200), nullable=True)   # Как подписчика видно в панели Click
    role          = Column(Enum(SubscriberRole), default=SubscriberRole.MANAGER, nullable=False)
    brand_id      = Column(Integer, ForeignKey("brands.id"), nullable=True)  # None = все бренды

    brand = relationship("Brand")

    def __repr__(self):
        return f"<Subscriber chat_id={self.chat_id} role={self.role} brand_id={self.brand_id}>"


class Review(Base):
    """
    Полная история обработанных отзывов.
    """
    __tablename__ = "reviews"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    brand_id         = Column(Integer, ForeignKey("brands.id"), nullable=False)
    city             = Column(String(200))
    platform         = Column(String(50))          # yandex / 2gis / google
    reviewer_name    = Column(String(200))
    rating           = Column(Integer)
    review_text      = Column(Text)
    review_url       = Column(String(1000))
    external_id      = Column(String(200))         # ID отзыва на площадке (для API)
    generated_reply  = Column(Text)                # Что сгенерил GPT
    final_reply      = Column(Text)                # Итоговый текст (после возможного редактирования)
    status           = Column(Enum(ReviewStatus), default=ReviewStatus.PENDING)
    telegram_msg_id  = Column(Integer)             # ID сообщения в Telegram (для edit)
    created_at       = Column(DateTime, default=datetime.utcnow)
    published_at     = Column(DateTime, nullable=True)
    error_message    = Column(Text, nullable=True) # Причина ошибки публикации

    brand = relationship("Brand", back_populates="reviews")

    def __repr__(self):
        return (
            f"<Review id={self.id} brand_id={self.brand_id} "
            f"rating={self.rating} status={self.status}>"
        )


# ---------------------------------------------------------------------------
# Движок и утилиты
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///review_bot.db")

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)


def init_db() -> None:
    """Создаёт все таблицы (если не существуют)."""
    Base.metadata.create_all(engine)


def get_session() -> Session:
    """Возвращает новую сессию БД. Не забудь закрыть после использования."""
    return Session(engine)
