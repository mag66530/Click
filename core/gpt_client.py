"""
Генерация ответов на отзывы через OpenAI API.

Архитектура промтов:
  GLOBAL_RULES  — общие правила для ВСЕХ брендов (запрещённые слова, стиль)
  brand.prompt_template — уникальный промт из БД для конкретного бренда
  Итоговый system_prompt = GLOBAL_RULES + brand_prompt (с подстановкой переменных)
"""
import logging
import os

from openai import AsyncOpenAI, APIError, APITimeoutError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Глобальные правила — добавляются к промту ЛЮБОГО бренда
# ---------------------------------------------------------------------------

GLOBAL_RULES = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА (нарушение недопустимо):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. ЗАПРЕЩЁННЫЕ СЛОВА — НИКОГДА не используй эти слова в ответе:
   "база", "металлобаза", "магазин", "склад", "металлопрокатный центр".

2. Название компании — упоминай СТРОГО ОДИН РАЗ за весь ответ.

3. Слово "команда" — упоминай СТРОГО ОДИН РАЗ за весь ответ.

4. Длина ответа — лаконично, 3–5 предложений. Без воды и лишних фраз.

5. Если известно имя клиента — обращайся к нему по имени в начале ответа.
   Пример: "Иван, благодарим вас за отзыв!"

6. Упоминай город СТРОГО в формате: "в нашем филиале в г. {city}".
   Если город неизвестен — пропусти упоминание города.

7. Тон — вежливый, профессиональный, без излишней льстивости.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ниже следует промт конкретного бренда:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


async def generate_reply(
    brand_prompt_template: str,
    reviewer_name: str,
    review_text: str,
    city: str,
) -> str:
    """
    Генерирует ответ на отзыв.

    Аргументы:
      brand_prompt_template — шаблон промта из БД (поле Brand.prompt_template).
                              Поддерживает переменные: {name}, {text}, {city}.
      reviewer_name         — имя автора отзыва (может быть пустым).
      review_text           — текст отзыва.
      city                  — город филиала.

    Возвращает строку с готовым ответом.
    Бросает исключение при ошибке API.
    """
    client = AsyncOpenAI(
        api_key=os.getenv("GROQ_API_KEY"),
        base_url="https://api.groq.com/openai/v1",
    )

    # Подставляем переменные в шаблон бренда
    brand_prompt = brand_prompt_template.format(
        name=reviewer_name or "Клиент",
        text=review_text,
        city=city or "вашем городе",
    )

    # Итоговый системный промт
    system_prompt = GLOBAL_RULES.strip() + "\n\n" + brand_prompt

    try:
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Напиши ответ на следующий отзыв.\n\n"
                        f"Автор: {reviewer_name or 'Клиент'}\n"
                        f"Город: {city or 'не указан'}\n"
                        f"Текст отзыва: {review_text}"
                    ),
                },
            ],
            temperature=0.7,   # Небольшая вариативность, но не слишком творчески
            max_tokens=500,
        )

        reply = response.choices[0].message.content.strip()
        logger.info(f"GPT сгенерировал ответ ({len(reply)} символов)")
        return reply

    except APITimeoutError:
        logger.error("Таймаут запроса к OpenAI")
        raise
    except APIError as e:
        logger.error(f"Ошибка OpenAI API: {e}")
        raise
    except Exception as e:
        logger.error(f"Неизвестная ошибка при обращении к GPT: {e}")
        raise
