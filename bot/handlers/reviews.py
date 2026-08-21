"""
Обработчики callback-кнопок под сообщениями с отзывами.

✅ approve:{review_id} — одобрить и опубликовать через Playwright
✏️ edit:{review_id}   — начать редактирование ответа (FSM)
"""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import get_review_keyboard, get_manual_publish_keyboard
from bot.states import EditReplyState
from core.database import Review, ReviewStatus, get_session
from core.publisher import publish_reply_for_review, handle_publish_result

logger = logging.getLogger(__name__)

router = Router()


# ---------------------------------------------------------------------------
# Одобрение отзыва
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("approve:"))
async def approve_review(callback: CallbackQuery, config) -> None:
    """
    Модератор нажал ✅ Одобрить и опубликовать.

    1. Находим отзыв в БД.
    2. Пробуем опубликовать через Playwright.
    3. Обновляем статус, редактируем сообщение в TG.
    """
    review_id = int(callback.data.split(":")[1])

    db = get_session()
    review = db.get(Review, review_id)

    if not review:
        await callback.answer("⚠️ Отзыв не найден в базе данных!", show_alert=True)
        db.close()
        return

    if review.status in (ReviewStatus.APPROVED, ReviewStatus.EDITED):
        await callback.answer("✅ Этот отзыв уже опубликован.", show_alert=True)
        db.close()
        return

    reply_text = review.final_reply or review.generated_reply or ""
    if not reply_text:
        await callback.answer("❌ Текст ответа пустой.", show_alert=True)
        db.close()
        return

    platform = (review.platform or "yandex").lower()

    # ── 2ГИС и Google — ручная публикация ────────────────────────────────
    if platform in ("2gis", "google"):
        await callback.answer("📋 Скопируйте ответ и вставьте вручную")
        try:
            await callback.message.edit_reply_markup(
                reply_markup=get_manual_publish_keyboard(review.id, review.review_url or "")
            )
        except Exception:
            pass
        await callback.message.answer(
            f"📋 <b>Готовый ответ для копирования</b> ({platform.upper()}):\n\n"
            f"<code>{reply_text}</code>\n\n"
            f"1. Нажмите на текст выше чтобы скопировать\n"
            f"2. Нажмите <b>🌐 Открыть отзыв</b> и вставьте ответ вручную\n"
            f"3. После публикации нажмите <b>✅ Отметить как отвеченный</b>",
            parse_mode="HTML",
        )
        db.close()
        return

    # ── Яндекс — автоматическая публикация через Playwright ───────────────
    await callback.answer("⏳ Публикуем ответ...")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    result = await publish_reply_for_review(review)

    await handle_publish_result(
        result      = result,
        review      = review,
        db          = db,
        bot         = callback.bot,
        chat_id     = callback.message.chat.id,
        message_id  = callback.message.message_id,
    )

    db.close()


# ---------------------------------------------------------------------------
# Начало редактирования
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("edit:"))
async def start_edit_reply(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Модератор нажал ✏️ Редактировать.
    Переводим в FSM-состояние ожидания нового текста.
    """
    review_id = int(callback.data.split(":")[1])

    db = get_session()
    review = db.get(Review, review_id)
    db.close()

    if not review:
        await callback.answer("⚠️ Отзыв не найден!", show_alert=True)
        return

    await state.update_data(review_id=review_id)
    await state.set_state(EditReplyState.waiting_for_text)

    current_text = review.final_reply or review.generated_reply or "(пусто)"

    await callback.message.answer(
        f"✏️ <b>Редактирование ответа</b>\n\n"
        f"<b>Текущий текст:</b>\n"
        f"<code>{current_text}</code>\n\n"
        f"Пришлите новый вариант ответа.\n"
        f"Или напишите /cancel чтобы отменить.",
        parse_mode="HTML",
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Сохранение отредактированного ответа
# ---------------------------------------------------------------------------

@router.message(EditReplyState.waiting_for_text)
async def save_edited_reply(message: Message, state: FSMContext) -> None:
    """
    Получаем отредактированный текст, сохраняем в БД.
    Предлагаем опубликовать.
    """
    data = await state.get_data()
    review_id = data.get("review_id")

    if not review_id:
        await state.clear()
        return

    new_text = message.text.strip()

    if not new_text:
        await message.answer("❌ Текст не может быть пустым. Попробуйте ещё раз.")
        return

    db = get_session()
    review = db.get(Review, review_id)

    if not review:
        await message.answer("⚠️ Отзыв не найден в БД.")
        await state.clear()
        db.close()
        return

    review.final_reply = new_text
    review.status      = ReviewStatus.EDITED
    db.commit()
    db.close()

    await state.clear()

    kb = get_review_keyboard(review_id)
    await message.answer(
        f"✅ <b>Ответ сохранён.</b> Опубликовать?\n\n"
        f"<code>{new_text}</code>",
        reply_markup=kb,
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Скопировать ответ (2ГИС / Google)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("copy_reply:"))
async def copy_reply(callback: CallbackQuery) -> None:
    review_id = int(callback.data.split(":")[1])
    db = get_session()
    review = db.get(Review, review_id)
    db.close()

    if not review:
        await callback.answer("⚠️ Отзыв не найден", show_alert=True)
        return

    reply_text = review.final_reply or review.generated_reply or ""
    await callback.answer("Текст выше — нажмите на него чтобы скопировать", show_alert=True)
    await callback.message.answer(
        f"<code>{reply_text}</code>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Отметить как отвеченный (2ГИС / Google — ручная публикация)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("mark_done:"))
async def mark_done(callback: CallbackQuery) -> None:
    review_id = int(callback.data.split(":")[1])
    db = get_session()
    review = db.get(Review, review_id)

    if not review:
        await callback.answer("⚠️ Отзыв не найден", show_alert=True)
        db.close()
        return

    from datetime import datetime
    review.status       = ReviewStatus.APPROVED
    review.published_at = datetime.utcnow()
    db.commit()
    db.close()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer("✅ Отмечен как отвеченный")
    await callback.message.answer(
        f"✅ <b>Отзыв отмечен как отвеченный.</b>\n"
        f"Отзыв ID: {review_id}",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Отмена
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "cancel")
@router.message(F.text == "/cancel")
async def cancel_action(event, state: FSMContext) -> None:
    await state.clear()
    text = "❌ Действие отменено."
    if isinstance(event, CallbackQuery):
        await event.message.answer(text)
        await event.answer()
    else:
        await event.answer(text)
