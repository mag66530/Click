"""
Модуль парсинга входящих писем с отзывами.
Поддерживает Яндекс Бизнес и 2ГИС.

ВАЖНО: Яндекс и 2ГИС могут менять шаблоны писем.
Если парсинг перестал работать — обновите регулярные выражения
в функции parse_review_email().
"""
import email
import email.message
import imaplib
import logging
import re
from datetime import date, timedelta

_IMAP_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

def _imap_date(days_ago: int = 3) -> str:
    """Возвращает дату в формате IMAP (DD-Mon-YYYY) с английскими месяцами."""
    d = date.today() - timedelta(days=days_ago)
    return f"{d.day:02d}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"
from email.header import decode_header
from typing import Optional

from bs4 import BeautifulSoup

from core.database import Brand

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Декодирование заголовков писем
# ---------------------------------------------------------------------------

def _decode_str(raw) -> str:
    """Декодирует заголовок письма (base64 / quoted-printable / plain)."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    parts = decode_header(raw)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


# ---------------------------------------------------------------------------
# Парсинг тела письма
# ---------------------------------------------------------------------------

def _detect_email_platform(links: list) -> Optional[str]:
    """Определяет платформу письма по трекинговым доменам."""
    for link in links:
        href = link.get("href", "")
        if "supersender.yandex.net" in href or "sender.yandex.net" in href:
            return "yandex"
        if "sender.2gis.com" in href:
            return "2gis"
    return None


def _parse_yandex_body(lines: list[str]) -> dict:
    """
    Парсит тело письма от Яндекс.Бизнес (email@business.yandex.ru).

    Структура:
      «Бренд» :
      Бренд               ← имя компании (ссылка)
      Город, ул. Адрес    ← адрес филиала
      Текст отзыва...     ← текст
      Ответить            ← кнопка (трекинговая ссылка)
    """
    result: dict = {}
    text_joined = "\n".join(lines)

    # Бренд — между «»
    brand_match = re.search(r'«([^»]+)»', text_joined)
    if brand_match:
        result["brand_name"] = brand_match.group(1).strip()

    brand = result.get("brand_name", "")

    # Ищем первое вхождение имени бренда отдельной строкой (это ссылка-карточка)
    brand_idx = None
    for i, line in enumerate(lines):
        if line == brand and i > 3:  # пропускаем первые строки (заголовок)
            brand_idx = i
            break

    if brand_idx is not None:
        # Строка после имени бренда = адрес ("Город, ул. Адрес, N")
        addr_idx = brand_idx + 1
        if addr_idx < len(lines):
            addr = lines[addr_idx]
            if "," in addr:
                result["city"] = addr.split(",")[0].strip()

        # Текст отзыва: строки между адресом и кнопкой "Ответить"
        review_start = brand_idx + 2
        review_end = None
        for i in range(review_start, min(review_start + 20, len(lines))):
            if lines[i].lower() == "ответить":
                review_end = i
                break

        if review_end is not None:
            review_lines = [l for l in lines[review_start:review_end] if l]
        elif review_start < len(lines):
            review_lines = [lines[review_start]]
        else:
            review_lines = []

        if review_lines:
            result["review_text"] = " ".join(review_lines).strip()

    # Рейтинг: ищем звёзды ★ (в HTML могут быть; если нет — позитивная почта → 5)
    stars_match = re.search(r'(★{1,5})', text_joined)
    result["rating"] = len(stars_match.group(1)) if stars_match else 5

    # Имя автора в письмах Яндекс.Бизнес не указывается
    result["reviewer_name"] = "Клиент"

    return result


def _parse_2gis_body(lines: list[str]) -> dict:
    """
    Парсит тело письма от 2ГИС (noreply@account.2gis.com).

    Структура:
      О компании
      [Бренд]                  ← имя компании
      написали хороший отзыв   ← тип отзыва (хороший / нейтральный / плохой)
      [Автор]                  ← имя автора
      [Текст отзыва...]        ← обрезанный текст
      Читать полностью         ← ссылка на полный текст
    """
    result: dict = {}

    # Ищем строку вида "написали * отзыв"
    wrote_idx = None
    for i, line in enumerate(lines):
        if "написали" in line.lower() and "отзыв" in line.lower():
            wrote_idx = i
            break

    if wrote_idx is not None:
        # Бренд — строка ДО "написали ... отзыв"
        if wrote_idx > 0:
            result["brand_name"] = lines[wrote_idx - 1]

        # Автор — строка ПОСЛЕ
        if wrote_idx + 1 < len(lines):
            result["reviewer_name"] = lines[wrote_idx + 1]

        # Текст отзыва — строки от автора до "Читать полностью"
        text_start = wrote_idx + 2
        text_end = None
        for i in range(text_start, min(text_start + 15, len(lines))):
            if "читать полностью" in lines[i].lower():
                text_end = i
                break

        if text_end is not None:
            review_lines = [l for l in lines[text_start:text_end] if l]
        elif text_start < len(lines):
            review_lines = [lines[text_start]]
        else:
            review_lines = []

        if review_lines:
            result["review_text"] = " ".join(review_lines).strip()

        # Рейтинг из описания типа отзыва
        desc = lines[wrote_idx].lower()
        if any(w in desc for w in ("хороший", "отличный", "восторженн", "положительн")):
            result["rating"] = 5
        elif any(w in desc for w in ("плохой", "негатив", "отрицатель", "недовольн", "плох")):
            result["rating"] = 2
        elif "нейтральн" in desc:
            result["rating"] = 3
        else:
            # Пробуем найти звёзды ★
            text_joined = "\n".join(lines)
            stars_match = re.search(r'(★{1,5})', text_joined)
            result["rating"] = len(stars_match.group(1)) if stars_match else 5
    else:
        result["rating"] = 5

    return result


def parse_review_email(html_body: str) -> Optional[dict]:
    """
    Разбирает HTML-тело письма от Яндекс Бизнеса / 2ГИС.

    Возвращает dict с ключами:
      brand_name, city, reviewer_name, rating, review_text, review_url, platform
    или None если не удалось распарсить.
    """
    soup = BeautifulSoup(html_body, "lxml")
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    all_links = soup.find_all("a", href=True)

    # Определяем платформу по трекинговым доменам
    platform = _detect_email_platform(all_links)

    if platform == "yandex":
        result = _parse_yandex_body(lines)
    elif platform == "2gis":
        result = _parse_2gis_body(lines)
    else:
        # Запасной парсер для нестандартных писем
        result = {}
        brand_match = re.search(r'[«"\']([\w\s\-]+)[»"\']', text)
        if brand_match:
            result["brand_name"] = brand_match.group(1).strip()
        stars_match = re.search(r'(★{1,5})', text)
        if stars_match:
            result["rating"] = len(stars_match.group(1))
        paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 30]
        if paragraphs:
            result["review_text"] = max(paragraphs, key=len)[:2000]

    result["platform"] = platform or "unknown"

    # --- Ссылка на отзыв ---

    # Вспомогательная функция: извлечь чистый URL из трекинговой ссылки Яндекса/2ГИС.
    # Формат: https://supersender.yandex.net/.../*https://yandex.ru/maps/org/brand/12345/
    def _extract_clean_url(href: str) -> Optional[str]:
        m = re.search(r'/\*(https?://.+)$', href)
        return m.group(1) if m else None

    # Приоритет 1+2: ищем ссылку на карточку организации — сначала декодируем трекинг
    YANDEX_TRACKERS = ("supersender.yandex.net", "click.sender.yandex.ru", "sender.yandex.net")
    for link in all_links:
        href = link.get("href", "")
        if href.startswith("mailto:"):
            continue

        # Если трекинговая ссылка Яндекса — пытаемся декодировать
        if any(t in href for t in YANDEX_TRACKERS):
            decoded = _extract_clean_url(href)
            if decoded and "yandex.ru/maps/org/" in decoded:
                clean = decoded.split("?")[0].rstrip("/")
                if not clean.endswith("/reviews"):
                    clean += "/reviews/"
                result["review_url"] = clean
                break
            continue  # Не декодировалось в maps — пропускаем

        # Прямая ссылка (не через трекинг)
        if any(domain in href for domain in (
            "yandex.ru/maps", "business.yandex.ru", "2gis.ru", "google.com/maps"
        )):
            if "review" in href.lower() or re.search(r'/\d{6,}', href):
                result["review_url"] = href
                break

    # Приоритет 3: декодируем URL из трекинговых ссылок 2ГИС
    if not result.get("review_url"):
        for link in all_links:
            href = link.get("href", "")
            if "sender.2gis.com" in href:
                decoded = _extract_clean_url(href)
                if decoded and "2gis.ru/" in decoded:
                    result["review_url"] = decoded.split("?")[0]
                    break

    # Приоритет 4: регексп по телу — ищем конкретные org/firm URL с числовым ID
    if not result.get("review_url"):
        url_match = re.search(
            r'https?://(?:yandex\.ru/maps/org/[^/]+/\d{6,}|2gis\.ru/[^/]+/firm/\d{6,})[^\s"\'<>]*',
            html_body,
        )
        if url_match:
            result["review_url"] = url_match.group(0)

    # Приоритет 5: трекинговые ссылки напрямую (последний резерв)
    if not result.get("review_url"):
        TRACKING_BUTTONS = {
            "supersender.yandex.net": ["ответить", "перейти к отзыву", "открыть отзыв"],
            "sender.yandex.net":      ["ответить", "перейти к отзыву"],
            "sender.2gis.com":        ["ответить на отзыв", "читать полностью"],
        }
        for tracker_domain, button_texts in TRACKING_BUTTONS.items():
            for link in all_links:
                href = link.get("href", "")
                if tracker_domain not in href:
                    continue
                btn = link.get_text(strip=True).lower()
                if any(btn == t or btn.startswith(t) for t in button_texts):
                    result["review_url"] = href
                    break
            if result.get("review_url"):
                break

        if not result.get("review_url"):
            for tracker_domain in TRACKING_BUTTONS:
                for link in all_links:
                    href = link.get("href", "")
                    if tracker_domain in href:
                        result["review_url"] = href
                        break
                if result.get("review_url"):
                    break

    # Считаем результат валидным только если есть URL и непустой текст отзыва
    if not result.get("review_url"):
        logger.warning("Не удалось найти URL отзыва в письме")
        return None

    if len(result.get("review_text") or "") < 15:
        logger.warning("Текст отзыва слишком короткий — возможно, сервисное письмо, пропускаем")
        return None

    return result


# ---------------------------------------------------------------------------
# Чтение писем через IMAP
# ---------------------------------------------------------------------------

# Маппинг папка → список отправителей для поиска.
# Каждая площадка фильтрует почту в свою папку, поэтому
# в каждой папке ищем только релевантных отправителей.
# Это сокращает кол-во IMAP-команд с 42 до 4 на бренд.
FOLDER_SENDERS: dict[str, list[str]] = {
    "INBOX": [
        "email@business.yandex.ru",
        "noreply@account.2gis.com",
        "noreply@2gis.ru",          # альтернативный адрес 2ГИС
        "no-reply@2gis.com",        # ещё вариант
        "reviews@2gis.com",
    ],
    "&BC8-.&BBEEOAQ3BD0ENQRB-": ["email@business.yandex.ru"],   # Я.Бизнес
    "2&BBMEGAQh-": [                # 2ГИС (кириллическая папка)
        "noreply@account.2gis.com",
        "noreply@2gis.ru",
        "no-reply@2gis.com",
        "reviews@2gis.com",
    ],
}


def _get_html_body(msg: email.message.Message) -> Optional[str]:
    """Извлекает HTML-тело из объекта email.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        if msg.get_content_type() == "text/html":
            charset = msg.get_content_charset() or "utf-8"
            return msg.get_payload(decode=True).decode(charset, errors="replace")
    return None


def fetch_new_reviews_from_imap(brand: Brand) -> list[dict]:
    """
    Подключается к IMAP-ящику бренда.
    Читает непрочитанные письма от известных отправителей.
    Возвращает список словарей с распарсенными отзывами.

    Каждый словарь содержит поля:
      brand_id, brand_name, city, reviewer_name, rating,
      review_text, review_url, platform
    """
    parsed_reviews: list[dict] = []

    try:
        logger.info(f"[{brand.name}] Подключаемся к {brand.imap_server}...")
        mail = imaplib.IMAP4_SSL(brand.imap_server, 993, timeout=30)
        mail.login(brand.imap_login, brand.imap_password)

        # Логируем список папок для диагностики (один раз при старте)
        try:
            _, folders_raw = mail.list()
            folder_names = [f.decode("utf-8", errors="replace") for f in folders_raw if f]
            logger.info(f"[{brand.name}] Папки на сервере ({len(folder_names)} шт.): {folder_names}")
        except Exception:
            pass

        # Яндекс IMAP игнорирует SINCE в пользовательских папках — берём последние N писем
        FETCH_LAST_N = 50

        all_msg_ids_with_folder: list[tuple[bytes, str]] = []

        for folder, senders in FOLDER_SENDERS.items():
            try:
                status, data = mail.select(f'"{folder}"')
                if status != "OK":
                    logger.debug(f"[{brand.name}] Папка {folder!r} недоступна, пропускаем")
                    continue

                for sender in senders:
                    status, ids = mail.search(None, f'(UNSEEN FROM "{sender}")')
                    if status == "OK" and ids[0]:
                        all_ids = ids[0].split()
                        logger.info(
                            f"[{brand.name}] Папка {folder!r}, FROM {sender!r}: "
                            f"найдено {len(all_ids)} писем, берём последние {FETCH_LAST_N}"
                        )
                        recent_ids = all_ids[-FETCH_LAST_N:]
                        for mid in recent_ids:
                            all_msg_ids_with_folder.append((mid, folder))
                    else:
                        logger.debug(f"[{brand.name}] Папка {folder!r}, FROM {sender!r}: писем не найдено")

            except Exception as e:
                logger.debug(f"[{brand.name}] Ошибка при работе с папкой {folder!r}: {e}")

        # Убираем дубли по (msg_id, folder)
        seen_keys: set[tuple[bytes, str]] = set()
        unique_pairs: list[tuple[bytes, str]] = []
        for pair in all_msg_ids_with_folder:
            if pair not in seen_keys:
                seen_keys.add(pair)
                unique_pairs.append(pair)

        logger.info(f"[{brand.name}] Найдено {len(unique_pairs)} писем для проверки")

        current_folder: str = ""
        for msg_id, folder in unique_pairs:
            try:
                # Переключаемся в нужную папку, если поменялась
                if folder != current_folder:
                    mail.select(f'"{folder}"')
                    current_folder = folder

                # Проверяем дату письма — пропускаем старше 3 дней
                _, date_data = mail.fetch(msg_id, "(INTERNALDATE)")
                date_str = date_data[0].decode("utf-8", errors="replace")
                date_match = re.search(r'"(\d{1,2}-\w{3}-\d{4})', date_str)
                if date_match:
                    try:
                        msg_date = date(*[int(x) if x.isdigit() else
                                         ["Jan","Feb","Mar","Apr","May","Jun",
                                          "Jul","Aug","Sep","Oct","Nov","Dec"].index(x)+1
                                         for x in date_match.group(1).split("-")])
                        if (date.today() - msg_date).days > 7:
                            logger.debug(f"Письмо {msg_id} старше 7 дней ({msg_date}), пропускаем")
                            continue
                    except Exception:
                        pass

                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                html_body = _get_html_body(msg)
                if not html_body:
                    logger.debug(f"Письмо {msg_id} ({folder}): нет HTML-части, пропускаем")
                    mail.store(msg_id, "+FLAGS", "\\Seen")
                    continue

                parsed = parse_review_email(html_body)

                if parsed:
                    parsed["brand_id"]   = brand.id
                    parsed["brand_name"] = parsed.get("brand_name") or brand.name
                    parsed_reviews.append(parsed)
                    mail.store(msg_id, "+FLAGS", "\\Seen")
                    logger.info(
                        f"[{brand.name}] Отзыв из папки {folder!r}: "
                        f"рейтинг={parsed.get('rating')}, город={parsed.get('city')}"
                    )
                else:
                    logger.warning(
                        f"[{brand.name}] Письмо {msg_id} ({folder}): не удалось распарсить"
                    )

            except Exception as e:
                logger.error(f"[{brand.name}] Ошибка обработки письма {msg_id}: {e}")

        mail.logout()

    except imaplib.IMAP4.error as e:
        logger.error(f"[{brand.name}] IMAP-ошибка: {e}")
    except ConnectionRefusedError:
        logger.error(f"[{brand.name}] Не удалось подключиться к {brand.imap_server}:993")
    except Exception as e:
        logger.error(f"[{brand.name}] Неизвестная ошибка IMAP: {e}")

    return parsed_reviews
