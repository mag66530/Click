"""
Планировщик задач и главный pipeline обработки отзывов.

Запускается внутри бота (bot/main.py).
По расписанию обходит все бренды в БД и собирает новые отзывы.

Pipeline одного отзыва:
  1. Проверить URL (жив ли отзыв)
  2. Сохранить в БД
  3. Негатив (1-3★) → уведомить в Telegram без GPT
  4. Позитив (4-5★) → сгенерировать ответ через GPT
  5. Отправить в Telegram с кнопками [Одобрить] / [Редактировать]
"""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.database import Brand, Review, ReviewStatus, Subscriber, SubscriberRole, get_session
from core.gpt_client import generate_reply
from core.imap_parser import fetch_new_reviews_from_imap
from core.google_scraper import fetch_unanswered_google_reviews, has_session
from core.review_validator import check_review_alive, check_already_answered

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Рассылка по ролям
# ---------------------------------------------------------------------------
#
# Роли назначаются в веб-панели Click, не в боте:
#   OWNER   (начальство)         — только негатив, коротким алертом
#   MANAGER (ответственный)      — все отзывы СВОЕГО бренда, полная карточка
#   VIEWER  (просто в курсе)     — одна строка о факте нового отзыва
#
# Подписчик без привязки к бренду (brand_id=None) видит все бренды —
# это поведение по умолчанию для ещё не настроенных через панель подписчиков.

def _recipients(db, brand_id: int, roles: tuple) -> list[Subscriber]:
    """Подписчики с одной из ролей `roles`, видящие бренд brand_id."""
    subs = db.query(Subscriber).filter(Subscriber.role.in_(roles)).all()
    return [s for s in subs if s.brand_id in (None, brand_id)]


async def _send_to(bot, subscribers: list[Subscriber], text: str, **kwargs) -> int | None:
    """Шлёт текст списку подписчиков. Возвращает message_id последней отправки."""
    last_msg_id = None
    for sub in subscribers:
        try:
            sent = await bot.send_message(sub.chat_id, text, **kwargs)
            last_msg_id = sent.message_id
        except Exception as e:
            logger.warning(f"Не удалось отправить подписчику {sub.chat_id}: {e}")
    return last_msg_id


async def notify_all_subscribers(bot, text: str, **kwargs) -> None:
    """Отправляет сообщение всем подписчикам вне зависимости от роли.
    Используется для служебных уведомлений (ошибки без привязки к бренду и т.п.)."""
    db = get_session()
    subscribers = db.query(Subscriber).all()
    db.close()
    await _send_to(bot, subscribers, text, **kwargs)


# ---------------------------------------------------------------------------
# Pipeline одного отзыва
# ---------------------------------------------------------------------------

async def process_single_review(
    review_data: dict,
    brand: Brand,
    bot,
    config,
) -> bool:
    """
    Полный цикл обработки одного отзыва.
    Возвращает True если отзыв новый и был обработан, False если пропущен.
    """
    from bot.keyboards import get_review_keyboard  # локальный импорт

    db = get_session()

    try:
        url         = review_data.get("review_url", "")
        rating      = int(review_data.get("rating", 0))
        review_text = (review_data.get("review_text") or "").strip()

        # ── Проверка: текст отзыва не должен быть пустым ─────────────────
        if len(review_text) < 15:
            logger.warning(
                f"[{brand.name}] Отзыв без текста (len={len(review_text)}), пропускаем. URL: {url}"
            )
            return False

        # ── Проверка дубликата по URL ─────────────────────────────────────
        if url and db.query(Review).filter_by(review_url=url).first():
            logger.info(f"[{brand.name}] Отзыв уже в БД, пропускаем: {url}")
            return False

        # ── Шаг 1: Проверка — жив ли отзыв ──────────────────────────────
        if url:
            alive_status = await check_review_alive(url)
            if alive_status == "deleted":
                review = Review(
                    brand_id      = brand.id,
                    city          = review_data.get("city"),
                    platform      = review_data.get("platform", "unknown"),
                    reviewer_name = review_data.get("reviewer_name"),
                    rating        = rating,
                    review_text   = review_data.get("review_text"),
                    review_url    = url,
                    status        = ReviewStatus.DELETED,
                    error_message = "Удалён площадкой до обработки",
                )
                db.add(review)
                db.commit()
                logger.info(f"[{brand.name}] Отзыв удалён площадкой, пропускаем: {url}")
                return
            # "unknown" → обрабатываем на всякий случай

        # ── Шаг 1б: Проверка — уже отвечен? ─────────────────────────────
        if url:
            already_answered = await check_already_answered(url)
            if already_answered:
                review = Review(
                    brand_id      = brand.id,
                    city          = review_data.get("city"),
                    platform      = review_data.get("platform", "unknown"),
                    reviewer_name = review_data.get("reviewer_name"),
                    rating        = rating,
                    review_text   = review_data.get("review_text"),
                    review_url    = url,
                    status        = ReviewStatus.APPROVED,
                    error_message = "Уже отвечен на площадке",
                )
                db.add(review)
                db.commit()
                logger.info(f"[{brand.name}] Отзыв уже имеет ответ на площадке, пропускаем: {url}")
                return False

        is_negative = rating <= 3

        # ── Шаг 2: Сохранение в БД ───────────────────────────────────────
        review = Review(
            brand_id      = brand.id,
            city          = review_data.get("city", "Неизвестен"),
            platform      = review_data.get("platform", "unknown"),
            reviewer_name = review_data.get("reviewer_name", "Клиент"),
            rating        = rating,
            review_text   = review_data.get("review_text", ""),
            review_url    = url,
            status        = ReviewStatus.NEGATIVE if is_negative else ReviewStatus.PENDING,
        )
        db.add(review)
        db.commit()
        db.refresh(review)

        stars = "⭐" * rating if rating > 0 else "—"

        # ── Шаг 3: Негатив → алерт без GPT, по ролям ─────────────────────
        if is_negative:
            db3 = get_session()
            owners   = _recipients(db3, brand.id, (SubscriberRole.OWNER,))
            managers = _recipients(db3, brand.id, (SubscriberRole.MANAGER,))
            viewers  = _recipients(db3, brand.id, (SubscriberRole.VIEWER,))
            db3.close()

            # Начальству — коротко и по делу, без простыни текста.
            owner_text = (
                f"🔴 <b>Негатив: {brand.name}</b> ({review.city})\n"
                f"{stars} ({rating}/5) · {review.reviewer_name}\n\n"
                f"«{(review.review_text or '')[:200]}»\n\n"
                f"🔗 <a href='{url}'>Открыть отзыв</a>"
            )
            await _send_to(bot, owners, owner_text, parse_mode="HTML", disable_web_page_preview=True)

            # Ответственному — полная карточка (без кнопок: ответ не генерируем автоматически).
            manager_text = (
                f"🔴 <b>Новый негативный отзыв</b>\n\n"
                f"Бренд: <b>{brand.name}</b>\n"
                f"Город: {review.city}\n"
                f"Оценка: {stars} ({rating}/5)\n"
                f"Автор: {review.reviewer_name}\n\n"
                f"<i>{(review.review_text or '')[:400]}</i>\n\n"
                f"🔗 <a href='{url}'>Открыть отзыв</a>\n\n"
                f"⚠️ Ответ не генерируется автоматически — при необходимости ответьте вручную."
            )
            await _send_to(bot, managers, manager_text, parse_mode="HTML", disable_web_page_preview=True)

            # Просто «в курсе» — одна строка, без текста отзыва.
            viewer_text = f"📩 Новый отзыв: <b>{brand.name}</b>, {review.city} — {stars} (негатив)"
            await _send_to(bot, viewers, viewer_text, parse_mode="HTML")

            logger.info(f"[{brand.name}] Негатив отправлен: rating={rating}")
            return True

        # ── Шаг 4: Позитив → генерация ответа GPT ────────────────────────
        try:
            generated = await generate_reply(
                brand_prompt_template = brand.prompt_template,
                reviewer_name         = review.reviewer_name,
                review_text           = review.review_text or "",
                city                  = review.city or "",
            )
            review.generated_reply = generated
            db.commit()

        except Exception as e:
            logger.error(f"[{brand.name}] Ошибка GPT для отзыва {review.id}: {e}")
            db_err = get_session()
            managers = _recipients(db_err, brand.id, (SubscriberRole.MANAGER,))
            db_err.close()
            await _send_to(
                bot, managers,
                f"⚠️ Не удалось сгенерировать ответ (GPT ошибка).\n"
                f"Отзыв ID: {review.id} | {brand.name} | {review.city}\n"
                f"🔗 <a href='{url}'>Открыть отзыв</a>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True

        # ── Шаг 5: Отправка ответственному (модерация) + просто «в курсе» ──
        tg_text = (
            f"✨ <b>Новый отзыв</b> — {brand.name}\n\n"
            f"Город: {review.city}\n"
            f"Оценка: {stars} ({rating}/5)\n"
            f"Автор: {review.reviewer_name}\n\n"
            f"📝 <b>Текст отзыва:</b>\n"
            f"<i>{(review.review_text or '')[:600]}</i>\n\n"
            f"🤖 <b>Сгенерированный ответ:</b>\n"
            f"{generated}\n\n"
            f"🔗 <a href='{url}'>Ссылка на отзыв</a>"
        )

        kb = get_review_keyboard(review.id)

        db2 = get_session()
        managers = _recipients(db2, brand.id, (SubscriberRole.MANAGER,))
        viewers  = _recipients(db2, brand.id, (SubscriberRole.VIEWER,))
        db2.close()

        last_msg_id = await _send_to(
            bot, managers, tg_text,
            reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True,
        )
        await _send_to(
            bot, viewers,
            f"📩 Новый отзыв: <b>{brand.name}</b>, {review.city} — {stars}",
            parse_mode="HTML",
        )

        review.telegram_msg_id = last_msg_id
        db.commit()

        logger.info(
            f"[{brand.name}] Отзыв ID={review.id} отправлен {len(managers)} ответственным, "
            f"{len(viewers)} наблюдателям"
        )
        return True

    except Exception as e:
        logger.error(f"[{brand.name}] Критическая ошибка pipeline: {e}")
        db.rollback()
        return False

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Задача планировщика
# ---------------------------------------------------------------------------

async def run_imap_check(bot, config) -> None:
    """
    Обходит все бренды в БД, читает почту, запускает pipeline.
    Вызывается планировщиком каждые 15 минут.
    """
    db = get_session()
    brands = db.query(Brand).all()
    db.close()

    if not brands:
        logger.debug("Нет брендов в БД, пропускаем проверку почты")
        return

    results = []

    for brand in brands:
        logger.info(f"Проверяем почту бренда: {brand.name}")
        try:
            reviews_data = fetch_new_reviews_from_imap(brand)

            # Google Business (Playwright) — если есть сессия
            if has_session(brand.name):
                google_reviews = await fetch_unanswered_google_reviews(brand.name)
                for r in google_reviews:
                    r["brand_id"]   = brand.id
                    r["brand_name"] = brand.name
                reviews_data.extend(google_reviews)
                logger.info(f"[{brand.name}] Google отзывов: {len(google_reviews)}")

            logger.info(f"[{brand.name}] Всего отзывов для обработки: {len(reviews_data)}")

            brand_new = 0
            for review_data in reviews_data:
                was_new = await process_single_review(review_data, brand, bot, config)
                if was_new:
                    brand_new += 1

            results.append((brand.name, brand_new, None))

        except Exception as e:
            logger.error(f"Ошибка при обработке бренда {brand.name}: {e}")
            results.append((brand.name, 0, str(e)))

    return results


def setup_scheduler(bot, config) -> AsyncIOScheduler:
    """
    Создаёт и настраивает планировщик APScheduler.
    Вызывается один раз при старте бота.
    """
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    # Проверка почты каждые 15 минут
    scheduler.add_job(
        run_imap_check,
        trigger="interval",
        minutes=15,
        args=[bot, config],
        id="imap_check",
        name="Проверка почты (IMAP)",
        replace_existing=True,
        max_instances=1,  # Не запускать параллельно
    )

    return scheduler
