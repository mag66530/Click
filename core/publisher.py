"""
Публикация ответов на отзывы через браузер (Playwright).

КЛЮЧЕВАЯ ЛОГИКА МУЛЬТИ-АККАУНТНОСТИ:
  У каждого бренда (СМУ, ИМП, МПЭ) свой аккаунт Яндекс Бизнеса.
  Путь к файлу сессии хранится в поле brands.cookie_file_path.
  При публикации мы читаем именно тот файл, что привязан к бренду отзыва.

  Схема:
    review.brand_id → Brand.cookie_file_path → Playwright context → публикация

ВАЖНО:
  - Яндекс периодически обновляет вёрстку и добавляет SmartCaptcha.
  - CSS-селекторы нужно проверять при обновлениях сайта Яндекса.
  - Файлы сессий обновлять раз в 2-3 недели через save_cookies.py.
"""
import logging
import os
from datetime import datetime
from typing import Literal, TypedDict

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

from core.database import Brand, Review, ReviewStatus, get_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы
# ---------------------------------------------------------------------------

class PublishResult(TypedDict):
    success: bool
    reason: Literal['captcha', 'deleted', 'timeout', 'no_session', 'unknown', '']


# ---------------------------------------------------------------------------
# Вспомогательная функция: получить путь к файлу сессии бренда
# ---------------------------------------------------------------------------

def get_brand_cookie_path(brand: Brand) -> str | None:
    """
    Возвращает путь к файлу сессии для бренда, или None если не задан.

    Проверяет:
      1. Поле cookie_file_path в БД (основной источник).
      2. Что файл реально существует на диске.

    Если поле не заполнено или файл не существует — возвращает None.
    Тогда publisher вернёт reason='no_session' и бот отправит инструкцию.
    """
    if not brand.cookie_file_path:
        logger.warning(
            f'[{brand.name}] Поле cookie_file_path не заполнено в БД. '
            f'Запустите migrations/save_cookies.py'
        )
        return None

    if not os.path.exists(brand.cookie_file_path):
        logger.warning(
            f'[{brand.name}] Файл сессии не найден: {brand.cookie_file_path}. '
            f'Запустите migrations/save_cookies.py'
        )
        return None

    return brand.cookie_file_path


# ---------------------------------------------------------------------------
# Основная функция публикации
# ---------------------------------------------------------------------------

async def publish_reply_yandex(
    review_url: str,
    reply_text: str,
    cookie_file: str,
) -> PublishResult:
    """
    Публикует ответ на отзыв через Playwright (headless Chrome).

    Параметры:
      review_url  — прямая ссылка на отзыв в Яндекс Картах / Бизнесе
      reply_text  — текст ответа для публикации
      cookie_file — путь к JSON-файлу сессии (берётся из brand.cookie_file_path)

    Алгоритм:
      1. Загружаем сессию из cookie_file.
      2. Открываем страницу отзыва.
      3. Проверяем наличие капчи → возвращаем reason='captcha'.
      4. Проверяем, что отзыв существует → возвращаем reason='deleted'.
      5. Нажимаем кнопку «Ответить», вводим текст, нажимаем «Отправить».
      6. Проверяем успех → возвращаем success=True.

    Возвращает PublishResult с полями success и reason.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
            ],
        )

        context = await browser.new_context(
            storage_state=cookie_file,    # ← Сессия конкретного бренда
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            ),
            viewport={'width': 1280, 'height': 800},
            locale='ru-RU',
        )

        page = await context.new_page()

        try:
            logger.info(f'Открываем страницу отзыва: {review_url}')
            await page.goto(review_url, wait_until='networkidle', timeout=25000)

            # ── 1. Проверка капчи ──────────────────────────────────────────
            captcha_selectors = [
                '.SmartCaptcha',
                "[class*='captcha']",
                "[id*='captcha']",
                'text=Я не робот',
                'text=Подтвердите, что вы не робот',
            ]
            for sel in captcha_selectors:
                if await page.query_selector(sel):
                    logger.warning(f'Обнаружена капча: {review_url}')
                    return {'success': False, 'reason': 'captcha'}

            # ── 2. Проверка: отзыв существует ─────────────────────────────
            deleted_indicators = [
                'text=Отзыв не найден',
                'text=Страница не найдена',
                'text=404',
                "[class*='error-page']",
                "[class*='not-found']",
            ]
            for sel in deleted_indicators:
                if await page.query_selector(sel):
                    logger.warning(f'Отзыв не найден на странице: {review_url}')
                    return {'success': False, 'reason': 'deleted'}

            # ── 3. Кнопка «Ответить» ──────────────────────────────────────
            reply_btn = None
            reply_selectors = [
                "button:has-text('Ответить')",
                "a:has-text('Ответить на отзыв')",
                "[class*='reply-button']",
                "[aria-label*='Ответить']",
            ]
            for sel in reply_selectors:
                reply_btn = await page.query_selector(sel)
                if reply_btn:
                    break

            if not reply_btn:
                logger.error(
                    'Кнопка «Ответить» не найдена. '
                    'Возможно, вёрстка изменилась.'
                )
                return {'success': False, 'reason': 'unknown'}

            await reply_btn.click()
            await page.wait_for_timeout(1500)

            # ── 4. Поле ввода ─────────────────────────────────────────────
            textarea = await page.query_selector('textarea')
            if not textarea:
                # Яндекс иногда использует contenteditable-div
                textarea = await page.query_selector("[contenteditable='true']")

            if not textarea:
                logger.error('Поле ввода ответа не найдено')
                return {'success': False, 'reason': 'unknown'}

            await textarea.click()
            await textarea.fill(reply_text)
            await page.wait_for_timeout(800)

            # ── 5. Кнопка «Отправить» ─────────────────────────────────────
            submit_btn = None
            submit_selectors = [
                "button:has-text('Отправить')",
                "button[type='submit']",
                "[class*='submit']",
            ]
            for sel in submit_selectors:
                submit_btn = await page.query_selector(sel)
                if submit_btn:
                    break

            if not submit_btn:
                logger.error('Кнопка «Отправить» не найдена')
                return {'success': False, 'reason': 'unknown'}

            await submit_btn.click()
            await page.wait_for_timeout(2500)

            # ── 6. Проверка успеха ─────────────────────────────────────────
            success_selectors = [
                'text=Ответ опубликован',
                'text=Ваш ответ сохранён',
                'text=Ответ отправлен',
                "[class*='success']",
            ]
            for sel in success_selectors:
                if await page.query_selector(sel):
                    logger.info(f'Ответ успешно опубликован: {review_url}')
                    return {'success': True, 'reason': ''}

            # Если явного подтверждения нет — делаем скриншот для отладки
            os.makedirs('logs', exist_ok=True)
            await page.screenshot(path='logs/publish_debug.png')
            logger.warning(
                'Индикатор успеха не найден. '
                'Скриншот сохранён: logs/publish_debug.png'
            )
            return {'success': False, 'reason': 'unknown'}

        except PWTimeout:
            logger.error(f'Таймаут Playwright при публикации: {review_url}')
            return {'success': False, 'reason': 'timeout'}

        except Exception as e:
            logger.error(f'Ошибка Playwright: {e}')
            return {'success': False, 'reason': 'unknown'}

        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Обёртка для handler'а: получает сессию бренда и вызывает publish
# ---------------------------------------------------------------------------

async def publish_reply_for_review(review: Review) -> PublishResult:
    """
    Публикует ответ на отзыв, автоматически выбирая сессию нужного бренда.

    Это главная точка входа из handlers/reviews.py.
    Сама читает brand и cookie_file_path из объекта review.

    Если сессия не настроена — возвращает reason='no_session'
    (бот покажет инструкцию в Telegram).
    """
    # Получаем бренд из БД (у review уже есть brand через relationship)
    brand = review.brand
    if brand is None:
        # Бренд не подгружен — читаем из БД
        db = get_session()
        try:
            brand = db.get(Brand, review.brand_id)
        finally:
            db.close()

    if brand is None:
        logger.error(f'Бренд для отзыва {review.id} не найден в БД!')
        return {'success': False, 'reason': 'unknown'}

    # Проверяем наличие файла сессии
    cookie_file = get_brand_cookie_path(brand)
    if cookie_file is None:
        logger.error(
            f'Нет файла сессии для бренда "{brand.name}". '
            f'Запустите: python migrations/save_cookies.py'
        )
        return {'success': False, 'reason': 'no_session'}

    reply_text = review.final_reply or review.generated_reply or ''
    if not reply_text:
        logger.error(f'Текст ответа для отзыва {review.id} пустой!')
        return {'success': False, 'reason': 'unknown'}

    logger.info(
        f'Публикуем ответ для отзыва {review.id} '
        f'(бренд: {brand.name}, сессия: {cookie_file})'
    )

    return await publish_reply_yandex(
        review_url=review.review_url,
        reply_text=reply_text,
        cookie_file=cookie_file,
    )


# ---------------------------------------------------------------------------
# Обработка результата публикации → обновление БД + уведомление в Telegram
# ---------------------------------------------------------------------------

async def handle_publish_result(
    result: PublishResult,
    review: Review,
    db,
    bot,
    chat_id: int,
    message_id: int,
) -> None:
    """
    Обрабатывает результат попытки публикации:
      - Обновляет статус в БД.
      - Редактирует или отправляет новое сообщение в Telegram.

    Вызывается из handlers/reviews.py после publish_reply_for_review().
    """
    # Получаем название бренда для сообщений
    brand_name = review.brand.name if review.brand else f'бренд ID {review.brand_id}'

    # ── Успех ────────────────────────────────────────────────────────────
    if result['success']:
        review.status       = ReviewStatus.APPROVED
        review.published_at = datetime.utcnow()
        db.commit()

        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=None,
            )
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f'✅ <b>Ответ опубликован!</b>\n'
                    f'Отзыв ID: {review.id} | {brand_name} | {review.city}'
                ),
                parse_mode='HTML',
            )
        except Exception as e:
            logger.error(f'Ошибка редактирования сообщения TG: {e}')
        return

    # ── Ошибка ───────────────────────────────────────────────────────────
    reason = result.get('reason', 'unknown')
    reply_to_copy = review.final_reply or review.generated_reply or ''

    if reason == 'no_session':
        # Самый важный кейс — нет файла сессии для этого бренда
        review.status        = ReviewStatus.FAILED
        review.error_message = f'Нет сессии для бренда "{brand_name}"'
        error_text = (
            f'🔐 <b>Нет сохранённой сессии для бренда «{brand_name}».</b>\n\n'
            f'Запустите в терминале:\n'
            f'<code>python migrations/save_cookies.py</code>\n\n'
            f'Выберите бренд <b>{brand_name}</b> и войдите в его аккаунт.'
        )

    elif reason == 'deleted':
        review.status        = ReviewStatus.DELETED
        review.error_message = 'Отзыв удалён площадкой во время публикации'
        error_text = (
            '❌ <b>Ошибка публикации.</b>\n'
            'Отзыв удалён площадкой или требуется ручная проверка.'
        )

    elif reason == 'captcha':
        review.status        = ReviewStatus.FAILED
        review.error_message = 'Яндекс показал капчу'
        error_text = (
            '⚠️ <b>Яндекс показал капчу.</b>\n'
            'Опубликуйте ответ вручную — это займёт 10 секунд:\n\n'
            f'<b>Скопируйте ответ:</b>\n'
            f'<code>{reply_to_copy}</code>\n\n'
            f'🔗 <a href=\'{review.review_url}\'>Открыть отзыв</a>'
        )

    elif reason == 'timeout':
        review.status        = ReviewStatus.FAILED
        review.error_message = 'Таймаут Playwright'
        error_text = (
            '❌ <b>Ошибка публикации (таймаут).</b>\n'
            'Опубликуйте ответ вручную:\n\n'
            f'<b>Текст ответа:</b>\n'
            f'<code>{reply_to_copy}</code>\n\n'
            f'🔗 <a href=\'{review.review_url}\'>Открыть отзыв</a>'
        )

    else:
        review.status        = ReviewStatus.FAILED
        review.error_message = 'Неизвестная ошибка Playwright'
        error_text = (
            '❌ <b>Ошибка публикации.</b>\n'
            'Опубликуйте ответ вручную:\n\n'
            f'<b>Текст ответа:</b>\n'
            f'<code>{reply_to_copy}</code>\n\n'
            f'🔗 <a href=\'{review.review_url}\'>Открыть отзыв</a>'
        )

    db.commit()

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=error_text,
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f'Ошибка отправки сообщения об ошибке публикации: {e}')