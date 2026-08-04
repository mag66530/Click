"""
streamlit_app.py – Click на Streamlit. Интерфейс повторяет оригинальное
приложение (app.js + _ui.js): те же 6 разделов, тот же дизайн, та же логика
черновик → очередь → задачи → прогон → отчёт.

Публикацией занимается runner.py (фоновый поток + защита от дублей),
браузером – yb_playwright.py (порт publish.js/actualize.js на Playwright).
Здесь только интерфейс и работа с конфигом проекта.

Запуск:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

import kp_sheet
import paths
import projects_data as pdata
import repo_store
import runner
import ui_theme as T
import yb_playwright as yb
from playwright_worker import PlaywrightWorker

# Стиль и разметка обязаны быть из одной сборки. Если Streamlit перезапустил
# скрипт, но оставил в памяти прежний ui_theme (так бывает в облаке при
# обновлении «на лету»), интерфейс собирается из новой разметки и старого CSS –
# и кнопки либо смещаются, либо становятся невидимыми. Проверяем метку и, если
# она не совпала, перезагружаем модуль сами.
UI_BUILD = "2026-08-04-ru-passport"
if getattr(T, "BUILD", "") != UI_BUILD:
    import importlib

    T = importlib.reload(T)

ROOT = Path(__file__).parent
USERS_DATA = paths.data_root()

st.set_page_config(page_title="Click – публикация постов", page_icon="📮", layout="wide")

SALT = "click-salt-v1-2026"
SECTIONS = ["🚀 Запуск", "📤 Публикация", "🔄 Актуализация", "🏙 Города", "📊 Отчёт", "⚙️ Настройки"]


def _hash(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha512", password.encode(), SALT.encode(), 100_000, dklen=64).hex()


def _url_token(pid: str) -> str:
    """
    Подпись проекта для адресной строки. session_state живёт до первого F5,
    поэтому вход держим в query-параметрах: ?p=SMU&k=<подпись>. Подпись
    выводится из хэша пароля – сам пароль в адрес не попадает, а поменяли
    пароль → старые ссылки перестают пускать.
    """
    return hashlib.sha256(f"{pid}:{PROJECTS[pid]['passwordHash']}:{SALT}".encode()).hexdigest()[:24]


def _project_from_url() -> str | None:
    try:
        pid = st.query_params.get("p")
        key = st.query_params.get("k")
    except Exception:  # noqa: BLE001
        return None
    if pid in PROJECTS and key == _url_token(pid):
        return pid
    return None


PROJECTS: dict[str, dict] = {
    "SMU": {"id": "SMU", "name": "СМУ", "fullName": "Стальметурал", "color": "#3b82f6", "icon": "🏗",
            "yandexEmail": "stalmetural19@yandex.ru", "passwordHash": _hash("1501"),
            "presetCities": pdata.SMU_CITIES, "endings": pdata.SMU_ENDINGS},
    "IMP": {"id": "IMP", "name": "ИМП", "fullName": "Инметпром", "color": "#10b981", "icon": "🔩",
            "yandexEmail": "inmetprom77@yandex.ru", "passwordHash": _hash("2205"),
            "presetCities": pdata.IMP_CITIES, "endings": pdata.IMP_ENDINGS},
    "MPE": {"id": "MPE", "name": "МПЭ", "fullName": "МетПромЭнерго", "color": "#f59e0b", "icon": "⚡",
            "yandexEmail": "mepen88@yandex.ru", "passwordHash": _hash("1101"),
            "presetCities": pdata.MPE_CITIES, "endings": pdata.MPE_ENDINGS},
    # МПИ – первый проект без вшитого списка городов: они приходят из КП.
    # Пароль от Яндекса не хранится в коде – он вводится в «Настройках».
    "MPI": {"id": "MPI", "name": "МПИ", "fullName": "МетПромИнтекс", "color": "#8b5cf6", "icon": "🛠",
            # Именно @yandex.ru, а не @yandex.com: у всех остальных проектов .ru,
            # и только у МПИ был .com – с него Яндекс уводит в международный
            # паспорт с другими правилами проверки (звонок вместо письма).
            "yandexEmail": "metpromintex@yandex.ru", "passwordHash": _hash("1717"),
            "presetCities": [], "endings": pdata.MPI_ENDINGS},
}

def flag(name: str, height: int = 14) -> str:
    """SVG-флажок. Эмодзи не годятся: на Windows вместо флага видно «RU», «KZ»."""
    return T.flag_svg(name, height)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 город, 2 города, 5 городов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def cities_word(n: int) -> str:
    return plural(n, "город", "города", "городов")


# ════════════════════════════════════════════════════════════════════
#  Конфиг проекта (формат 1:1 с projects-config.json из app.js)
# ════════════════════════════════════════════════════════════════════

def project_base(project_id: str) -> Path:
    d = USERS_DATA / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_dirs(project_id: str) -> None:
    base = project_base(project_id)
    for sub in ("tasks", "tasks-actualize", "reports", "reports-actualize", "logs", "uploads", "session", "temp"):
        (base / sub).mkdir(parents=True, exist_ok=True)


def config_path(project_id: str) -> Path:
    return project_base(project_id) / "projects-config.json"


def safe_filename(s: str) -> str:
    return re.sub(r"[^a-zA-Zа-яА-Я0-9._-]", "_", str(s))[:80]


def _slug(s: str) -> str:
    """
    Идентификатор из названия. ВАЖНО: кириллицу оставляем – если её вырезать,
    у всех русских названий получится один и тот же slug, а значит одинаковые id
    у разных стран/городов (и Streamlit падает на дублях ключей виджетов).
    """
    return re.sub(r"[^0-9a-zа-яё]+", "-", str(s).lower()).strip("-") or "x"


def _default_subproject(project_id: str) -> dict:
    """Первичная инициализация: пресет городов проекта, сгруппированный по странам."""
    preset = PROJECTS[project_id]["presetCities"] or []
    by_country: dict[str, list] = {}
    for city in preset:
        by_country.setdefault(city["country"], []).append(city)
    countries = []
    for n, (cname, cities) in enumerate(by_country.items()):
        # Индекс в id – страховка от совпадения slug'ов у разных названий.
        countries.append({
            "id": f"c-{project_id}-{n}-{_slug(cname)}",
            "name": cname,
            "cities": [
                {"id": f"ct-{project_id}-{n}-{i}-{_slug(c['name'])}", "name": c["name"], "url": c["url"]}
                for i, c in enumerate(cities)
            ],
        })
    return {
        "id": f"p-{project_id.lower()}-default",
        "name": PROJECTS[project_id]["fullName"],
        "email": PROJECTS[project_id]["yandexEmail"],
        "password": "",
        "countries": countries,
    }


# Что из конфига храним снаружи (в репозитории). Пароль от Яндекса – НИКОГДА:
# он остаётся только в этом контейнере.
_KEPT = ("countries", "email", "kpSheetUrl", "kpSyncedAt")


def load_raw_config(project_id: str) -> dict:
    fp = config_path(project_id)
    if fp.exists():
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            if raw.get("projects"):
                return _merge_kept(project_id, raw)
        except (json.JSONDecodeError, OSError):
            pass
    sub = _default_subproject(project_id)
    return _merge_kept(project_id, {"projects": [sub], "activeProjectId": sub["id"], "settings": {}})


def _merge_kept(project_id: str, raw: dict) -> dict:
    """
    Города и настройки проекта поднимаем из репозитория, если локально пусто.
    В облаке файловая система временная: без этого после каждого перезапуска
    список городов пришлось бы набирать заново.
    """
    saved = repo_store.load(f"project-{project_id}")
    if not saved:
        return raw
    sub = next((x for x in raw["projects"] if x["id"] == raw.get("activeProjectId")), raw["projects"][0])
    for key in _KEPT:
        if key in saved and not sub.get(key):
            sub[key] = saved[key]
    return raw


def get_config(project_id: str) -> dict:
    """Активный под-проект. Кэшируется в session_state, чтобы правки не терялись между рерайтами."""
    key = f"_cfg_{project_id}"
    if key not in st.session_state:
        st.session_state[key] = load_raw_config(project_id)
    raw = st.session_state[key]
    active = raw.get("activeProjectId")
    for sub in raw["projects"]:
        if sub["id"] == active:
            return sub
    return raw["projects"][0]


# Домены-двойники Яндекса: ящик один, а паспорт разный. Международный
# (.com и прочие) подтверждает вход звонком на телефон, российский – письмом.
_YANDEX_ALIASES = ("yandex.com", "yandex.com.tr", "yandex.by", "yandex.kz",
                   "yandex.ua", "ya.ru")


def _ru_domain(email: str) -> str:
    """Тот же ящик, но в российском написании: metpromintex@yandex.ru."""
    email = (email or "").strip()
    name, _, domain = email.partition("@")
    if name and domain.lower() in _YANDEX_ALIASES:
        return f"{name}@yandex.ru"
    return email


def local_time(iso: str | None) -> str:
    """
    Время отчёта – по часам человека. В файлы оно пишется в UTC, и в шапке
    отчёта стояло «07:33», когда в логе рядом было «12:33»: выглядело так,
    будто показан какой-то посторонний, старый отчёт.
    """
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return str(iso)[:19].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%d.%m.%Y, %H:%M:%S")


def can_show_browser() -> bool:
    """Окно браузера возможно только на своём компьютере: в облаке экрана нет."""
    return not yb.in_cloud()


# Галочку храним НЕ в ключе виджета. Streamlit выбрасывает состояние виджетов,
# которых не было на текущем экране: стоило уйти с «Настроек» на «Формирование
# поста» – и галочка молча гасла. Публикация после этого шла скрыто, отсюда и
# «галочка сбрасывается» и «прогресса всё равно не видно».
HEADED_KEY = "headed_browser"


def show_browser_window() -> bool:
    if not can_show_browser():
        return False
    if str(os.environ.get("CLICK_HEADED", "")).strip().lower() in ("1", "true", "yes", "да"):
        return True
    return bool(st.session_state.get(HEADED_KEY))


def _remember_headed() -> None:
    st.session_state[HEADED_KEY] = bool(st.session_state.get("show-browser"))


def get_settings(project_id: str) -> dict:
    """
    Параметры прогона НЕ настраиваются: зашиты безопасные значения.
    Любая из этих ручек в чужих руках – способ получить дубли или публикацию
    не с того аккаунта, а выигрыша от них нет.
    """
    return {
        # В облаке экрана нет – всегда скрыто. Локально можно смотреть, как идёт
        # публикация: галочка в «Настройках» или переменная CLICK_HEADED=1.
        "headless": not show_browser_window(),
        "delayBetweenPosts": 3,         # меньше – ловим антифлуд Яндекса
        "strictAccountCheck": True,     # чужой аккаунт – стоп, а не «опубликуем куда-нибудь»
        "retryUnknown": False,          # повтор после неподтверждённого клика = дубль
        "dedupWindowHours": runner.DEDUP_WINDOW_HOURS,
    }


def save_config(project_id: str) -> None:
    raw = st.session_state.get(f"_cfg_{project_id}")
    if not raw:
        return
    fp = config_path(project_id)
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(fp)
    _save_kept(project_id, raw)


def _save_kept(project_id: str, raw: dict) -> None:
    """Города и настройки – наружу, чтобы пережили перезапуск. Пароль не берём."""
    sub = next((x for x in raw["projects"] if x["id"] == raw.get("activeProjectId")), raw["projects"][0])
    data = {k: sub.get(k) for k in _KEPT if sub.get(k)}
    if not data.get("countries"):
        return
    try:
        repo_store.save(f"project-{project_id}", data, f"Click: города и настройки {project_id}")
    except Exception as e:  # noqa: BLE001
        st.session_state["_store_error"] = str(e)


def country_by_id(config: dict, cid: str) -> dict | None:
    return next((c for c in config["countries"] if c["id"] == cid), None)


# ════════════════════════════════════════════════════════════════════
#  Окончания постов: значения по умолчанию из кода + правки заказчика
# ════════════════════════════════════════════════════════════════════

def endings_defaults(project_id: str) -> dict:
    return copy.deepcopy(PROJECTS[project_id]["endings"] or {})


# Подмена окончаний на время предпросмотра в редакторе: предпросмотр обязан
# собираться тем же build_final_text, что и публикация, иначе он врёт.
_ENDINGS_OVERRIDE: dict[str, dict] = {}


def project_endings(project_id: str) -> dict:
    """
    Окончания проекта: заготовка из кода, поверх – то, что вписали в приложении.
    Правки лежат снаружи (репозиторий), поэтому переживают перезапуск в облаке.
    """
    if project_id in _ENDINGS_OVERRIDE:
        return _ENDINGS_OVERRIDE[project_id]
    base = endings_defaults(project_id)
    saved = repo_store.load(f"endings-{project_id}")
    if not saved:
        return base
    base.setdefault("contacts", {})
    base.setdefault("templates", {})
    for country, c in (saved.get("contacts") or {}).items():
        base["contacts"][country] = {**(base["contacts"].get(country) or {}), **c}
    base["templates"].update(saved.get("templates") or {})
    return base


def save_project_endings(project_id: str, data: dict) -> str:
    return repo_store.save(f"endings-{project_id}", data,
                           f"Click: окончания постов {project_id}")


# ════════════════════════════════════════════════════════════════════
#  Текст поста – построчный порт buildFinalText из _ui.js
# ════════════════════════════════════════════════════════════════════

def build_final_text(project_id: str, country_name: str, post_type: str, body: str) -> str:
    lines: list[str] = []
    if (body or "").strip():
        lines.append(body.strip())

    endings = project_endings(project_id)

    # Окончание одно на все проекты: шаблон по типу поста + контакты по стране.
    if endings and endings.get("__dynamic"):
        by_country = endings.get("contacts") or {}
        contacts = by_country.get(country_name)
        if contacts is None and endings.get("fallback"):
            contacts = by_country.get(endings["fallback"])
        template = (endings.get("templates") or {}).get(post_type)
        if not template or not contacts:
            return "\n".join(lines)

        phone = contacts.get("phone")

        def subst(ln: str) -> str | None:
            stripped = ln.strip()
            # Строка целиком из телефонного плейсхолдера, а телефона нет → строку убираем
            if stripped in ("{phoneLine}", "{phoneSpecialLine}", "{phoneSpecialLineMpe}") and not phone:
                return None
            return (ln.replace("{site}", contacts.get("site") or "")
                      .replace("{email}", contacts.get("email") or "")
                      .replace("{phoneLine}", f"📞 {phone}" if phone else "")
                      .replace("{phoneSpecialLine}", f"☎️ {phone}" if phone else "")
                      .replace("{phoneSpecialLineMpe}", f"📱 Телефон: {phone}" if phone else "")
                      .replace("{phone}", phone or ""))

        substituted = [x for x in (subst(ln) for ln in template.split("\n")) if x is not None]
        collapsed: list[str] = []
        for ln in substituted:                       # схлопываем подряд идущие пустые строки
            if not ln.strip() and collapsed and not collapsed[-1].strip():
                continue
            collapsed.append(ln)
        lines.append("")
        lines.append("\n".join(collapsed))
        return "\n".join(lines)

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
#  Очередь → файлы задач (формат читает runner.py, он же формат publish.js)
# ════════════════════════════════════════════════════════════════════

def save_queue_to_tasks(project_id: str, config: dict, queue: list[dict]) -> int:
    tasks_dir = project_base(project_id) / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    saved = 0
    for idx, item in enumerate(queue):
        country = country_by_id(config, item["countryId"])
        if not country:
            continue
        city_tasks = []
        for cid in item["cityIds"]:
            city = next((c for c in country["cities"] if c["id"] == cid), None)
            if not city:
                continue
            city_tasks.append({
                "cityName": city["name"],
                "companyUrl": city["url"],
                "companyId": yb.extract_company_id(city["url"]),
                "postText": item["text"],
                "imageUrl": item.get("imageUrl") or None,
                "imagePath": item.get("imagePath") or None,
                "extraImages": item.get("extraImages") or None,
                "productPhotos": item.get("productPhotos") or None,
            })
        if not city_tasks:
            continue
        payload = {
            "credentials": {"email": config.get("email", ""), "password": config.get("password", "")},
            "projectName": PROJECTS[project_id]["name"],
            "country": country["name"],
            "postType": item.get("postType"),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "delayBetweenPosts": 3000,
            "headlessMode": True,
            "tasks": city_tasks,
        }
        name = f"{idx + 1:02d}-{safe_filename(country['name'])}-{ts}.json"
        (tasks_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        saved += 1
    return saved


def save_actualize_tasks(project_id: str, config: dict, selection: dict[str, list[str]]) -> int:
    folder = project_base(project_id) / "tasks-actualize"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.json"):          # чистим прошлые – иначе прогон подхватит лишнее
        old.unlink(missing_ok=True)
    ts = int(time.time() * 1000)
    total = 0
    for idx, (country_id, city_ids) in enumerate(selection.items()):
        country = country_by_id(config, country_id)
        if not country or not city_ids:
            continue
        city_tasks = [
            {"cityName": c["name"], "companyUrl": c["url"], "companyId": yb.extract_company_id(c["url"])}
            for c in country["cities"] if c["id"] in city_ids
        ]
        if not city_tasks:
            continue
        (folder / f"{idx + 1:02d}-{safe_filename(country['name'])}-{ts}.json").write_text(
            json.dumps({"country": country["name"], "projectName": PROJECTS[project_id]["name"],
                        "generatedAt": datetime.now(timezone.utc).isoformat(), "tasks": city_tasks},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        total += len(city_tasks)
    return total


def clear_tasks(project_id: str) -> None:
    for fp in (project_base(project_id) / "tasks").glob("*.json"):
        fp.unlink(missing_ok=True)


# ════════════════════════════════════════════════════════════════════
#  Общие элементы интерфейса
# ════════════════════════════════════════════════════════════════════

def get_worker() -> PlaywrightWorker:
    """
    Один постоянный поток для Playwright: sync API нельзя дёргать из разных
    потоков. Если поток по какой-то причине умер, заводим новый – иначе любой
    вызов повиснет навсегда в ожидании ответа от несуществующего потока.
    """
    worker = st.session_state.get("pw_worker")
    if worker is None or not worker.alive():
        worker = PlaywrightWorker()
        st.session_state.pw_worker = worker
    return worker


def theme() -> str:
    return st.session_state.get("theme", "dark")


def goto_section(name: str) -> None:
    """
    Перейти в раздел из кода. Ключ уже отрисованного виджета Streamlit менять
    запрещает, поэтому переключаем «поколение»: радио получает новый ключ и
    создаётся заново, взяв нужный раздел из index.
    """
    st.session_state["section_name"] = name
    st.session_state["nav-gen"] = st.session_state.get("nav-gen", 0) + 1


def _toggle_theme() -> None:
    st.session_state["theme"] = "light" if theme() == "dark" else "dark"


@st.cache_data(show_spinner=False)
def _css(theme_name: str, build: str) -> str:
    """
    31 КБ строки незачем собирать заново на каждой перерисовке.

    ВАЖНО: в ключе кэша обязателен build. Кэш Streamlit живёт в процессе и
    переживает и перерисовки, и перезагрузку модуля – без метки сборки после
    обновления приложения продолжал бы отдаваться СТАРЫЙ CSS, и любая правка
    оформления просто не доезжала бы до экрана.
    """
    return T.css(theme_name)


# Страховка живёт здесь, в файле с самой разметкой, и потому не может оказаться
# «старой» относительно неё. Она отменяет приёмы прежней вёрстки, когда плитка
# была картинкой, а кнопка – невидимым слоем поверх: именно из-за него клики
# уходили мимо, а при новой разметке плитки вовсе пропадали.
_TILE_SAFETY_CSS = """
<style>
[class*="st-key-tile-"] { position: static !important; }
[class*="st-key-tile-"] .stButton { position: static !important; inset: auto !important; }
[class*="st-key-tile-"] .stButton > button {
  opacity: 1 !important; height: auto !important; visibility: visible !important;
}
</style>
"""


def inject_css() -> None:
    st.markdown(_css(theme(), UI_BUILD) + _TILE_SAFETY_CSS, unsafe_allow_html=True)
    if getattr(T, "BUILD", "") != UI_BUILD:
        st.error("Интерфейс загружен частично (стиль от прежней сборки). "
                 "Нажмите «Reboot app» в меню Streamlit – это лечится перезапуском.")


def html(markup: str | None) -> None:
    """Пустое не рисуем: иначе Streamlit заводит лишний элемент страницы, а он
    тянет за собой отступ 16px – так над шапкой и набегала пустая полоса."""
    if not markup:
        return
    st.markdown(markup, unsafe_allow_html=True)


@st.cache_data(ttl=2, show_spinner=False)
def _pending_cached(project_id: str) -> tuple[int, int]:
    return runner.count_pending(project_id)


@st.cache_data(ttl=2, show_spinner=False)
def _session_cached(project_id: str) -> bool:
    return yb.has_saved_session(project_id)


def status_pills(project_id: str) -> list[tuple[str, str]]:
    """Те же три пилюли, что в оригинале: установка, авторизация, очередь."""
    files, cities = _pending_cached(project_id)
    state = runner.read_state(project_id)
    pills = [
        ("ok", "Программа установлена"),
        ("ok", "Авторизован") if _session_cached(project_id) else ("warn", "Требуется вход"),
        ("info", f"{cities_word(cities)} в очереди") if cities else ("warn", "Очередь пуста"),
    ]
    if state.get("status") == "running":
        pills.append(("warn", "Идёт " + ("публикация" if state.get("action") == "publish" else "актуализация")))
    elif state.get("status") == "error":
        pills.append(("err", "Последний прогон с ошибкой"))
    return pills


def city_selector(key_prefix: str, country: dict, default_all: bool = True) -> list[str]:
    """
    Города страны – строка-заголовок и сетка галочек, как в оригинале.
    По умолчанию выбраны ВСЕ города страны: в оригинале выбор страны означал
    «шлём во все её города», а лишние снимают руками.
    """
    state_key = f"{key_prefix}-cities-{country['id']}"
    options = [c["id"] for c in country["cities"]]
    if state_key not in st.session_state:
        st.session_state[state_key] = list(options) if default_all else []
    chosen = set(st.session_state.get(state_key) or [])
    n = len(chosen)

    idx = abs(hash(country["id"])) % 10_000          # ключ контейнера – только латиница
    open_key = f"{key_prefix}-open-{country['id']}"
    is_open = st.session_state.get(open_key) is True
    html(T.tile_css([(f"tile-row-city-{key_prefix}-{idx}",
                      row_vars(country, chosen, "свернуть ▾" if is_open else "изменить ▸"))]))
    with st.container(key=f"tile-row-city-{key_prefix}-{idx}"):
        st.button(country["name"], key=f"{key_prefix}-row-{country['id']}",
                  use_container_width=True, type="primary" if is_open else "secondary",
                  on_click=_flip, args=(open_key,))
    if not is_open:
        return [c for c in options if c in chosen]

    with st.container(border=True):
        head, hint = st.columns([1, 4])
        head.button("Снять все" if n == len(options) else "Выбрать все",
                    key=f"{key_prefix}-toggle-{country['id']}",
                    on_click=_set_cities_sync,
                    args=(state_key, options, [] if n == len(options) else list(options)))
        with hint:
            html('<div class="hint" style="padding-top:9px">Кликайте по городам '
                 'чтобы исключить или добавить</div>')
        with st.container(key="city-grid"):
            per_row = 7
            for i in range(0, len(country["cities"]), per_row):
                cols = st.columns(per_row)
                for col, ct in zip(cols, country["cities"][i:i + per_row]):
                    wkey = f"{key_prefix}-cb-{ct['id']}"
                    col.checkbox(ct["name"], value=ct["id"] in chosen, key=wkey,
                                 on_change=_city_toggle, args=(state_key, ct["id"], wkey))
    return [c for c in options if c in set(st.session_state.get(state_key) or [])]


def _flip(key: str) -> None:
    st.session_state[key] = not st.session_state.get(key)


def _set_cities_sync(state_key: str, options: list[str], value: list[str]) -> None:
    """Меняем и набор, и сами галочки – иначе счётчик уедет, а галочки останутся."""
    st.session_state[state_key] = list(value)
    prefix = state_key.split("-cities-")[0]
    for cid in options:
        st.session_state[f"{prefix}-cb-{cid}"] = cid in value


def _city_toggle(state_key: str, city_id: str, widget_key: str) -> None:
    cur = set(st.session_state.get(state_key) or [])
    cur.add(city_id) if st.session_state.get(widget_key) else cur.discard(city_id)
    st.session_state[state_key] = list(cur)


def _toggle_open(open_key: str, value: str) -> None:
    st.session_state[open_key] = None if st.session_state.get(open_key) == value else value


def _set_cities(state_key: str, value: list[str]) -> None:
    st.session_state[state_key] = value


def _set_post_type(type_id: str) -> None:
    st.session_state["compose-type"] = type_id


def _toggle_country(key_prefix: str, country_id: str) -> None:
    key = f"{key_prefix}-cb-{country_id}"
    now = not st.session_state.get(key)
    st.session_state[key] = now
    if not now:
        st.session_state[f"{key_prefix}-{country_id}-cities-{country_id}"] = []


def _toggle_all_countries(key_prefix: str, country_ids: list[str], turn_on: bool) -> None:
    for cid in country_ids:
        st.session_state[f"{key_prefix}-cb-{cid}"] = turn_on
        if not turn_on:
            st.session_state[f"{key_prefix}-{cid}-cities-{cid}"] = []


def _city_duplicate(config: dict, url: str, name: str, country_id: str) -> str | None:
    """Куда этот город уже добавлен. None – дубля нет, можно добавлять."""
    cid = yb.extract_company_id(url)
    norm = lambda t: re.sub(r"\s+", " ", (t or "")).strip().lower()  # noqa: E731
    for c in config["countries"]:
        for ct in c["cities"]:
            if cid and yb.extract_company_id(ct.get("url")) == cid:
                return f"эта карточка уже есть: {c['name']} / {ct['name']}"
            if c["id"] == country_id and norm(ct.get("name")) == norm(name):
                return f"город «{ct['name']}» уже есть в стране {c['name']}"
    return None


def country_picker(key_prefix: str, config: dict, title: str = "Страны",
                   with_cities: bool = False, cities: dict | None = None) -> list[dict]:
    """
    Выбор стран карточками, как в оригинале: флаг, название, «N гор.».
    Сама карточка – HTML (Streamlit такого не умеет), клик – кнопкой под ней.
    """
    countries = config["countries"]
    cities = cities if cities is not None else {}
    if not countries:
        return []

    def cb_key(country_id: str) -> str:
        return f"{key_prefix}-cb-{country_id}"

    chosen = [c for c in countries if st.session_state.get(cb_key(c["id"]))]

    with st.container(border=True):
        head, act = st.columns([3, 1])
        head.markdown(
            f'<div class="card-title">{title} '
            f'<span class="badge badge-{"accent" if chosen else "muted"}">{len(chosen)}</span></div>',
            unsafe_allow_html=True)
        all_on = len(chosen) == len(countries)
        act.button("Снять все" if all_on else "Выбрать все",
                   key=f"{key_prefix}-toggle-countries", use_container_width=True,
                   on_click=_toggle_all_countries, args=(key_prefix, [c["id"] for c in countries], not all_on))

        # Ключи плиток – только латиница и цифры (см. T.tile_css).
        html(T.tile_css([
            (f"tile-cc-{key_prefix}-{n}", {
                "--flag": T.flag_data_uri(c["name"]),
                "--meta": T.css_text(f'{len(c["cities"])} гор.'),
            }) for n, c in enumerate(countries)
        ]))
        per_row = 4
        for start in range(0, len(countries), per_row):
            cols = st.columns(per_row)
            for col, (n, c) in zip(cols, list(enumerate(countries))[start:start + per_row]):
                active = bool(st.session_state.get(cb_key(c["id"])))
                with col, st.container(key=f"tile-cc-{key_prefix}-{n}"):
                    st.button(c["name"], key=f"{key_prefix}-pick-{c['id']}",
                              use_container_width=True,
                              type="primary" if active else "secondary",
                              on_click=_toggle_country, args=(key_prefix, c["id"]))

        chosen = [c for c in countries if st.session_state.get(cb_key(c["id"]))]
        # Города выбираются здесь же, под странами – как в оригинале, а не внизу
        # страницы рядом с предпросмотром.
        if with_cities and chosen:
            st.divider()
            for c in chosen:
                cities[c["id"]] = city_selector(key_prefix, c)

    return chosen


# ════════════════════════════════════════════════════════════════════
#  ЭКРАН ВХОДА
# ════════════════════════════════════════════════════════════════════

def show_login() -> None:
    inject_css()
    html('<div class="auth-wrap"><div class="auth-logo">➤</div>'
         '<div class="auth-title">Click</div>'
         '<div class="auth-sub">Пакетная публикация в Яндекс.Бизнес – выберите проект</div></div>')

    selected = st.session_state.get("selected_project_id")
    cols = st.columns(len(PROJECTS))
    for col, (pid, p) in zip(cols, PROJECTS.items()):
        with col:
            html(T.project_tile(p, len(p["presetCities"] or []), selected == pid))
            if st.button(f"Выбрать {p['name']}", key=f"pick-{pid}", use_container_width=True,
                         type="primary" if selected == pid else "secondary"):
                st.session_state.selected_project_id = pid
                st.rerun()

    if not selected:
        return

    st.write("")
    left, mid, right = st.columns([1, 2, 1])
    with mid:
        with st.form("login_form"):
            st.markdown(f"**Вход в проект {PROJECTS[selected]['icon']} {PROJECTS[selected]['fullName']}**")
            password = st.text_input("Пароль доступа", type="password")
            ok = st.form_submit_button("Войти", type="primary", use_container_width=True)
        if ok:
            if _hash(password) == PROJECTS[selected]["passwordHash"]:
                ensure_dirs(selected)
                st.session_state.current_project_id = selected
                st.query_params.update({"p": selected, "k": _url_token(selected)})
                st.rerun()
            else:
                st.error("Неверный пароль")


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: ЗАПУСК
# ════════════════════════════════════════════════════════════════════

def _render_live_panel(project_id: str, was_running: bool = False) -> None:
    state = runner.read_state(project_id)
    status = state.get("status")
    action_ru = "Публикация" if state.get("action") == "publish" else "Актуализация"

    # Прогон только что закончился – перерисовываем страницу целиком, чтобы
    # разблокировалась кнопка запуска и обновились счётчики в шапке.
    if was_running and status != "running":
        st.rerun()

    if status == "running":
        total = max(1, int(state.get("total") or 1))
        current = int(state.get("current") or 0)
        totals = state.get("totals") or {}
        done_line = " · ".join(
            f"{icon} {totals.get(k, 0)}" for icon, k in
            (("✅", "ok"), ("🟡", "noImage"), ("⚠️", "unknown"), ("❌", "failed"), ("⏭", "skipped"))
            if totals.get(k)
        )
        html(T.run_progress(
            f"{action_ru}: {current} из {total}",
            (f"Сейчас: {state.get('currentCity') or '…'}" + (f" · {done_line}" if done_line else "")),
        ))
        st.progress(min(1.0, current / total))
    elif status == "done":
        st.success(f"{action_ru} завершена.")
        with st.container(key="go-report"):
            if st.button("📊 Посмотреть отчёт", key=f"btn-report-{state.get('runId', '')}"):
                goto_section(SECTIONS[4])
                st.rerun()
    elif status == "stopped":
        st.warning(f"{action_ru} остановлена пользователем. Сделанное сохранено в отчёте.")
    elif status == "error":
        st.error(f"{action_ru} завершилась с ошибкой: {state.get('error') or 'см. лог'}")
    elif status == "interrupted":
        st.warning(state.get("error") or "Прогон был прерван.")

    html(T.log_box(runner.read_live_log(project_id)))


def tab_run(project_id: str, config: dict) -> None:
    settings = get_settings(project_id)
    state = runner.read_state(project_id)
    running = state.get("status") == "running"
    files, cities = runner.count_pending(project_id)
    has_session = yb.has_saved_session(project_id)
    has_creds = bool((config.get("email") or "").strip())

    # ─── Степпер как в оригинале ───
    html(T.step(1, "Вход в Яндекс",
                "Нужен один раз: Click сохранит сессию, дальше публикация идёт в фоне без 2FA.",
                "done" if has_session else "active",
                "Сессия сохранена" if has_session else ""))
    if not has_session:
        st.info("Перейдите в раздел «⚙️ Настройки» → «Вход в Яндекс».")

    html(T.step(2, "Очередь задач",
                f"Посты собираются во вкладке «Публикация» и складываются в очередь. "
                f"Сейчас: <b>{plural(files, 'файл', 'файла', 'файлов')}</b>, "
                f"<b>{cities_word(cities)}</b>.",
                "done" if cities else ("active" if has_session else "locked"),
                f"{cities_word(cities)} готово" if cities else ""))

    html(T.step(3, "Публикация",
                ("Окно браузера будет видно – галочка в «Настройках» включена."
                 if show_browser_window() else "Браузер работает скрыто.")
                + " Каждый город подтверждается ответом API Яндекса – "
                "в отчёт попадает реальный результат, а не «наверное получилось».",
                "active" if (cities and has_session and not running) else ("done" if running else "locked")))

    # ─── Кнопки ───
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        disabled = running or not cities or not has_session
        if st.button(f"▶ Опубликовать ({cities_word(cities)})", type="primary",
                     use_container_width=True, disabled=disabled, key="btn-publish"):
            ok, msg = runner.start_publish(
                project_id,
                headless=bool(settings["headless"]),
                delay_between_posts_s=float(settings["delayBetweenPosts"]),
                expected_email=(config.get("email") or "").strip(),
                strict_account_check=bool(settings["strictAccountCheck"]),
                retry_unknown=bool(settings["retryUnknown"]),
                dedup_window_hours=float(settings["dedupWindowHours"]),
            )
            (st.toast if ok else st.error)(msg)
            time.sleep(0.6)
            st.rerun()
    with c2:
        if st.button("⏹ Остановить", use_container_width=True, disabled=not running, key="btn-stop"):
            runner.request_stop(project_id)
            st.rerun()
    with c3:
        if st.button("🔄 Обновить", use_container_width=True, key="btn-refresh-run"):
            st.rerun()

    if not has_creds:
        st.warning("В «Настройках» не указан email Яндекс.Бизнеса – без него не работает "
                   "защита от публикации не с того аккаунта.")
    if running:
        st.caption("Кнопка запуска заблокирована, пока идёт прогон – это защита от повторного старта "
                   "и дублей постов.")

    st.divider()

    # ─── Живой лог: обновляется сам, если версия Streamlit умеет фрагменты ───
    fragment = getattr(st, "fragment", None)
    if fragment and running:
        @fragment(run_every=2)
        def _live():
            _render_live_panel(project_id, was_running=True)
        _live()
    else:
        _render_live_panel(project_id)
        if running:
            st.caption("Обновите страницу, чтобы увидеть свежий прогресс.")

    # ─── Очередь задач ───
    st.divider()
    if cities:
        with st.expander(f"📋 Файлы задач в очереди ({files})"):
            for fp in sorted((project_base(project_id) / "tasks").glob("*.json")):
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                html(f'<div class="city-row"><span class="city-row-name">{T.esc(data.get("country", "–"))}</span>'
                     f'<span class="city-row-url">{T.esc(fp.name)}</span>'
                     f'<span class="badge badge-accent">{len(data.get("tasks") or [])} гор.</span></div>')
            with st.container(key="danger-clear-tasks"):
                if st.button("Очистить очередь", disabled=running, key="btn-clear-tasks"):
                    clear_tasks(project_id)
                    st.rerun()
    else:
        html(T.empty("📭", "Очередь пуста", "Соберите пост во вкладке «Публикация» и добавьте города в очередь."))


# ════════════════════════════════════════════════════════════════════
#  РЕДАКТОР ОКОНЧАНИЙ (шестерёнка в «Типе поста»)
# ════════════════════════════════════════════════════════════════════

def _show_endings_dialog(project_id: str, config: dict) -> None:
    """Окно поверх страницы: список городов и текст поста остаются на месте."""
    dialog = getattr(st, "dialog", None)
    if dialog is None:                       # старая версия Streamlit – рисуем врезкой
        with st.container(border=True):
            html('<div class="card-title">⚙ Окончания постов</div>')
            _endings_editor(project_id, config)
        return

    @dialog(f"Окончания постов · {PROJECTS[project_id]['name']}", width="large")
    def _win() -> None:
        _endings_editor(project_id, config)

    _win()


PLACEHOLDERS = [
    ("{site}", "сайт страны"),
    ("{email}", "почта страны"),
    ("{phone}", "телефон страны"),
    ("{phoneLine}", "строка «📞 телефон»; без телефона строка исчезает целиком"),
    ("{phoneSpecialLine}", "то же со значком ☎️"),
    ("{phoneSpecialLineMpe}", "то же в виде «📱 Телефон: …»"),
]


def _endings_editor(project_id: str, config: dict) -> None:
    """
    Окно правки окончаний. Контакты – общие по стране (решение заказчика),
    хэштеги входят в текст шаблона, отдельного поля под них нет.
    """
    data = project_endings(project_id)
    countries = [c["name"] for c in config["countries"]] or list((data.get("contacts") or {}).keys())
    types = pdata.POST_TYPES

    ok, note = repo_store.check()
    if ok:
        st.caption(f"✅ Правки сохраняются в {repo_store.where()} – переживают перезапуск в облаке.")
    else:
        st.warning(f"Правки сохранятся только внутри приложения и пропадут при перезапуске: {note}.",
                   icon="💾")

    tab_tpl, tab_contacts = st.tabs(["Шаблоны по типам постов", "Контакты по странам"])

    with tab_tpl:
        ids = [t["id"] for t in types]
        titles = {t["id"]: f'{t["icon"]} {t["title"]}' for t in types}
        chosen = st.selectbox("Тип поста", ids, format_func=lambda i: titles[i], key="end-type")
        st.text_area("Шаблон окончания", value=(data.get("templates") or {}).get(chosen, ""),
                     key=f"end-tpl-{chosen}", height=190,
                     help="Пустой шаблон – пост уйдёт без окончания")
        html('<div class="ph-list">' + "".join(
            f'<div><code>{T.esc(k)}</code> <span>{T.esc(v)}</span></div>'
            for k, v in PLACEHOLDERS) + "</div>")

    with tab_contacts:
        for country in countries:
            c = (data.get("contacts") or {}).get(country) or {}
            st.markdown(f'<div class="hint" style="margin:8px 0 2px">{T.flag_svg(country)} '
                        f'<b style="color:var(--text)">{T.esc(country)}</b></div>',
                        unsafe_allow_html=True)
            a, b, d = st.columns(3)
            a.text_input("Сайт", value=c.get("site") or "", key=f"end-site-{country}")
            b.text_input("Почта", value=c.get("email") or "", key=f"end-mail-{country}")
            d.text_input("Телефон", value=c.get("phone") or "", key=f"end-phone-{country}")

    # ── Как получится ──
    st.divider()
    prev_country = st.selectbox("Проверить на стране", countries or ["Россия"],
                                key="end-preview-country")
    draft = _endings_from_form(project_id, config, data)
    html('<div class="card-title" style="margin-top:6px">Как получится</div>')
    html(T.preview_box(_preview_with(project_id, draft, prev_country,
                                     st.session_state.get("end-type") or types[0]["id"])))

    c1, c2, c3 = st.columns([1, 1, 2])
    if c1.button("Сохранить", type="primary", use_container_width=True, key="end-save"):
        try:
            note = save_project_endings(project_id, draft)
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
            return
        st.success(f"Окончания {note}.")
        time.sleep(1.0)
        st.rerun()
    if c2.button("Вернуть как было", use_container_width=True, key="end-reset"):
        for k in [k for k in st.session_state if k.startswith("end-")]:
            del st.session_state[k]
        try:
            save_project_endings(project_id, {"contacts": {}, "templates": {}})
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
            return
        st.rerun()
    if c3.button("Закрыть", use_container_width=True, key="end-close"):
        st.rerun()                       # перерисовка закрывает окно Streamlit


def _endings_from_form(project_id: str, config: dict, data: dict) -> dict:
    """Собрать окончания из полей окна. Незаполненные поля – пустые строки, не None."""
    countries = [c["name"] for c in config["countries"]] or list((data.get("contacts") or {}).keys())
    contacts = {}
    for country in countries:
        contacts[country] = {
            "site": (st.session_state.get(f"end-site-{country}") or "").strip(),
            "email": (st.session_state.get(f"end-mail-{country}") or "").strip(),
            "phone": (st.session_state.get(f"end-phone-{country}") or "").strip(),
        }
    templates = dict(data.get("templates") or {})
    for t in pdata.POST_TYPES:
        key = f"end-tpl-{t['id']}"
        if key in st.session_state:
            templates[t["id"]] = st.session_state[key]
    return {"contacts": contacts, "templates": templates}


def _preview_with(project_id: str, draft: dict, country: str, post_type: str) -> str:
    """Предпросмотр собирается тем же кодом, что и публикация – иначе это гадание."""
    base = endings_defaults(project_id)
    base.setdefault("contacts", {})
    base.setdefault("templates", {})
    for name, c in (draft.get("contacts") or {}).items():
        base["contacts"][name] = {**(base["contacts"].get(name) or {}), **c}
    base["templates"].update(draft.get("templates") or {})

    saved = _ENDINGS_OVERRIDE.get(project_id)
    _ENDINGS_OVERRIDE[project_id] = base
    try:
        return build_final_text(project_id, country, post_type, "Текст вашего поста")
    finally:
        if saved is None:
            _ENDINGS_OVERRIDE.pop(project_id, None)
        else:
            _ENDINGS_OVERRIDE[project_id] = saved


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: ПУБЛИКАЦИЯ
# ════════════════════════════════════════════════════════════════════

def tab_compose(project_id: str, config: dict) -> None:
    if not config["countries"]:
        html(T.empty("🏙", "Нет городов", "Добавьте страны и города во вкладке «Города»."))
        return

    queue: list[dict] = st.session_state.setdefault("queue", [])

    # ─── Тип поста: плитки, как в оригинале ───
    types = pdata.POST_TYPES
    post_type = st.session_state.get("compose-type") or types[0]["id"]
    with st.container(border=True):
        title_col, gear_col = st.columns([12, 1])
        with title_col:
            html('<div class="card-title">📄 Тип поста</div>')
        with gear_col, st.container(key="endings-gear"):
            open_endings = st.button("⚙", key="btn-endings", use_container_width=True,
                                     help="Окончания постов: контакты и хэштеги")
        # Иконку рисуем через CSS ::before у самой кнопки – так плитка остаётся
        # настоящей кнопкой и клик по ней срабатывает всегда.
        html(T.tile_css([(f"tile-pt-{t['id']}", {"--ico": T.css_text(t["icon"])}) for t in types]))
        if open_endings:
            _show_endings_dialog(project_id, config)
        cols = st.columns(len(types))
        for col, t in zip(cols, types):
            with col, st.container(key=f"tile-pt-{t['id']}"):
                st.button(t["title"], key=f"pt-{t['id']}", use_container_width=True,
                          type="primary" if t["id"] == post_type else "secondary",
                          on_click=_set_post_type, args=(t["id"],))
    type_def = next(t for t in types if t["id"] == post_type)

    # ─── Страны для публикации: карточки со счётчиком городов, как в оригинале ───
    per_country: dict[str, list[str]] = {}
    selected_countries = country_picker("compose", config, title="🌍 Страны для публикации",
                                        with_cities=True, cities=per_country)

    # ─── Текст и картинки – одна карточка, названная типом поста ───
    text_card = st.container(border=True)
    with text_card:
      html(f'<div class="card-title">{type_def["icon"]} {T.esc(type_def["title"])}</div>')
      body = st.text_area(
          "Основной текст" + ("" if type_def["isInfo"] else " (без контактов)"),
          height=200, key="compose-body", placeholder="Текст поста...",
      )

      with st.container(key="img-row"):
          c1, c2 = st.columns([3, 1])
          image_urls_raw = c1.text_area(
              "Картинки (до 4 для поста)", height=118, key="compose-image-urls",
              placeholder="Можно: ссылки (по строке) ИЛИ загрузить файлы кнопкой справа\n"
                          "https://ibb.co/abc/foto1.jpg\nhttps://ibb.co/xyz/foto2.jpg",
          )
          uploaded = c2.file_uploader("Выбрать файл", type=["jpg", "jpeg", "png", "gif", "webp"],
                                      accept_multiple_files=True, key=f"compose-images-{st.session_state.get('upl-gen', 0)}",
                                      label_visibility="collapsed")
      html('<div class="hint">Можно ссылки (ImgBB / Imgur / Я.Диск) ИЛИ загрузить файлы с компьютера. '
           'Если есть проблемы с интернетом – лучше загружайте файлы. До 20 МБ.</div>')

      with st.container(key="goods-row"):
          g1, g2 = st.columns([3, 1])
          product_photos_raw = g1.text_area(
              "Фото в раздел «Товары» (необязательно)", height=118, key="compose-product-photos",
              placeholder="Ссылки или пути, по одной в строке\n"
                          "Заливаются в карточку после успешной публикации поста",
          )
          goods_files = g2.file_uploader("Фото товаров", type=["jpg", "jpeg", "png", "gif", "webp"],
                                         accept_multiple_files=True, key=f"compose-goods-files-{st.session_state.get('upl-gen', 0)}",
                                         label_visibility="collapsed")

    image_urls = [u.strip() for u in (image_urls_raw or "").splitlines() if u.strip()]
    product_photos = [u.strip() for u in (product_photos_raw or "").splitlines() if u.strip()]

    saved_paths: list[str] = []
    if uploaded:
        uploads = project_base(project_id) / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        for f in uploaded[:4]:
            digest = hashlib.md5(f.getvalue()).hexdigest()[:10]
            path = uploads / f"{digest}-{safe_filename(f.name)}"
            if not path.exists():
                path.write_bytes(f.getvalue())
            saved_paths.append(str(path))

    if goods_files:
        uploads = project_base(project_id) / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        for f in goods_files:
            digest = hashlib.md5(f.getvalue()).hexdigest()[:10]
            path = uploads / f"{digest}-{safe_filename(f.name)}"
            if not path.exists():
                path.write_bytes(f.getvalue())
            product_photos.append(str(path))

    all_images: list[str] = saved_paths + image_urls
    if len(all_images) > 4:
        st.warning(f"Яндекс берёт максимум 4 фото в пост – лишние {len(all_images) - 4} не отправятся.")

    if not selected_countries:
        st.info("Выберите хотя бы одну страну выше.")
        return

    if (body or "").strip():
        with st.container(border=True):
            html('<div class="card-title">👁 Так пост уйдёт в Яндекс</div>')
            for country in selected_countries:
                html(f'<div class="hint" style="margin:8px 0 4px">{flag(country["name"])} '
                     f'<b style="color:var(--text)">{T.esc(country["name"])}</b></div>')
                html(T.preview_box(build_final_text(project_id, country["name"], post_type, body)))

    total_cities = sum(len(v) for v in per_country.values())

    # ─── Шаг 5: в очередь ───
    st.divider()
    c1, c2 = st.columns([2, 3])
    with c1:
        can_add = bool((body or "").strip()) and total_cities > 0
        if st.button(f"➕ Добавить в очередь ({cities_word(total_cities)})", type="primary",
                     use_container_width=True, disabled=not can_add, key="btn-add-queue"):
            added = 0
            for country in selected_countries:
                city_ids = per_country.get(country["id"]) or []
                if not city_ids:
                    continue
                if any(q["countryId"] == country["id"] and q["text"] ==
                       build_final_text(project_id, country["name"], post_type, body) for q in queue):
                    st.warning(f"{country['name']}: такой же пост уже в очереди – пропускаю.")
                    continue
                queue.append({
                    "countryId": country["id"],
                    "countryName": country["name"],
                    "cityIds": list(city_ids),
                    "postType": post_type,
                    "text": build_final_text(project_id, country["name"], post_type, body),
                    "imagePath": all_images[0] if all_images and not all_images[0].startswith("http") else None,
                    "imageUrl": all_images[0] if all_images and all_images[0].startswith("http") else None,
                    "extraImages": all_images[1:4] or None,
                    "productPhotos": product_photos or None,
                })
                added += 1
            if added:
                st.toast(f"Добавлено стран в очередь: {added}")
            st.rerun()
    with c2:
        if not can_add:
            st.caption("Нужно: текст поста + хотя бы один выбранный город.")

    # ─── Очередь ───
    if queue:
        st.divider()
        html(f'<div class="card-title">📦 Очередь на публикацию – '
             f'{plural(len(queue), "пакет", "пакета", "пакетов")}, '
             f'{cities_word(sum(len(q["cityIds"]) for q in queue))}</div>')
        for i, item in enumerate(queue):
            preview = item["text"][:180] + ("…" if len(item["text"]) > 180 else "")
            html(f'<div class="queue-item">'
                 f'<div class="queue-item-title">{flag(item["countryName"])} {T.esc(item["countryName"])} '
                 f'<span class="badge badge-accent">{len(item["cityIds"])} гор.</span></div>'
                 f'<div class="queue-item-text">{T.esc(preview)}</div></div>')
            if st.button("Убрать", key=f"queue-del-{i}"):
                queue.pop(i)
                st.rerun()

        c1, c2 = st.columns([2, 1])
        with c1:
            if st.button("💾 Сохранить очередь в задачи", type="primary",
                         use_container_width=True, key="btn-save-queue"):
                saved = save_queue_to_tasks(project_id, config, queue)
                st.session_state["queue"] = []
                goto_section(SECTIONS[0])       # дальше человеку всё равно на «Запуск»
                # Новое поколение ключей очищает загрузчики файлов: их состояние
                # переживает перерисовки, и старые картинки тихо прицеплялись к
                # СЛЕДУЮЩЕМУ посту («я вообще ничего не прикрепляла»).
                st.session_state["upl-gen"] = st.session_state.get("upl-gen", 0) + 1
                st.toast(f"Сохранено файлов задач: {saved}. Открываю «Запуск».")
                time.sleep(0.6)
                st.rerun()
        with c2:
            with st.container(key="danger-clear-queue"):
                if st.button("Очистить очередь", use_container_width=True, key="btn-drop-queue"):
                    st.session_state["queue"] = []
                    st.rerun()


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: АКТУАЛИЗАЦИЯ
# ════════════════════════════════════════════════════════════════════

# Выбор городов для актуализации держим в СВОЁМ наборе, а не в ключах чекбоксов:
# Streamlit удаляет состояние виджетов, которые не отрисовались в прогоне (а свёрнутые
# страны мы намеренно не рисуем), и галочки бы слетали.
def row_vars(country: dict, chosen: set[str] | None, action: str) -> dict[str, str]:
    """
    Переменные CSS для строки страны: флаг слева, отметка и действие справа.
    Так строка остаётся настоящей кнопкой, а выглядит как .country-row оригинала.
    """
    ids = [ct["id"] for ct in country["cities"]]
    if chosen is None:                                   # вкладка «Города»
        mark, color = cities_word(len(ids)), "var(--muted)"
    else:
        n = sum(1 for cid in ids if cid in chosen)
        if n == len(ids):
            mark, color = f"✓ все {len(ids)}", "var(--grn)"
        elif n:
            mark, color = f"{n} из {len(ids)}", "var(--yel)"
        else:
            mark, color = "не выбрано", "var(--dim)"
    return {"--flag": T.flag_data_uri(country["name"]),
            "--mark": T.css_text(mark), "--mark-c": color,
            "--act": T.css_text(action)}


def _act_selected(all_ids: list[str]) -> set[str]:
    sel = st.session_state.get("act-selected")
    if sel is None:
        sel = set(all_ids)                     # по умолчанию выбраны все, как в оригинале
        st.session_state["act-selected"] = sel
    return sel


def _act_set(city_ids: list[str], value: bool) -> None:
    """
    Вызывается ТОЛЬКО из on_click кнопки: в этот момент виджеты ещё не созданы,
    поэтому их состояние можно переписать. Держим в согласии свой набор и сами
    галочки – иначе «Выбрать все» меняло бы счётчик, а галочки нет.
    """
    sel = st.session_state.setdefault("act-selected", set())
    sel.update(city_ids) if value else sel.difference_update(city_ids)
    for cid in city_ids:
        st.session_state[f"act-cb-{cid}"] = value


def _act_toggle(city_id: str, widget_key: str) -> None:
    sel = st.session_state.setdefault("act-selected", set())
    if st.session_state.get(widget_key):
        sel.add(city_id)
    else:
        sel.discard(city_id)


def tab_actualize(project_id: str, config: dict) -> None:
    countries = config["countries"]
    if not countries:
        html(T.empty("🏙", "Нет городов", "Добавьте страны и города во вкладке «Города»."))
        return

    all_ids = [ct["id"] for c in countries for ct in c["cities"]]
    chosen = _act_selected(all_ids)
    selected = [cid for cid in all_ids if cid in chosen]

    state = runner.read_state(project_id)
    running = state.get("status") == "running"

    with st.container(border=True):
        html('<div class="card-title">🔄 Актуализация данных</div>')
        html('<div class="hint" style="margin-bottom:12px">Скрипт зайдёт в раздел «Данные» каждого города '
             'и нажмёт кнопку <b>«Данные актуальны»</b>, если она там есть. Кнопка появляется на странице '
             'периодически – Яндекс просит подтверждать, что данные не изменились. '
             'Если кнопки нет – актуализация не требуется.</div>')

        head, act = st.columns([3, 1])
        head.markdown(
            f'<div style="font-size:13px;font-weight:700;color:var(--text)">Выбрано: '
            f'<span style="color:var(--acc)">{len(selected)}</span> / {len(all_ids)} городов</div>',
            unsafe_allow_html=True)
        all_on = len(selected) == len(all_ids)
        act.button("Снять все" if all_on else "Выбрать все", key="act-toggle-all",
                   use_container_width=True, on_click=_act_set, args=(all_ids, not all_on))

        html(T.tile_css([
            (f"tile-row-act-{n}", row_vars(c, chosen,
                                           "свернуть ▾" if st.session_state.get("act-open") == c["id"]
                                           else "изменить ▸"))
            for n, c in enumerate(countries)
        ]))
        for n, c in enumerate(countries):
            ids = [ct["id"] for ct in c["cities"]]
            picked = sum(1 for cid in ids if cid in chosen)
            is_open = st.session_state.get("act-open") == c["id"]
            with st.container(key=f"tile-row-act-{n}"):
                st.button(c["name"], key=f"act-row-{c['id']}", use_container_width=True,
                          type="primary" if is_open else "secondary",
                          on_click=_toggle_open, args=("act-open", c["id"]))
            # Города рисуем только для раскрытой страны – иначе 117 чекбоксов
            # строились бы при каждом клике по чему угодно.
            if not is_open:
                continue
            with st.container(border=True):
                st.button("Снять все в стране" if picked == len(ids) else "Выбрать все в стране",
                          key=f"act-toggle-{c['id']}",
                          on_click=_act_set, args=(ids, picked != len(ids)))
                # Галочка переключается сразу, без кнопки «применить»: on_change
                # правит только набор выбранных, а не пересобирает состояние.
                with st.container(key="city-grid"):
                    per_row = 7
                    for start_i in range(0, len(c["cities"]), per_row):
                        cols = st.columns(per_row)
                        for col, ct in zip(cols, c["cities"][start_i:start_i + per_row]):
                            wkey = f"act-cb-{ct['id']}"
                            col.checkbox(ct["name"], value=ct["id"] in chosen, key=wkey,
                                         on_change=_act_toggle, args=(ct["id"], wkey))

    if not yb.has_saved_session(project_id):
        st.warning("Сначала войдите в Яндекс в разделе «⚙️ Настройки».")
        return

    with st.container(border=True):
        _render_live_panel(project_id, was_running=False)

    if st.button(f"🔄 Запустить актуализацию ({cities_word(len(selected))})", type="primary",
                 use_container_width=True, disabled=running or not selected, key="btn-actualize"):
        selection = {c["id"]: [ct["id"] for ct in c["cities"] if ct["id"] in chosen]
                     for c in countries}
        save_actualize_tasks(project_id, config, selection)
        ok, msg = runner.start_actualize(project_id, headless=bool(get_settings(project_id)["headless"]))
        (st.toast if ok else st.error)(msg)
        time.sleep(0.6)
        st.rerun()
    if running:
        st.button("⏹ Остановить", use_container_width=True, key="btn-stop-act",
                  on_click=runner.request_stop, args=(project_id,))

    reports = runner.list_reports(project_id, "actualize", limit=1)
    if reports:
        data = runner.read_report(project_id, "actualize", reports[0]["name"])
        if data:
            with st.container(border=True):
                when = local_time(data.get("finishedAt"))
                # Пока прогон идёт, отчёт наполняется на ходу. Называть его
                # «последним» в этот момент нечестно: заказчик видел нули и
                # решила, что отчёт не формируется вообще.
                title = ("📊 Отчёт актуализации – заполняется по ходу"
                         if data.get("state") == "in-progress"
                         else "📊 Последний отчёт актуализации")
                head, date = st.columns([3, 1])
                head.markdown(f'<div class="card-title">{title}</div>',
                              unsafe_allow_html=True)
                date.markdown(f'<div class="hint" style="text-align:right;padding-top:6px">{when}</div>',
                              unsafe_allow_html=True)
                html(T.report_summary(data.get("totals") or {}, data.get("durationSec"),
                                      keys=["actualized", "notNeeded", "failed"], with_total=False))
                with st.expander(f"Показать детали ({cities_word(len(data.get('results') or []))})"):
                    for r in data.get("results") or []:
                        html(T.report_row(r))


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: ГОРОДА
# ════════════════════════════════════════════════════════════════════

def _cities_source_block(project_id: str, config: dict) -> None:
    """
    Источник городов – Google-таблица КП. В облаке файловая система временная,
    поэтому набитый руками список пропадает при перезапуске; таблица живёт
    снаружи и подтягивается обратно.
    """
    saved_url = (config.get("kpSheetUrl") or "").strip()
    effective = kp_sheet.sheet_url(project_id, saved_url)
    has_key = kp_sheet.service_account_info() is not None

    with st.expander("📊 Источник городов – Google-таблица КП",
                     expanded=not config["countries"]):
        url = st.text_input("Ссылка на таблицу КП этого проекта", value=saved_url,
                            key=f"kp-url-{project_id}",
                            placeholder="https://docs.google.com/spreadsheets/d/…")
        if url.strip() != saved_url:
            config["kpSheetUrl"] = url.strip()
            save_config(project_id)
            st.rerun()

        if not effective:
            st.caption("Ссылку можно задать здесь или секретом `kp_sheet_url_"
                       f"{project_id}` в настройках приложения.")
        elif not saved_url:
            st.caption(f"Используется таблица проекта по умолчанию: {effective}")
        if not has_key:
            st.warning(
                "Не найден ключ сервисного аккаунта Google. Добавьте в секреты приложения "
                "`gcp_service_account_b64` – весь JSON-ключ в base64. Таблица должна быть "
                "расшарена на этот аккаунт как Читатель.",
                icon="🔑",
            )

        c1, c2 = st.columns([2, 3])
        if c1.button("⬇️ Загрузить города из таблицы", type="primary",
                     disabled=not (effective and has_key), key=f"kp-pull-{project_id}",
                     use_container_width=True):
            try:
                with st.spinner("Читаю таблицу КП…"):
                    cities, diag = kp_sheet.load_cities(project_id, saved_url)
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
                return
            if diag.get("error"):
                st.error(diag["error"])
                return
            if not cities:
                st.warning("В таблице не нашлось ни одного города со ссылкой на Яндекс.Бизнес.")
                return
            config["countries"] = kp_sheet.to_countries(cities, project_id)
            config["kpSyncedAt"] = datetime.now(timezone.utc).isoformat()
            save_config(project_id)
            note = f'Загружено: {cities_word(len(cities))} в {diag.get("countries", 0)} странах.'
            if diag.get("skippedDeleted"):
                note += f' Пропущено удалённых карточек: {diag["skippedDeleted"]}.'
            st.success(note)
            time.sleep(1.2)
            st.rerun()

        synced = (config.get("kpSyncedAt") or "")[:19].replace("T", " ")
        c2.caption(f"Последняя загрузка: {synced} UTC" if synced else
                   "Города из таблицы ещё не загружались.")
        st.caption("Загрузка ЗАМЕНЯЕТ список стран и городов данными из таблицы. "
                   "Карточки со статусом «Удалена» не попадают.")


def tab_cities(project_id: str, config: dict) -> None:
    _cities_source_block(project_id, config)
    html('<div class="card-title">Страны и города проекта</div>')
    st.caption("Ссылка города – адрес карточки Яндекс.Бизнеса. Подойдёт любой вид "
               "(/edit/, /edit/photos/, /p/edit/posts/) – Click сам приведёт его к разделу «Посты».")

    with st.expander("➕ Добавить страну"):
        c1, c2 = st.columns([3, 1])
        new_country = c1.text_input("Название страны", key="new-country-name", label_visibility="collapsed",
                                    placeholder="Например: Казахстан")
        if c2.button("Добавить", key="btn-add-country", use_container_width=True) and new_country.strip():
            name = new_country.strip()
            if any(c["name"].lower() == name.lower() for c in config["countries"]):
                st.warning("Такая страна уже есть")
            else:
                config["countries"].append({"id": f"c-{project_id}-{_slug(name)}-{int(time.time())}",
                                            "name": name, "cities": []})
                save_config(project_id)
                st.rerun()

    html(T.tile_css([
        (f"tile-row-city-{n}", row_vars(
            country, None,
            "свернуть ▾" if st.session_state.get(f"cities-open-{country['id']}") == country["id"]
            else "изменить ▸"))
        for n, country in enumerate(config["countries"])
    ]))
    for n, country in enumerate(list(config["countries"])):
        # ВАЖНО: st.expander рисует содержимое ВСЕГДА, даже свёрнутый – он лишь прячет
        # его стилями. С 117 городами это сотни виджетов на каждый клик, отсюда тормоза.
        # Поэтому раскрытие своё: содержимое строится только для открытой страны.
        open_key = f"cities-open-{country['id']}"
        is_open = st.session_state.get(open_key) == country["id"]
        with st.container(key=f"tile-row-city-{n}"):
            st.button(country["name"], key=f"cities-toggle-{country['id']}",
                      use_container_width=True,
                      type="primary" if is_open else "secondary",
                      on_click=_toggle_open, args=(open_key, country["id"]))
        if not is_open:
            continue
        with st.container(border=True):
            tab_add, tab_bulk = st.tabs(["Один город", "Списком"])

            with tab_add:
                c1, c2, c3 = st.columns([2, 4, 1])
                name = c1.text_input("Город", key=f"add-city-name-{country['id']}")
                url = c2.text_input("Ссылка на карточку", key=f"add-city-url-{country['id']}")
                c3.write("")
                if c3.button("＋", key=f"add-city-btn-{country['id']}", use_container_width=True):
                    dup = _city_duplicate(config, url, name, country["id"])
                    if not (name.strip() and url.strip()):
                        st.warning("Нужны и название, и ссылка")
                    elif dup:
                        st.error(f"Не добавлено – {dup}.")
                    else:
                        country["cities"].append({
                            "id": f"ct-{_slug(name)}-{int(time.time() * 1000)}",
                            "name": name.strip(), "url": url.strip(),
                        })
                        save_config(project_id)
                        st.rerun()

            with tab_bulk:
                st.caption("По строке на город: `Название | ссылка` или `ссылка | Название`.")
                bulk = st.text_area("Списком", key=f"bulk-{country['id']}", height=120,
                                    label_visibility="collapsed")
                if st.button("Добавить списком", key=f"bulk-btn-{country['id']}"):
                    added = 0
                    for line in (bulk or "").splitlines():
                        parts = [p.strip() for p in re.split(r"[|\t]", line) if p.strip()]
                        if len(parts) < 2:
                            continue
                        if parts[0].startswith("http"):
                            url_, name_ = parts[0], parts[1]
                        else:
                            name_, url_ = parts[0], parts[1]
                        if not url_.startswith("http"):
                            continue
                        country["cities"].append({
                            "id": f"ct-{_slug(name_)}-{int(time.time() * 1000)}-{added}",
                            "name": name_, "url": url_,
                        })
                        added += 1
                    if added:
                        save_config(project_id)
                        st.toast(f"Добавлено городов: {added}")
                        st.rerun()

            st.divider()
            for i, city in enumerate(country["cities"]):
                c1, c2 = st.columns([9, 1])
                with c1:
                    bad = "" if yb.extract_company_id(city["url"]) else \
                        ' <span class="badge badge-danger">нет ID компании в ссылке</span>'
                    html(f'<div class="city-row"><span class="city-row-num">{i + 1}</span>'
                         f'<span class="city-row-name">{T.esc(city["name"])}</span>'
                         f'<span class="city-row-url">{T.esc(city["url"])}</span>{bad}</div>')
                with c2:
                    with st.container(key=f"danger-del-city-{city['id']}"):
                        if st.button("✕", key=f"del-city-{city['id']}", use_container_width=True):
                            country["cities"].remove(city)
                            save_config(project_id)
                            st.rerun()

            st.divider()
            with st.container(key=f"danger-del-country-{country['id']}"):
                if st.button(f"Удалить страну «{country['name']}»", key=f"del-country-{country['id']}"):
                    config["countries"].remove(country)
                    save_config(project_id)
                    st.rerun()


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: ОТЧЁТ
# ════════════════════════════════════════════════════════════════════

_FILTERS = {"all": "Все", "ok": "✅ Успешно", "no-image": "🟡 Без картинки",
            "unknown": "⚠️ Проверьте", "failed": "❌ Ошибки", "skipped-duplicate": "⏭ Пропущено"}


def _report_csv(data: dict) -> bytes:
    rows = ["Страна;Город;Статус;Причина;Время_сек;URL"]
    for r in data.get("results") or []:
        reason = re.sub(r"[;\r\n]", " ", (r.get("reason") or ""))
        if r.get("imageError"):
            reason += f' · фото: {r["imageError"]}'
        rows.append(";".join([
            str(r.get("country") or r.get("package") or ""),
            str(r.get("cityName") or ""),
            str(r.get("status") or ""),
            reason,
            f'{(r.get("durationMs") or 0) / 1000:.1f}',
            str(r.get("companyUrl") or ""),
        ]))
    return ("﻿" + "\n".join(rows)).encode("utf-8")


def tab_report(project_id: str) -> None:
    reports = runner.list_reports(project_id, "publish")
    if not reports:
        html(T.empty("📊", "Отчётов пока нет", "Отчёт появится после первой публикации."))
    else:
        c1, c2 = st.columns([3, 1])
        with c1:
            names = [r["name"] for r in reports]

            def _label(name: str) -> str:
                r = next(x for x in reports if x["name"] == name)
                t = r["totals"] or {}
                when = local_time(r.get("finishedAt"))
                return f'{when} · ✅ {t.get("ok", 0)}/{t.get("total", 0)}'

            selected = st.selectbox("Отчёт", names, format_func=_label, key="report-select")
        with c2:
            st.write("")
            if st.button("🔄 Обновить список", use_container_width=True, key="btn-refresh-reports"):
                st.rerun()

        data = runner.read_report(project_id, "publish", selected)
        if data:
            totals = data.get("totals") or {}
            html(T.report_summary(totals, data.get("durationSec")))

            notes = []
            if data.get("stoppedByUser"):
                notes.append("⏹ Прогон был остановлен вручную – часть городов не обработана.")
            if data.get("state") == "crashed":
                notes.append("💥 Прогон упал: отчёт содержит всё, что успели сделать до падения.")
            if data.get("state") == "in-progress":
                notes.append("⏳ Прогон ещё идёт – отчёт обновляется после каждого города.")
            if totals.get("unknown"):
                notes.append(f'⚠️ {cities_word(totals["unknown"])} с неподтверждённой публикацией. '
                             "Клик «Создать» был сделан, но Яндекс не подтвердил. "
                             "Проверьте вручную – повторять автоматически опасно (дубль).")
            if totals.get("skipped"):
                notes.append(f'⏭ {cities_word(totals["skipped"])} пропущено: этот же текст уже уходил '
                             "в эти карточки недавно (защита от дублей).")
            if totals.get("retried"):
                notes.append(f'⚡ {cities_word(totals["retried"])} удалось со второй попытки.')
            for n in notes:
                st.caption(n)

            f_cols = st.columns(len(_FILTERS))
            current = st.session_state.get("report-filter", "all")
            for col, (key, label) in zip(f_cols, _FILTERS.items()):
                if col.button(label, key=f"rf-{key}", use_container_width=True,
                              type="primary" if current == key else "secondary"):
                    st.session_state["report-filter"] = key
                    st.rerun()
            current = st.session_state.get("report-filter", "all")

            results = [r for r in (data.get("results") or [])
                       if current == "all" or r.get("status") == current]

            by_country: dict[str, list[dict]] = {}
            for r in results:
                by_country.setdefault(r.get("country") or r.get("package") or "–", []).append(r)

            for country, rows in by_country.items():
                ok = sum(1 for r in rows if r.get("status") == "ok")
                bad = sum(1 for r in rows if r.get("status") == "failed")
                with st.expander(f"{country} – {ok}/{len(rows)} успешно"
                                 + (f" · {plural(bad, 'ошибка', 'ошибки', 'ошибок')}" if bad else ""),
                                 expanded=current != "all" or bad > 0):
                    for r in rows:
                        html(T.report_row(r))

            st.download_button("⬇️ Выгрузить CSV", data=_report_csv(data),
                               file_name=selected.replace(".json", ".csv"), mime="text/csv",
                               key="btn-csv")

    st.divider()
    html('<div class="card-title">📄 Логи</div>')
    logs = runner.list_logs(project_id)
    if not logs:
        st.caption("Логов пока нет.")
    for name in logs:
        with st.expander(name):
            html(T.log_box(runner.read_log(project_id, name)[-40_000:]))


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: НАСТРОЙКИ
# ════════════════════════════════════════════════════════════════════

def tab_settings(project_id: str, config: dict) -> None:
    settings = get_settings(project_id)
    project = PROJECTS[project_id]

    html('<div class="card-title">🔑 Доступ к Яндекс.Бизнесу</div>')
    c1, c2 = st.columns(2)
    email = c1.text_input("Email аккаунта Яндекса", value=config.get("email", ""), key="set-email")
    password = c2.text_input("Пароль (нужен только для авто-входа)", value=config.get("password", ""),
                             type="password", key="set-password")
    if email != config.get("email") or password != config.get("password"):
        config["email"] = email
        config["password"] = password
        save_config(project_id)
    st.caption(f"Проект {project['name']} рассчитан на аккаунт **{project['yandexEmail']}**. "
               "Click сверит его с тем, что реально залогинен, и не даст опубликовать не туда.")

    # Один и тот же ящик у Яндекса открывается и как @yandex.ru, и как
    # @yandex.com – но это РАЗНЫЕ паспорта. Международный проверяет вход
    # строже и требует звонок на телефон вместо письма. У трёх проектов
    # стоял .ru и вход шёл спокойно, у одного .com – и упирался в телефон.
    fixed = _ru_domain(email)
    if fixed != email:
        st.warning(f"Адрес **{email}** уводит Click в международный паспорт Яндекса – "
                   "там вход подтверждается звонком на телефон, а не письмом. "
                   f"Тот же ящик по-русски: **{fixed}**.")
        if st.button(f"Исправить на {fixed}", key="fix-domain", type="primary"):
            config["email"] = fixed
            save_config(project_id)
            st.rerun()

    st.divider()
    _yandex_login_block(project_id, config)

    st.divider()
    engine = yb.current_engine()
    c1, c2 = st.columns([1, 3])
    if c1.button("Проверить браузер", key="btn-check-browser", use_container_width=True):
        try:
            with st.spinner("Пробую запустить браузер… Первый запуск может занять 1-3 минуты."):
                chosen = get_worker().call(yb.resolve_engine, None)
            st.success(f"Браузер работает: {chosen}")
        except Exception as e:  # noqa: BLE001
            _browser_error(e)
    c2.caption(f"Браузер: **{engine}**" if engine else "Браузер ещё не запускался.")

    st.caption("⚠️ В облаке файловая система временная: при перезапуске приложения пропадут "
               "сессия Яндекса и отчёты.")

    with st.container(key="danger-logout"):
        if st.button("Выйти из проекта", key="btn-logout"):
            st.query_params.clear()
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()


def _browser_error(exc: Exception) -> None:
    """
    Playwright при неудачном старте отдаёт несколько тысяч строк лога запуска.
    Показываем только суть плюс подсказку, а полный лог прячем под спойлер.
    """
    text = str(exc)
    if isinstance(exc, RuntimeError) and "Ни один браузер не запустился" in text:
        st.error(text)                      # это уже наше готовое сообщение с подсказкой
    else:
        st.error(f"Не удалось открыть браузер: {yb._short_error(exc)}")  # noqa: SLF001
        if "shared libraries" in text or "libgbm" in text:
            st.info(
                "Браузеру не хватает системной библиотеки. Click должен был сам перейти на "
                "запасной движок – если этого не произошло, перезапустите приложение "
                "(в облаке: Manage app → Reboot) или задайте переменную `CLICK_BROWSER=firefox`."
            )
    with st.expander("Технические подробности"):
        st.code(text[:6000])


def _yandex_login_block(project_id: str, config: dict) -> None:
    """
    Вход в Яндекс. Основной путь – автоматический: логин и пароль уже есть
    в «Настройках», Click сам читает, что просит страница, и заполняет.
    Человек нужен только там, где иначе нельзя: код, капча, подтверждение
    в приложении – тогда показываем снимок экрана и обычные поля.
    """
    worker = get_worker()
    html('<div class="card-title">🔐 Вход в Яндекс</div>')
    headless_login = bool(get_settings(project_id)["headless"])

    if can_show_browser():
        st.checkbox("Показывать окно браузера – видно, как идёт публикация",
                    value=bool(st.session_state.get(HEADED_KEY)),
                    key="show-browser", on_change=_remember_headed,
                    help="Только на своём компьютере. В облаке экрана нет, окно показать негде.")

    if yb.has_saved_session(project_id):
        st.success("Сессия Яндекса сохранена – публикация пойдёт в фоне без повторного входа.")
        if st.button("Проверить сессию", key="btn-check-session"):
            with st.spinner("Открываю профиль Яндекса…"):
                who = _check_session(project_id, config.get("email") or project["yandexEmail"])
            if who["state"] == "ok":
                st.success(f"В Яндексе залогинен {who['account']} – всё верно.")
            elif who["state"] == "other":
                st.error(f"В Яндексе залогинен другой аккаунт: {who['account']}. "
                         "Сбросьте сессию и войдите заново.")
            elif who["state"] == "anonymous":
                st.error("Сессия недействительна: Яндекс показывает форму входа. Войдите заново.")
            else:
                st.warning("Определить аккаунт не удалось – "
                           + (who.get("error") or "страница профиля ничего не отдала"))
        with st.container(key="danger-reset-session"):
            if st.button("Войти заново (сбросить сессию)", key="yb-reset"):
                yb.session_path(project_id).unlink(missing_ok=True)
                for k in ("yb_flow", "yb_state", "yb_step"):
                    st.session_state.pop(k, None)
                st.rerun()
        return

    step = st.session_state.get("yb_step", "idle")

    email = (config.get("email") or "").strip()
    password = config.get("password") or ""

    if step == "idle":
        if email:
            st.caption(f"Click подставит логин **{email}** и попросит Яндекс прислать "
                       "письмо для входа. Пароль вводить не будет: после пароля Яндекс "
                       "сам решает, чем подтвердить вход, и обычно требует звонок на телефон.")
        else:
            st.caption("Заполните email аккаунта выше – тогда Click сможет войти сам. "
                       "Без него вход будет пошаговым: по снимку экрана.")
        if yb.current_engine() is None:
            st.caption("⏳ Самый первый вход дольше обычного: Click докачивает браузер (1-3 минуты). "
                       "Дальше он открывается за секунды.")

        c1, c2 = st.columns([2, 1])
        # Основной способ – подтверждение с почты. Пароль остаётся на своём
        # шаге: если понадобится, его можно ввести руками.
        auto = c1.button("✉️ Войти (подтверждение с почты)", type="primary", key="yb-auto",
                         disabled=not email, use_container_width=True,
                         help="Click дойдёт до экрана пароля и нажмёт там «Отправить письмо "
                              "для входа». Ссылка придёт на почту аккаунта.")
        manual = c2.button("Вручную", key="yb-start", use_container_width=True)
        by_mail = auto
        if not (auto or manual):
            return

        old = st.session_state.get("yb_flow")
        if old is not None:
            try:
                worker.call(old.close)
            except Exception:
                pass
        try:
            with st.spinner("Открываю браузер… Первый запуск может занять 1-3 минуты – Click скачивает браузер."):
                flow = yb.YbLoginFlow(project_id, headless=headless_login)
                state = worker.call(flow.start)
        except Exception as e:  # noqa: BLE001
            _browser_error(e)
            return
        st.session_state.yb_flow = flow
        st.session_state.yb_state = state
        st.session_state.yb_step = "manual"

        if manual:
            st.rerun()

        # ── Автоматический вход ──
        spinner = "Подставляю логин и прошу Яндекс прислать письмо…"
        try:
            with st.spinner(spinner):
                res = worker.call(flow.auto_login, email, password, 6, by_mail)
        except Exception as e:  # noqa: BLE001
            _browser_error(e)
            return
        if res.get("ok"):
            _finish_login(project_id, worker, flow)
            return
        # Не дошли сами – показываем, на чём встали, и передаём человеку
        st.session_state.yb_note = res.get("reason") or ""
        fresh = worker.call(flow.state)
        if res.get("step") == "mail-sent" and fresh.get("step") != "done":
            fresh["step"] = "mail-sent"
        st.session_state.yb_state = fresh
        st.rerun()

    flow: yb.YbLoginFlow = st.session_state.yb_flow
    _login_steps(project_id, worker, flow, email, password)


# Что писать над полем на каждом шаге. Шаг определяет САМА страница Яндекса –
# раньше он угадывался один раз после авто-входа и больше не пересматривался,
# из-за чего на экране «Введите логин» приложение показывало графы «Пароль» и
# «Код»: логин вводить было буквально некуда.
_STEP_TITLES = {
    "login":     ("Яндекс просит логин", "Логин или e-mail"),
    "password":  ("Яндекс просит пароль", "Пароль"),
    "code":      ("Яндекс просит код", "Код из SMS / письма / звонка"),
    "phone":     ("Яндекс просит номер телефона", "Номер телефона"),
    "captcha":   ("Яндекс показывает капчу", "Символы с картинки"),
    "challenge": ("Яндекс ждёт подтверждения", ""),
    "mail-sent": ("Письмо для входа отправлено", ""),
    "mail-wait": ("Яндекс ждёт перехода по ссылке из письма", ""),
    "unknown":   ("Click не разобрал экран", ""),
}


def _login_controls(project_id: str, worker, flow) -> dict | None:
    """
    Кнопки, которые нужны на ЛЮБОМ шаге входа.

    «Назад» – чтобы уйти с экрана, который навязал Яндекс (звонок на телефон),
    обратно к паролю: там есть «Отправить письмо для входа». Без неё выбора у
    человека нет вообще – только вводить то, что требуют.
    «Остановить вход» – закрыть браузер и начать заново.
    """
    new_state = None
    c1, c2 = st.columns(2)
    if c1.button("← Назад на прошлый экран", key="yb-back", use_container_width=True):
        with st.spinner("Возвращаюсь…"):
            new_state = worker.call(flow.go_back)
    with c2, st.container(key="danger-stop-login"):
        if st.button("⏹ Остановить вход", key="yb-abort", use_container_width=True):
            try:
                worker.call(flow.close)
            except Exception:  # noqa: BLE001
                pass
            for k in ("yb_flow", "yb_state", "yb_step", "yb_note"):
                st.session_state.pop(k, None)
            st.rerun()
    return new_state


def _login_steps(project_id: str, worker, flow, email: str, password: str) -> None:
    """Пошаговый вход. Поле на экране всегда одно – ровно то, что просит Яндекс."""
    note = st.session_state.pop("yb_note", "")
    if note:
        st.info(note)

    state = st.session_state.get("yb_state") or {}
    step = state.get("step", "unknown")
    if step == "done":
        _finish_login(project_id, worker, flow)
        return
    if state.get("screenshot"):
        st.image(state["screenshot"], caption="Что сейчас на экране Яндекса")

    heading, field_label = _STEP_TITLES.get(step, _STEP_TITLES["unknown"])
    st.markdown(f"**{heading}**")
    if state.get("title"):
        st.caption(state["title"])

    new_state = None
    try:
        if step == "login":
            value = st.text_input(field_label, value=email, key="yb-login")
            if st.button("Отправить логин", key="yb-submit-login", type="primary") and value:
                with st.spinner("Отправляю логин…"):
                    new_state = worker.call(flow.submit_login, value)

        elif step == "password":
            value = st.text_input(field_label, value=password, type="password", key="yb-password")
            c1, c2 = st.columns(2)
            if c1.button("Войти по паролю", key="yb-submit-password",
                         type="primary", use_container_width=True) and value:
                with st.spinner("Проверяю пароль…"):
                    new_state = worker.call(flow.submit_password, value)
            # Запасные пути с этого же экрана – когда пароль не подходит или
            # Яндекс упирается в бесконечную проверку.
            if c2.button("Отправить письмо для входа", key="yb-login-mail",
                         use_container_width=True):
                with st.spinner("Прошу Яндекс прислать письмо…"):
                    new_state = worker.call(flow.send_login_email)
                st.info("Письмо со ссылкой ушло на почту аккаунта. Откройте его на любом "
                        "устройстве, подтвердите вход и нажмите «Обновить экран».")
            if st.button("Войти с помощью SMS", key="yb-login-sms"):
                with st.spinner("Прошу Яндекс прислать SMS…"):
                    new_state = worker.call(flow.send_sms_code)

        elif step == "code":
            value = st.text_input(field_label, key="yb-code")
            if st.button("Подтвердить код", key="yb-submit-code", type="primary") and value:
                with st.spinner("Проверяю код…"):
                    new_state = worker.call(flow.submit_code, value)
            # Звонок на телефон Яндекс навязывает сам. Уйти с этого экрана
            # можно только назад – к паролю, где есть вход по письму.
            st.caption("Не подходит этот способ? Вернитесь назад – на экране пароля "
                       "есть «Отправить письмо для входа».")
            if st.button("Другой способ входа", key="yb-another-code"):
                with st.spinner("Открываю список способов…"):
                    new_state = worker.call(flow.another_way)

        elif step == "phone":
            st.caption("Click сам переключит на вход по логину – нажмите кнопку. "
                       "Если не выйдет, на снимке это «Ещё» → «Войти по логину».")
            if st.button("Перейти ко входу по логину", key="yb-to-login", type="primary"):
                with st.spinner("Переключаю…"):
                    worker.call(flow._switch_to_login_by_password)  # noqa: SLF001
                    new_state = worker.call(flow.state)

        elif step in ("mail-sent", "mail-wait"):
            st.success("Письмо со ссылкой для входа отправлено на почту аккаунта. "
                       "Откройте его на любом устройстве, подтвердите вход и нажмите "
                       "«Обновить экран» внизу.")
            if st.button("Другой способ входа", key="yb-another-mail"):
                with st.spinner("Открываю список способов…"):
                    new_state = worker.call(flow.another_way)

        elif step == "challenge":
            st.caption("На экране нет полей – Яндекс просто ждёт подтверждения. "
                       "Кнопка нажмёт то же, что вы нажали бы сами.")
            if st.button("Подтвердить", key="yb-confirm", type="primary"):
                with st.spinner("Подтверждаю…"):
                    new_state = worker.call(flow.press_confirm)

        elif step == "captcha":
            value = st.text_input(field_label, key="yb-captcha")
            if st.button("Отправить", key="yb-submit-captcha", type="primary") and value:
                with st.spinner("Отправляю…"):
                    new_state = worker.call(flow.submit_login, value)

        else:  # unknown – показываем всё сразу, чтобы не оказаться в тупике
            st.caption("Экран непонятный. Введите то, что просит Яндекс на снимке, "
                       "или нажмите «Продолжить».")
            c1, c2 = st.columns(2)
            free = c1.text_input("Что ввести", key="yb-free")
            if c1.button("Отправить", key="yb-submit-free") and free:
                with st.spinner("Отправляю…"):
                    new_state = worker.call(flow.submit_login, free)
            if c2.button("Продолжить (нажать кнопку на экране)", key="yb-just-next"):
                with st.spinner("Жму кнопку…"):
                    new_state = worker.call(flow.press_confirm)
    except Exception as e:  # noqa: BLE001
        st.error(str(e) if isinstance(e, RuntimeError) else f"Ошибка: {type(e).__name__}: {e}")
        try:
            st.session_state.yb_state = worker.call(flow.state)
        except Exception:  # noqa: BLE001
            pass
        return

    st.caption("Подтвердили вход на телефоне или по письму? Нажмите «Обновить экран».")
    if st.button("Обновить экран", key="yb-check", type="primary"):
        with st.spinner("Смотрю, что на экране…"):
            new_state = worker.call(flow.state)

    st.divider()
    back = _login_controls(project_id, worker, flow)
    if back is not None:
        new_state = back

    if new_state is None:
        return

    st.session_state.yb_state = new_state
    if new_state.get("step") == "done":
        _finish_login(project_id, worker, flow)
    st.rerun()


def _check_session(project_id: str, expected_email: str) -> dict:
    """Открыть профиль Яндекса сохранённой сессией и честно сказать, кто залогинен."""
    worker = PlaywrightWorker()
    try:
        browser = yb.YbBrowser(project_id, headless=True)
        worker.call(browser.start)
        try:
            res = worker.call(yb.verify_account, browser.page, expected_email)
        finally:
            worker.call(browser.close)
        emails = res.get("emails") or []
        return {"state": res.get("state", "unknown"),
                "account": (expected_email if res.get("state") == "ok" else ", ".join(emails)) or "не определено"}
    except Exception as e:  # noqa: BLE001
        return {"state": "unknown", "account": "", "error": f"{type(e).__name__}: {e}"}
    finally:
        worker.stop()


def _finish_login(project_id: str, worker, flow) -> None:
    """
    Сессия получена: сохраняем, закрываем браузер, чистим состояние шага.

    Сохранение проверяем по факту: раньше приложение писало «Вход выполнен»
    просто потому, что дошло сюда – и после неудачного входа сохраняло
    анонимные куки, а на публикации выяснялось, что в Яндексе никого нет.
    """
    # Куки авторизации нет – браузер НЕ закрываем: человек продолжит с того же
    # места пошагово. Закрыть его здесь означало бы «начните всё сначала».
    try:
        if not worker.call(flow.has_auth_cookie):
            st.session_state.yb_step = "manual"
            st.session_state.yb_note = (
                "Вход ещё не завершён: Яндекс не выдал куки авторизации. Посмотрите "
                "на снимок ниже – обычно осталась непройденная проверка: код из "
                "письма, капча или подтверждение в приложении.")
            try:
                st.session_state.yb_state = worker.call(flow.state)
            except Exception:  # noqa: BLE001
                pass
            st.rerun()
            return
    except Exception:  # noqa: BLE001
        pass

    account = None
    try:
        account = worker.call(flow.current_account)
    except Exception:
        pass
    try:
        worker.call(flow.save_session)
    finally:
        try:
            worker.call(flow.close)
        except Exception:
            pass
    for k in ("yb_flow", "yb_state", "yb_step", "yb_note"):
        st.session_state.pop(k, None)
    st.success("Вход выполнен, сессия сохранена." + (f" Аккаунт: {account}" if account else ""))


# ════════════════════════════════════════════════════════════════════
#  ГЛАВНЫЙ ЭКРАН
# ════════════════════════════════════════════════════════════════════

def show_main(project_id: str) -> None:
    inject_css()
    ensure_dirs(project_id)
    config = get_config(project_id)
    project = PROJECTS[project_id]

    with st.container(key="click-topbar"):
      head, badge, ctrl = st.columns([9, 3, 1])
      with head:
        html(T.topbar(None, status_pills(project_id)))
      with badge, st.container(key="projbadge"):
        # Плашка проекта – это кнопка: по нажатию открывается «Сменить проект».
        html(f'<style>.st-key-projbadge{{--dot:{project["color"]}}}</style>')
        with st.popover(f'{project["name"]} – {project["fullName"]}', use_container_width=True):
            st.caption(f'Аккаунт Яндекса: {config.get("email") or "не задан"}')
            # Версия видна прямо в приложении: иначе после скачивания архива
            # не понять, свежий он или старый – на этом уже обожглись.
            st.caption(f"Версия: {UI_BUILD}")
            if st.button("↻ Сменить проект", key="btn-switch", use_container_width=True):
                st.query_params.clear()
                for k in list(st.session_state.keys()):
                    del st.session_state[k]
                st.rerun()
      with ctrl:
        st.button("🌙" if theme() == "dark" else "☀️", key="btn-theme", use_container_width=True,
                  help="Переключить тему", on_click=_toggle_theme)

    # ВАЖНО: активный раздел держим в СВОЁМ ключе, а не только в ключе виджета.
    # Streamlit удаляет состояние виджетов, которые не успели отрисоваться в прогоне
    # (а кнопка темы выше делает st.rerun() до радио) – и вкладка сбрасывалась на первую.
    current = st.session_state.get("section_name", SECTIONS[0])
    if current not in SECTIONS:
        current = SECTIONS[0]
    with st.container(key="click-tabs"):
        section = st.radio("Раздел", SECTIONS, index=SECTIONS.index(current), horizontal=True,
                           label_visibility="collapsed",
                           key=f"main-section-{st.session_state.get('nav-gen', 0)}")
    st.session_state["section_name"] = section

    if section == SECTIONS[0]:
        tab_run(project_id, config)
    elif section == SECTIONS[1]:
        tab_compose(project_id, config)
    elif section == SECTIONS[2]:
        tab_actualize(project_id, config)
    elif section == SECTIONS[3]:
        tab_cities(project_id, config)
    elif section == SECTIONS[4]:
        tab_report(project_id)
    else:
        tab_settings(project_id, config)


def main() -> None:
    project_id = st.session_state.get("current_project_id")
    if not project_id:
        project_id = _project_from_url()          # переживает F5, в отличие от session_state
        if project_id:
            ensure_dirs(project_id)
            st.session_state.current_project_id = project_id
    if project_id:
        show_main(project_id)
    else:
        show_login()


main()
