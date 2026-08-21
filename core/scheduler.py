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

from core.database import Brand, Review, ReviewStatus, Subscriber, get_session
from core.gpt_client import generate_reply
from core.imap_parser import fetch_new_reviews_from_imap
from core.google_scraper import fetch_unanswered_google_reviews, has_session
from core.review_validator import check_review_alive, check_already_answered

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Рассылка всем подписчикам
# ---------------------------------------------------------------------------

async def notify_all_subscribers(bot, text: str, **kwargs) -> None:
    """Отправляет сообщение всем подписчикам. Возвращает список message_id."""
    db = get_session()
    subscribers = db.query(Subscriber).all()
    chat_ids = [s.chat_id for s in subscribers]
    db.close()

    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text, **kwargs)
        except Exception as e:
            logger.warning(f"Не удалось отправить подписчику {chat_id}: {e}")


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

        # ── Шаг 3: Негатив → алерт без GPT ──────────────────────────────
        if is_negative:
            text = (
                f"🚨 <b>НЕГАТИВ!</b>\n\n"
                f"Бренд: <b>{brand.name}</b>\n"
                f"Город: {review.city}\n"
                f"Оценка: {stars} ({rating}/5)\n"
                f"Автор: {review.reviewer_name}\n\n"
                f"<i>{(review.review_text or '')[:400]}</i>\n\n"
                f"🔗 <a href='{url}'>Открыть отзыв</a>"
            )
            await notify_all_subscribers(
                bot, text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            logger.info(f"[{brand.name}] Негатив отправлен в Telegram: rating={rating}")
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
            await notify_all_subscribers(
                bot,
                f"⚠️ Не удалось сгенерировать ответ (GPT ошибка).\n"
                f"Отзыв ID: {review.id} | {brand.name} | {review.city}\n"
                f"🔗 <a href='{url}'>Открыть отзыв</a>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True

        # ── Шаг 5: Отправка модератору ────────────────────────────────────
        tg_text = (
            f"✨ <b>Новый отзыв!</b>\n\n"
            f"Бренд: <b>{brand.name}</b>\n"
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
        subscribers = db2.query(Subscriber).all()
        chat_ids = [s.chat_id for s in subscribers]
        db2.close()

        last_msg_id = None
        for chat_id in chat_ids:
            try:
                sent = await bot.send_message(
                    chat_id,
                    tg_text,
                    reply_markup=kb,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                last_msg_id = sent.message_id
            except Exception as e:
                logger.warning(f"Не удалось отправить подписчику {chat_id}: {e}")

        review.telegram_msg_id = last_msg_id
        db.commit()

        logger.info(
            f"[{brand.name}] Отзыв ID={review.id} отправлен {len(chat_ids)} подписчикам"
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
