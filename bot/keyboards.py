"""
Inline-клавиатуры для Telegram-сообщений.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_review_keyboard(review_id: int) -> InlineKeyboardMarkup:
    """Кнопки под отзывом Яндекс — публикация через браузер."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Одобрить и опубликовать",
                callback_data=f"approve:{review_id}",
            ),
            InlineKeyboardButton(
                text="✏️ Редактировать",
                callback_data=f"edit:{review_id}",
            ),
        ]
    ])


def get_manual_publish_keyboard(review_id: int, review_url: str) -> InlineKeyboardMarkup:
    """Кнопки для 2ГИС/Google — ручная публикация (скопировать + открыть)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📋 Скопировать ответ",
                callback_data=f"copy_reply:{review_id}",
            ),
            InlineKeyboardButton(
                text="✏️ Редактировать",
                callback_data=f"edit:{review_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🌐 Открыть отзыв",
                url=review_url,
            ),
        ],
        [
            InlineKeyboardButton(
                text="✅ Отметить как отвеченный",
                callback_data=f"mark_done:{review_id}",
            ),
        ],
    ])


def get_brand_list_keyboard(brands: list) -> InlineKeyboardMarkup:
    """
    Список брендов для выбора при добавлении города.
    """
    buttons = [
        [InlineKeyboardButton(
            text=f"{b.id}. {b.name}",
            callback_data=f"select_brand:{b.id}",
        )]
        for b in brands
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])
