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
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

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

# Таймауты – 1:1 из DEFAULTS в publish.js
NAVIGATION_TIMEOUT = 45_000
ACTION_TIMEOUT = 15_000
API_WAIT_TIMEOUT = 8.0      # сколько ждём POST-ответ Яндекса после клика «Создать»
UI_READY_TIMEOUT = 15_000   # сколько ждём, пока React отрисует интерфейс
IMAGE_PREVIEW_TIMEOUT = 8_000
TEXT_CONFIRM_TIMEOUT = 10_000

_LOG: Callable[[str, str], None] | None = None


def set_logger(fn: Callable[[str, str], None] | None) -> None:
    """Внешний логгер: fn(level, message). Используется runner.py."""
    global _LOG
    _LOG = fn


def _log(level: str, msg: str) -> None:
    if _LOG:
        try:
            _LOG(level, msg)
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

def extract_company_id(url: str | None) -> str | None:
    m = re.search(r"sprav/(\d+)", url or "")
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


def _launch(pw, engine: str, headless: bool = True):
    args = CHROMIUM_ARGS if engine == "chromium" else []
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

    def __init__(self, project_id: str, headless: bool = True):
        self.project_id = project_id
        self.headless = headless
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def start(self) -> None:
        engine = resolve_engine()
        self._pw = sync_playwright().start()
        self.browser = _launch(self._pw, engine, headless=self.headless)
        state = session_path(self.project_id)
        self.context = self.browser.new_context(
            storage_state=str(state) if state.exists() else None,
            viewport={"width": 1280, "height": 900},
            user_agent=UA,
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

    def save_session(self) -> None:
        try:
            if self.context:
                self.context.storage_state(path=str(session_path(self.project_id)))
        except Exception as e:  # noqa: BLE001
            warn(f"Не удалось сохранить сессию: {e}")

    def restart(self) -> None:
        """Полный перезапуск браузера – аналог детектора зависшего Chrome в publish.js."""
        self.save_session()
        self.close()
        time.sleep(3)
        self.start()

    def close(self) -> None:
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
  const posts = document.querySelectorAll('[class*="Post"], [class*="post-card"], [class*="PostsList"] [class*="card"]');
  for (const el of posts) {
    const text = el.textContent || '';
    if (!text.includes(needle)) continue;
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


def check_post_already_exists(page: Page, post_text: str) -> dict:
    full = (post_text or "").strip()
    if len(full) < 20:
        return {"found": False, "fresh": False, "reason": ""}
    needle = full[: min(80, len(full))]
    try:
        return page.evaluate(_CHECK_EXISTS_JS, needle)
    except Exception:
        return {"found": False, "fresh": False, "reason": ""}


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
        file_input = page.locator('input[type="file"]').first
        if file_input.count() == 0:
            btn = wait_for_text_button(page, r"(прикрепить|добавить\s*(фото|картинк|изображ)|загрузить)", 3000)
            if btn:
                click_at(page, btn)
                page.wait_for_timeout(400)
            file_input = page.locator('input[type="file"]').first
            if file_input.count() == 0:
                return False, "Поле загрузки файла не найдено"

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
  // Сначала кнопки ВНУТРИ формы поста, потом остальной документ: «Создать»
  // где-нибудь в шапке не должна перехватить публикацию (PLAN 1.4).
  document.querySelectorAll('[data-click-target]').forEach(e => e.removeAttribute('data-click-target'));
  const roots = [];
  for (const f of document.querySelectorAll('[class*="PostForm"], [class*="PostAddForm"], [role="dialog"], [class*="odal"]')) {
    const r = f.getBoundingClientRect();
    if (r.width > 150 && r.height > 150) roots.push(f);
  }
  roots.push(document);
  const seen = new Set();
  const allBtns = [];
  for (const root of roots)
    for (const b of root.querySelectorAll('button, [role="button"], [type="submit"]')) {
      if (seen.has(b)) continue;
      seen.add(b);
      allBtns.push(b);
    }
  const candidates = [];
  for (const b of allBtns) {
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
    candidates.push({ el: b, text: low, r });
  }
  const pick = (c) => { c.el.setAttribute('data-click-target', '1');
                        return { x: c.r.x + c.r.width / 2, y: c.r.y + c.r.height / 2,
                                 text: (c.el.textContent || '').trim().slice(0, 60),
                                 w: Math.round(c.r.width), h: Math.round(c.r.height) }; };
  for (const name of ['создать', 'опубликовать', 'отправить', 'publish', 'submit']) {
    for (const c of candidates) if (c.text === name) return pick(c);
  }
  for (const c of candidates) if (/(опубликовать|создать пост|отправить пост)/i.test(c.text)) return pick(c);
  for (const c of candidates) if (c.text === 'создать' || c.text.startsWith('создать ')) return pick(c);
  return null;
}
"""

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
    return bool(re.search(r"sprav|/api/|/posts?\b", path))


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

    Возвращает dict:
      status: 'ok' | 'no-image' | 'unknown' | 'failed'
      reason: человеческое объяснение
      steps:  {navigate, postsSection, addButton, text, image, publish}
      durationMs, cityName, companyUrl, imageError
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

    # ─── 2. Кнопка «Добавить пост» ───
    add_btn = wait_for_text_button(
        page,
        r"(добавить|создать|новый|написать|опубликовать).*(пост|публикац|запис)|^(добавить|создать|опубликовать)$",
        6000,
    )
    if not add_btn:
        add_btn = page.evaluate(
            """() => {
                for (const b of document.querySelectorAll('button, [role="button"]')) {
                    const meta = ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('title') || '')).toLowerCase();
                    if (/добавить|создать|новый|опубликовать/.test(meta) && /пост|публикац/.test(meta)) {
                        const r = b.getBoundingClientRect();
                        if (r.width > 20 && r.height > 20 && !b.disabled)
                            return { x: r.x + r.width / 2, y: r.y + r.height / 2, text: meta.slice(0, 40) };
                    }
                }
                return null;
            }"""
        )
    if not add_btn:
        add_btn = page.evaluate(
            """() => {
                for (const b of document.querySelectorAll('button, [role="button"]')) {
                    const t = (b.textContent || '').trim();
                    if (t === '+' || t === '＋') {
                        const r = b.getBoundingClientRect();
                        if (r.width > 20 && r.height > 20 && !b.disabled)
                            return { x: r.x + r.width / 2, y: r.y + r.height / 2, text: '+' };
                    }
                }
                return null;
            }"""
        )

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
        result["steps"]["addButton"] = "missing"
        error("  ❌ Кнопка «Добавить пост» не найдена")
        return finish("failed", "Кнопка «Добавить пост» не найдена")

    click_at(page, add_btn)
    page.wait_for_timeout(700)
    result["steps"]["addButton"] = "ok"

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
    # С опросом до 12 сек: пока Яндекс крутит загрузку картинки, «Создать»
    # отключена и однократный поиск её «не находил» – хотя через пару секунд
    # она появлялась (это видно по тому, что ретрай кнопку находил).
    pub = None
    deadline_btn = time.time() + 12
    while True:
        pub = page.evaluate(_FIND_PUBLISH_BTN_JS)
        if pub or time.time() >= deadline_btn:
            break
        page.wait_for_timeout(700)
    if not pub:
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
    info(f"  🎯 Кнопка публикации: «{pub['text']}» {pub['w']}×{pub['h']}")

    # ─── 6. ВЕРИФИКАЦИЯ ЧЕРЕЗ ПЕРЕХВАТ API-ОТВЕТА ───
    # Слушателя ставим ДО клика. Ответ 2xx на POST к API постов = пост точно создан.
    api_result: dict[str, Any] = {}

    def on_response(response):  # pragma: no cover - сетевой колбэк
        try:
            if not _is_publish_api_response(response.url, response.request.method):
                return
            api_result["url"] = response.url
            api_result["status"] = response.status
            api_result["method"] = response.request.method
        except Exception:
            pass

    page.on("response", on_response)
    page.wait_for_timeout(200)

    click_error = None
    try:
        # locator.click сам прокручивает к кнопке и ждёт кликабельности – как
        # elementHandle.click() в оригинале. Координаты остаются запасным путём:
        # кнопка ниже сгиба по координатам не нажимается вовсе (PLAN 1.1).
        try:
            page.locator('[data-click-target="1"]').first.click(timeout=5_000)
        except Exception:
            click_at(page, pub)
        info(f"  🔘 Клик «{pub['text']}» сделан")
        result["steps"]["publish"] = "clicked"
    except Exception as e:  # noqa: BLE001
        click_error = str(e)

    deadline = time.time() + API_WAIT_TIMEOUT
    while time.time() < deadline and not api_result:
        page.wait_for_timeout(150)
    try:
        page.remove_listener("response", on_response)
    except Exception:
        pass

    _cleanup(temp_files)

    # ─── 7. Разбор результата ───
    if api_result:
        info(f"  📡 API Яндекса: {api_result['method']} {api_result['url'][:100]} → HTTP {api_result['status']}")
        if 200 <= api_result["status"] < 300:
            result["steps"]["publish"] = "api-confirmed"
            status = "no-image" if result["steps"]["image"] == "failed" else "ok"
            reason = ("Пост опубликован (API подтвердил)" if status == "ok"
                      else "Пост опубликован без картинки (API подтвердил)")
            page.wait_for_timeout(800)
            out = finish(status, reason)
            info(f"  ✅ {label} – {reason} ({out['durationMs'] / 1000:.1f} сек)")
            return out
        if api_result["status"] >= 400:
            result["steps"]["publish"] = "api-rejected"
            error(f"  ❌ Яндекс отклонил публикацию: HTTP {api_result['status']}")
            return finish("failed", f"Яндекс отклонил публикацию: HTTP {api_result['status']}")

    if click_error and not api_result:
        # Клик упал и API-ответа нет – чаще всего это УСПЕШНАЯ публикация с навигацией.
        # Ретрай запрещён: лучше «проверьте вручную», чем дубль.
        result["steps"]["publish"] = "click-error-no-api"
        warn("  ⚠️ Клик дал ошибку, API-ответа не было – возможно опубликовано")
        return finish(
            "unknown",
            f"Клик дал ошибку ({click_error[:100]}), но API-ответа не было. "
            "Возможно опубликовано – проверьте вручную.",
        )

    if click_error:
        result["steps"]["publish"] = "click-failed"
        error(f"  ❌ Ошибка клика «Создать»: {click_error}")
        return finish("failed", f"Ошибка клика «Создать»: {click_error}")

    # API-ответа не поймали – смотрим DOM: форма закрылась и пост появился в ленте?
    page.wait_for_timeout(2000)
    try:
        fb = page.evaluate(_DOM_FALLBACK_JS, text.strip())
    except Exception:
        fb = {"formOpen": False, "postFound": False}

    if fb.get("postFound") and not fb.get("formOpen"):
        result["steps"]["publish"] = "dom-confirmed"
        status = "no-image" if result["steps"]["image"] == "failed" else "ok"
        reason = "Пост опубликован (по DOM)" if status == "ok" else "Пост опубликован без картинки (по DOM)"
        out = finish(status, reason)
        info(f"  ✅ {label} – {reason} ({out['durationMs'] / 1000:.1f} сек)")
        return out

    result["steps"]["publish"] = "unknown"
    warn("  ⚠️ Публикация не подтверждена ни API, ни DOM")
    return finish("unknown", "Публикация не подтверждена ни API, ни DOM. Проверьте вручную.")


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


def upload_product_photos(page: Page, task: dict, photo_urls: list[str], temp_dir: Path) -> dict:
    """
    Загружает фото в раздел «Фото и видео → Товары». Изолирована: любая ошибка
    здесь НЕ влияет на статус публикации поста.
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

    local: list[str] = []
    for url in photo_urls:
        try:
            local.append(str(url) if Path(str(url)).exists() else download_image(str(url), temp_dir))
        except Exception as e:  # noqa: BLE001
            result["failed"] += 1
            result["errors"].append(f"Не скачать {str(url)[:50]}: {e}")
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
#  АКТУАЛИЗАЦИЯ (порт actualize.js)
# ════════════════════════════════════════════════════════════════════

_ACTUALIZE_BTN_JS = r"""
() => {
  for (const b of document.querySelectorAll('button, [role="button"]')) {
    const t = (b.textContent || '').trim().toLowerCase();
    if (!t || t.length > 50) continue;
    if (/^данные\s+актуальны$/i.test(t) || /^актуализ[а-я]*\s+данные$/i.test(t)) {
      const r = b.getBoundingClientRect();
      if (r.width >= 80 && r.height >= 20 && !b.disabled)
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    }
  }
  return null;
}
"""

_ACTUALIZE_TOAST_JS = r"""
() => {
  const cands = document.querySelectorAll(
    '[class*="oast"], [class*="otification"], [class*="otice"], [role="alert"], [role="status"]');
  for (const el of cands) {
    const text = (el.textContent || '').trim();
    if (!text || text.length < 5 || text.length > 100) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 10) continue;
    if (/(данные\s+актуализирован|актуализирован|сохранено|обновлено)/i.test(text)) return true;
  }
  return false;
}
"""


def actualize_city(page: Page, task: dict, idx: int = 0, total: int = 1) -> dict:
    """
    Статусы (как в actualize.js):
      'actualized' – кнопку нажали
      'not-needed' – кнопки нет, актуализация не требуется (это НЕ ошибка)
      'failed'     – не открылась страница / ошибка клика
    """
    started = time.time()
    label = f"[{idx + 1}/{total}] {task.get('cityName', '?')}"
    result = {
        "cityName": task.get("cityName"),
        "companyUrl": task.get("companyUrl"),
        "status": "failed",
        "reason": "",
        "durationMs": 0,
    }

    def finish(status: str, reason: str) -> dict:
        result["status"] = status
        result["reason"] = reason
        result["durationMs"] = int((time.time() - started) * 1000)
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
        return finish("failed", f"Страница не найдена (404): {edit_url}")

    coords = None
    deadline = time.time() + 4
    while time.time() < deadline:
        coords = page.evaluate(_ACTUALIZE_BTN_JS)
        if coords:
            break
        page.wait_for_timeout(400)

    if not coords:
        out = finish("not-needed", "Кнопка «Данные актуальны» не найдена – актуализация не требуется")
        info(f"  ✓ {label}: {out['reason']} ({out['durationMs'] / 1000:.1f} сек)")
        return out

    try:
        click_at(page, coords)
        info("  🔘 Клик «Данные актуальны»")
    except Exception as e:  # noqa: BLE001
        return finish("failed", f"Ошибка клика «Данные актуальны»: {e}")

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
    coords = page.evaluate(
        """(texts) => {
            for (const btn of document.querySelectorAll('button, [role="button"]')) {
                const t = (btn.textContent || '').trim().toLowerCase();
                if (texts.includes(t)) {
                    const r = btn.getBoundingClientRect();
                    if (r.width > 30 && r.height > 15) return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
                }
            }
            return null;
        }""",
        texts,
    )
    if coords:
        page.mouse.click(coords["x"], coords["y"])
        return True
    return False


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

    def start(self) -> bytes:
        engine = resolve_engine()
        try:
            self._pw = sync_playwright().start()
            self.browser = _launch(self._pw, engine, headless=self.headless)
            self.context = self.browser.new_context(viewport={"width": 1000, "height": 700}, user_agent=UA)
            self.page = self.context.new_page()
            self.page.goto("about:blank", timeout=10_000)
            self.page.goto(PASSPORT_URL, wait_until="domcontentloaded")
            self.page.wait_for_timeout(1500)
            self._dismiss_cookie_banner()
            self._switch_to_login_by_password()
            return self.page.screenshot(full_page=True)
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

    def _switch_to_login_by_password(self) -> None:
        if self.page.locator(OTHER_METHOD_BUTTON).count() > 0:
            self.page.click(OTHER_METHOD_BUTTON)
            self.page.wait_for_timeout(600)
        if self.page.locator(SWITCH_TO_LOGIN_OPTION).count() > 0:
            self.page.click(SWITCH_TO_LOGIN_OPTION)
            self.page.wait_for_timeout(800)

    def _skip_post_login_prompts(self) -> None:
        for sel in POST_LOGIN_SKIP_BUTTONS:
            if self.page.locator(sel).count() > 0:
                self.page.click(sel)
                self.page.wait_for_timeout(800)

    def _submit_generic_field(self, value: str, next_button_selector: str) -> None:
        field = self.page.locator(GENERIC_TEXT_FIELD).first
        field.click()
        field.fill("")
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

    def click_next(self) -> bytes:
        self._dismiss_cookie_banner()
        if self.page.locator(LOGIN_NEXT_BUTTON).count() > 0:
            self.page.click(LOGIN_NEXT_BUTTON)
            self.page.wait_for_timeout(1200)
        if not _click_exact_button(self.page, ["next", "continue", "войти", "продолжить", "далее"]):
            self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(1500)
        return self.page.screenshot(full_page=True)

    def submit_phone(self, phone: str) -> bytes:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if digits.startswith(("7", "8")):
            digits = digits[1:]
        self.page.click('input[type="tel"]')
        self.page.keyboard.type(digits, delay=40)
        self.page.wait_for_timeout(500)
        if not _click_exact_button(self.page, ["log in", "войти"]):
            self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(2500)
        return self.page.screenshot(full_page=True)

    def submit_login(self, login_value: str) -> bytes:
        self._submit_generic_field(login_value, LOGIN_NEXT_BUTTON)
        return self.page.screenshot(full_page=True)

    def submit_password(self, password: str) -> bytes:
        self._submit_generic_field(password, PASSWORD_NEXT_BUTTON)
        self._skip_post_login_prompts()
        return self.page.screenshot(full_page=True)

    def submit_code(self, code: str) -> bytes:
        # Пока человек ждал код, паспорт мог откатиться на первый экран – тогда
        # код улетел бы в поле логина. Проверяем, что на экране действительно код.
        if self.detect_step() == "login":
            raise RuntimeError(
                "Яндекс вернулся на экран логина – коду сейчас некуда. Введите логин "
                "(или нажмите «Продолжить»), дождитесь НОВОГО кода и введите его.")
        self._submit_generic_field(code, EMAIL_CODE_NEXT_BUTTON)
        self._skip_post_login_prompts()
        return self.page.screenshot(full_page=True)

    def screenshot(self) -> bytes:
        return self.page.screenshot(full_page=True)

    # ── Автоматический вход ──────────────────────────────────────────
    # Логин и пароль у нас уже есть в «Настройках», поэтому нет смысла заставлять
    # человека вводить их руками по картинке. Читаем, ЧТО просит страница, и
    # заполняем сами. Останавливаемся только там, где человек действительно нужен:
    # код из SMS/почты, капча, подтверждение в приложении.

    def detect_step(self) -> str:
        """
        Что сейчас на экране Яндекса:
          'login'    – просит логин или e-mail
          'password' – просит пароль
          'code'     – просит код (SMS / почта / приложение)
          'captcha'  – капча
          'done'     – похоже, уже вошли
          'unknown'  – не разобрали, нужен человек
        """
        try:
            if self.page.locator(EMAIL_CODE_NEXT_BUTTON).count() > 0:
                return "code"
            if self.page.locator('input[type="password"]').count() > 0 or \
                    self.page.locator(PASSWORD_NEXT_BUTTON).count() > 0:
                return "password"

            page_text = (self.page.inner_text("body") or "").lower()
            if self.page.locator('img[src*="captcha"], [class*="captcha" i], [data-testid*="captcha"]').count() > 0 \
                    or "captcha" in page_text or "введите символы" in page_text:
                return "captcha"
            if re.search(r"\bкод\b|подтвержд|confirmation code|one-time", page_text) and \
                    self.page.locator(GENERIC_TEXT_FIELD).count() > 0:
                return "code"
            if self.page.locator(LOGIN_NEXT_BUTTON).count() > 0 or \
                    self.page.locator(GENERIC_TEXT_FIELD).count() > 0:
                return "login"
            if "passport" not in self.page.url or "profile" in self.page.url:
                return "done"
        except Exception:
            return "unknown"
        return "unknown"

    def auto_login(self, email: str, password: str, max_steps: int = 6) -> dict:
        """
        Пробует войти сам по сохранённым email и паролю.

        Возвращает {'ok': bool, 'step': ..., 'reason': ..., 'screenshot': bytes}.
        ok=False – не провал, а «дальше нужен человек»: код, капча или
        неизвестный экран. Браузер при этом остаётся живым, и пользователь
        продолжает ровно с этого места обычным пошаговым вводом.
        """
        reasons = {
            "code": "Яндекс просит код – его может ввести только человек.",
            "captcha": "Яндекс показывает капчу – введите её вручную по картинке.",
            "unknown": "Не разобрал, что просит страница, – дальше вручную.",
        }
        last = "unknown"
        for _ in range(max_steps):
            self._dismiss_cookie_banner()
            step = self.detect_step()
            last = step

            if step == "login":
                if not email:
                    return {"ok": False, "step": "login", "screenshot": self.screenshot(),
                            "reason": "В «Настройках» не заполнен email аккаунта Яндекса."}
                self._submit_generic_field(email, LOGIN_NEXT_BUTTON)
                self._skip_post_login_prompts()
                continue

            if step == "password":
                if not password:
                    return {"ok": False, "step": "password", "screenshot": self.screenshot(),
                            "reason": "В «Настройках» не заполнен пароль – введите его здесь вручную."}
                self._submit_generic_field(password, PASSWORD_NEXT_BUTTON)
                self._skip_post_login_prompts()
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
        self.context.storage_state(path=str(path))
        return path

    def close(self) -> None:
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
