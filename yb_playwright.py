"""
yb_playwright.py – движок Яндекс.Бизнеса на Playwright.

Это построчный перенос логики publish.js / actualize.js (Puppeteer) на Playwright,
включая ВСЕ проверки, которые в первой версии переноса были потеряны:

  • нормализация URL карточки  → /edit/posts/ или /p/edit/posts/ (buildPostsUrl)
  • обработка редиректа Яндекса на новый companyId
  • ожидание, пока React-интерфейс реально отрисуется (а не «спим 2 сек»)
  • проверка 404
  • ввод текста ДО картинки + подтверждение, что текст доехал в DOM
    (иначе Яндекс публикует обрезанный пост)
  • перехват API-ответа Яндекса на клик «Создать» – ГЛАВНЫЙ источник истины:
        HTTP 2xx → пост точно создан
        HTTP 4xx → Яндекс отклонил
        нет ответа → fallback на DOM → иначе статус 'unknown' («проверьте вручную»)
  • распознавание тоста «не удалось загрузить» на картинке → статус 'no-image'
  • проверка, что залогинен ИМЕННО аккаунт проекта (защита от публикации не туда)
  • checkPostAlreadyExists – поиск СВЕЖЕГО поста (плашка «Публикация на модерации»
    или метка «N минут назад») перед повторной попыткой, чтобы не создать дубль
  • загрузка фото в раздел «Товары» (uploadProductPhotos)
  • скачивание картинок по URL (ImgBB / Imgur / Я.Диск / Google Drive) с ретраями

Статусы публикации – те же 4, что в оригинале:
    'ok'        – пост опубликован
    'no-image'  – пост опубликован, но картинка не прикрепилась
    'unknown'   – клик сделан, подтверждения нет. РЕТРАЙ ЗАПРЕЩЁН (риск дубля)
    'failed'    – до клика «Создать» не дошли, публиковать безопасно повторно
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

# Метка сборки – одна на всё приложение, лежит в build.py. Держать её
# в каждом модуле своей строкой уже ломалось: у одного файла осталось
# старое значение, и Click принялся перезагружать все модули на каждое
# нажатие, пока не выбило по памяти.
from build import BUILD  # noqa: F401

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import paths

ROOT = Path(__file__).parent
USERS_DATA = paths.data_root()

PASSPORT_URL = (
    "https://passport.yandex.ru/auth/welcome?origin=passport_auth2"
    "&retpath=https%3A%2F%2Fpassport.yandex.ru%2Fprofile"
)
PROFILE_URL = "https://passport.yandex.ru/profile"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Язык окна. Без него Яндекс отдаёт АНГЛИЙСКИЙ паспорт – а это не перевод, а
# другой экран: вместо «Введите номер телефона» и «Ещё» там «Log in with ID»
# с вкладками Email/Phone. Заказчик видела в своём браузере одно, Click в
# своём – другое, и поиск по русским надписям промахивался мимо всего.
LOCALE = "ru-RU"
LANG_HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"}

# Таймауты – 1:1 из DEFAULTS в publish.js
NAVIGATION_TIMEOUT = 45_000
ACTION_TIMEOUT = 15_000
# Ответ создания поста прилетал позже, чем мы успевали его ждать: окно было
# 8 секунд на весь цикл, и в шести городах прогона 12.08 в него попадал только
# ответ загрузчика картинки. 20 секунд – с запасом; ждать столько приходится
# редко, выход из цикла происходит сразу по точному ответу.
API_WAIT_TIMEOUT = 20.0     # сколько ждём POST-ответ Яндекса после клика «Создать»
API_GRACE_AFTER_HIT = 1.2   # …и сколько ещё, если ответ пришёл неточный
UI_READY_TIMEOUT = 15_000   # сколько ждём, пока React отрисует интерфейс
IMAGE_PREVIEW_TIMEOUT = 8_000
TEXT_CONFIRM_TIMEOUT = 10_000
FORM_CLOSE_TIMEOUT = 6.0    # сколько ждём закрытия формы, прежде чем перечитать ленту
FEED_RELOAD_TRIES = 3       # сколько раз перечитываем ленту, прежде чем сказать «поста нет»
FEED_RELOAD_PAUSE = 2_500   # пауза между перечитываниями, мс

# Логгер ставится ДЛЯ СВОЕГО ПОТОКА, а не на весь модуль. Прогоны идут
# параллельно, каждый в своём потоке, и общий логгер сваливал бы строки одного
# прогона в лог другого, а снятие его в конце обрывало бы лог соседу. Ещё
# и вкладки, которые водят браузер сами (отправка ответов на отзывы, вход
# в Яндекс), больше не пишут в лог чужого прогона.
_LOG_LOCAL = threading.local()


def set_logger(fn: Callable[[str, str], None] | None) -> None:
    """Внешний логгер этого потока: fn(level, message). Используется runner.py."""
    _LOG_LOCAL.fn = fn


def _log(level: str, msg: str) -> None:
    fn = getattr(_LOG_LOCAL, "fn", None)
    if fn:
        try:
            fn(level, msg)
            return
        except Exception:
            pass
    print(f"[{level}] {msg}")


def info(msg: str) -> None:
    _log("INFO", msg)


def warn(msg: str) -> None:
    _log("WARN", msg)


def error(msg: str) -> None:
    _log("ERROR", msg)


# ════════════════════════════════════════════════════════════════════
#  URL-хелперы (порт extractCompanyId / buildPostsUrl / buildEditUrl)
# ════════════════════════════════════════════════════════════════════

# Яндекс завёл второй адрес той же карточки:
#   старый  https://yandex.ru/sprav/21461411/edit/
#   новый   https://yandex.ru/business/companies/company/70210624498/
# Номер в обоих один и тот же. Без разбора нового вида такие города молча
# выпадали: ссылка не опознавалась, и до формы поста дело не доходило.
COMPANY_ID_RX = re.compile(r"(?:sprav|/business/companies/company)/(\d+)", re.I)


def extract_company_id(url: str | None) -> str | None:
    m = COMPANY_ID_RX.search(url or "")
    return m.group(1) if m else None


def build_posts_url(company_url: str | None, company_id: str | None = None) -> str | None:
    """
    Приводит любой URL карточки к разделу «Посты», сохраняя формат (со /p/ или без).

      .../sprav/40691746/edit/photos/       → .../sprav/40691746/edit/posts/
      .../sprav/188702920373/p/edit/        → .../sprav/188702920373/p/edit/posts/

    НИКОГДА не возвращает /sprav/{id}/posts/ – такого пути нет, Яндекс отдаёт 404.
    """
    if company_url:
        m = re.match(r"^(https?://[^/]+/sprav/\d+/(?:p/)?edit)\b", company_url)
        if m:
            return m.group(1) + "/posts/"
        m = re.match(r"^(https?://[^/]+/sprav/\d+)", company_url)
        if m:
            # Формат неизвестен – берём более частый «новый», со /p/
            return m.group(1) + "/p/edit/posts/"
        # Новый адрес карточки – номер тот же, приводим к рабочему виду.
        cid = extract_company_id(company_url)
        if cid:
            return f"https://yandex.ru/sprav/{cid}/p/edit/posts/"
    if company_id:
        return f"https://yandex.ru/sprav/{company_id}/p/edit/posts/"
    return None


def build_edit_url(company_url: str | None) -> str | None:
    """URL раздела «Данные» карточки (для актуализации). Порт buildEditUrl из actualize.js."""
    if not company_url:
        return None
    m = re.search(r"/sprav/(\d+)/(p/)?edit", company_url)
    if not m:
        cid = extract_company_id(company_url)
        return f"https://yandex.ru/sprav/{cid}/p/edit/" if cid else None
    company_id, has_p = m.group(1), m.group(2)
    return f"https://yandex.ru/sprav/{company_id}/p/edit/" if has_p else f"https://yandex.ru/sprav/{company_id}/edit/"


def alt_posts_url(url: str | None) -> str | None:
    """
    Второй формат раздела «Посты»: /edit/posts/ ↔ /p/edit/posts/. Яндекс держит
    старые и новые карточки на разных форматах; когда угадали не тот – 404,
    хотя карточка живая. Раньше город на этом терялся (PLAN 1.5).
    """
    if not url:
        return None
    if "/p/edit/" in url:
        return url.replace("/p/edit/", "/edit/")
    if "/edit/" in url:
        return url.replace("/edit/", "/p/edit/")
    return None


def build_photos_url(company_url: str | None, company_id: str | None = None) -> str | None:
    cid = company_id or extract_company_id(company_url)
    if not cid:
        return None
    has_p = bool(re.search(r"/sprav/\d+/p/", company_url or ""))
    return (
        f"https://yandex.ru/sprav/{cid}/p/edit/photos/"
        if has_p
        else f"https://yandex.ru/sprav/{cid}/edit/photos/"
    )


def build_reviews_url(company_url: str | None, company_id: str | None = None) -> str | None:
    """
    URL раздела «Отзывы». Схема ровно та же, что у постов и фото –
    проверено на живой карточке: https://yandex.ru/sprav/20761435635/p/edit/reviews/

    Формат сохраняем (со /p/ или без): угадать неверно – значит получить
    404 на живой карточке. Если не угадали, `alt_posts_url` даёт второй
    вариант, как и для постов.
    """
    if company_url:
        m = re.match(r"^(https?://[^/]+/sprav/\d+/(?:p/)?edit)\b", company_url)
        if m:
            return m.group(1) + "/reviews/"
        m = re.match(r"^(https?://[^/]+/sprav/\d+)", company_url)
        if m:
            # Формат неизвестен – берём более частый «новый», со /p/, как в build_posts_url
            return m.group(1) + "/p/edit/reviews/"
        cid = extract_company_id(company_url)
        if cid:
            return f"https://yandex.ru/sprav/{cid}/p/edit/reviews/"
    if company_id:
        return f"https://yandex.ru/sprav/{company_id}/p/edit/reviews/"
    return None


# ════════════════════════════════════════════════════════════════════
#  Картинки: превращаем ссылку-галерею в прямую и скачиваем с ретраями
#  (порт resolveImageUrl / downloadImage из publish.js)
# ════════════════════════════════════════════════════════════════════

def _fetch_text(url: str, redirects: int = 0) -> str:
    if redirects > 5:
        raise RuntimeError("Слишком много редиректов")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def resolve_image_url(url: str) -> str:
    """Страница-галерея → прямая ссылка на файл. ImgBB / Imgur / Я.Диск / Google Drive."""
    if re.search(r"\.(jpg|jpeg|png|gif|webp|bmp)(\?|$)", url, re.I):
        return url

    if re.search(r"ibb\.co/", url, re.I):
        info("  🔍 Страница ImgBB, ищу прямую ссылку...")
        html = _fetch_text(url)
        m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.I) or \
            re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html, re.I)
        if m:
            info(f"  ✅ Найдена прямая ссылка: {m.group(1)}")
            return m.group(1)
        raise RuntimeError("Не удалось найти прямую ссылку на странице ImgBB")

    if re.search(r"imgur\.com/", url, re.I) and not re.search(r"i\.imgur\.com", url, re.I):
        m = re.search(r"imgur\.com/([a-zA-Z0-9]+)", url)
        if m:
            return f"https://i.imgur.com/{m.group(1)}.jpg"

    if re.search(r"disk\.yandex\.[a-z]+", url, re.I):
        info("  🔍 Яндекс.Диск, получаю download-ссылку...")
        api = ("https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key="
               + urllib.parse.quote(url, safe=""))
        parsed = json.loads(_fetch_text(api))
        if parsed.get("href"):
            return parsed["href"]
        raise RuntimeError("Не удалось получить download-ссылку Яндекс.Диска")

    if re.search(r"drive\.google\.com", url, re.I):
        m = re.search(r"/d/([a-zA-Z0-9_-]+)", url) or re.search(r"id=([a-zA-Z0-9_-]+)", url)
        if m:
            return f"https://drive.google.com/uc?export=download&id={m.group(1)}"

    return url


def _download_direct(url: str, temp_dir: Path) -> str:
    temp_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(urllib.parse.urlparse(url).path).suffix.split("?")[0] or ".jpg"
    if len(ext) > 5:
        ext = ".jpg"
    filepath = temp_dir / f"img-{int(time.time() * 1000)}{ext}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        filepath.write_bytes(resp.read())
    return str(filepath)


def download_image(url: str, temp_dir: Path) -> str:
    """
    До 5 попыток с нарастающей паузой – спасает от «Client network socket disconnected»,
    ETIMEDOUT, ECONNRESET и коротких провалов ImgBB. Порт downloadImage из publish.js.
    """
    direct = resolve_image_url(url)
    pauses = [0, 2, 5, 10, 20]
    last_err: Exception | None = None
    for attempt, pause in enumerate(pauses):
        if pause:
            time.sleep(pause)
        try:
            return _download_direct(direct, temp_dir)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < len(pauses) - 1:
                info(f"  ↻ Попытка {attempt + 1}/{len(pauses)} скачать картинку не удалась: {e}. Повторяю...")
    reason = str(last_err) if last_err else "неизвестная сетевая ошибка"
    if re.search(r"socket|TLS|timed out|timeout|ETIMEDOUT|ECONNRESET|getaddrinfo|Name or service", reason, re.I):
        raise RuntimeError(
            f"Сеть нестабильна – не удалось скачать картинку за {len(pauses)} попыток. "
            "Проверьте интернет / антивирус, либо попробуйте через несколько минут."
        )
    raise RuntimeError(f"Не удалось скачать картинку: {reason}")


# ════════════════════════════════════════════════════════════════════
#  Браузер: ОДИН на весь прогон (в первой версии переноса Firefox
#  перезапускался на каждый город – это ×10 времени и рваная сессия)
# ════════════════════════════════════════════════════════════════════

# Какой движок реально заработал в этом процессе. Считаем один раз.
_ENGINE: str | None = None

CHROMIUM_ARGS = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]


def _launch(pw, engine: str, headless: bool = True, extra_args: list[str] | None = None):
    """
    Запустить браузер. `extra_args` – дополнительные ключи для конкретного
    вызова; по умолчанию пусто, то есть Яндекс и 2ГИС стартуют ровно как
    раньше. Нужен соцсетям: ВК показывает проверку «вы не робот» скрытому
    браузеру заметно чаще, и один ключ (см. vk_social) её приглушает.
    """
    args = (CHROMIUM_ARGS if engine == "chromium" else []) + list(extra_args or [])
    return getattr(pw, engine).launch(headless=headless, args=args)


def _short_error(exc: object) -> str:
    """
    Playwright вываливает несколько тысяч строк лога запуска. Для человека полезны
    одна-две: чего именно не хватило. Их и достаём.
    """
    text = str(exc)
    for pattern in (
        r"error while loading shared libraries: ([^\s:]+)",
        r"(Host system is missing dependencies[^\n]*)",
        r"(Executable doesn't exist[^\n]*)",
        r"(browserType\.launch: [^\n]*)",
    ):
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return text.split("\n")[0][:200]


def _is_not_installed(exc: object) -> bool:
    text = str(exc)
    return "Executable doesn't exist" in text or "playwright install" in text


def in_cloud() -> bool:
    """
    Мы в Streamlit Cloud? Там приложение лежит в /mount/src/<репозиторий>
    и работает под пользователем appuser.

    Это важно не для красоты: в облаке нельзя доставить системные библиотеки
    (root'а нет, есть только packages.txt, который собирается при деплое),
    и памяти там около 1 ГБ.
    """
    if (os.environ.get("CLICK_ENV") or "").strip().lower() == "cloud":
        return True
    return str(ROOT).startswith("/mount/src") or os.environ.get("HOME") == "/home/appuser"


def _default_order() -> list[str]:
    """
    Какой движок пробовать первым.

    ЛОКАЛЬНО – Chromium: все селекторы Яндекс.Бизнеса писались и проверялись
    под Chrome (оригинал – Puppeteer/Chrome), это максимальная точность.

    В ОБЛАКЕ – Firefox: он заметно легче по памяти, а на бесплатном тарифе
    (~1 ГБ RAM) headless Chromium падал на тяжёлой SPA-странице Яндекс.Паспорта.

    Раньше в облаке не поднимался ни один движок: не хватало системных библиотек.
    Образ там – Ubuntu 24.04 (доказано ошибкой apt на виртуальном libasound2),
    поэтому в packages.txt добавлены libgbm1 для Chromium и libasound2t64 со
    свитой – именно их отсутствие давало «Host system is missing dependencies».

    Порядок именно такой ещё и ради экономии: в облаке браузер скачивается при
    первом запуске. Начав с Chromium, мы бы выкачали 150 МБ, выяснили что он не
    стартует, и выкачали ещё 90 МБ Firefox.
    """
    return ["firefox", "chromium"] if in_cloud() else ["chromium", "firefox"]


def resolve_engine(force: str | None = None) -> str:
    """
    Выбирает движок, который РЕАЛЬНО запускается здесь. Результат кэшируется
    на процесс. Жёстко задать: CLICK_BROWSER=chromium | firefox.
    Если браузер не скачан – скачиваем и пробуем снова.
    """
    global _ENGINE
    if _ENGINE and not force:
        return _ENGINE

    pref = (force or os.environ.get("CLICK_BROWSER") or "").strip().lower()
    order = [pref] if pref in ("chromium", "firefox", "webkit") else _default_order()

    problems: list[str] = []
    for engine in order:
        for attempt in (1, 2):
            try:
                with sync_playwright() as p:
                    _launch(p, engine).close()
                info(f"🌐 Браузер: {engine}" + (" (облако)" if in_cloud() else ""))
                _ENGINE = engine
                return engine
            except Exception as e:  # noqa: BLE001
                if attempt == 1 and _is_not_installed(e):
                    info(f"⬇️  Скачиваю браузер {engine} (разово, 1-3 минуты)…")
                    subprocess.run([sys.executable, "-m", "playwright", "install", engine], check=False)
                    # install-deps ставит системные пакеты через apt и требует root.
                    # В облаке root'а нет – вызывать бессмысленно, только шум в логах.
                    if hasattr(os, "geteuid") and os.geteuid() == 0:
                        subprocess.run([sys.executable, "-m", "playwright", "install-deps", engine],
                                       check=False)
                    continue
                problems.append(f"{engine} – {_short_error(e)}")
                break

    where = ("Streamlit Cloud" if in_cloud() else "этой машине")
    raise RuntimeError(
        f"Ни один браузер не запустился на {where}.\n\n"
        + "\n".join(f"• {p}" for p in problems)
        + "\n\nЧто делать:\n"
        "• Локально: `python -m playwright install chromium` (или firefox).\n"
        "• В облаке: перезапустите приложение (Manage app → Reboot). Если не помогло – "
        "задайте переменную CLICK_BROWSER=firefox в настройках приложения."
    )


def current_engine() -> str | None:
    """Какой движок выбран сейчас (None – ещё не запускали браузер). Для интерфейса."""
    return _ENGINE


def ensure_browser_installed(engine: str | None = None) -> None:
    resolve_engine(engine)


def session_path(project_id: str) -> Path:
    d = USERS_DATA / project_id / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d / "yb_storage_state.json"


def _save_storage_state(context, path: Path) -> None:
    """
    Сохранить сессию так, чтобы её нельзя было застать на середине записи.

    Прогоны идут параллельно, и каждый в конце дописывает сессию. Пиши мы
    прямо в файл – второй прогон мог бы прочитать его наполовину записанным
    и остаться без входа в Яндекс. Пишем во временный файл рядом и
    переименовываем: подмена мгновенная, читающий видит либо старый файл
    целиком, либо новый целиком.
    """
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        context.storage_state(path=str(tmp))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def device_path(project_id: str) -> Path:
    """
    Куки «этого устройства» – их Яндекс ставит ещё до входа (yandexuid и
    компания) и по ним потом узнаёт браузер. Раньше окно входа открывалось
    каждый раз с нуля, и для Яндекса это был НОВЫЙ компьютер: отсюда
    бесконечные «подтвердите вход» даже после письма. Файл живёт рядом с
    сессией и наружу не уезжает (users-data в .gitignore).
    """
    d = USERS_DATA / project_id / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d / "yb_device_state.json"


# Куки, по которым Яндекс узнаёт залогиненного человека. Всё остальное
# (yandexuid, spravka и прочее) выдаётся и анонимному посетителю.
AUTH_COOKIES = {"session_id", "sessionid2", "yandex_login"}


def has_saved_session(project_id: str) -> bool:
    """
    Есть ли НАСТОЯЩАЯ сессия. Раньше сюда годился любой файл с куками, и
    приложение бодро писало «Сессия сохранена» после неудачного входа –
    с анонимными куками, которые в кабинет не пускают.
    """
    path = session_path(project_id)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return any(
        (c.get("name") or "").lower() in AUTH_COOKIES and "yandex" in (c.get("domain") or "").lower()
        for c in data.get("cookies") or []
    )


class YbBrowser:
    """
    Живёт весь прогон: один запуск браузера на 137 городов вместо 137 запусков.
    Умеет пересоздавать вкладку – это лечит «Execution context was destroyed»,
    ради чего в publish.js был browser.newPage().
    """

    def __init__(self, project_id: str, headless: bool = True,
                 session_file: Path | None = None):
        self.project_id = project_id
        self.headless = headless
        # Файл сессии можно подменить – так этим же браузером работает 2ГИС
        # (gis_playwright), не заводя второй такой же класс. По умолчанию –
        # сессия Яндекса.
        self.session_file = session_file or session_path(project_id)
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        # Вторая сессия в том же браузере – см. side_page().
        self._side: BrowserContext | None = None
        self._side_page: Page | None = None
        self._side_file: Path | None = None

    def start(self) -> None:
        engine = resolve_engine()
        self._pw = sync_playwright().start()
        self.browser = _launch(self._pw, engine, headless=self.headless)
        state = self.session_file
        self.context = self.browser.new_context(
            storage_state=str(state) if state.exists() else None,
            viewport={"width": 1280, "height": 900},
            user_agent=UA,
            locale=LOCALE,
            extra_http_headers=LANG_HEADERS,
        )
        self.context.set_default_timeout(ACTION_TIMEOUT)
        self.context.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
        self.page = self.context.new_page()

    def new_page(self) -> Page:
        """Пересоздать вкладку – сбрасывает подвисшие контексты, cookies остаются (тот же context)."""
        try:
            if self.page and not self.page.is_closed():
                self.page.close()
        except Exception:
            pass
        self.page = self.context.new_page()
        return self.page

    def side_page(self, session_file: Path) -> Page:
        """
        Вкладка с ДРУГОЙ сессией в этом же браузере.

        Нужна там, где посреди прогона Яндекса надо заглянуть в 2ГИС: те же
        снимки отгрузки уходят на обе площадки. Второй браузер поднимать
        нельзя – это ещё полгигабайта на бесплатном облаке, и прогон падал бы
        по памяти. А отдельный контекст внутри уже запущенного браузера – это
        просто свой набор кук: сессии не смешиваются, процесс один.

        Контекст создаётся один раз и живёт до конца прогона: открывать и
        закрывать его на каждый город – это лишние секунды на ровном месте.
        Но переживает он не всё: перезапуск браузера (сторож памяти, зависший
        Chrome) уносит вкладку с собой, и тогда она заводится заново. Сам
        вопрос «жива ли вкладка» может упасть – у закрытого Playwright нет
        даже цикла событий, – поэтому спрашиваем осторожно.
        """
        try:
            if self._side_page and not self._side_page.is_closed():
                return self._side_page
        except Exception:  # noqa: BLE001 – вкладка от прошлого браузера
            self._side, self._side_page = None, None
        if self.browser is None:
            raise RuntimeError("Браузер не запущен")
        self._side_file = session_file
        self._side = self.browser.new_context(
            storage_state=str(session_file) if session_file.exists() else None,
            viewport={"width": 1280, "height": 900},
            user_agent=UA,
            locale=LOCALE,
            extra_http_headers=LANG_HEADERS,
        )
        self._side.set_default_timeout(ACTION_TIMEOUT)
        self._side.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
        self._side_page = self._side.new_page()
        return self._side_page

    def close_side(self) -> None:
        """Закрыть вторую сессию, сохранив её куки: они могли продлиться."""
        if self._side is None:
            return
        try:
            if self._side_file is not None:
                _save_storage_state(self._side, self._side_file)
        except Exception as e:  # noqa: BLE001
            warn(f"Не удалось сохранить вторую сессию: {e}")
        try:
            self._side.close()
        except Exception:  # noqa: BLE001
            pass
        self._side, self._side_page = None, None

    def save_session(self) -> None:
        try:
            if self.context:
                _save_storage_state(self.context, self.session_file)
        except Exception as e:  # noqa: BLE001
            warn(f"Не удалось сохранить сессию: {e}")

    def restart(self) -> None:
        """Полный перезапуск браузера – аналог детектора зависшего Chrome в publish.js."""
        self.save_session()
        self.close()
        time.sleep(3)
        self.start()

    def close(self) -> None:
        self.close_side()
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        finally:
            self.browser = None
            self.context = None
            self.page = None
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._pw = None


# ════════════════════════════════════════════════════════════════════
#  DOM-хелперы (порт findClickableByText / waitForTextButton)
# ════════════════════════════════════════════════════════════════════

_FIND_CLICKABLE_JS = """
(args) => {
  const re = new RegExp(args.src, args.flags || 'i');
  const nodes = document.querySelectorAll('button, a, [role="button"], [role="tab"], [role="menuitem"]');
  for (const el of nodes) {
    const text = (el.textContent || '').trim();
    if (!re.test(text)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    return { x: r.x + r.width / 2, y: r.y + r.height / 2, text: text.slice(0, 60) };
  }
  return null;
}
"""


def find_clickable_by_text(page: Page, regex_src: str, flags: str = "i") -> dict | None:
    try:
        return page.evaluate(_FIND_CLICKABLE_JS, {"src": regex_src, "flags": flags})
    except Exception:
        return None


def wait_for_text_button(page: Page, regex_src: str, timeout_ms: int = 10_000, flags: str = "i") -> dict | None:
    """Ждём появления кнопки, а не проверяем один раз – React дорисовывает UI не мгновенно."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        hit = find_clickable_by_text(page, regex_src, flags)
        if hit:
            return hit
        page.wait_for_timeout(300)
    return None


def click_at(page: Page, coords: dict) -> None:
    page.mouse.click(coords["x"], coords["y"])


def click_element(page: Page, el, what: str) -> str:
    """
    Нажать НАЙДЕННЫЙ элемент, а не точку экрана.

    Клик по координатам живёт ровно до первого скролла: страница
    дорисовывается, кнопка уезжает – и клик уходит в пустоту. Так ломались
    «Создать» и «Данные актуальны». Здесь элемент сам подъезжает под курсор.
    Возвращает, каким способом получилось нажать (для лога).
    """
    try:
        el.scroll_into_view_if_needed(timeout=3_000)
    except Exception:  # noqa: BLE001
        pass
    try:
        el.click(timeout=8_000)
        return "по элементу"
    except Exception as e1:  # noqa: BLE001
        warn(f"  ⚠️ Обычный клик по «{what}» не прошёл ({_short_error(str(e1))}) – жму событием")
        el.dispatch_event("click")
        return "событием click"


# Кнопка «Добавить пост». Три способа найти – по тексту, по aria/title и по
# плюсику – но результат один: САМ ЭЛЕМЕНТ.
_FIND_ADD_POST_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) return false;
    const st = window.getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') return false;
    return !(el.disabled || el.getAttribute('aria-disabled') === 'true');
  };
  const BY_TEXT = /(добавить|создать|новый|написать|опубликовать).*(пост|публикац|запис)|^(добавить|создать|опубликовать)$/i;

  // 1. По тексту кнопки – основной путь.
  for (const el of document.querySelectorAll(
        'button, a, [role="button"], [role="tab"], [role="menuitem"]')) {
    if (!BY_TEXT.test((el.textContent || '').trim())) continue;
    if (visible(el)) return el;
  }
  // 2. По подписи для скринридера – когда на кнопке только иконка.
  for (const el of document.querySelectorAll('button, [role="button"]')) {
    const meta = ((el.getAttribute('aria-label') || '') + ' '
                + (el.getAttribute('title') || '')).toLowerCase();
    if (!/добавить|создать|новый|опубликовать/.test(meta)) continue;
    if (!/пост|публикац/.test(meta)) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 20 && r.height > 20 && visible(el)) return el;
  }
  // 3. Голый плюсик.
  for (const el of document.querySelectorAll('button, [role="button"]')) {
    const t = (el.textContent || '').trim();
    if (t !== '+' && t !== '＋') continue;
    const r = el.getBoundingClientRect();
    if (r.width > 20 && r.height > 20 && visible(el)) return el;
  }
  return null;
}
"""


def wait_for_element(page: Page, js: str, timeout_ms: int = 6_000):
    """Ждём появления элемента: React дорисовывает интерфейс не мгновенно."""
    deadline = time.time() + timeout_ms / 1000
    while True:
        try:
            el = page.evaluate_handle(js).as_element()
        except Exception:  # noqa: BLE001
            el = None
        if el is not None or time.time() >= deadline:
            return el
        page.wait_for_timeout(300)


def wait_ui_ready(page: Page, timeout_ms: int = UI_READY_TIMEOUT) -> bool:
    """
    Ждём, пока React смонтирует интерфейс: на странице появилось ≥5 кнопок с текстом.
    Без этого поиск «Добавить пост» идёт по пустой странице со спиннерами.
    """
    try:
        page.wait_for_function(
            """() => {
                const btns = document.querySelectorAll('button, [role="button"], a');
                let withText = 0;
                for (const b of btns) {
                    const t = (b.textContent || '').trim();
                    if (t.length > 1 && t.length < 50) withText++;
                    if (withText >= 5) return true;
                }
                return false;
            }""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def is_404(page: Page) -> bool:
    try:
        return bool(page.evaluate(
            """() => /Страница\\s+не\\s+найдена/i.test((document.body.textContent || '').slice(0, 1500))"""
        ))
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════
#  Проверка аккаунта (порт защиты из publish.js – publish не с того аккаунта)
# ════════════════════════════════════════════════════════════════════

# Признаки страницы входа Яндекса. Ловим и по-русски, и по-английски: в облаке
# у браузера английская локаль, и «Войти» там не появится вовсе.
_LOGIN_MARKERS = (
    "create id", "qr code", "face or fingerprint",
    "войти", "создать id", "по qr-коду", "забыли пароль",
)


def host_path(page: Page) -> str:
    """
    Адрес без строки запроса. Это важно: паспорт уводит на
    …/auth?retpath=…%2Fprofile – в ПОЛНОМ адресе слово profile есть, и проверка
    «мы уже в кабинете» отвечала «да» прямо на форме входа.
    """
    from urllib.parse import urlsplit
    u = urlsplit(page.url or "")
    return f"{u.netloc}{u.path}".lower()


def looks_like_login_page(page: Page) -> bool:
    """
    Мы на странице входа, а не в кабинете? Проверяем по адресу и по содержимому:
    Яндекс уводит на паспорт с разных доменов и не всегда меняет путь.
    """
    hp = host_path(page)
    if re.search(r"passport\.yandex\.[a-z.]+", hp) and "/profile" not in hp:
        return True
    try:
        return bool(page.evaluate(
            "() => !!document.querySelector('input[name=passwd], input[type=password], "
            "form[action*=auth], [class*=passp-]')"
        ))
    except Exception:  # noqa: BLE001
        return False


def verify_account(page: Page, expected_email: str) -> dict:
    """
    Кто сейчас залогинен в Яндексе. Возвращает state:

      ok        – нужный аккаунт;
      other     – НАЙДЕН другой аккаунт (только это повод останавливать прогон);
      anonymous – не залогинен: паспорт показывает форму входа;
      unknown   – определить не удалось: страница не отдала ни логина, ни почты.

    Разделение принципиальное. Раньше «ничего не нашли» приравнивалось к «чужой
    аккаунт», и прогон падал с текстом «залогинен НЕ ТОТ аккаунт, найдено: не
    определено» – при том что аккаунт был правильный, просто страница профиля
    не отдала данные. Останавливать работу из-за неудачи самой проверки нельзя.

    Разбор текста делаем в Python, а не в браузере: JS-регулярку приходилось
    экранировать дважды, и одна лишняя обратная косая превращала её в
    «unterminated regular expression literal» – проверка молча ломалась.

    Сравниваем по ЛОГИНУ (часть до @): в профиле Яндекс показывает и привязанные
    адреса, из-за чего mepen88@yandex.ru с привязанным mepen888@gmail.com
    когда-то считался чужим.
    """
    expected = (expected_email or "").lower().strip()
    login = expected.split("@")[0]
    if not login:
        return {"state": "unknown", "matched": True, "emails": [], "checked": False}

    try:
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=30_000)
        try:                                   # профиль – SPA, ждём тишины в сети
            page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:                      # noqa: BLE001
            page.wait_for_timeout(2_000)

        text = page.evaluate("() => document.body.innerText || document.body.textContent || ''") or ""
        low = text.lower()

        if looks_like_login_page(page) or any(m in low for m in _LOGIN_MARKERS):
            return {"state": "anonymous", "matched": False, "emails": [], "checked": True}

        esc = re.escape(login)
        matched = bool(
            re.search(rf"(^|[^\w.-]){esc}([^\w.-]|$)", text, re.I)
            or re.search(rf"(^|[^\w.+-]){esc}@(yandex\.(ru|com|by|kz|ua)|ya\.ru)", text, re.I)
        )
        emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)[:8]

        if matched:
            return {"state": "ok", "matched": True, "emails": emails, "checked": True}
        if emails:
            return {"state": "other", "matched": False, "emails": emails, "checked": True}
        return {"state": "unknown", "matched": True, "emails": [], "checked": True}
    except Exception as e:  # noqa: BLE001
        warn(f"⚠️ Не удалось проверить аккаунт Яндекса: {e}")
        return {"state": "unknown", "matched": True, "emails": [], "checked": False}


# ════════════════════════════════════════════════════════════════════
#  Проверка «пост уже есть и он СВЕЖИЙ» (порт checkPostAlreadyExists)
# ════════════════════════════════════════════════════════════════════

_CHECK_EXISTS_JS = r"""
(needle) => {
  // Свежесть поста определяем ТОЛЬКО по явным маркерам Яндекса:
  //   1) плашка «Публикация на модерации» – 100% только что отправленный пост
  //   2) метка времени «только что» / «N минут назад»
  // Просто совпадение текста – это может быть ПРОШЛАЯ публикация, не считаем свежей.
  const FRESH_TIME = /^(\s*(только что|меньше минуты|минут[уы]?\s+назад|\d+\s+минут[уы]?\s+назад|несколько\s+минут\s+назад)\s*)$/i;
  const MODERATION = /Публикация\s+на\s+модерации/i;
  // Пробелы приводим к одному виду: лента переносит строки и подставляет
  // &nbsp; – точное сравнение промахивалось мимо своего же поста.
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const want = norm(needle);
  const posts = document.querySelectorAll('[class*="Post"], [class*="post-card"], [class*="PostsList"] [class*="card"]');
  for (const el of posts) {
    const text = el.textContent || '';
    if (!norm(text).includes(want)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 100 || r.height < 50) continue;
    let moderation = false, freshTime = false;
    for (const e of el.querySelectorAll('*')) {
      if (e.children.length !== 0) continue;
      const t = (e.textContent || '').trim();
      if (MODERATION.test(t)) moderation = true;
      if (FRESH_TIME.test(t)) freshTime = true;
      if (moderation && freshTime) break;
    }
    if (moderation) return { found: true, fresh: true, reason: 'moderation' };
    if (freshTime)  return { found: true, fresh: true, reason: 'fresh-time' };
    return { found: true, fresh: false, reason: 'old-match' };
  }
  return { found: false, fresh: false, reason: '' };
}
"""


_NO_ACCESS_JS = r"""
() => {
  const texts = [...document.querySelectorAll('button, [role="button"], a')]
    .map(b => (b.textContent || '').trim().toLowerCase())
    .filter(Boolean);
  // На рабочей карточке кнопок много. Заглушка «нет доступа» показывает
  // одну-единственную – увести на список организаций.
  const own = texts.filter(t => t.length > 2 && t.length < 60);
  const escape = own.some(t => /перейти на страницу организац|к списку организац|все организации/.test(t));
  const body = (document.body.innerText || '').toLowerCase();
  const denied = /нет доступа|доступ ограничен|организация не найдена|у вас нет прав/.test(body);
  return denied || (escape && own.length <= 3);
}
"""


def _looks_like_no_access(page: Page) -> bool:
    try:
        return bool(page.evaluate(_NO_ACCESS_JS))
    except Exception:  # noqa: BLE001
        return False


def _feed_needle(post_text: str) -> str:
    """
    Кусок текста, по которому ищем пост в ленте.

    Берём ОДНУ строку, а не первые 80 символов подряд. Перевод строки лента
    отрисовывает отдельным блоком, и в textContent карточки соседние строки
    склеиваются без пробела – подстрока с «\\n» не находится там НИКОГДА.
    Для проверки публикации такой промах означал бы «поста нет» → повтор →
    дубль, поэтому строку берём целиком и только одну.

    Строка нужна из тела поста: концовки (контакты, хэштеги) у всех городов
    проекта одинаковые, и по ним нашёлся бы любой чужой пост.
    """
    for line in (post_text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) >= 25:
            return line[:80]
    flat = re.sub(r"\s+", " ", post_text or "").strip()
    return flat[:80]


def check_post_already_exists(page: Page, post_text: str) -> dict:
    full = (post_text or "").strip()
    if len(full) < 20:
        return {"found": False, "fresh": False, "reason": ""}
    try:
        return page.evaluate(_CHECK_EXISTS_JS, _feed_needle(full))
    except Exception:
        return {"found": False, "fresh": False, "reason": ""}


# ════════════════════════════════════════════════════════════════════
#  Снимок ленты: единственный источник истины о публикации
# ════════════════════════════════════════════════════════════════════

# Считаем не «похоже ли на успех», а сколько раз наш текст лежит в ленте.
# Снимок делается ДО клика и ПОСЛЕ него: пост появился, если совпадений стало
# больше. Так работает даже там, где тексты повторяются (у МетПромИнтекс в
# ленте висит прошлая публикация с тем же началом) – и не требуется сверять
# часы браузера с часами карточки.
_FEED_SNAPSHOT_JS = r"""
(needle) => {
  const out = { matches: 0, cards: 0, fresh: '', hasImage: false, sample: '' };
  const FRESH_TIME = /^(\s*(только что|меньше минуты|минут[уы]?\s+назад|\d+\s+минут[уы]?\s+назад|несколько\s+минут\s+назад|сегодня(\s+в\s+\d{1,2}[:.]\d{2})?)\s*)$/i;
  const MODERATION = /(публикация|пост)\s+на\s+модерации|на\s+модерации/i;

  for (const el of document.querySelectorAll('[class*="Post"], [class*="post-card"]')) {
    const r = el.getBoundingClientRect();
    if (r.width >= 100 && r.height >= 50) out.cards++;
  }

  if (!needle || needle.length < 10) return out;

  // Пробелы приводим к одному виду с обеих сторон: лента переносит строки и
  // подставляет &nbsp;, из-за чего точное сравнение промахивалось.
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const want = norm(needle);
  const has = (el) => norm(el.textContent).includes(want);

  // Совпадения текста – по САМЫМ ВНУТРЕННИМ узлам, содержащим иглу: на один
  // пост приходится ровно одно совпадение, сколько бы обёрток вокруг ни было.
  const hits = [];
  for (const el of document.querySelectorAll('body *')) {
    const tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'TEXTAREA') continue;
    if (!has(el)) continue;
    // Открытая форма – это не лента: текст в редакторе за публикацию не считаем.
    if (el.closest('[contenteditable="true"], [class*="PostAddForm"], [class*="PostForm"], [role="dialog"]')) continue;
    let inChild = false;
    for (const ch of el.children)
      if (has(ch)) { inChild = true; break; }
    if (inChild) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 10) continue;   // скрытое не считаем
    hits.push(el);
  }
  out.matches = hits.length;
  if (!hits.length) return out;

  hits.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

  // Карточка верхнего совпадения. Если подходящего предка нет – берём три
  // уровня вверх, но никогда не body: иначе «картинка поста» найдётся где
  // угодно на странице.
  let card = null, cur = hits[0];
  for (let i = 0; i < 8 && cur.parentElement; i++) {
    cur = cur.parentElement;
    const r = cur.getBoundingClientRect();
    if (r.height >= 120 && r.width >= 200 && /post|card/i.test(String(cur.className || ''))) {
      card = cur;
      break;
    }
  }
  if (!card) {
    card = hits[0];
    for (let i = 0; i < 3 && card.parentElement; i++) card = card.parentElement;
  }
  if (card === document.body || card === document.documentElement) card = hits[0];

  if (MODERATION.test(card.textContent || '')) out.fresh = 'moderation';
  if (!out.fresh)
    for (const e of card.querySelectorAll('*')) {
      if (e.children.length !== 0) continue;
      if (FRESH_TIME.test((e.textContent || '').trim())) { out.fresh = 'fresh-time'; break; }
    }

  // Картинка поста. Логотип компании в шапке карточки – тоже <img>, поэтому
  // засчитываем либо CDN постов, либо заведомо крупную картинку.
  for (const img of card.querySelectorAll('img')) {
    const src = img.getAttribute('src') || '';
    const r = img.getBoundingClientRect();
    if (/get-sprav-posts/.test(src) || (r.width > 200 && r.height > 100)) { out.hasImage = true; break; }
  }
  if (!out.hasImage)
    for (const el of card.querySelectorAll('*')) {
      if (/get-sprav-posts/.test(window.getComputedStyle(el).backgroundImage || '')) {
        out.hasImage = true;
        break;
      }
    }

  out.sample = (card.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  return out;
}
"""

_EMPTY_FEED = {"matches": 0, "cards": 0, "fresh": "", "hasImage": False, "sample": ""}


def read_feed_state(page: Page, post_text: str) -> dict:
    """Снимок ленты по началу текста поста. Не бросает – при ошибке пустой снимок."""
    full = (post_text or "").strip()
    if len(full) < 20:
        return dict(_EMPTY_FEED)
    try:
        got = page.evaluate(_FEED_SNAPSHOT_JS, _feed_needle(full))
        return {**_EMPTY_FEED, **(got or {})}
    except Exception:  # noqa: BLE001
        return dict(_EMPTY_FEED)


_FRESH_MARKER_RU = {"moderation": "плашка «Публикация на модерации»",
                    "fresh-time": "метка «только что»"}

# Умеет ли поиск по тексту находить посты в ЭТОЙ вёрстке.
#
# «Поста в ленте нет» разрешает повтор – а значит промах поиска стоит дубля.
# Вёрстку Яндекса мы не контролируем: он может переименовать классы или начать
# рисовать текст поста иначе, и тогда не найдётся ни один пост, включая только
# что созданный. Поэтому отсутствие считается доказанным только после того, как
# поиск хотя бы раз НАШЁЛ пост по тексту: разметка у всех карточек одна, и
# первый же подтверждённый город доказывает, что читать её мы умеем.
#
# Обратный случай тоже запоминаем: пост подтверждён плашкой модерации, а по
# тексту не нашёлся – это прямая улика, что поиску верить нельзя.
_FEED_MATCH = {"proven": False, "missed": False}


def feed_match_reliable() -> bool:
    """Можно ли считать «текста в ленте нет» доказательством отсутствия поста."""
    return _FEED_MATCH["proven"] and not _FEED_MATCH["missed"]


def _note_feed_match(before: dict, now: dict) -> None:
    """Пост подтверждён – заодно смотрим, каким признаком, и что это говорит о поиске."""
    if int((now or {}).get("matches") or 0) > int((before or {}).get("matches") or 0):
        _FEED_MATCH["proven"] = True
    else:
        # Нашли по маркеру свежести, а по тексту – нет. Поиск промахивается.
        if not _FEED_MATCH["missed"]:
            warn("  ⚠️ Пост найден по плашке, но не по тексту – "
                 "поиску по ленте больше не доверяем, повторы отключены")
        _FEED_MATCH["missed"] = True


def _feed_says_published(before: dict, now: dict) -> str:
    """
    Появился ли НАШ пост. Возвращает объяснение или пустую строку.

    Два независимых признака: совпадений текста стало больше, чем было до
    клика, либо у верхнего совпадения появился маркер свежести. Первого
    достаточно и там, где тексты в ленте повторяются.
    """
    was = int((before or {}).get("matches") or 0)
    became = int((now or {}).get("matches") or 0)
    if became > was:
        return f"совпадений в ленте {was} → {became}"
    fresh = (now or {}).get("fresh") or ""
    if fresh and not (before or {}).get("fresh"):
        return _FRESH_MARKER_RU.get(fresh, fresh)
    return ""


def _feed_absence_reason(before: dict, now: dict) -> str:
    """Чем именно доказано отсутствие поста – это идёт в отчёт заказчику."""
    was = int((before or {}).get("matches") or 0)
    if was and int((now or {}).get("matches") or 0) == was:
        return "в ленте только прошлый пост с таким же текстом"
    return f"текста поста в ленте нет (карточек в ленте: {(now or {}).get('cards', 0)})"


def verify_post_in_feed(page: Page, posts_url: str, post_text: str, before: dict) -> dict:
    """
    Есть ли пост в ленте. ЕДИНСТВЕННЫЙ источник истины о публикации.

    Пост считаем появившимся, если совпадений нашего текста стало больше, чем
    было до клика, либо у верхнего совпадения появился маркер свежести
    («на модерации» / «только что»). Совпало столько же и маркера нет – значит
    в ленте висит ПРОШЛЫЙ пост, а новый не создался.

    Возвращает verdict: 'published' | 'absent' | 'unknown'.
    'unknown' – это «лента не прочиталась», а не «поста нет»: повторять по нему
    публикацию нельзя, иначе получим дубль.
    """
    seen_feed = False
    last = dict(_EMPTY_FEED)

    for attempt in range(1, FEED_RELOAD_TRIES + 1):
        try:
            page.goto(posts_url, wait_until="domcontentloaded", timeout=25_000)
            wait_ui_ready(page, 12_000)
            page.wait_for_timeout(1200)
        except Exception as e:  # noqa: BLE001
            warn(f"  ⚠️ Лента не открылась ({_short_error(e)}) – попытка {attempt}")
            continue

        # Страница постов действительно отрисовалась? Кнопка «Добавить пост» –
        # доказательство того, что мы смотрим на ленту, а не на пустой каркас.
        try:
            feed_ready = page.evaluate_handle(_FIND_ADD_POST_JS).as_element() is not None
        except Exception:  # noqa: BLE001
            feed_ready = False

        last = read_feed_state(page, post_text)
        seen_feed = seen_feed or feed_ready or last["cards"] > 0 or last["matches"] > 0

        why = _feed_says_published(before, last)
        if why:
            _note_feed_match(before, last)
            return {"verdict": "published", "hasImage": last["hasImage"], "state": last,
                    "detail": why}

        if attempt < FEED_RELOAD_TRIES:
            # Лента могла отстать на секунду. Перечитываем, прежде чем сказать
            # «поста нет»: ошибка в эту сторону стоит дубля при следующей попытке.
            page.wait_for_timeout(FEED_RELOAD_PAUSE)

    if not seen_feed:
        return {"verdict": "unknown", "hasImage": False, "state": last,
                "detail": "лента не прочиталась"}
    if int(last["cards"]) > int((before or {}).get("cards") or 0):
        # Карточек стало больше, а текст не совпал. Скорее всего пост всё-таки
        # создан, просто лента отрисовала текст иначе. Повторять нельзя.
        return {"verdict": "unknown", "hasImage": last["hasImage"], "state": last,
                "detail": "в ленте появилась новая карточка, но текст поста в ней не найден"}
    return {"verdict": "absent", "hasImage": False, "state": last,
            "detail": _feed_absence_reason(before, last)}


# ════════════════════════════════════════════════════════════════════
#  Картинка в форме поста
# ════════════════════════════════════════════════════════════════════

_ERROR_TOAST_JS = r"""
() => {
  const candidates = document.querySelectorAll(
    '[class*="oast"], [class*="otification"], [class*="otice"], [role="alert"], [class*="essage"][class*="rror"]'
  );
  for (const el of candidates) {
    const text = (el.textContent || '').trim().toLowerCase();
    if (!text || text.length > 200) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 10) continue;
    if (/не\s*удал(ось|ся)\s*загруз|загруз.*не\s*удал|fail.*upload|upload.*fail|невозможно\s*загруз/i.test(text)) {
      return text;
    }
  }
  return null;
}
"""

_PREVIEW_READY_JS = """
() => {
  const all = document.querySelectorAll('img[src*="get-sprav-posts"], [style*="get-sprav-posts"]');
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (r.width >= 100 && r.height >= 100) return true;
  }
  return false;
}
"""


def _attach_images(page: Page, image_paths: list[str]) -> tuple[bool, str]:
    """
    Прикрепляет файлы в форму поста. Возвращает (ok, error_message).
    Порт шага 5 из publishToCity: input[type=file] → превью на CDN → проверка тоста ошибки.
    """
    try:
        chosen = False
        file_input = page.locator('input[type="file"]').first
        if file_input.count() == 0:
            btn = wait_for_text_button(page, r"(прикрепить|добавить\s*(фото|картинк|изображ)|загрузить)", 3000)
            if btn:
                # Клик по «Добавить» открывает НАСТОЯЩЕЕ окно выбора файла
                # операционной системы. Playwright перехватывает его только
                # когда его ждут через expect_file_chooser – без этого окно
                # повисает поверх браузера и остаётся на экране даже после
                # успешной публикации (заказчик прислала снимок).
                try:
                    with page.expect_file_chooser(timeout=5_000) as fc:
                        click_at(page, btn)
                    fc.value.set_files(image_paths)
                    chosen = True
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(400)
            file_input = page.locator('input[type="file"]').first
            if not chosen and file_input.count() == 0:
                return False, "Поле загрузки файла не найдено"

        if not chosen:
            file_input.set_input_files(image_paths)

        try:
            page.wait_for_function(_PREVIEW_READY_JS, timeout=IMAGE_PREVIEW_TIMEOUT)
        except Exception:
            return False, "Превью не появилось за 8 секунд"

        # Превью появилось – но Яндекс мог показать тост «Не удалось загрузить».
        page.wait_for_timeout(1500)
        toast = page.evaluate(_ERROR_TOAST_JS)
        if toast:
            # Закрываем тост и убираем битое превью, чтобы не мешало клику «Создать»
            try:
                page.evaluate(
                    """() => {
                        document.querySelectorAll('[class*="lose"], [aria-label*="акрыть"], [aria-label*="lose"]')
                          .forEach(b => { const r = b.getBoundingClientRect();
                                          if (r.width < 50 && r.width > 5) { try { b.click(); } catch (e) {} } });
                        document.querySelectorAll('[class*="emove"], [class*="elete"], [aria-label*="алить"]')
                          .forEach(b => { if (b.closest('[class*="PostPhotosCollection"], [class*="Post"]')) {
                                            try { b.click(); } catch (e) {} } });
                    }"""
                )
                page.wait_for_timeout(400)
            except Exception:
                pass
            return False, "Яндекс отклонил картинку: " + toast[:100]

        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ════════════════════════════════════════════════════════════════════
#  Кнопка публикации
# ════════════════════════════════════════════════════════════════════

_FIND_PUBLISH_BTN_JS = r"""
() => {
  // Возвращаем САМ ЭЛЕМЕНТ, а не координаты. Оригинал делал именно так
  // (page.evaluateHandle + elementHandle.click()): Playwright и Puppeteer сами
  // прокручивают элемент в зону видимости и жмут по нему, а не по точке экрана.
  // Координаты успевают протухнуть – страница дорисовывается и уезжает.
  //
  // Сначала кнопки ВНУТРИ формы поста, потом остальной документ: «Создать»
  // где-нибудь в шапке не должна перехватить публикацию.
  const roots = [];
  for (const f of document.querySelectorAll('[class*="PostForm"], [class*="PostAddForm"], [role="dialog"], [class*="odal"]')) {
    const r = f.getBoundingClientRect();
    if (r.width > 150 && r.height > 150) roots.push(f);
  }
  roots.push(document);
  const seen = new Set();
  const candidates = [];
  for (const root of roots)
    for (const b of root.querySelectorAll('button, [role="button"], [type="submit"]')) {
      if (seen.has(b)) continue;
      seen.add(b);
      const text = (b.textContent || '').trim();
      if (!text || text.length > 30) continue;
      const low = text.toLowerCase();
      // Запрещённые – чтобы никогда не нажать «Сохранить черновик» / «Отменить» / «Удалить»
      if (/(черновик|отмен|удал|закр|назад|отказ|войти|выйти|настрой|регистр)/i.test(low)) continue;
      const r = b.getBoundingClientRect();
      if (r.width < 30 || r.height < 18) continue;
      if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
      const st = window.getComputedStyle(b);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      candidates.push({ el: b, text: low });
    }
  for (const name of ['создать', 'опубликовать', 'отправить', 'publish', 'submit'])
    for (const c of candidates) if (c.text === name) return c.el;
  for (const c of candidates) if (/(опубликовать|создать пост|отправить пост)/i.test(c.text)) return c.el;
  for (const c of candidates) if (c.text === 'создать' || c.text.startsWith('создать ')) return c.el;
  return null;
}
"""

_BTN_INFO_JS = """(b) => { const r = b.getBoundingClientRect();
    return { text: (b.textContent || '').trim().slice(0, 60),
             x: Math.round(r.x), y: Math.round(r.y),
             w: Math.round(r.width), h: Math.round(r.height) }; }"""


_DOM_FALLBACK_JS = r"""
(needleText) => {
  const out = { formOpen: false, postFound: false };
  const forms = document.querySelectorAll('[class*="PostAddForm"], [class*="post-add-form"], form[class*="Post"]');
  for (const f of forms) {
    const r = f.getBoundingClientRect();
    if (r.width > 100 && r.height > 100) { out.formOpen = true; break; }
  }
  if (needleText && needleText.length >= 10) {
    const needle = needleText.slice(0, 30);
    const posts = document.querySelectorAll('[class*="Post"], [class*="post-card"], [class*="PostsList"] *, [class*="card"]');
    for (const el of posts) {
      if ((el.textContent || '').includes(needle)) {
        const r = el.getBoundingClientRect();
        if (r.width > 100 && r.height > 30) { out.postFound = true; break; }
      }
    }
  }
  return out;
}
"""


def _is_publish_api_response(url: str, method: str) -> bool:
    """
    Ответ ли это API создания поста. Смотрим ТОЛЬКО на хост и путь.

    Строка запроса – ловушка: Метрика передаёт в параметрах адрес текущей
    страницы (…/edit/posts/…), и «sprav» с «posts» находились В ЧУЖОМ запросе.
    mc.yandex.ru/watch получал HTTP 200 – и несуществующая публикация
    рапортовалась как «API подтвердил».
    """
    if method not in ("POST", "PUT"):
        return False
    from urllib.parse import urlsplit
    u = urlsplit(url)
    host = (u.netloc or "").lower().split(":")[0]
    path = (u.path or "").lower()
    if not re.search(r"(^|\.)yandex\.(ru|com|net|by|kz|ua)$", host):
        return False
    # Счётчики и аналитика живут на своих поддоменах и путях
    if re.match(r"^(mc|an|mtr|log|clck|informer|matchid|adstat|strm|metri)", host):
        return False
    if re.search(r"/(watch|metric|counter|track|stat|analytics|csp-report|ping|heartbeat|clck|informer)\b", path):
        return False
    # ЗАГРУЗКА КАРТИНКИ – НЕ подтверждение поста. Хранилище фотографий отвечает
    # на avatars.mds.yandex.net/…/get-sprav-posts/… и photo.upload.maps.yandex.ru:
    # хост яндексовый, в пути есть «sprav» – прежнее правило засчитывало это за
    # публикацию. В прогоне 12.08 шесть городов получили «API подтвердил» именно
    # по ответу загрузчика, и в Самаре поста при этом не появилось.
    if re.match(r"^(avatars?|photo|upload|storage|images?)\b", host) or ".mds." in host:
        return False
    if re.search(r"/(get|put|upload)-|/upload\b", path):
        return False
    # Путь должен быть похож на создание поста, а не просто содержать «sprav»:
    # мимо проходили любые POST кабинета, от сохранения черновика до телеметрии.
    return _publish_response_score(url) > 0


# Насколько ответ похож на «пост создан». Нужно, когда после клика прилетело
# несколько подходящих ответов: раньше брался ПЕРВЫЙ подвернувшийся, и
# случайный запрос мог перебить настоящий. Теперь выигрывает самый точный.
def _publish_response_score(url: str) -> int:
    from urllib.parse import urlsplit
    path = (urlsplit(url).path or "").lower()
    if re.search(r"/company-post|/posts?/create|/create-post", path):
        return 3        # именно создание поста – точнее не бывает
    if re.search(r"/sprav/api/.*/posts?\b|/api/.*/company-post", path):
        return 2        # API справочника про посты
    if re.search(r"/posts?\b", path):
        return 1
    return 0


# ════════════════════════════════════════════════════════════════════
#  ПУБЛИКАЦИЯ ОДНОГО ГОРОДА
# ════════════════════════════════════════════════════════════════════

def publish_to_city(
    page: Page,
    task: dict,
    task_index: int = 0,
    total_tasks: int = 1,
    temp_dir: Path | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """
    Полный порт publishToCity из publish.js.

    Опубликован пост или нет – решает ЛЕНТА карточки, а не HTTP-ответы:
    после клика страница перечитывается и в ней ищется наш текст.

    Возвращает dict:
      status: 'ok' | 'no-image' | 'unknown' | 'failed' | 'skipped-duplicate' | 'no-session'
              ok        – пост найден в ленте (и картинка на месте, если была)
              no-image  – пост в ленте есть, картинки в нём нет
              failed    – поста в ленте НЕТ (при retrySafe=True повтор безопасен)
              unknown   – ленту прочитать не удалось; повторять нельзя, будет дубль
      reason: человеческое объяснение
      steps:  {navigate, postsSection, addButton, text, image, publish}
      durationMs, cityName, companyUrl, imageError, feedDetail, retrySafe
    """
    label = f"[{task_index + 1}/{total_tasks}] {task.get('cityName', '?')}"
    started = time.time()
    has_image_src = bool(task.get("imageUrl") or task.get("imagePath"))
    result: dict[str, Any] = {
        "status": "failed",
        "reason": "",
        "cityName": task.get("cityName"),
        "companyUrl": task.get("companyUrl"),
        "steps": {
            "navigate": None, "postsSection": None, "addButton": None,
            "text": None, "image": "pending" if has_image_src else "skipped", "publish": None,
        },
        "durationMs": 0,
    }

    def finish(status: str, reason: str) -> dict:
        result["status"] = status
        result["reason"] = reason
        result["durationMs"] = int((time.time() - started) * 1000)
        return result

    # Пустой текст – последняя черта, дальше которой не идём. В боевом прогоне
    # эта функция бодро отчиталась «Текст введён полностью (0 символов)» и
    # опубликовала пустой пост в трёх городах: задачу «только фото в 2ГИС»
    # прогон прежней сборки принял за обычную. Пустой пост в карточке
    # заказчика не исправить ничем, кроме рук, – поэтому проверка стоит и
    # здесь, у самой кнопки, а не только у того, кто сюда позвал.
    if not (task.get("postText") or "").strip():
        warn(f"⛔ {label}: пустой текст поста – публиковать нечего, город пропущен")
        return finish("failed", "Пустой текст поста – публиковать нечего")

    info(f"📍 {label} – начинаем...")

    company_id = task.get("companyId") or extract_company_id(task.get("companyUrl"))
    posts_url = build_posts_url(task.get("companyUrl"), company_id) or task.get("companyUrl")
    if posts_url != task.get("companyUrl"):
        info(f"  🔗 URL нормализован: {task.get('companyUrl')} → {posts_url}")
    else:
        info(f"  🔗 URL: {posts_url}")

    # ─── 1. Открываем страницу постов ───
    try:
        page.goto(posts_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector("body", timeout=10_000)
        page.wait_for_timeout(400)

        if is_404(page):
            alt = alt_posts_url(posts_url)
            if alt:
                info(f"  🔁 404 – пробую другой формат адреса: {alt}")
                page.goto(alt, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(500)
            if not alt or is_404(page):
                result["steps"]["navigate"] = "404"
                error(f"  ❌ Страница не найдена (404): {posts_url}")
                return finish("failed", f"Страница не найдена (404): {posts_url}. "
                                        "Проверьте URL карточки в «Городах».")
            posts_url = alt
        actual_url = page.url

        # Яндекс мог редиректнуть старый ID на новый – пересобираем URL под новый формат
        effective_id = company_id
        if company_id and f"/sprav/{company_id}/" not in actual_url:
            m = re.search(r"/sprav/(\d+)/", actual_url)
            if m:
                effective_id = m.group(1)
                info(f"  ↪️ Яндекс редиректнул: компания {company_id} → {effective_id}")

        if not re.search(r"/edit/posts/?$", actual_url.split("?")[0]):
            fixed = build_posts_url(actual_url, effective_id)
            if fixed and fixed != actual_url:
                info(f"  🔁 Корректирую URL после редиректа: {fixed}")
                page.goto(fixed, wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(400)

        info("  ⏳ Жду пока интерфейс прогрузится...")
        if wait_ui_ready(page):
            info("  ✅ Интерфейс прогрузился")
        else:
            warn("  ⚠️ Интерфейс не успел прогрузиться за 15 сек – продолжаю всё равно")
        page.wait_for_timeout(800)

        result["steps"]["navigate"] = "ok"
        result["steps"]["postsSection"] = "direct"
    except Exception as e:  # noqa: BLE001
        error(f"  ❌ Не удалось открыть страницу: {e}")
        return finish("failed", f"Не удалось открыть страницу: {e}")

    if should_stop and should_stop():
        return finish("failed", "Остановлено пользователем до начала публикации")

    # ─── 1б. ЛЕНТА УЖЕ СОДЕРЖИТ ЭТОТ ПОСТ? ───
    # Последний рубеж против дубля, и он не зависит от причины. Реестр
    # публикаций лежит в файлах, а в облаке файлы стираются при перезапуске:
    # после ребута тот же прогон уходил заново и создавал ВТОРОЙ пост. Здесь
    # мы смотрим на саму ленту Яндекса – она не теряется никогда.
    try:
        already = check_post_already_exists(page, task.get("postText", ""))
    except Exception:  # noqa: BLE001
        already = {"found": False, "fresh": False, "reason": ""}

    # Снимок ленты ДО публикации. По нему потом узнаем, появился ли пост:
    # сравнение «было/стало» работает и когда тексты в ленте повторяются.
    before_feed = read_feed_state(page, task.get("postText", ""))
    if before_feed["matches"]:
        info(f"  📋 В ленте уже есть {before_feed['matches']} совпадени(й) по началу текста – учту при проверке")

    if already.get("found") and already.get("fresh"):
        result["steps"]["publish"] = "already-published"
        reason = ("Такой пост уже есть в ленте города"
                  + (f" ({already['reason']})" if already.get("reason") else "")
                  + " – повтор не отправлен")
        warn(f"  ⏭ {label}: {reason}")
        return finish("skipped-duplicate", reason)

    # ─── 2. Кнопка «Добавить пост» ───
    # Ищем ЭЛЕМЕНТ, а не координаты: кнопка бывает ниже сгиба, и клик по
    # снятой точке уходил в пустоту – город падал с «кнопка не найдена»,
    # хотя кнопка была. Та же поломка, что была с «Создать».
    add_btn = wait_for_element(page, _FIND_ADD_POST_JS, 6000)

    if not add_btn:
        try:
            btns = page.evaluate(
                """() => Array.from(document.querySelectorAll('button, [role="button"]'))
                        .slice(0, 30)
                        .map(b => { const r = b.getBoundingClientRect();
                                    if (r.width < 5 || r.height < 5) return null;
                                    return (b.textContent || '').trim().slice(0, 60)
                                           || '(no text, aria=' + (b.getAttribute('aria-label') || '') + ')'; })
                        .filter(Boolean)"""
            )
            info(f"  🔬 Кнопки на странице: {json.dumps(btns[:15], ensure_ascii=False)}")
        except Exception:
            pass
        # Частый случай: сессия слетела, и вместо кабинета открылась страница
        # входа. Раньше это выглядело как «кнопка не найдена» и повторялось для
        # каждого города – 137 бессмысленных попыток вместо одной понятной.
        if looks_like_login_page(page):
            result["steps"]["addButton"] = "no-session"
            error("  ❌ Открылась страница входа Яндекса – сессия не активна")
            return finish("no-session", "Сессия Яндекса не активна: открылась страница входа")
        # Карточка бывает просто НЕ ДОСТУПНА этому аккаунту: Яндекс показывает
        # заглушку с единственной кнопкой «Перейти на страницу организаций».
        # «Кнопка не найдена» тут врёт – искать нечего, доступа нет.
        if _looks_like_no_access(page):
            result["steps"]["addButton"] = "no-access"
            error("  ❌ У аккаунта нет доступа к этой карточке")
            return finish("failed", "Карточка недоступна этому аккаунту Яндекса – "
                                    "проверьте ссылку и права на организацию")
        result["steps"]["addButton"] = "missing"
        error("  ❌ Кнопка «Добавить пост» не найдена")
        return finish("failed", "Кнопка «Добавить пост» не найдена")

    try:
        how_add = click_element(page, add_btn, "Добавить пост")
    except Exception as e:  # noqa: BLE001
        result["steps"]["addButton"] = "click-failed"
        error(f"  ❌ Не удалось нажать «Добавить пост»: {_short_error(e)}")
        return finish("failed", f"Не удалось нажать «Добавить пост»: {_short_error(e)}")
    page.wait_for_timeout(700)
    result["steps"]["addButton"] = "ok"
    if how_add != "по элементу":
        info(f"  🔘 «Добавить пост» нажата {how_add}")

    # ─── 3. ТЕКСТ (строго ДО картинки – как в оригинале) ───
    text = task.get("postText") or ""
    try:
        text_field = page.wait_for_selector(
            '[contenteditable="true"], textarea[name*="text"], textarea[placeholder*="екст"], textarea',
            timeout=8_000,
            state="visible",
        )
    except Exception:
        text_field = None
    if not text_field:
        result["steps"]["text"] = "missing"
        error("  ❌ Не найдено поле для текста")
        return finish("failed", "Не найдено поле для текста")

    try:
        text_field.click()
        page.wait_for_timeout(150)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(100)
        text_field.type(text, delay=0)

        # КРИТИЧНО: убеждаемся, что весь текст реально доехал в DOM.
        # Без этого клик «Создать» может произойти раньше – и Яндекс публикует обрезанный пост.
        try:
            page.wait_for_function(
                """(expected) => {
                    const ae = document.activeElement
                      || document.querySelector('[contenteditable="true"], textarea[placeholder*="екст"], textarea');
                    if (!ae) return false;
                    const actual = (ae.value || ae.textContent || '').replace(/\\s+/g, ' ').trim();
                    const exp = expected.replace(/\\s+/g, ' ').trim();
                    if (actual.length < exp.length * 0.95) return false;
                    return actual.includes(exp.slice(0, 30)) && actual.includes(exp.slice(-30));
                }""",
                arg=text,
                timeout=TEXT_CONFIRM_TIMEOUT,
            )
            info(f"  ✓ Текст введён полностью ({len(text)} символов)")
        except Exception:
            warn("  ⚠️ Текст не подтверждён в DOM за 10 сек – публикация может быть с обрезанным текстом")
            page.wait_for_timeout(2000)

        page.wait_for_timeout(300)
        result["steps"]["text"] = "ok"
    except Exception as e:  # noqa: BLE001
        error(f"  ❌ Ошибка ввода текста: {e}")
        return finish("failed", f"Ошибка ввода текста: {e}")

    # ─── 4. КАРТИНКА (после текста, один раз, без ретраев) ───
    temp_dir = temp_dir or (USERS_DATA / "_temp")
    temp_files: list[str] = []
    if has_image_src:
        info("  🖼️  Загрузка картинки...")
        paths: list[str] = []
        upload_error = ""
        try:
            local = task.get("imagePath")
            if local and Path(local).exists():
                paths.append(str(local))
                info(f"  📁 Использую локальный файл: {Path(local).name}")
            elif task.get("imageUrl"):
                p = download_image(task["imageUrl"], temp_dir)
                paths.append(p)
                temp_files.append(p)

            # Доп. фото: Яндекс берёт максимум 4 на пост
            for extra in (task.get("extraImages") or [])[:3]:
                try:
                    src = extra if isinstance(extra, str) else (extra.get("path") or extra.get("url"))
                    if not src:
                        continue
                    if not str(src).startswith("http") and Path(src).exists():
                        paths.append(str(src))
                    else:
                        p = download_image(str(src), temp_dir)
                        paths.append(p)
                        temp_files.append(p)
                except Exception as e:  # noqa: BLE001
                    warn(f"  ⚠️ Доп. фото не добавлено: {e}")

            if not paths:
                upload_error = "Не удалось получить файл картинки"
            else:
                if len(paths) > 1:
                    info(f"  🖼️  Загружаю {len(paths)} фото в пост...")
                ok, upload_error = _attach_images(page, paths)
                if ok:
                    info("  ✅ Картинка загружена в редактор")
        except Exception as e:  # noqa: BLE001
            upload_error = str(e)
            ok = False
        else:
            ok = not upload_error

        if ok:
            result["steps"]["image"] = "ok"
        else:
            result["steps"]["image"] = "failed"
            result["imageError"] = upload_error
            warn(f"  ⚠️ {upload_error} – публикуем без картинки")

    # ─── 5. Кнопка публикации ───
    # С опросом: пока Яндекс дожёвывает картинку, «Создать» отключена, и
    # однократный поиск её «не находил» – хотя через пару секунд она появлялась.
    pub_el = None
    pub = None
    deadline_btn = time.time() + 12
    while True:
        handle = page.evaluate_handle(_FIND_PUBLISH_BTN_JS)
        pub_el = handle.as_element()
        if pub_el is not None:
            try:
                pub = pub_el.evaluate(_BTN_INFO_JS)
            except Exception:  # noqa: BLE001
                pub_el = None
        if pub_el is not None or time.time() >= deadline_btn:
            break
        page.wait_for_timeout(700)
    if pub_el is None:
        try:
            btns = page.evaluate(
                """() => Array.from(document.querySelectorAll('button, [role="button"]'))
                        .map(b => { const r = b.getBoundingClientRect();
                                    if (r.width < 5 || r.height < 5) return null;
                                    const t = (b.textContent || '').trim().slice(0, 40);
                                    return (b.disabled ? '[выкл] ' : '') + (t || '(без текста)'); })
                        .filter(Boolean).slice(0, 15)"""
            )
            info(f"  🔬 Кнопки на странице: {json.dumps(btns, ensure_ascii=False)}")
        except Exception:
            pass
        result["steps"]["publish"] = "missing"
        error("  ❌ Кнопка «Создать»/«Опубликовать» не найдена")
        _cleanup(temp_files)
        return finish("failed", "Кнопка «Создать»/«Опубликовать» не найдена")
    info(f"  🎯 Кнопка публикации: «{pub['text']}» {pub['w']}×{pub['h']} на y={pub['y']}")

    # ─── 6. ПЕРЕХВАТ API-ОТВЕТА – ДЛЯ ДИАГНОСТИКИ, НЕ ДЛЯ ВЕРДИКТА ───
    # Слушателя ставим ДО клика. Ответ нужен ради причины отказа в его теле:
    # решает, опубликован пост или нет, шаг 7 по ленте. Собираем ВСЕ подходящие
    # ответы, а не первый попавшийся: возьмём самый точный, среди равных –
    # последний (он и есть окончательный ответ Яндекса).
    api_hits: list[dict] = []
    api_result: dict[str, Any] = {}

    def on_response(response):  # pragma: no cover - сетевой колбэк
        try:
            if not _is_publish_api_response(response.url, response.request.method):
                return
            api_hits.append({"url": response.url, "status": response.status,
                             "method": response.request.method,
                             "score": _publish_response_score(response.url),
                             "resp": response})
        except Exception:
            pass

    page.on("response", on_response)
    page.wait_for_timeout(200)

    click_error = None
    how = ""
    try:
        # Кликаем ПО ЭЛЕМЕНТУ, как оригинал: сам прокрутит и нажмёт куда надо.
        # Клик по координатам оставлен последним запасным путём – он мажет,
        # если страница успела дорисоваться и уехать (а она успевает).
        try:
            pub_el.scroll_into_view_if_needed(timeout=3_000)
        except Exception:  # noqa: BLE001
            pass
        try:
            pub_el.click(timeout=8_000)
            how = "по элементу"
        except Exception as e1:  # noqa: BLE001
            warn(f"  ⚠️ Обычный клик не прошёл ({_short_error(str(e1))}) – жму событием")
            try:
                pub_el.dispatch_event("click")
                how = "событием click"
            except Exception:  # noqa: BLE001
                click_at(page, pub)
                how = "по координатам"
        info(f"  🔘 Клик «{pub['text']}» сделан ({how})")
        result["steps"]["publish"] = "clicked"
    except Exception as e:  # noqa: BLE001
        click_error = str(e)

    # Ждём ответ. Если пришёл ТОЧНЫЙ (создание поста) – дальше ждать нечего.
    # Если пришло что-то похожее, но не точное, дадим Яндексу досказать:
    # вдруг следом придёт настоящий ответ.
    deadline = time.time() + API_WAIT_TIMEOUT
    grace_until = None
    while time.time() < deadline:
        if any(h["score"] >= 3 for h in api_hits):
            break
        if api_hits:
            if grace_until is None:
                grace_until = time.time() + API_GRACE_AFTER_HIT
            elif time.time() >= grace_until:
                break
        page.wait_for_timeout(150)
    try:
        page.remove_listener("response", on_response)
    except Exception:
        pass

    if api_hits:
        # Самый точный ответ; среди одинаково точных – последний по времени.
        best = max(range(len(api_hits)), key=lambda i: (api_hits[i]["score"], i))
        api_result = dict(api_hits[best])
        if len(api_hits) > 1:
            info(f"  📡 Подходящих ответов: {len(api_hits)} – взят самый точный")
        # Тело ответа раньше выбрасывалось – а причина отказа лежит именно в нём.
        # Читаем ДО перезагрузки страницы: после неё тело уже не достать.
        try:
            body = api_result["resp"].json()
        except Exception:  # noqa: BLE001
            body = None
        if isinstance(body, dict):
            api_result["postId"] = str(body.get("id") or body.get("postId") or "") or None
            err = body.get("error") or body.get("errors") or body.get("message")
            if err:
                api_result["error"] = json.dumps(err, ensure_ascii=False)[:200]

    _cleanup(temp_files)

    # ─── 7. ВЕРДИКТ ВЫНОСИТ ЛЕНТА, И ТОЛЬКО ОНА ───
    # Сетевые сигналы оказались ненадёжны в обе стороны. Ответ загрузчика
    # картинки выдавался за подтверждение публикации – так Самара 12.08 попала
    # в отчёт как «ok» без поста. Отсутствие ответа тоже ничего не значило:
    # Уфа и Оренбург опубликовались, не показав его в окне ожидания. Поэтому
    # HTTP-ответ теперь идёт только в текст причины, а решение принимает лента.
    api_note = ""
    if api_result:
        info(f"  📡 API Яндекса: {api_result['method']} {api_result['url'][:100]} → HTTP {api_result['status']}"
             + (f", id={api_result['postId']}" if api_result.get("postId") else ""))
        if api_result.get("error"):
            warn(f"  📡 Ошибка в теле ответа: {api_result['error']}")
            api_note = f"ответ Яндекса: {api_result['error']}"
        elif api_result["status"] >= 400:
            api_note = f"company-post вернул HTTP {api_result['status']}"
    if click_error:
        warn(f"  ⚠️ Клик дал ошибку ({click_error[:100]}) – смотрю ленту")

    # Не перечитываем страницу, пока форма ещё открыта: навигация в момент
    # отправки оборвала бы запрос – мы бы своими руками сделали тот самый
    # провал, который ищем.
    deadline_form = time.time() + FORM_CLOSE_TIMEOUT
    while time.time() < deadline_form:
        try:
            # Пустая игла: здесь нас интересует только форма, пост ищет шаг 7.
            if not page.evaluate(_DOM_FALLBACK_JS, "").get("formOpen"):
                break
        except Exception:  # noqa: BLE001
            break
        page.wait_for_timeout(300)

    info("  🔍 Проверяю ленту...")
    verdict = verify_post_in_feed(page, posts_url, text, before_feed)

    if verdict["verdict"] == "published":
        result["steps"]["publish"] = "feed-confirmed"
        result["feedDetail"] = verdict["detail"]
        if has_image_src and not verdict["hasImage"]:
            # Пост есть, картинки нет. Повтор тут запрещён: он даст второй пост.
            # Так выглядела Тюмень 12.08 – по логу отличить её было нечем.
            result["steps"]["image"] = "missing-in-feed"
            # Причину подставит отчёт из imageError – здесь не дублируем.
            result.setdefault("imageError", "")
            if not result["imageError"]:
                result["imageError"] = "картинка не дошла до ленты"
            reason = "Пост опубликован, но БЕЗ картинки (проверено в ленте)"
            out = finish("no-image", reason)
            warn(f"  ⚠️ {label} – {reason} ({out['durationMs'] / 1000:.1f} сек)")
            return out
        reason = f"Пост опубликован (проверен в ленте: {verdict['detail']})"
        out = finish("ok", reason)
        info(f"  ✅ {label} – {reason} ({out['durationMs'] / 1000:.1f} сек)")
        return out

    if verdict["verdict"] == "absent":
        # Отсутствие ПОДТВЕРЖДЕНО – значит повтор безопасен и дубля не даст.
        result["steps"]["publish"] = "feed-absent"
        result["retrySafe"] = True
        result["feedDetail"] = verdict["detail"]
        reason = f"Пост не найден в ленте после публикации ({verdict['detail']})"
        if api_note:
            reason += f"; {api_note}"
        error(f"  ❌ {label} – {reason}")
        return finish("failed", reason)

    # Ни подтвердить, ни опровергнуть. Повтор запрещён: как раз здесь он и
    # рождает дубли – пост мог быть создан, просто мы его не увидели.
    result["steps"]["publish"] = "feed-unknown"
    result["feedDetail"] = verdict["detail"]
    warn(f"  ⚠️ Лента не дала ответа: {verdict['detail']}")
    return finish("unknown", f"Публикация не подтверждена по ленте ({verdict['detail']})"
                             + (f"; {api_note}" if api_note else "")
                             + ". Проверьте Яндекс.Бизнес вручную.")


def _cleanup(paths: Iterable[str]) -> None:
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
#  Фото в раздел «Товары» (порт uploadProductPhotos)
# ════════════════════════════════════════════════════════════════════

_GOODS_SECTION_JS = r"""
() => {
  let section = document.querySelector('.PhotosPage-Row_type_goods');
  if (!section) {
    const titleEl = document.querySelector('#goods-title');
    if (titleEl) {
      let p = titleEl;
      for (let lvl = 0; lvl < 6 && p; lvl++) {
        if (p.className && /PhotosPage-Row/.test(p.className.toString())) { section = p; break; }
        p = p.parentElement;
      }
    }
  }
  if (!section) {
    for (const r of document.querySelectorAll('.PhotosPage-Row, [class*="PhotosPage-Row"], [class*="Row_type"]')) {
      const title = r.querySelector('.PhotosPage-Title, [class*="Title"]');
      if (!title) continue;
      const direct = Array.from(title.childNodes)
        .filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent.trim()).join('');
      if (/^товары$/i.test(direct)) { section = r; break; }
    }
  }
  if (!section) return { error: 'section-not-found' };

  const countEl = section.querySelector('.PhotosPage-Count, [class*="Count"]');
  const count = countEl ? (parseInt((countEl.textContent || '0').replace(/\D/g, ''), 10) || 0) : 0;
  section.setAttribute('data-click-goods-section', '1');

  let uploadLabel = null;
  for (const lbl of section.querySelectorAll('label')) {
    if (/загрузить\s*(фотограф|фото)/.test((lbl.textContent || '').trim().toLowerCase())) { uploadLabel = lbl; break; }
  }
  if (uploadLabel) uploadLabel.setAttribute('data-click-upload-label', '1');
  section.scrollIntoView({ behavior: 'instant', block: 'start' });
  return { ok: true, count, hasUploadLabel: !!uploadLabel };
}
"""

_GOODS_SCROLL_JS = """
async () => {
  const step = 600;
  for (let i = 0; i < 30; i++) {
    if (document.querySelector('.PhotosPage-Row_type_goods') || document.querySelector('#goods-title')) {
      window.scrollBy(0, step);
      await new Promise(r => setTimeout(r, 200));
      window.scrollTo(0, 0);
      return;
    }
    if (window.scrollY + window.innerHeight >= document.body.scrollHeight) {
      await new Promise(r => setTimeout(r, 300));
      if (document.querySelector('.PhotosPage-Row_type_goods')) return;
      break;
    }
    window.scrollBy(0, step);
    await new Promise(r => setTimeout(r, 130));
  }
  window.scrollTo(0, 0);
}
"""

_GOODS_COUNT_JS = """
() => {
  const s = document.querySelector('[data-click-goods-section]') || document.querySelector('.PhotosPage-Row_type_goods');
  if (!s) return null;
  const c = s.querySelector('.PhotosPage-Count, [class*="Count"]');
  return c ? (parseInt((c.textContent || '0').replace(/\\D/g, ''), 10) || 0) : null;
}
"""


def fetch_photos(photo_urls: list[str], temp_dir: Path) -> tuple[list[str], list[str]]:
    """
    Ссылки и пути → файлы на диске. Возвращает (что получилось, что нет).

    Вынесено из загрузки в «Товары», потому что теперь те же снимки уходят на
    ДВЕ площадки: качать их дважды – это вдвое дольше и вдвое больше поводов
    упереться в чужой сервер.
    """
    local, errors = [], []
    for url in photo_urls or []:
        try:
            local.append(str(url) if Path(str(url)).exists() else download_image(str(url), temp_dir))
        except Exception as e:  # noqa: BLE001
            errors.append(f"Не скачать {str(url)[:50]}: {e}")
    return local, errors


def upload_product_photos(page: Page, task: dict, photo_urls: list[str], temp_dir: Path,
                          local_files: list[str] | None = None) -> dict:
    """
    Загружает фото в раздел «Фото и видео → Товары». Изолирована: любая ошибка
    здесь НЕ влияет на статус публикации поста.

    local_files – уже скачанные файлы. Прогон качает их один раз и отдаёт и
    сюда, и в 2ГИС.
    """
    result = {"uploaded": 0, "failed": 0, "errors": []}
    if not photo_urls:
        return result

    info(f"  📸 Загрузка {len(photo_urls)} фото в раздел «Товары»...")
    photos_url = build_photos_url(task.get("companyUrl"), task.get("companyId"))
    if not photos_url:
        result["errors"].append("Не определён ID компании")
        result["failed"] = len(photo_urls)
        return result

    if local_files is None:
        local, errors = fetch_photos(photo_urls, temp_dir)
        result["failed"] += len(errors)
        result["errors"].extend(errors)
    else:
        local = list(local_files)
    if not local:
        return result

    opened = False
    for attempt in range(2):
        try:
            page.wait_for_timeout(1000 if attempt == 0 else 2000)
            page.goto(photos_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1200)
            opened = True
            break
        except Exception as e:  # noqa: BLE001
            warn(f"  ⚠️ Попытка {attempt + 1} открыть /photos/ упала: {e}")
    if not opened:
        result["errors"].append("Не удалось открыть страницу фото")
        result["failed"] = len(local)
        return result

    try:
        page.evaluate(_GOODS_SCROLL_JS)
    except Exception as e:  # noqa: BLE001
        warn(f"  ⚠️ Ошибка при скролле: {e}")
    page.wait_for_timeout(300)

    section = page.evaluate(_GOODS_SECTION_JS)
    if section.get("error") or not section.get("hasUploadLabel"):
        result["errors"].append("Секция «Товары» или кнопка загрузки не найдена")
        result["failed"] = len(local)
        warn("  ⚠️ Секция «Товары» не найдена – возможно изменилась разметка Яндекса")
        return result

    before = section.get("count", 0)
    info(f"  ✅ Секция «Товары» найдена, сейчас {before} фото")

    try:
        handle = page.evaluate_handle(
            """() => {
                const label = document.querySelector('[data-click-upload-label]');
                if (!label) return null;
                const forId = label.getAttribute('for');
                if (forId) { const i = document.getElementById(forId);
                             if (i && i.tagName === 'INPUT' && i.type === 'file') return i; }
                const inside = label.querySelector('input[type="file"]');
                if (inside) return inside;
                const section = document.querySelector('[data-click-goods-section]');
                if (section) { const inputs = section.querySelectorAll('input[type="file"]');
                               if (inputs.length) return inputs[0]; }
                return null;
            }"""
        )
        element = handle.as_element()
        if element is None:
            raise RuntimeError("input[type=file] для секции «Товары» не найден")
        element.set_input_files(local)
        info(f"  📤 Файлы переданы в «Товары» ({len(local)} шт.)")
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"Ошибка передачи файлов: {e}")
        result["failed"] = len(local)
        return result

    info(f"  ⏳ Жду рост счётчика «Товары» (был {before})...")
    deadline = time.time() + 60
    last_seen = before
    while time.time() < deadline:
        cur = page.evaluate(_GOODS_COUNT_JS)
        if cur is None:
            page.wait_for_timeout(800)
            continue
        if cur != last_seen:
            info(f"  📈 Счётчик «Товары»: {before} → {cur}")
            last_seen = cur
        if cur >= before + len(local):
            result["uploaded"] = len(local)
            info(f"  ✅ Все {len(local)} фото загружены в «Товары»")
            break
        page.wait_for_timeout(500)
    else:
        delta = last_seen - before
        result["uploaded"] = delta if delta > 0 else len(local)
        warn(f"  🟡 Подтверждение неполное: счётчик вырос на {max(delta, 0)}/{len(local)}. Проверьте вручную.")

    return result


# ════════════════════════════════════════════════════════════════════
#  СВЕРКА КАРТОЧЕК С КП
# ════════════════════════════════════════════════════════════════════

COMPANIES_URL = "https://yandex.ru/sprav/companies/"

# Яндекс кладёт готовые данные страницы в window.__PRELOAD_DATA: там и список
# организаций, и карточка целиком – название, адрес, телефоны, почта, сайты.
# Это ровно то, что рисуется на экране, только без разбора вёрстки: строки
# списка дорисовываются скриптом и в разметке их нет, а классы у Яндекса
# меняются от сборки к сборке. Из огромного объекта забираем нужное прямо в
# браузере – тащить наружу мегабайт настроек пользователя незачем.
_SLIM_JS = r"""
  const one = (co) => {
    const addr = co.address || {};
    const fmt = (addr.formatted || {}).value || '';
    const urls = co.urls || [];
    const main = urls.filter(u => u && u.type === 'main').map(u => u.value).filter(Boolean);
    const social = {};
    for (const u of urls) {
      if (u && u.type === 'social' && u.social_network) social[u.social_network] = u.value || '';
    }
    // Название повторяется по одному на язык – в списке оно нужно один раз.
    const names = [...new Set((co.names || [])
      .map(n => ((n || {}).value || {}).value || '')
      .filter(Boolean))];
    return {
      id: String(co.id || co.permanent_id || ''),
      // Второй номер той же карточки. В КП ссылки записаны старым номером
      // (yandex.ru/sprav/25755702), а список отдаёт новый – без tycoon_id
      // сверка считала бы, что ссылка ведёт на чужую карточку.
      tycoonId: String(co.tycoon_id || ''),
      permanentId: String(co.permanent_id || ''),
      type: co.type || '',
      publishing: co.publishing_status || '',
      name: co.displayName || names[0] || '',
      names: names,
      address: fmt,
      regionCode: addr.region_code || '',
      geoId: addr.geo_id || 0,
      site: main[0] || '',
      sites: main,
      social: social,
      emails: (co.emails || []).filter(Boolean),
      phones: (co.phones || []).filter(p => p && !p.hide)
                               .map(p => p.formatted || p.number || '').filter(Boolean),
      rubrics: (co.rubrics || []).map(r => r.name || '').filter(Boolean),
      noAccess: !!co.noAccess,
      rating: co.rating === undefined ? null : co.rating,
    };
  };
"""

_COMPANIES_JS = r"""
() => {
  __SLIM__
  const st = ((window.__PRELOAD_DATA || {}).initialState) || {};
  const cl = st.companiesList;
  if (!cl || !Array.isArray(cl.listCompanies)) return null;
  return {
    page: cl.page || 1,
    limit: cl.limit || 0,
    total: cl.total || 0,
    items: cl.listCompanies.map(one),
  };
}
""".replace("__SLIM__", _SLIM_JS)

_CARD_JS = r"""
() => {
  __SLIM__
  const st = ((window.__PRELOAD_DATA || {}).initialState) || {};
  const co = ((st.edit || {}).info || {}).company;
  if (!co) return null;
  return one(co);
}
""".replace("__SLIM__", _SLIM_JS)

# Запасной разбор карточки по вёрстке – на случай, если Яндекс перестанет
# отдавать __PRELOAD_DATA. Поля подписаны: «Веб-сайт», «Телефон», «Электронная
# почта». Регулярки только по русским буквам: \w в JS кириллицу не ловит.
_CARD_DOM_JS = r"""
() => {
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const out = { phones: [], names: [], site: '', sites: [], emails: [], social: {},
                address: '', name: '', rubrics: [], id: '', type: '', publishing: '',
                noAccess: false, rating: null };
  for (const label of document.querySelectorAll('.ya-business-input__label')) {
    const wrap = label.closest('.ya-business-input') || label.parentElement?.parentElement;
    if (!wrap) continue;
    const field = wrap.querySelector('input, textarea');
    if (!field) continue;
    const name = clean(label.textContent);
    const value = clean(field.value);
    if (!value) continue;
    if (/^веб-?сайт$/i.test(name)) { if (!out.site) out.site = value; out.sites.push(value); }
    else if (/^телефон$/i.test(name)) out.phones.push(value);
    else if (/почт/i.test(name)) out.emails.push(value);
    else if (/название/i.test(name)) out.names.push(value);
    else if (/^(вконтакте|telegram|whatsapp|youtube|одноклассники)$/i.test(name))
      out.social[name.toLowerCase()] = value;
  }
  out.names = [...new Set(out.names)];
  out.name = out.names[0] || '';
  const addr = document.querySelector('.InfoAddress input, [class*="Address"] input');
  if (addr) out.address = clean(addr.value);
  return out;
}
"""


def _companies_page(page: Page, num: int, limit: int) -> dict | None:
    """Одна страница списка организаций. None – Яндекс не отдал данные."""
    url = f"{COMPANIES_URL}?no_redirect=1&page={num}&limit={limit}"
    page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
    wait_ui_ready(page)
    if looks_like_login_page(page):
        raise RuntimeError("Яндекс открыл страницу входа – сессия закончилась. "
                           "Зайдите заново в «Настройки» → «Вход в Яндекс».")
    return page.evaluate(_COMPANIES_JS)


def collect_companies(page: Page, log_every: int = 0, page_limit: int = 50,
                      stats: dict | None = None) -> list[dict]:
    """
    Все организации аккаунта из раздела «Организации».

    Список постраничный. Сколько страниц – Яндекс говорит сам (total и limit),
    поэтому не гадаем: читаем первую, считаем страницы, добираем остальные.
    Размер страницы Яндекс может урезать до своего – берём тот, что вернулся,
    иначе на limit=50 при фактических 10 мы потеряли бы четыре пятых списка.
    """
    first = _companies_page(page, 1, page_limit)
    if first is None:
        raise RuntimeError("Раздел «Организации» не отдал список. "
                           "Проверьте вход в Яндекс: возможно, сессия закончилась.")

    items: list[dict] = list(first.get("items") or [])
    total = int(first.get("total") or len(items))
    limit = int(first.get("limit") or len(items) or page_limit)
    pages = max(1, math.ceil(total / limit)) if limit else 1
    if log_every:
        info(f"  📃 Организаций в аккаунте: {total} · страниц: {pages}")

    seen = {c.get("id") for c in items}
    for num in range(2, pages + 1):
        chunk = _companies_page(page, num, limit)
        got = (chunk or {}).get("items") or []
        if not got:
            warn(f"  🟡 Страница {num} списка организаций пришла пустой – останавливаемся")
            break
        for co in got:
            if co.get("id") in seen:
                continue
            seen.add(co.get("id"))
            items.append(co)
        if log_every:
            info(f"  📃 Прочитано {len(items)} из {total}…")
    if stats is not None:
        stats["total"] = total          # сколько насчитал САМ Яндекс
        stats["read"] = len(items)
    if len(items) < total:
        warn(f"  🟡 Прочитано {len(items)} организаций из {total}, которые обещал Яндекс")
    return items


def read_company_card(page: Page, company_url: str) -> dict:
    """Название, адрес, сайт, телефоны и почта одной карточки."""
    url = build_edit_url(company_url) or company_url
    page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
    wait_ui_ready(page)
    if is_404(page):
        return {"error": "Страница карточки не найдена (404)", "url": url}
    if looks_like_login_page(page):
        return {"error": "Открылась страница входа – сессия не активна", "url": url}
    data = page.evaluate(_CARD_JS)
    if not data:
        page.wait_for_timeout(1500)
        data = page.evaluate(_CARD_DOM_JS) or {}
        data["fromDom"] = True
    data["url"] = url
    data["companyId"] = data.get("id") or extract_company_id(url)
    return data


# ════════════════════════════════════════════════════════════════════
#  АКТУАЛИЗАЦИЯ (порт actualize.js)
# ════════════════════════════════════════════════════════════════════

# Возвращаем САМ ЭЛЕМЕНТ, а не координаты. Кнопка «Данные актуальны» у Яндекса
# прилипшая и уезжает при дорисовке страницы: снятая точка устаревает, клик
# уходит в пустоту – в логе «клик прошёл», а подтверждения нет. Ровно на этом
# ломалась публикация («Создать»), и здесь было то же самое: у заказчика
# 11 городов из 19 получили жёлтое «тост не появился».
_ACTUALIZE_BTN_JS = r"""
() => {
  for (const b of document.querySelectorAll('button, [role="button"]')) {
    const t = (b.textContent || '').trim().toLowerCase();
    if (!t || t.length > 50) continue;
    if (/^данные\s+актуальны$/i.test(t) || /^актуализ[а-я]*\s+данные$/i.test(t)) {
      const r = b.getBoundingClientRect();
      if (r.width >= 80 && r.height >= 20 && !b.disabled) return b;
    }
  }
  return null;
}
"""

# Ищем ТЕКСТ уведомления по всей странице, а не только внутри элементов с
# «правильными» классами. Заказчик показала: уведомление «Данные
# актуализированы» всплывает, а Click его не видел – значит контейнер
# называется иначе. Смотрим на самый глубокий элемент с этим текстом:
# так не поймаем body целиком и не упрёмся в ограничение длины.
_ACTUALIZE_TOAST_JS = r"""
() => {
  const RX = /(данные\s+актуализирован|актуализирован[оы]|сохранено|обновлено)/i;
  for (const el of document.querySelectorAll('body *')) {
    const text = (el.textContent || '').trim();
    if (!text || text.length < 5 || text.length > 160) continue;
    if (!RX.test(text)) continue;
    // Берём только тот элемент, у которого совпадающего текста нет глубже:
    // иначе засчитаем родителя половины страницы.
    let deeper = false;
    for (const child of el.querySelectorAll('*')) {
      const t = (child.textContent || '').trim();
      if (t && t.length <= 160 && RX.test(t)) { deeper = true; break; }
    }
    if (deeper) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 8) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    return true;
  }
  return false;
}
"""


def actualize_city(page: Page, task: dict, idx: int = 0, total: int = 1) -> dict:
    """
    Статусы (как в actualize.js), плюс сверка со статусом из КП:
      'actualized'      – кнопку нажали
      'not-needed'      – кнопки нет, актуализация не требуется (это НЕ ошибка)
      'status-mismatch' – КП обещал живую карточку («Активная», «Онлайн»…), а
                          её нет – это не сбой приложения, а неверный статус
                          в таблице; заходить и жать нечего
      'failed'          – не открылась страница / ошибка клика
      'no-session'      – сессия Яндекса не активна

    task['kpStatus'] – значение колонки «Статус» из КП. Сверяем его с тем,
    что видим в кабинете, и кладём в cardAlive/statusVerdict/statusNote –
    отдельно от итогового status: «получилось ли актуализировать» и «совпал
    ли статус» – два разных вопроса, путать их в отчёте нельзя.
    """
    import kp_sheet

    started = time.time()
    label = f"[{idx + 1}/{total}] {task.get('cityName', '?')}"
    kp_status = (task.get("kpStatus") or "").strip()
    result = {
        "cityName": task.get("cityName"),
        "companyUrl": task.get("companyUrl"),
        "status": "failed",
        "reason": "",
        "durationMs": 0,
        "kpStatus": kp_status,
        "cardAlive": None,      # True/False/None – прочитали ли, жива ли карточка
        "statusVerdict": "",    # 'ok' | 'mismatch' | 'skip' (kp_sheet.status_verdict)
        "statusNote": "",
    }

    def finish(status: str, reason: str) -> dict:
        result["status"] = status
        result["reason"] = reason
        result["durationMs"] = int((time.time() - started) * 1000)
        verdict, note = kp_sheet.status_verdict(kp_status, result["cardAlive"])
        result["statusVerdict"], result["statusNote"] = verdict, note
        return result

    info(f"🏙  {label} – актуализация...")
    edit_url = build_edit_url(task.get("companyUrl"))
    if not edit_url:
        return finish("failed", "Не удалось определить URL раздела «Данные»")

    try:
        page.goto(edit_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2500)  # sticky-кнопка появляется не сразу
    except Exception as e:  # noqa: BLE001
        return finish("failed", f"Не удалось открыть страницу: {e}")

    if is_404(page):
        # Не угадали формат /edit/ ↔ /p/edit/ – пробуем второй, прежде чем
        # решить, что карточки нет: неверный формат уже путали с удалением
        # для раздела «Посты» (alt_posts_url – та же подмена подстроки, имя
        # осталось от исходной задачи, но работает для любого /…/edit/ URL).
        alt = alt_posts_url(edit_url)
        if alt and alt != edit_url:
            info(f"  🔁 404 – пробую другой формат адреса: {alt}")
            try:
                page.goto(alt, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(2500)
            except Exception:  # noqa: BLE001
                pass
            else:
                edit_url = alt
        if is_404(page):
            result["cardAlive"] = False
            verdict, note = kp_sheet.status_verdict(kp_status, False)
            if verdict == "mismatch":
                out = finish("status-mismatch", note)
                warn(f"  🚦 {label}: {note}")
                return out
            return finish("failed", f"Страница не найдена (404): {edit_url}")

    # Сессия слетела – Яндекс отдаёт страницу входа. Без этой проверки кнопки
    # на ней, разумеется, нет, и город получал «актуализация не требуется»:
    # неправда, которая повторялась для каждого города и попадала в отчёт как
    # успех. Публикация так умеет давно, актуализация – не умела.
    if looks_like_login_page(page):
        error("  ❌ Открылась страница входа Яндекса – сессия не активна")
        return finish("no-session", "Сессия Яндекса не активна: открылась страница входа")

    result["cardAlive"] = True
    verdict, note = kp_sheet.status_verdict(kp_status, True)
    if verdict == "mismatch":
        # Карточка живая, а КП обещал, что её быть не должно («Удалена» и
        # т.п.). В прогон такой город обычно и не попадает – parse_rows его
        # отсеивает, – но если статус сменили ПОСЛЕ того, как собрали список
        # задач, это всё равно стоит увидеть. Карточка открыта – жмём как
        # обычно, расхождение просто едет в отчёте рядом с результатом.
        warn(f"  🚦 {label}: {note}")

    btn = None
    deadline = time.time() + 4
    while time.time() < deadline:
        btn = page.evaluate_handle(_ACTUALIZE_BTN_JS).as_element()
        if btn is not None:
            break
        page.wait_for_timeout(400)

    if btn is None:
        out = finish("not-needed", "Кнопка «Данные актуальны» не найдена – актуализация не требуется")
        info(f"  ✓ {label}: {out['reason']} ({out['durationMs'] / 1000:.1f} сек)")
        return out

    # Жмём ПО ЭЛЕМЕНТУ: он сам подскроллится под курсор. Координаты устаревают.
    try:
        btn.scroll_into_view_if_needed(timeout=3_000)
    except Exception:  # noqa: BLE001
        pass
    try:
        btn.click(timeout=8_000)
        how = "по элементу"
    except Exception as e1:  # noqa: BLE001
        warn(f"  ⚠️ Обычный клик не прошёл ({_short_error(str(e1))}) – жму событием")
        try:
            btn.dispatch_event("click")
            how = "событием click"
        except Exception as e2:  # noqa: BLE001
            return finish("failed", f"Ошибка клика «Данные актуальны»: {e2}")
    info(f"  🔘 Клик «Данные актуальны» ({how})")

    toast = False
    deadline = time.time() + 6
    while time.time() < deadline:
        toast = bool(page.evaluate(_ACTUALIZE_TOAST_JS))
        if toast:
            break
        page.wait_for_timeout(300)

    out = finish(
        "actualized",
        "Данные актуализированы (тост подтвердил)" if toast else "Клик прошёл (тост не появился, но кнопка нажата)",
    )
    info(f"  {'✅' if toast else '🟡'} {label}: {out['reason']} ({out['durationMs'] / 1000:.1f} сек)")
    return out


# ════════════════════════════════════════════════════════════════════
#  ОТЗЫВЫ
# ════════════════════════════════════════════════════════════════════
#
# Читаем не вёрстку, а состояние страницы: Яндекс кладёт весь список
# отзывов готовым JSON в window.__PRELOAD_DATA (путь
# initialState.edit.reviews.list). Проверено на живой карточке –
# 13 отзывов, у 12 есть owner_comment, один без него.
#
# Так надёжнее: подписи и классы Яндекс меняет регулярно, а формат
# состояния живёт дольше. И «есть ли ответ» становится однозначным
# фактом, а не догадкой по внешнему виду поля.

REVIEWS_WAIT_MS = 15_000


# Читаем не весь __PRELOAD_DATA, а только нужные поля – и делаем это ВНУТРИ
# браузера. Раньше в Python приезжал весь блок состояния страницы, а это
# двести килобайт JSON на каждое чтение. При отправке 14 ответов подряд
# приложение просто съело всю память облака и упало.
_READ_REVIEWS_JS = r"""
() => {
  const st = ((((window.__PRELOAD_DATA || {}).initialState || {}).edit || {}).reviews) || {};
  const list = st.list || {};
  const pager = list.pager || {};
  const items = (list.items || []).map(it => ({
    id: it.id,
    author: ((it.author || {}).user || ''),
    rating: it.rating || 0,
    text: it.full_text || it.snippet || '',
    time_created: it.time_created || 0,
    answered: !!((it.owner_comment || {}).text || '').trim(),
  }));
  return { items, total: pager.total || items.length, limit: pager.limit || items.length };
}
"""


def _reviews_on_page(page: Page) -> dict | None:
    """Список отзывов с УЖЕ открытой страницы. None – состояния на ней нет."""
    try:
        data = page.evaluate(_READ_REVIEWS_JS)
    except Exception:  # noqa: BLE001 – страница ещё перерисовывается
        return None
    return data if data and data.get("items") is not None else None


def read_reviews(page: Page, url: str, navigate: bool = True) -> dict:
    """
    Открыть раздел отзывов и вернуть {'ok', 'items', 'total', 'shown', 'reason', 'url'}.

    navigate=False – страница уже открыта, грузить заново не надо. На отправке
    пачки это главное: один заход на город вместо перезагрузки под каждый ответ.
    """
    out = {"ok": False, "items": [], "total": 0, "shown": 0, "reason": "", "url": url}
    if not url:
        out["reason"] = "Не удалось определить URL раздела «Отзывы»"
        return out

    if navigate:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:  # noqa: BLE001
            out["reason"] = f"Не удалось открыть страницу отзывов: {_short_error(e)}"
            return out

        # 404 – возможно, не угадали формат адреса. Пробуем второй, как с постами.
        if is_404(page):
            alt = alt_posts_url(url)
            if alt:
                try:
                    page.goto(alt, wait_until="domcontentloaded", timeout=30_000)
                    out["url"] = alt
                except Exception:  # noqa: BLE001
                    pass
            if is_404(page):
                out["reason"] = f"Страница отзывов не найдена (404): {url}"
                return out

        if looks_like_login_page(page):
            out["reason"] = "Яндекс увёл на страницу входа – сессия не действует"
            out["noSession"] = True     # прогону это знак остановиться, а не идти дальше
            return out

    data = None
    deadline = time.time() + (REVIEWS_WAIT_MS / 1000 if navigate else 3)
    while time.time() < deadline:
        data = _reviews_on_page(page)
        if data:
            break
        page.wait_for_timeout(400)

    if not data:
        out["reason"] = ("На странице отзывов не нашли window.__PRELOAD_DATA – "
                         "Яндекс мог поменять отдачу страницы")
        return out

    out.update(ok=True, items=data["items"], total=int(data["total"] or 0),
               shown=len(data["items"]))
    return out


def review_state(page: Page, url: str, review_id: str, navigate: bool = True) -> dict:
    """
    Состояние одного отзыва. Пока черновик ждал человека, на отзыв мог
    ответить кто-то другой; второй ответ на тот же отзыв – ровно то, чего
    заказчик не хочет.
    """
    data = read_reviews(page, url, navigate=navigate)
    if not data["ok"]:
        return {"ok": False, "found": False, "answered": False, "reason": data["reason"]}
    for it in data["items"]:
        if it.get("id") == review_id:
            return {"ok": True, "found": True, "answered": bool(it.get("answered")),
                    "reason": "", "item": it}
    return {"ok": True, "found": False, "answered": False,
            "reason": "Отзыв не найден на первой странице – возможно, уехал ниже"}


# Ищем поле ответа именно у нужного отзыва, а не первое попавшееся:
# на странице их столько же, сколько отзывов, и промах означал бы ответ
# не тому человеку. Опора – текст отзыва, он уникален в пределах карточки.
_FIND_ANSWER_FIELD_JS = r"""
(payload) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const needle = norm(payload.text).slice(0, 60);
  if (!needle) return null;

  document.querySelectorAll('[data-click-answer]').forEach(n => n.removeAttribute('data-click-answer'));

  const editable = el =>
    el.matches('textarea, input[type="text"], [contenteditable="true"]');

  // Самый узкий блок, который содержит и текст отзыва, и поле для ответа.
  let best = null, bestLen = Infinity;
  for (const el of document.querySelectorAll('div, li, article, section, form')) {
    const t = norm(el.innerText);
    if (!t.includes(needle)) continue;
    const field = el.querySelector('textarea, input[type="text"], [contenteditable="true"]');
    if (!field) continue;
    if (t.length < bestLen) { best = { box: el, field }; bestLen = t.length; }
  }
  if (!best) return null;

  best.field.setAttribute('data-click-answer', '1');
  best.box.setAttribute('data-click-answer-box', '1');
  return { tag: best.field.tagName.toLowerCase(), contentEditable: editable(best.field) };
}
"""

# Кнопка отправки ответа – КРУЖОК СО СТРЕЛКОЙ, без единой буквы внутри.
# Первая версия искала кнопку по тексту («Отправить», «Ответить») и,
# понятно, не находила ничего: жала Ctrl+Enter, Яндекс её игнорировал, а
# Click рапортовал «отправлено». Ответы не уходили.
#
# Ищем иначе и надёжнее: перед вводом текста помечаем ВСЕ кнопки страницы
# как уже виденные. Стрелка появляется только после ввода – значит, это
# кнопка без пометки. Из непомеченных отбрасываем «Отменить редактирование»
# и берём ближайшую к полю ответа.

_MARK_SEEN_BUTTONS_JS = r"""
() => {
  let n = 0;
  document.querySelectorAll('button, [role="button"], input[type="submit"]').forEach(b => {
    b.setAttribute('data-click-seen', '1');
    n++;
  });
  document.querySelectorAll('[data-click-answer-send]').forEach(
    b => b.removeAttribute('data-click-answer-send'));
  return n;
}
"""

# Кнопка отправки – кружок со стрелкой, без единой буквы внутри. Разметку
# прислал заказчик из инспектора браузера:
#
#   <div class="BusinessResponseDraft-SendButtonWrapper">
#     <button class="… BusinessResponseDraft-SendButton">
#       <span class="… Icon_depot_ArrowLongForward16White" aria-hidden="true">
#
# Ищем сначала по этому классу – он говорит сам за себя. Прошлая версия
# опиралась на «кнопка, которой до ввода текста не было», и промахнулась:
# кнопка есть в разметке всегда, до ввода она просто свёрнута
# (BusinessResponseDraft-Controls_expanded). Поиск по «новизне» оставлен
# запасным путём на случай, если Яндекс переименует классы.
_FIND_SEND_BUTTON_JS = r"""
() => {
  const box = document.querySelector('[data-click-answer-box="1"]') || document;
  const field = document.querySelector('[data-click-answer="1"]');
  const fr = field ? field.getBoundingClientRect() : null;

  const usable = b => {
    const r = b.getBoundingClientRect();
    return r.width >= 8 && r.height >= 8 &&
           !b.disabled && b.getAttribute('aria-disabled') !== 'true';
  };
  const take = (b, how) => {
    b.setAttribute('data-click-answer-send', '1');
    return how;
  };

  // 1. Своё имя у кнопки отправки ответа.
  for (const b of box.querySelectorAll('[class*="SendButton"], [class*="sendButton"]')) {
    const btn = b.tagName === 'BUTTON' ? b : b.querySelector('button, [role="button"]');
    if (btn && usable(btn)) return take(btn, 'SendButton');
  }

  // 2. Кнопка со стрелкой внутри.
  for (const icon of box.querySelectorAll('[class*="ArrowLongForward"], [class*="ArrowForward"]')) {
    const btn = icon.closest('button, [role="button"]');
    if (btn && usable(btn)) return take(btn, 'стрелка');
  }

  // 3. Запасной путь: кнопка, которой до ввода текста на странице не было.
  const fresh = [];
  document.querySelectorAll('button, [role="button"], input[type="submit"]').forEach(b => {
    if (b.getAttribute('data-click-seen') || !usable(b)) return;
    const label = ((b.textContent || '') + ' ' +
                   (b.getAttribute('aria-label') || '') + ' ' +
                   (b.getAttribute('title') || '')).replace(/\s+/g, ' ').trim();
    if (/отмен|cancel|удал|delete/i.test(label)) return;
    const dist = fr ? Math.abs(b.getBoundingClientRect().top - fr.bottom) : 0;
    fresh.push({ el: b, dist });
  });
  if (fresh.length) {
    fresh.sort((a, b) => a.dist - b.dist);
    return take(fresh[0].el, 'новая кнопка у поля');
  }
  return null;
}
"""

# Зелёное уведомление «Ответ отправлен» в правом верхнем углу – первый
# признак успеха, ещё до перезагрузки страницы.
_ANSWER_TOAST_JS = r"""
() => /ответ\s+(отправлен|добавлен|опубликован)/i.test(document.body.innerText || '')
"""


# Появился ли наш ответ рядом с отзывом. Проверяем по живой странице, а не
# перезагружая её: каждая перезагрузка раздела отзывов – это тяжёлая страница
# Яндекса, и четыре штуки на один ответ клали приложение по памяти.
_ANSWER_SHOWN_JS = r"""
(payload) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const needle = norm(payload.review).slice(0, 60);
  const mark = norm(payload.answer).slice(0, 40);
  if (!needle || !mark) return false;
  for (const el of document.querySelectorAll('div, li, article, section')) {
    const t = norm(el.innerText);
    if (t.includes(needle) && t.includes(mark)) {
      // Поле ввода с нашим же текстом не считается: ждём именно опубликованный ответ.
      const field = el.querySelector('textarea, [contenteditable="true"]');
      if (!field || !norm(field.value || field.innerText).includes(mark)) return true;
    }
  }
  return false;
}
"""


def publish_review_answer(page: Page, url: str, review_id: str, text: str,
                          review_text: str = "", navigate: bool = True) -> dict:
    """
    Опубликовать подтверждённый человеком ответ на конкретный отзыв.

    Порядок ровно тот, что человек делает руками: вписать текст в поле
    «Ответ компании» → нажать кружок со стрелкой справа → убедиться, что
    ответ появился.

    navigate=False – страница города уже открыта, грузить её заново не надо.
    Так пачка ответов по одному городу обходится ОДНИМ заходом на страницу.

    'answered' возвращаем, только когда ответ реально видно. «Клик был,
    подтверждения нет» уходило как успех – отзыв исчезал из очереди, а в
    Яндексе ничего не появлялось.
    """
    body = (text or "").strip()
    if not body:
        return {"status": "failed", "reason": "Пустой текст ответа"}

    state = review_state(page, url, review_id, navigate=navigate)
    if not state["ok"] and not navigate:
        # Страница оказалась не та – зайдём честно.
        state = review_state(page, url, review_id, navigate=True)
    if not state["ok"]:
        return {"status": "failed", "reason": state["reason"]}
    if state["answered"]:
        return {"status": "already", "reason": "На отзыв уже ответили – публикацию отменили"}
    if not state["found"]:
        return {"status": "failed", "reason": state["reason"]}

    needle = review_text or ((state.get("item") or {}).get("text") or "")
    if not needle:
        return {"status": "failed", "reason": "Нечем опознать отзыв на странице"}

    # React дорисовывает карточки отзывов не мгновенно.
    field = None
    deadline = time.time() + REVIEWS_WAIT_MS / 1000
    while time.time() < deadline:
        try:
            field = page.evaluate(_FIND_ANSWER_FIELD_JS, {"text": needle})
        except Exception:  # noqa: BLE001
            field = None
        if field:
            break
        page.wait_for_timeout(500)

    if not field:
        return {"status": "failed",
                "reason": "Не нашли поле «Ответ компании» у этого отзыва на странице"}

    target = page.locator('[data-click-answer="1"]').first
    try:
        target.scroll_into_view_if_needed(timeout=3_000)
    except Exception:  # noqa: BLE001
        pass

    # Помечаем кнопки ДО ввода – запасной путь поиска кнопки отправки.
    try:
        page.evaluate(_MARK_SEEN_BUTTONS_JS)
    except Exception:  # noqa: BLE001
        pass

    try:
        target.click(timeout=8_000)
        if field.get("contentEditable") and field.get("tag") not in ("textarea", "input"):
            page.keyboard.insert_text(body)
        else:
            target.fill(body, timeout=8_000)
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "reason": f"Не удалось вписать ответ: {_short_error(e)}"}

    label = None
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            label = page.evaluate(_FIND_SEND_BUTTON_JS)
        except Exception:  # noqa: BLE001
            label = None
        if label:
            break
        page.wait_for_timeout(400)

    if not label:
        return {"status": "failed",
                "reason": "Текст вписали, но кнопка отправки рядом с полем не появилась"}

    try:
        page.locator('[data-click-answer-send="1"]').first.click(timeout=8_000)
        info(f"  🔘 Ответ отправлен кнопкой «{label}»")
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "reason": f"Кнопка отправки не нажалась: {_short_error(e)}"}

    # Ждём, пока ответ появится на самой странице – без перезагрузки.
    deadline = time.time() + 12
    while time.time() < deadline:
        try:
            if page.evaluate(_ANSWER_SHOWN_JS, {"review": needle, "answer": body}):
                return {"status": "answered", "reason": "Ответ опубликован – Яндекс его показывает"}
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(700)

    # Не увидели глазами – один раз перечитываем страницу начисто.
    after = review_state(page, url, review_id, navigate=True)
    if after["ok"] and after["answered"]:
        return {"status": "answered", "reason": "Ответ опубликован"}
    return {"status": "failed",
            "reason": "Кнопку нажали, но ответ на карточке не появился. "
                      "Ответьте вручную по ссылке из списка"}


# ════════════════════════════════════════════════════════════════════
#  ВХОД В ЯНДЕКС (пошагово, со скриншотом вместо окна браузера)
# ════════════════════════════════════════════════════════════════════

OTHER_METHOD_BUTTON = '[data-testid="split-add-user-more-button"]'
SWITCH_TO_LOGIN_OPTION = '[data-testid="menu-option-switchToLogin"]'
GENERIC_TEXT_FIELD = '[data-testid="text-field-input"]'
LOGIN_NEXT_BUTTON = '[data-testid="split-add-user-next-login"]'
PASSWORD_NEXT_BUTTON = '[data-testid="password-next"]'
EMAIL_CODE_NEXT_BUTTON = '[data-testid="challenges-email-code-next"]'
POST_LOGIN_SKIP_BUTTONS = [
    '[data-testid="webauthn-reg-later-button"]',
    '[data-testid="identification-promo-start-skip-btn"]',
]


def _click_exact_button(page: Page, texts: list[str]) -> bool:
    """
    Нажать кнопку по её надписи.

    Кликаем ПО ЭЛЕМЕНТУ, а не по координатам. Координаты живут ровно до
    первого скролла: страница подъезжает – и клик уходит в пустоту. На этом
    уже ломалась публикация («Создать» на y=2201 в окне высотой 600), тот же
    промах здесь молча съедал «Далее» и «Подтвердить» на входе.
    """
    handle = page.evaluate_handle(
        """(texts) => {
            for (const btn of document.querySelectorAll('button, [role="button"]')) {
                if (btn.disabled) continue;
                const t = (btn.textContent || '').trim().toLowerCase();
                if (texts.includes(t)) {
                    const r = btn.getBoundingClientRect();
                    if (r.width > 30 && r.height > 15) return btn;
                }
            }
            return null;
        }""",
        texts,
    )
    el = handle.as_element()
    if el is None:
        return False
    try:
        el.scroll_into_view_if_needed(timeout=2_000)
    except Exception:  # noqa: BLE001
        pass
    try:
        el.click(timeout=5_000)
        return True
    except Exception:  # noqa: BLE001
        try:
            el.dispatch_event("click")
            return True
        except Exception:  # noqa: BLE001
            return False


# Кнопки-подтверждения на «челлендж»-экранах паспорта: страница без единого
# поля ввода, только текст и кнопка («Безопасный вход – подтвердите номер»).
CONFIRM_BUTTON_TEXTS = [
    "подтвердить", "продолжить", "далее", "хорошо", "готово", "войти",
    "confirm", "continue", "next", "ok",
]

# Надписи держим на двух языках. Паспорт иногда отдаёт английский вариант, и
# это не перевод, а другой экран: «Send email for log in» вместо «Отправить
# письмо для входа». По русской строке такая кнопка не находилась вовсе.
MAIL_LOGIN_TEXTS = [
    "отправить письмо для входа", "войти по почте", "войти с помощью почты",
    "send email for log in", "send email for login", "send a login link",
    "log in with email",
]
SMS_LOGIN_TEXTS = [
    "войти с помощью смс", "войти по смс",
    "log in with sms code", "log in with sms",
]
LOGIN_BY_NAME_TEXTS = [
    "войти по логину", "log in with username", "log in with id", "войти по id",
]
MORE_TEXTS = ["ещё", "еще", "more", "other options", "другие способы"]
ANOTHER_WAY_TEXTS = [
    "другой способ входа", "другой способ", "выбрать другой способ",
    "another way to log in", "other login method", "try another way",
]

# Один заход в браузер вместо десятка мелких: забираем всё, по чему потом
# на стороне Python решаем, что за экран перед нами.
_PAGE_STATE_JS = """() => {
    const visible = (el) => {
        const r = el.getBoundingClientRect();
        if (r.width < 10 || r.height < 8) return false;
        const s = getComputedStyle(el);
        return s.visibility !== 'hidden' && s.display !== 'none';
    };
    const inputs = [...document.querySelectorAll('input, textarea')].filter(visible);
    return {
        url: location.href,
        text: (document.body.innerText || '').slice(0, 4000),
        inputs: inputs.map((i) => ({
            type: (i.type || 'text').toLowerCase(),
            hint: [i.name, i.id, i.autocomplete, i.getAttribute('inputmode'),
                   i.placeholder, i.getAttribute('data-testid')]
                  .filter(Boolean).join(' ').toLowerCase(),
        })),
        buttons: [...document.querySelectorAll('button, [role="button"]')]
            .filter(visible)
            .filter((b) => !b.disabled)
            .map((b) => (b.textContent || '').trim())
            .filter(Boolean),
    };
}"""


class YbLoginFlow:
    """Пошаговый вход: на каждом шаге отдаём скриншот, человек вводит логин/пароль/код."""

    def __init__(self, project_id: str, headless: bool = True):
        self.project_id = project_id
        # headless=False имеет смысл только при локальном запуске: в облаке экрана нет.
        self.headless = headless
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def start(self) -> dict:
        engine = resolve_engine()
        try:
            self._pw = sync_playwright().start()
            self.browser = _launch(self._pw, engine, headless=self.headless)
            # Куки устройства с прошлого раза: для Яндекса это тот же браузер,
            # а не новый компьютер – проверок он просит заметно меньше.
            dev = device_path(self.project_id)
            self.context = self.browser.new_context(
                storage_state=str(dev) if dev.exists() else None,
                viewport={"width": 1000, "height": 700}, user_agent=UA,
                locale=LOCALE, extra_http_headers=LANG_HEADERS)
            self.page = self.context.new_page()
            self.page.goto("about:blank", timeout=10_000)
            self.page.goto(PASSPORT_URL, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1500)
            self._dismiss_cookie_banner()
            # Паспорт встречает формой телефона – нам нужен вход по логину.
            if self.detect_step() == "phone":
                self._switch_to_login_by_password()
            return self.state()
        except Exception:
            self.close()
            raise

    def _dismiss_cookie_banner(self) -> None:
        names = ["Allow all", "Принять все", "Accept all", "Разрешить все"]
        for _ in range(3):
            for frame in self.page.frames:
                for name in names:
                    try:
                        btn = frame.get_by_role("button", name=name, exact=True)
                        if btn.count() > 0:
                            btn.first.click(timeout=1500, force=True)
                            self.page.wait_for_timeout(400)
                            return
                    except Exception:
                        continue
            self.page.wait_for_timeout(500)

    def _switch_to_login_by_password(self) -> bool:
        """
        Уйти с экрана «Введите номер телефона» на вход по логину.

        Новый паспорт (/pwl-yandex/) открывается на телефоне, а нужный нам
        путь спрятан за кнопкой «Ещё» → «Войти по логину». Ищем по НАДПИСЯМ:
        старые data-testid на этом экране больше не встречаются, и молчаливый
        промах оставлял Click на форме телефона – туда он потом и печатал
        логин, а Яндекс отвечал ошибкой.
        """
        for _ in range(2):
            if _click_exact_button(self.page, LOGIN_BY_NAME_TEXTS):
                self.page.wait_for_timeout(900)
                return True
            if self.page.locator(SWITCH_TO_LOGIN_OPTION).count() > 0:
                self.page.click(SWITCH_TO_LOGIN_OPTION)
                self.page.wait_for_timeout(900)
                return True
            # Пункта не видно – он под «Ещё».
            opened = _click_exact_button(self.page, MORE_TEXTS)
            if not opened and self.page.locator(OTHER_METHOD_BUTTON).count() > 0:
                self.page.click(OTHER_METHOD_BUTTON)
                opened = True
            if not opened:
                return False
            self.page.wait_for_timeout(700)
        return False

    def send_login_email(self) -> dict:
        """
        «Отправить письмо для входа» – Яндекс пришлёт ссылку на почту, и вход
        подтверждается с любого устройства. Когда пароль не подходит или
        Яндекс требует звонок на телефон, это самый короткий путь.
        """
        self._dismiss_cookie_banner()
        ok = _click_exact_button(self.page, MAIL_LOGIN_TEXTS)
        self.page.wait_for_timeout(2500)
        st = self.state()
        st["mail_sent"] = ok
        if not ok:
            st["note"] = ("Кнопки «Отправить письмо для входа» на этом экране нет. "
                          "Она появляется на шаге ввода пароля – вернитесь назад.")
        return st

    def send_sms_code(self) -> dict:
        """«Войти с помощью смс» – код придёт на привязанный номер."""
        self._dismiss_cookie_banner()
        _click_exact_button(self.page, SMS_LOGIN_TEXTS)
        self.page.wait_for_timeout(2500)
        return self.state()

    def another_way(self) -> dict:
        """
        «Другой способ входа» – с экрана ожидания письма Яндекс отсюда
        предлагает картинки, QR и прочее. Кнопка есть на экране, значит
        должна быть и в приложении: иначе выбор только у Яндекса.
        """
        self._dismiss_cookie_banner()
        _click_exact_button(self.page, ANOTHER_WAY_TEXTS)
        self.page.wait_for_timeout(2000)
        return self.state()

    def go_back(self) -> dict:
        """
        Стрелка «назад» в карточке Яндекса.

        Нужна, чтобы уйти с экрана, который навязал сам Яндекс (звонок на
        телефон), обратно к паролю – там есть «Отправить письмо для входа».
        Иначе выбора у человека нет вообще.
        """
        self._dismiss_cookie_banner()
        for sel in ('[data-testid*="back" i]', 'button[aria-label*="азад"]',
                    'button[aria-label*="ack" i]'):
            try:
                loc = self.page.locator(sel)
                if loc.count() > 0:
                    loc.first.click(timeout=3_000)
                    self.page.wait_for_timeout(1800)
                    return self.state()
            except Exception:  # noqa: BLE001
                continue
        try:
            self.page.go_back(wait_until="domcontentloaded", timeout=8_000)
            self.page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            pass
        return self.state()

    def _skip_post_login_prompts(self) -> None:
        for sel in POST_LOGIN_SKIP_BUTTONS:
            if self.page.locator(sel).count() > 0:
                self.page.click(sel)
                self.page.wait_for_timeout(800)

    def _field_for(self, kind: str):
        """
        Поле, которое соответствует ШАГУ, а не «первое попавшееся».

        Слепое «первое текстовое поле» приводило к тому, что пароль улетал в
        графу кода, а код – в графу логина: экран уже сменился, а код об этом
        не знал. Пароль отличается типом поля, остальное – по подсказкам.
        """
        if kind == "password":
            pw = self.page.locator('input[type="password"]')
            if pw.count() > 0:
                return pw.first
        candidates = self.page.locator(
            f'{GENERIC_TEXT_FIELD}, input[type="text"], input[type="email"], '
            f'input[type="tel"], input:not([type])'
        )
        if candidates.count() > 0:
            return candidates.first
        return self.page.locator("input:not([type=hidden])").first

    def _submit_generic_field(self, value: str, next_button_selector: str,
                              kind: str = "login") -> None:
        field = self._field_for(kind)
        field.click()
        try:
            field.fill("")
        except Exception:  # noqa: BLE001
            pass
        field.type(value, delay=40)   # посимвольно: иначе кнопка «Далее» остаётся серой
        self._dismiss_cookie_banner()
        self.page.wait_for_timeout(400)
        field.press("Enter")
        self.page.wait_for_timeout(1200)
        if self.page.locator(next_button_selector).count() > 0:
            try:
                self.page.click(next_button_selector, timeout=3000)
            except Exception:
                pass
            self.page.wait_for_timeout(1200)
        _click_exact_button(self.page, ["next", "continue", "войти", "продолжить", "далее"])
        self.page.wait_for_timeout(1500)

    def click_next(self) -> dict:
        self._dismiss_cookie_banner()
        if self.page.locator(LOGIN_NEXT_BUTTON).count() > 0:
            self.page.click(LOGIN_NEXT_BUTTON)
            self.page.wait_for_timeout(1200)
        if not _click_exact_button(self.page, ["next", "continue", "войти", "продолжить", "далее"]):
            self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(1500)
        return self.state()

    def submit_phone(self, phone: str) -> dict:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if digits.startswith(("7", "8")):
            digits = digits[1:]
        self.page.click('input[type="tel"]')
        self.page.keyboard.type(digits, delay=40)
        self.page.wait_for_timeout(500)
        if not _click_exact_button(self.page, ["log in", "войти"]):
            self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(2500)
        return self.state()

    # Экран сменился, пока человек печатал – значит вводить сюда нельзя.
    # Иначе пароль попадает в графу кода (и наоборот), а Яндекс отвечает
    # ошибкой, из которой вообще не понять, что произошло.
    _WRONG_SCREEN = {
        "login": "Яндекс сейчас просит не логин, а {}. Посмотрите на снимок ниже.",
        "password": "Яндекс сейчас просит не пароль, а {}. Пароль вводить некуда – "
                    "посмотрите на снимок ниже.",
        "code": "Яндекс сейчас просит не код, а {}. Код улетел бы не в ту графу.",
    }
    _STEP_NAMES = {
        "login": "логин", "password": "пароль", "code": "код",
        "phone": "номер телефона", "captcha": "капчу",
        "challenge": "подтверждение (кнопка, без ввода)", "done": "ничего – вход уже выполнен",
        "unknown": "что-то, чего Click не разобрал",
    }

    def _require_step(self, kind: str) -> None:
        step = self.detect_step()
        if step == kind or step == "unknown":
            return
        raise RuntimeError(self._WRONG_SCREEN[kind].format(self._STEP_NAMES.get(step, step)))

    def submit_login(self, login_value: str) -> dict:
        if self.detect_step() == "phone":
            self._switch_to_login_by_password()
        self._require_step("login")
        self._submit_generic_field(login_value, LOGIN_NEXT_BUTTON, "login")
        self._skip_post_login_prompts()
        return self.state()

    def submit_password(self, password: str) -> dict:
        """
        Пароль: вписать – пауза – ОДНО нажатие «Далее». Без Enter и без
        запасных кликов: заказчику важно, чтобы Click не жал ничего лишнего –
        лишние нажатия улетали в кнопки следующего экрана.
        """
        self._require_step("password")
        field = self._field_for("password")
        field.click()
        try:
            field.fill("")
        except Exception:  # noqa: BLE001
            pass
        field.type(password, delay=40)
        self.page.wait_for_timeout(1000)          # секунда «посмотреть», как просили
        clicked = False
        if self.page.locator(PASSWORD_NEXT_BUTTON).count() > 0:
            try:
                self.page.click(PASSWORD_NEXT_BUTTON, timeout=3000)
                clicked = True
            except Exception:  # noqa: BLE001
                pass
        if not clicked:
            _click_exact_button(self.page, ["далее", "войти", "next", "log in"])
        self.page.wait_for_timeout(2500)
        self._skip_post_login_prompts()
        return self.state()

    def type_only(self, value: str) -> dict:
        """Только вписать в поле текущего экрана – НИЧЕГО не нажимая."""
        field = self._field_for(self.detect_step())
        field.click()
        try:
            field.fill("")
        except Exception:  # noqa: BLE001
            pass
        field.type(value, delay=40)
        self.page.wait_for_timeout(400)
        return self.state()

    def submit_code(self, code: str) -> dict:
        # Пока человек ждал код, паспорт мог откатиться на первый экран – тогда
        # код улетел бы в поле логина. Проверяем, что на экране действительно код.
        self._require_step("code")
        self._submit_code_field(code)
        self._skip_post_login_prompts()
        return self.state()

    def _submit_code_field(self, code: str) -> None:
        """
        Код из SMS. Паспорт рисует его либо одним полем, либо цепочкой
        квадратиков по цифре в каждом – во втором случае fill() затирает
        только первый, поэтому печатаем посимвольно: фокус переезжает сам.
        """
        digits = "".join(ch for ch in code if ch.isalnum())
        field = self.page.locator(f"{GENERIC_TEXT_FIELD}, input:not([type=hidden])").first
        field.click()
        try:
            field.fill("")
        except Exception:  # noqa: BLE001
            for _ in range(len(digits) + 2):
                self.page.keyboard.press("Backspace")
        self.page.keyboard.type(digits, delay=90)
        self.page.wait_for_timeout(800)
        # Часто форма уходит сама, как только введена последняя цифра.
        if self.page.locator(EMAIL_CODE_NEXT_BUTTON).count() > 0:
            try:
                self.page.click(EMAIL_CODE_NEXT_BUTTON, timeout=3000)
            except Exception:  # noqa: BLE001
                pass
        elif not _click_exact_button(self.page, ["войти", "продолжить", "далее", "next", "continue"]):
            self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(2500)

    def screenshot(self) -> bytes:
        return self.page.screenshot(full_page=True)

    # ── Автоматический вход ──────────────────────────────────────────
    # Логин и пароль у нас уже есть в «Настройках», поэтому нет смысла заставлять
    # человека вводить их руками по картинке. Читаем, ЧТО просит страница, и
    # заполняем сами. Останавливаемся только там, где человек действительно нужен:
    # код из SMS/почты, капча, подтверждение в приложении.

    def page_state(self) -> dict:
        """
        Что паспорт показывает ПРЯМО СЕЙЧАС.

        Раньше шаг угадывался один раз – после авто-входа – и больше не
        пересматривался. Паспорт тем временем спокойно возвращался на экран
        логина, а приложение продолжало показывать поля «Пароль» и «Код»:
        вводить логин было буквально некуда. Поэтому шаг теперь читается
        со страницы заново перед каждой отрисовкой.

        step:
          'login'     – просит логин или e-mail
          'password'  – просит пароль
          'code'      – просит код (SMS / почта / приложение)
          'challenge' – экран без полей, нужно просто нажать кнопку
          'captcha'   – капча
          'done'      – вход выполнен (есть кука авторизации)
          'unknown'   – не разобрали, показываем всё сразу
        """
        if self.has_auth_cookie():
            return {"step": "done", "url": (self.page.url if self.page else ""),
                    "title": "Вход выполнен.", "buttons": []}
        try:
            raw = self.page.evaluate(_PAGE_STATE_JS)
        except Exception as e:  # noqa: BLE001
            return {"step": "unknown", "url": "", "title": _short_error(e), "buttons": []}

        text = (raw.get("text") or "").lower()
        url = (raw.get("url") or "").lower()
        inputs = raw.get("inputs") or []
        buttons = raw.get("buttons") or []
        hints = " ".join(i.get("hint", "") for i in inputs)

        def title() -> str:
            for line in (raw.get("text") or "").splitlines():
                line = line.strip()
                if len(line) > 3:
                    return line[:120]
            return ""

        def out(step: str) -> dict:
            return {"step": step, "url": raw.get("url") or "", "title": title(), "buttons": buttons}

        if "captcha" in url or "captcha" in text or "введите символы" in text \
                or "введите текст с картинки" in text or "enter the characters" in text:
            return out("captcha")

        if any(i["type"] == "password" for i in inputs):
            return out("password")

        # Код: экран сам про это пишет («Введите код из смс», «код из письма»).
        # Проверяем ДО логина – на обоих экранах поле выглядит одинаково.
        # Отдельно – звонок: Яндекс звонит и просит последние цифры номера.
        code_words = ("код из смс", "код из письма", "код из приложения", "введите код",
                      "одноразовый", "код подтверждения", "цифр номера", "вам звонит",
                      "confirmation code", "one-time", "enter the code",
                      "digits of the calling number", "code from")
        if inputs and (any(w in text for w in code_words)
                       or ("/challenges/" in url and "code" in url)
                       or "otp" in hints or "one-time-code" in hints):
            return out("code")

        # Новый паспорт открывается на телефоне, а нам нужен вход по логину.
        if "введите номер телефона" in text or "enter your phone number" in text or (
                inputs and all(i["type"] == "tel" for i in inputs)):
            return out("phone")

        if inputs and ("введите логин" in text or "логин или email" in text
                       or "enter your login" in text or "username or email" in text
                       or "log in with id" in text
                       or "username" in hints or "логин" in hints or "email" in hints):
            return out("login")

        # Письмо ушло – Яндекс ждёт, пока по ссылке нажмут. Полей тут нет,
        # только «Другой способ входа»: экран отдельный, иначе он попадал в
        # «непонятный» и человек не понимал, чего ждать.
        if not inputs and ("письмо отправлено" in text or "email sent" in text
                           or "нажмёте на кнопку в письме" in text):
            return out("mail-wait")

        # Полей нет вовсе, а кнопка есть – это «Безопасный вход: подтвердите
        # номер телефона» и подобные. Нажать её может кто угодно, секрета там нет.
        if not inputs and any(
            (b or "").strip().lower() in CONFIRM_BUTTON_TEXTS for b in buttons
        ):
            return out("challenge")

        if inputs:
            return out("login")
        if "passport" not in url:
            return out("done")
        return out("unknown")

    def detect_step(self) -> str:
        return self.page_state()["step"]

    def state(self) -> dict:
        """Шаг + снимок экрана одним заходом: картинка всегда от того же момента."""
        st = self.page_state()
        try:
            st["screenshot"] = self.page.screenshot(full_page=True)
        except Exception:  # noqa: BLE001
            st["screenshot"] = None
        return st

    def press_confirm(self) -> dict:
        """Нажать кнопку на экране-подтверждении («Подтвердить», «Далее»)."""
        self._dismiss_cookie_banner()
        if not _click_exact_button(self.page, CONFIRM_BUTTON_TEXTS):
            self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(2500)
        self._skip_post_login_prompts()
        return self.state()

    def auto_login(self, email: str, password: str, max_steps: int = 6,
                   by_mail: bool = False) -> dict:
        """
        Пробует войти сам по сохранённым email и паролю.

        by_mail=True – вход по письму: Click доходит до экрана пароля и жмёт
        там «Отправить письмо для входа», пароль не вводит вовсе. Иначе Яндекс
        после пароля навязывает звонок на телефон, и выбора у человека уже нет.

        Возвращает {'ok': bool, 'step': ..., 'reason': ..., 'screenshot': bytes}.
        ok=False – не провал, а «дальше нужен человек»: код, капча или
        неизвестный экран. Браузер при этом остаётся живым, и пользователь
        продолжает ровно с этого места обычным пошаговым вводом.
        """
        reasons = {
            "code": "Яндекс просит код – его может ввести только человек.",
            "captcha": "Яндекс показывает капчу – введите её вручную по картинке.",
            "unknown": "Не разобрал, что просит страница, – дальше вручную.",
            "phone": "Яндекс просит номер телефона – перейдите на вход по логину.",
        }
        last = "unknown"
        confirms = 0
        backs = 0
        for _ in range(max_steps):
            self._dismiss_cookie_banner()
            step = self.detect_step()
            last = step

            # «Безопасный вход – подтвердите номер телефона» появляется СРАЗУ
            # после логина, до экрана пароля. Раньше Click жал тут «Подтвердить»
            # – и этим сам запускал отправку SMS, хотя мы шли за письмом.
            # Идём назад, к экрану пароля: «Отправить письмо для входа» там.
            if by_mail and step in ("challenge", "code") and backs < 3:
                backs += 1
                self.go_back()
                continue

            # Идём за письмом – «Подтвердить» на телефонном экране НЕ ЖМЁМ
            # ни при каких условиях: это и есть отправка SMS.
            if by_mail and step == "challenge":
                return {"ok": False, "step": "challenge", "screenshot": self.screenshot(),
                        "reason": "Яндекс требует подтверждение по телефону и не пускает "
                                  "на экран пароля. Нажмите «← Назад на прошлый экран» – "
                                  "на экране пароля есть «Отправить письмо для входа». "
                                  "Если назад не выходит, подтвердите по телефону один раз: "
                                  "дальше Click запомнит браузер и спрашивать перестанет."}

            # Экран без полей ввода: «Безопасный вход – подтвердите номер».
            # Тут нечего вводить, нужно просто нажать кнопку – жмём сами,
            # иначе человек упирается в тупик: полей нет, кнопки в UI тоже.
            if step == "challenge" and confirms < 2:
                confirms += 1
                self.press_confirm()
                continue

            # Форма телефона: уходим на вход по логину («Ещё» → «Войти по логину»).
            if step == "phone":
                if not self._switch_to_login_by_password():
                    return {"ok": False, "step": "phone", "screenshot": self.screenshot(),
                            "reason": "Яндекс просит номер телефона, а перейти на вход по логину "
                                      "не вышло. Нажмите «Ещё» → «Войти по логину» на снимке ниже."}
                continue

            if step == "login":
                if not email:
                    return {"ok": False, "step": "login", "screenshot": self.screenshot(),
                            "reason": "В «Настройках» не заполнен email аккаунта Яндекса."}
                self._submit_generic_field(email, LOGIN_NEXT_BUTTON, "login")
                self._skip_post_login_prompts()
                continue

            if step == "password":
                # Вход по письму: пароль не трогаем вообще. После пароля
                # Яндекс сам решает, чем подтвердить вход, и обычно выбирает
                # звонок на телефон – с этого экрана назад уже не выбраться.
                if by_mail:
                    st = self.send_login_email()
                    if st.get("mail_sent"):
                        return {"ok": False, "step": "mail-sent", "screenshot": st.get("screenshot"),
                                "reason": f"Письмо со ссылкой для входа отправлено на почту "
                                          f"аккаунта {email}. Откройте письмо на любом устройстве, "
                                          f"подтвердите вход и нажмите «Обновить экран»."}
                    return {"ok": False, "step": "password", "screenshot": st.get("screenshot"),
                            "reason": "Кнопку «Отправить письмо для входа» найти не удалось – "
                                      "нажмите её на снимке ниже вручную."}
                if not password:
                    return {"ok": False, "step": "password", "screenshot": self.screenshot(),
                            "reason": "В «Настройках» не заполнен пароль – введите его здесь вручную."}
                self.submit_password(password)
                continue

            # «done» само по себе – только вид страницы. Успехом считаем лишь
            # появившуюся куку авторизации, иначе приложение рапортует о входе,
            # которого не было.
            if (step == "done" or self.is_logged_in()) and self.has_auth_cookie():
                return {"ok": True, "step": "done", "screenshot": self.screenshot(),
                        "reason": "Вход выполнен автоматически."}
            if step == "done":
                return {"ok": False, "step": "unknown", "screenshot": self.screenshot(),
                        "reason": "Яндекс показал кабинет, но куки авторизации не выдал – "
                                  "скорее всего осталась непройденная проверка. Продолжите вручную."}

            return {"ok": False, "step": step, "screenshot": self.screenshot(),
                    "reason": reasons.get(step, reasons["unknown"])}

        if self.has_auth_cookie():
            return {"ok": True, "step": "done", "screenshot": self.screenshot(),
                    "reason": "Вход выполнен автоматически."}
        if by_mail:
            return {"ok": False, "step": last, "screenshot": self.screenshot(),
                    "reason": "Яндекс не пустил на экран пароля – он сразу требует "
                              "подтверждение по телефону. Нажмите «← Назад на прошлый "
                              "экран»: на экране пароля есть «Отправить письмо для входа»."}
        return {"ok": False, "step": last, "screenshot": self.screenshot(),
                "reason": "Слишком много шагов – дальше вручную."}

    def has_auth_cookie(self) -> bool:
        """
        Единственная надёжная правда о входе: кука авторизации в самом браузере.
        Вид страницы обманывает – паспорт уводит на форму входа с адресом, где
        встречается слово profile, и «мы в кабинете» отвечало «да» зря.
        """
        try:
            return any(
                (c.get("name") or "").lower() in AUTH_COOKIES
                and "yandex" in (c.get("domain") or "").lower()
                for c in self.context.cookies()
            )
        except Exception:  # noqa: BLE001
            return False

    def is_logged_in(self) -> bool:
        # Кука авторизации появляется сразу после успешного входа, и проверить
        # её можно БЕЗ переходов. Ходить на /profile ради проверки нельзя:
        # переход сбрасывает непройденную проверку кода, паспорт откатывается
        # на первый экран – и код потом печатался в поле ЛОГИНА (у заказчика:
        # «Username can't begin with a number»).
        return self.has_auth_cookie()

    def current_account(self) -> str | None:
        """Какой аккаунт реально залогинен – показываем в UI после входа."""
        try:
            self.page.goto(PROFILE_URL, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1200)
            emails = self.page.evaluate(
                """() => (document.body.textContent || '').match(/[\\w.+-]+@[\\w-]+\\.[\\w.-]+/g) || []"""
            )
            return emails[0].lower() if emails else None
        except Exception:
            return None

    def save_session(self) -> Path:
        path = session_path(self.project_id)
        _save_storage_state(self.context, path)
        return path

    def save_device(self) -> None:
        """
        Запомнить браузер, чтобы следующий вход не выглядел «с нового устройства».

        Куки авторизации СЮДА НЕ КЛАДЁМ. Иначе «Войти заново (сбросить сессию)»
        перестало бы работать: сессию стёрли, а вход всё равно под старым
        аккаунтом – ровно то, ради чего кнопка и нужна.
        """
        try:
            if not self.context:
                return
            data = self.context.storage_state()
            data["cookies"] = [c for c in (data.get("cookies") or [])
                               if (c.get("name") or "").lower() not in AUTH_COOKIES]
            data["origins"] = []
            device_path(self.project_id).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self.save_device()
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        finally:
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._pw = None
            self.browser = None
            self.context = None
            self.page = None
