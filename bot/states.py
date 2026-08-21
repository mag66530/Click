"""
FSM-состояния для aiogram 3.x.

FSM (Finite State Machine) — это механизм aiogram для
ведения многошагового диалога с пользователем.

Пример: при добавлении бренда бот последовательно спрашивает:
  1. Название → waiting_name
  2. Промт → waiting_prompt
  3. IMAP-данные → waiting_imap
"""
from aiogram.fsm.state import State, StatesGroup


class EditReplyState(StatesGroup):
    """Редактирование ответа на отзыв."""
    waiting_for_text = State()


class AddBrandState(StatesGroup):
    """Добавление нового бренда."""
    waiting_name   = State()
    waiting_prompt = State()
    waiting_imap   = State()


class AddCityState(StatesGroup):
    """Добавление города к бренду."""
    waiting_brand_id = State()
    waiting_city     = State()


class EditBrandPromptState(StatesGroup):
    """Редактирование промта существующего бренда."""
    waiting_brand_id = State()
    waiting_prompt   = State()
