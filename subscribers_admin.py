"""
Веб-панель управления подписчиками Telegram-бота отзывов (review_bot).

Отдельная подсистема: бот отзывов хранит свои данные в собственной БД
(review_bot.db, core/database.py — Brand, City, Subscriber), никак не
связанной с проектами/конфигами Click. Здесь только управление ролями
получателей уведомлений — кто и что получает при новом отзыве:

  OWNER   (начальство)      — только негатив, коротким алертом
  MANAGER (ответственный)   — все отзывы своего бренда, с кнопками модерации
  VIEWER  (просто в курсе)  — одна строка о факте нового отзыва

Подписчики появляются в списке сами — когда человек пишет /start боту.
Здесь администратор только раздаёт им роль и бренд (для OWNER/VIEWER
бренд опционален — можно оставить «Все бренды»).
"""

from __future__ import annotations

import streamlit as st

BUILD = "2026-08-21"


def _load_review_bot_db():
    """
    Импортирует модели review_bot лениво и без падения всего Click,
    если бот отзывов не настроен в этом окружении (нет review_bot.db,
    не установлен sqlalchemy для этого модуля и т.п.).
    """
    try:
        from core.database import Brand, Subscriber, SubscriberRole, get_session
        return Brand, Subscriber, SubscriberRole, get_session
    except Exception as e:  # noqa: BLE001
        return None, None, None, e


_ROLE_LABELS = {
    "owner":   "👔 Начальство — только негатив",
    "manager": "🛠 Ответственный — все отзывы бренда",
    "viewer":  "👀 Просто в курсе — короткая строка",
}


def render() -> None:
    st.subheader("🔔 Уведомления об отзывах — роли подписчиков")

    Brand, Subscriber, SubscriberRole, get_session = _load_review_bot_db()
    if Brand is None:
        st.info(
            "Бот отзывов (review_bot) здесь не настроен — модуль `core.database` "
            "не импортировался.\n\n"
            f"Подробность: {get_session}"
        )
        return

    db = get_session()
    try:
        subscribers = db.query(Subscriber).order_by(Subscriber.id).all()
        brands = db.query(Brand).order_by(Brand.name).all()
    except Exception as e:  # noqa: BLE001
        st.warning(
            "Не удалось прочитать базу бота отзывов (возможно, ещё не создана — "
            f"запустите `python migrations/init_db.py`). Ошибка: {e}"
        )
        db.close()
        return

    if not subscribers:
        st.info(
            "Пока никто не подписан. Подписчики появляются сами — когда пишут "
            "/start Telegram-боту отзывов."
        )
        db.close()
        return

    if not brands:
        st.warning(
            "В боте отзывов ещё нет ни одного бренда (/add_brand в боте). "
            "Роль MANAGER без бренда видит отзывы всех брендов — это ок для одного бренда, "
            "но если брендов будет несколько, добавьте их сначала в боте."
        )

    brand_options = {b.id: b.name for b in brands}

    st.caption(
        "Роль решает **что** получает человек, бренд — **о ком**. "
        "Подписчик без бренда (`Все бренды`) видит уведомления по всем брендам."
    )

    changed = False

    for sub in subscribers:
        label = sub.display_name or f"chat_id {sub.chat_id}"
        with st.container(border=True):
            cols = st.columns([2, 2, 2, 2])

            with cols[0]:
                st.markdown(f"**{label}**")
                st.caption(f"chat_id: `{sub.chat_id}`")
                new_name = st.text_input(
                    "Имя для панели", value=sub.display_name or "",
                    key=f"sub_name_{sub.id}", placeholder="Например: Иванов (директор)",
                    label_visibility="collapsed",
                )

            with cols[1]:
                role_keys = list(_ROLE_LABELS.keys())
                current_role = sub.role.value if sub.role else "manager"
                new_role_label = st.selectbox(
                    "Роль", [_ROLE_LABELS[k] for k in role_keys],
                    index=role_keys.index(current_role) if current_role in role_keys else 1,
                    key=f"sub_role_{sub.id}",
                )
                new_role = role_keys[[_ROLE_LABELS[k] for k in role_keys].index(new_role_label)]

            with cols[2]:
                brand_ids = [None] + list(brand_options.keys())
                brand_labels = ["Все бренды"] + [brand_options[i] for i in brand_options]
                current_idx = brand_ids.index(sub.brand_id) if sub.brand_id in brand_ids else 0
                new_brand_label = st.selectbox(
                    "Бренд", brand_labels, index=current_idx, key=f"sub_brand_{sub.id}",
                )
                new_brand_id = brand_ids[brand_labels.index(new_brand_label)]

            with cols[3]:
                st.write("")
                st.write("")
                if st.button("💾 Сохранить", key=f"sub_save_{sub.id}", use_container_width=True):
                    sub.display_name = new_name.strip() or None
                    sub.role = SubscriberRole(new_role)
                    sub.brand_id = new_brand_id
                    db.commit()
                    changed = True
                    st.success("Сохранено")

    db.close()

    if changed:
        st.rerun()

    with st.expander("Как это выглядит у получателя"):
        st.markdown(
            "**👔 Начальство (OWNER)** при негативе:\n"
            "> 🔴 **Негатив: Инметпром** (Москва)\n"
            "> ⭐⭐ (2/5) · Иван П.\n"
            ">\n"
            "> «Долго ждали доставку...»\n"
            ">\n"
            "> 🔗 Открыть отзыв\n\n"
            "**🛠 Ответственный (MANAGER)** — полная карточка с кнопками "
            "«✅ Одобрить и опубликовать» / «✏️ Редактировать» (для позитива) "
            "или подробности без кнопок (для негатива — ответ вручную).\n\n"
            "**👀 Просто в курсе (VIEWER)**:\n"
            "> 📩 Новый отзыв: **Инметпром**, Москва — ⭐⭐⭐⭐"
        )
