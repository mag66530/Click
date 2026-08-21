"""
Административные команды бота.
Доступны только пользователям из списка ADMIN_IDS (из .env).

Команды:
  /add_brand    — добавить новый бренд
  /add_city     — добавить город к бренду
  /list_brands  — показать все бренды
  /edit_prompt  — изменить промт бренда
  /check_now    — принудительно запустить проверку почты
"""
import logging
import os

import os
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot.keyboards import get_brand_list_keyboard, get_cancel_keyboard, get_review_keyboard, get_manual_publish_keyboard
from bot.states import AddBrandState, AddCityState, EditBrandPromptState
from core.database import Brand, City, Review, ReviewStatus, Subscriber, get_session
from sqlalchemy.orm import joinedload

logger = logging.getLogger(__name__)

router = Router()


def get_admin_ids() -> list[int]:
    """Загружает список admin ID из .env."""
    raw = os.getenv("ADMIN_IDS", "")
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]


def is_admin(user_id: int) -> bool:
    return user_id in get_admin_ids()

# --- НОВЫЙ БЛОК: ПРИВЕТСТВИЕ И ИНСТРУКЦИЯ ---

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    db = get_session()
    existing = db.query(Subscriber).filter_by(chat_id=message.chat.id).first()
    if not existing:
        db.add(Subscriber(chat_id=message.chat.id))
        db.commit()
        subscribed = True
    else:
        subscribed = False
    db.close()

    if subscribed:
        text = (
            "✅ Вы подписались на уведомления об отзывах!\n\n"
            "Как только придёт новый отзыв — я сразу пришлю его сюда.\n\n"
            "Для отписки напишите /stop"
        )
    else:
        text = (
            "👋 Вы уже подписаны на уведомления.\n\n"
            "Для отписки напишите /stop"
        )

    if is_admin(message.from_user.id):
        text += (
            "\n\n<b>Команды администратора:</b>\n"
            "/status — сводка по боту\n"
            "/check_now — проверить почту сейчас\n"
            "/pending — неотвеченные отзывы\n"
            "/logs — последние 30 строк лога\n"
            "/logs_file — скачать полный лог\n"
            "/list_brands — список компаний\n"
            "/add_brand — добавить компанию\n"
            "/edit_prompt — изменить промпт"
        )

    await message.answer(text, parse_mode="HTML")


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    db = get_session()
    sub = db.query(Subscriber).filter_by(chat_id=message.chat.id).first()
    if sub:
        db.delete(sub)
        db.commit()
        await message.answer("❌ Вы отписались от уведомлений. Напишите /start чтобы подписаться снова.")
    else:
        await message.answer("Вы не подписаны на уведомления.")
    db.close()


def admin_only(func):
    """Декоратор: проверяет, что пользователь — администратор."""
    async def wrapper(event, *args, **kwargs):
        user_id = (
            event.from_user.id
            if isinstance(event, (Message, CallbackQuery))
            else None
        )
        if not user_id or not is_admin(user_id):
            if isinstance(event, Message):
                await event.answer("❌ У вас нет прав администратора.")
            elif isinstance(event, CallbackQuery):
                await event.answer("❌ Нет доступа.", show_alert=True)
            return
        return await func(event, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# /list_brands — список брендов
# ---------------------------------------------------------------------------

@router.message(Command("list_brands"))
async def cmd_list_brands(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Нет доступа.")

    db = get_session()
    # Добавили .options(joinedload(Brand.cities)) чтобы не было ошибки DetachedInstanceError
    brands = db.query(Brand).options(joinedload(Brand.cities)).all()
    db.close()

    if not brands:
        return await message.answer(
            "Брендов нет. Добавьте первый командой /add_brand"
        )

    lines = ["<b>Список брендов:</b>\n"]
    for b in brands:
        city_count = len(b.cities)
        lines.append(
            f"<b>{b.id}. {b.name}</b>\n"
            f"   📧 {b.imap_login}\n"
            f"   🏙️ Городов: {city_count}\n"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")

# ---------------------------------------------------------------------------
# /add_brand — добавление бренда (многошаговый диалог)
# ---------------------------------------------------------------------------

@router.message(Command("add_brand"))
async def cmd_add_brand(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Нет доступа.")

    await state.set_state(AddBrandState.waiting_name)
    await message.answer(
        "➕ <b>Добавление нового бренда</b>\n\n"
        "Шаг 1/3: Введите название бренда.\n"
        "Пример: <code>Инметпром</code>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(),
    )


@router.message(AddBrandState.waiting_name)
async def brand_step_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if len(name) < 2:
        return await message.answer("❌ Слишком короткое название. Попробуйте ещё раз.")

    # Проверяем, что бренд с таким именем ещё не существует
    db = get_session()
    exists = db.query(Brand).filter_by(name=name).first()
    db.close()

    if exists:
        return await message.answer(
            f"⚠️ Бренд <b>{name}</b> уже существует (ID: {exists.id}).\n"
            "Введите другое название или нажмите Отмена.",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard(),
        )

    await state.update_data(name=name)
    await state.set_state(AddBrandState.waiting_prompt)
    await message.answer(
        f"✅ Название: <b>{name}</b>\n\n"
        f"Шаг 2/3: Введите промт для GPT.\n\n"
        f"Используйте переменные:\n"
        f"  <code>{{name}}</code> — имя автора отзыва\n"
        f"  <code>{{text}}</code> — текст отзыва\n"
        f"  <code>{{city}}</code> — город филиала\n\n"
        f"<b>Пример:</b>\n"
        f"<code>Напиши ответ на отзыв от лица компании Инметпром. "
        f"Отзыв написал {{name}}. Текст: {{text}}. "
        f"Ответ заканчивай так: 'С уважением, команда Инметпром.'</code>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(),
    )


@router.message(AddBrandState.waiting_prompt)
async def brand_step_prompt(message: Message, state: FSMContext) -> None:
    prompt = message.text.strip()
    if len(prompt) < 20:
        return await message.answer("❌ Промт слишком короткий. Попробуйте ещё раз.")

    await state.update_data(prompt=prompt)
    await state.set_state(AddBrandState.waiting_imap)
    await message.answer(
        "Шаг 3/3: Введите IMAP-данные в одну строку:\n\n"
        "<code>логин пароль imap-сервер</code>\n\n"
        "Пример:\n"
        "<code>reviews@inmetprom.ru MySecretPass123 imap.yandex.ru</code>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(),
    )


@router.message(AddBrandState.waiting_imap)
async def brand_step_imap(message: Message, state: FSMContext) -> None:
    parts = message.text.strip().split()
    if len(parts) != 3:
        return await message.answer(
            "❌ Неверный формат. Нужно три значения через пробел:\n"
            "<code>логин пароль imap-сервер</code>",
            parse_mode="HTML",
        )

    login, password, server = parts
    data = await state.get_data()

    db = get_session()
    brand = Brand(
        name            = data["name"],
        prompt_template = data["prompt"],
        imap_login      = login,
        imap_password   = password,
        imap_server     = server,
    )
    db.add(brand)
    db.commit()
    db.refresh(brand)
    db.close()

    await state.clear()
    await message.answer(
        f"✅ <b>Бренд добавлен!</b>\n\n"
        f"ID: {brand.id}\n"
        f"Название: {brand.name}\n"
        f"Почта: {login}\n\n"
        f"Теперь добавьте города командой /add_city",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /add_city — добавление города
# ---------------------------------------------------------------------------

@router.message(Command("add_city"))
async def cmd_add_city(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Нет доступа.")

    db = get_session()
    brands = db.query(Brand).all()
    db.close()

    if not brands:
        return await message.answer(
            "⚠️ Нет ни одного бренда. Сначала добавьте /add_brand"
        )

    await state.set_state(AddCityState.waiting_brand_id)
    await message.answer(
        "🏙️ <b>Добавление города</b>\n\nВыберите бренд:",
        parse_mode="HTML",
        reply_markup=get_brand_list_keyboard(brands),
    )


@router.callback_query(F.data.startswith("select_brand:"), AddCityState.waiting_brand_id)
async def city_brand_selected(callback: CallbackQuery, state: FSMContext) -> None:
    brand_id = int(callback.data.split(":")[1])
    await state.update_data(brand_id=brand_id)
    await state.set_state(AddCityState.waiting_city)

    db = get_session()
    brand = db.get(Brand, brand_id)
    db.close()

    await callback.message.edit_text(
        f"Бренд: <b>{brand.name}</b>\n\n"
        f"Введите название города (можно несколько через запятую):\n"
        f"<code>Казань, Уфа, Самара</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AddCityState.waiting_city)
async def city_name_received(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    brand_id = data["brand_id"]

    # Поддержка нескольких городов через запятую
    city_names = [c.strip() for c in message.text.split(",") if c.strip()]

    db = get_session()
    brand = db.get(Brand, brand_id)

    added = []
    for city_name in city_names:
        city = City(brand_id=brand_id, city_name=city_name)
        db.add(city)
        added.append(city_name)

    db.commit()
    db.close()

    await state.clear()
    cities_str = ", ".join(added)
    await message.answer(
        f"✅ Добавлено городов: {len(added)}\n"
        f"Бренд: <b>{brand.name}</b>\n"
        f"Города: {cities_str}",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /edit_prompt — редактирование промта бренда
# ---------------------------------------------------------------------------

@router.message(Command("edit_prompt"))
async def cmd_edit_prompt(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Нет доступа.")

    db = get_session()
    brands = db.query(Brand).all()
    db.close()

    if not brands:
        return await message.answer("Нет брендов.")

    await state.set_state(EditBrandPromptState.waiting_brand_id)
    await message.answer(
        "Выберите бренд для редактирования промта:",
        reply_markup=get_brand_list_keyboard(brands),
    )


@router.callback_query(
    F.data.startswith("select_brand:"),
    EditBrandPromptState.waiting_brand_id
)
async def edit_prompt_brand_selected(callback: CallbackQuery, state: FSMContext) -> None:
    brand_id = int(callback.data.split(":")[1])
    await state.update_data(brand_id=brand_id)
    await state.set_state(EditBrandPromptState.waiting_prompt)

    db = get_session()
    brand = db.get(Brand, brand_id)
    db.close()

    await callback.message.edit_text(
        f"Бренд: <b>{brand.name}</b>\n\n"
        f"<b>Текущий промт:</b>\n<code>{brand.prompt_template}</code>\n\n"
        f"Введите новый промт:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(EditBrandPromptState.waiting_prompt)
async def edit_prompt_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    db = get_session()
    brand = db.get(Brand, data["brand_id"])
    brand.prompt_template = message.text.strip()
    db.commit()
    db.close()

    await state.clear()
    await message.answer(f"✅ Промт для бренда <b>{brand.name}</b> обновлён!", parse_mode="HTML")


# ---------------------------------------------------------------------------
# /check_now — принудительная проверка почты
# ---------------------------------------------------------------------------

@router.message(Command("check_now"))
async def cmd_check_now(message: Message, config) -> None:
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Нет доступа.")

    await message.answer("⏳ Запускаю проверку почты вручную...")

    from core.scheduler import run_imap_check
    results = await run_imap_check(message.bot, config)

    total_new = sum(count for _, count, err in results if err is None)
    lines = []
    for brand_name, count, err in results:
        if err:
            lines.append(f"• {brand_name}: ошибка ⚠️")
        else:
            lines.append(f"• {brand_name}: {count} новых")

    summary = "\n".join(lines)
    if total_new == 0:
        await message.answer(f"🔍 Новых отзывов нет\n\n{summary}")
    else:
        await message.answer(f"✅ Найдено новых отзывов: {total_new}\n\n{summary}")


# ---------------------------------------------------------------------------
# /status — сводка по боту
# ---------------------------------------------------------------------------

@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Нет доступа.")

    db = get_session()

    brands_count     = db.query(Brand).count()
    subscribers_count = db.query(Subscriber).count()

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    reviews_today = db.query(Review).filter(Review.created_at >= today_start).count()
    reviews_total = db.query(Review).count()

    approved_today = db.query(Review).filter(
        Review.created_at >= today_start,
        Review.status.in_([ReviewStatus.APPROVED, ReviewStatus.EDITED]),
    ).count()

    db.close()

    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    text = (
        f"📊 <b>Статус бота</b>\n"
        f"<i>{now}</i>\n\n"
        f"👥 Подписчиков: <b>{subscribers_count}</b>\n"
        f"🏢 Брендов: <b>{brands_count}</b>\n\n"
        f"📬 Отзывов сегодня: <b>{reviews_today}</b>\n"
        f"✅ Опубликовано сегодня: <b>{approved_today}</b>\n"
        f"📦 Всего отзывов в базе: <b>{reviews_total}</b>"
    )
    await message.answer(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /logs — последние строки лога
# ---------------------------------------------------------------------------

@router.message(Command("logs"))
async def cmd_logs(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Нет доступа.")

    log_path = "logs/bot.log"
    if not os.path.exists(log_path):
        return await message.answer("⚠️ Лог-файл не найден.")

    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    last_lines = lines[-30:]
    text = "".join(last_lines).strip()

    if len(text) > 4000:
        text = text[-4000:]

    await message.answer(f"<pre>{text}</pre>", parse_mode="HTML")


@router.message(Command("logs_file"))
async def cmd_logs_file(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Нет доступа.")

    log_path = "logs/bot.log"
    if not os.path.exists(log_path):
        return await message.answer("⚠️ Лог-файл не найден.")

    with open(log_path, "rb") as f:
        data = f.read()

    filename = f"bot_{datetime.now().strftime('%Y%m%d_%H%M')}.log"
    await message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption="📄 Полный лог-файл",
    )


@router.message(Command("pending"))
async def cmd_pending(message: Message) -> None:
    """Показывает все неотвеченные отзывы с кнопками."""
    if not is_admin(message.from_user.id):
        return

    from sqlalchemy.orm import joinedload
    import html

    db = get_session()
    pending = (
        db.query(Review)
        .options(joinedload(Review.brand))
        .filter(Review.status.in_([ReviewStatus.PENDING, ReviewStatus.NEGATIVE, ReviewStatus.EDITED, ReviewStatus.FAILED]))
        .order_by(Review.id.desc())
        .limit(10)
        .all()
    )

    if not pending:
        db.close()
        await message.answer("✅ Неотвеченных отзывов нет.")
        return

    await message.answer(f"📋 Неотвеченных отзывов: <b>{len(pending)}</b>", parse_mode="HTML")

    for review in pending:
        rating = review.rating or 0
        stars = "⭐" * rating if rating > 0 else "—"
        platform = (review.platform or "yandex").lower()
        reply_text = review.final_reply or review.generated_reply or ""
        brand_name = review.brand.name if review.brand else "?"
        review_url = review.review_url or ""

        text = (
            f"{'🔴' if rating <= 3 else '✨'} <b>{'НЕГАТИВ' if rating <= 3 else 'Отзыв'}</b>\n\n"
            f"Бренд: <b>{html.escape(brand_name)}</b>\n"
            f"Платформа: {platform.upper()}\n"
            f"Оценка: {stars} ({rating}/5)\n"
            f"Автор: {html.escape(review.reviewer_name or 'Клиент')}\n\n"
            f"📝 <b>Текст:</b>\n<i>{html.escape((review.review_text or '')[:400])}</i>\n\n"
            f"🤖 <b>Ответ:</b>\n{html.escape(reply_text[:400]) if reply_text else '⚠️ Ответ не сгенерирован'}\n\n"
            f"🔗 <a href='{html.escape(review_url)}'>Открыть отзыв</a>"
        )

        if platform in ("2gis", "google"):
            kb = get_manual_publish_keyboard(review.id, review_url)
        else:
            kb = get_review_keyboard(review.id)

        try:
            await message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Ошибка отправки отзыва {review.id}: {e}")

    db.close()
