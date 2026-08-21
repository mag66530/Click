"""
Парсинг неотвеченных отзывов из Google Business Profile.
Использует сохранённую сессию браузера (setup_google_session.py).
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path(__file__).parent.parent / "google_sessions"


def _session_path(brand_name: str) -> Path:
    return SESSIONS_DIR / f"{brand_name}.json"


def has_session(brand_name: str) -> bool:
    return _session_path(brand_name).exists()


def _load_config(brand_name: str) -> dict:
    import json
    config_file = SESSIONS_DIR / f"{brand_name}_config.json"
    if config_file.exists():
        return json.loads(config_file.read_text(encoding="utf-8"))
    return {"reviews_url": "https://business.google.com/reviews?hl=ru"}


async def fetch_unanswered_google_reviews(brand_name: str) -> list[dict]:
    """
    Возвращает список неотвеченных отзывов из Google Business для бренда.

    Каждый отзыв — dict с ключами:
      reviewer_name, rating, review_text, review_url, platform="google"
    """
    session_file = _session_path(brand_name)
    if not session_file.exists():
        logger.warning(f"[{brand_name}] Нет Google-сессии. Запустите setup_google_session.py")
        return []

    cfg = _load_config(brand_name)
    reviews_url = cfg.get("reviews_url", "https://business.google.com/reviews?hl=ru")
    reviews = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                storage_state=str(session_file),
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            logger.info(f"[{brand_name}] Открываем Google Business отзывы: {reviews_url}")
            await page.goto(
                reviews_url,
                wait_until="networkidle",
                timeout=30_000,
            )

            # Проверяем — не выбросило ли нас на страницу входа
            if "accounts.google.com" in page.url or "signin" in page.url:
                logger.error(
                    f"[{brand_name}] Сессия истекла. Запустите setup_google_session.py заново."
                )
                await browser.close()
                return []

            # Ждём появления карточек отзывов
            try:
                await page.wait_for_selector(
                    "[data-review-id], .review-card, [jsname='Vf3gFd']",
                    timeout=15_000,
                )
            except PlaywrightTimeout:
                logger.warning(f"[{brand_name}] Отзывы не загрузились (возможно их нет)")
                await browser.close()
                return []

            # Прокручиваем страницу чтобы подгрузить все отзывы
            for _ in range(5):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1)

            # Ищем неотвеченные отзывы — у них есть кнопка "Ответить"
            review_blocks = await page.query_selector_all(
                "[data-review-id], .review-card, [jsname='Vf3gFd']"
            )

            logger.info(f"[{brand_name}] Найдено блоков отзывов: {len(review_blocks)}")

            for block in review_blocks:
                try:
                    # Проверяем: есть кнопка "Ответить" = не отвечен
                    reply_btn = await block.query_selector(
                        "button:has-text('Ответить'), button:has-text('Reply')"
                    )
                    if not reply_btn:
                        # Уже есть ответ — пропускаем
                        continue

                    # Имя автора
                    name_el = await block.query_selector(
                        "[class*='reviewer'], [class*='author'], .reviewer-name, "
                        "[jsname*='name'], h3"
                    )
                    reviewer_name = (await name_el.inner_text()).strip() if name_el else "Клиент"

                    # Рейтинг (считаем звёзды)
                    stars = await block.query_selector_all(
                        "img[src*='star_rate'], [aria-label*='звезд'], [class*='star']"
                    )
                    rating = len(stars) if stars else 0

                    # Если рейтинг не нашли через звёзды — ищем aria-label
                    if rating == 0:
                        rating_el = await block.query_selector("[aria-label*='из 5']")
                        if rating_el:
                            label = await rating_el.get_attribute("aria-label") or ""
                            import re
                            m = re.search(r'(\d)', label)
                            if m:
                                rating = int(m.group(1))

                    # Текст отзыва
                    text_el = await block.query_selector(
                        "[class*='review-text'], [jsname*='fbQN7e'], "
                        "[class*='comment'], p"
                    )
                    review_text = (await text_el.inner_text()).strip() if text_el else ""

                    # Ссылка на отзыв
                    link_el = await block.query_selector("a[href*='google.com']")
                    if link_el:
                        review_url = await link_el.get_attribute("href")
                    else:
                        import hashlib
                        key = f"{brand_name}:{reviewer_name}:{review_text[:100]}"
                        review_url = "google://review/" + hashlib.md5(key.encode()).hexdigest()

                    reviews.append({
                        "reviewer_name": reviewer_name,
                        "rating":        rating,
                        "review_text":   review_text,
                        "review_url":    review_url,
                        "platform":      "google",
                        "city":          "",
                    })
                    logger.info(
                        f"[{brand_name}] Неотвеченный отзыв: {reviewer_name}, {rating}★"
                    )

                except Exception as e:
                    logger.debug(f"[{brand_name}] Ошибка разбора блока отзыва: {e}")

            await browser.close()

    except Exception as e:
        logger.error(f"[{brand_name}] Ошибка Google-скрапера: {e}")

    logger.info(f"[{brand_name}] Неотвеченных Google-отзывов: {len(reviews)}")
    return reviews
