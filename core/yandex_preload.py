"""
Точная проверка "отвечен ли отзыв" на Яндекс.Бизнесе через window.__PRELOAD_DATA.

Приём подсмотрен и портирован из уже проверенного на живой карточке кода
Click (yb_playwright.py, раздел «ОТЗЫВЫ»): Яндекс кладёт в открытую в браузере
страницу редактирования отзывов ГОТОВЫЙ JSON со списком отзывов —
window.__PRELOAD_DATA.initialState.edit.reviews.list. У каждого отзыва есть
owner_comment — если там есть непустой text, компания уже ответила. Это
факт из данных страницы, а не догадка по вёрстке или ключевым словам —
редизайн Яндекса такую проверку не ломает (см. комментарий в yb_playwright.py).

Почему это лучше, чем core/review_validator.check_already_answered():
  - та функция скачивает HTML без выполнения JS — а __PRELOAD_DATA кладётся
    в window уже ПОСЛЕ загрузки страницы её собственным JS-кодом Яндекса,
    поэтому без браузера с реальным JS его просто не существует в ответе
  - та функция проверяет страницу ОРГАНИЗАЦИИ целиком, а не конкретный
    отзыв — здесь же получаем статус каждого отзыва по отдельности

Требует: залогиненную Playwright-сессию бренда (Brand.cookie_file_path —
тот же файл, что уже используется для публикации ответов, core/publisher.py)
и Brand.yandex_permalink_id — числовой ID организации из ссылки раздела
редактирования отзывов (https://yandex.ru/sprav/<ID>/p/edit/reviews/).
"""
import logging
import os

from playwright.async_api import async_playwright

from core.yandex_api_client import review_signature

logger = logging.getLogger(__name__)

REVIEWS_WAIT_MS = 15_000

# Тот же скрипт, что читает состояние страницы в yb_playwright.py —
# сознательно не переписан заново, чтобы не разойтись с проверенной версией.
_READ_REVIEWS_JS = r"""
() => {
  const st = ((((window.__PRELOAD_DATA || {}).initialState || {}).edit || {}).reviews) || {};
  const list = st.list || {};
  const items = (list.items || []).map(it => ({
    id: it.id,
    author: ((it.author || {}).user || ''),
    rating: it.rating || 0,
    text: it.full_text || it.snippet || '',
    answered: !!((it.owner_comment || {}).text || '').trim(),
  }));
  return items;
}
"""


async def fetch_answered_signatures(brand) -> set | None:
    """
    Открывает раздел отзывов бренда в headless-браузере (сессия из
    Brand.cookie_file_path) и возвращает множество сигнатур ОТВЕЧЕННЫХ
    отзывов. None — если у бренда не настроены сессия или permalink_id,
    либо если что-то пошло не так (тогда вызывающий код должен
    использовать запасной вариант проверки).
    """
    cookie_file = getattr(brand, "cookie_file_path", None)
    permalink_id = getattr(brand, "yandex_permalink_id", None)

    if not cookie_file or not os.path.exists(cookie_file):
        logger.debug(f"[{brand.name}] Нет сессии Яндекса (cookie_file_path) — пропускаем PRELOAD-проверку")
        return None
    if not permalink_id:
        logger.debug(f"[{brand.name}] Не задан yandex_permalink_id — пропускаем PRELOAD-проверку")
        return None

    url = f"https://yandex.ru/sprav/{permalink_id}/p/edit/reviews/"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=cookie_file)
            page = await context.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            items = None
            import time
            deadline = time.monotonic() + REVIEWS_WAIT_MS / 1000
            while time.monotonic() < deadline:
                try:
                    data = await page.evaluate(_READ_REVIEWS_JS)
                except Exception:
                    data = None
                if data is not None:
                    items = data
                    break
                await page.wait_for_timeout(400)

            await browser.close()

        if items is None:
            logger.warning(
                f"[{brand.name}] Не нашли window.__PRELOAD_DATA на странице отзывов — "
                f"Яндекс мог поменять отдачу страницы, либо сессия истекла"
            )
            return None

        answered = {
            review_signature(it.get("rating"), it.get("author"), it.get("text"))
            for it in items if it.get("answered")
        }
        logger.info(f"[{brand.name}] PRELOAD_DATA: {len(items)} отзывов на странице, {len(answered)} отвечено")
        return answered

    except Exception as e:
        logger.warning(f"[{brand.name}] Ошибка при чтении __PRELOAD_DATA: {e}")
        return None
