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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

import kp_audit
import kp_sheet
import llm
import paths
import projects_data as pdata
import repo_store
import reviews as rv
import runner
import ui_theme as T
import yb_playwright as yb
import playwright_worker
from playwright_worker import PlaywrightWorker

# ВСЕ модули обязаны быть из одной сборки. Облако умеет обновить главный
# скрипт «на лету», оставив соседние модули в памяти прежними – и тогда новая
# страница зовёт функцию, которой в старом модуле ещё нет. Сначала это ловили
# только для ui_theme (разъезжались вёрстка и CSS), потом ровно так же
# посыпались отзывы: страница уже знала про looks_cut_off, а reviews в памяти –
# нет, и вкладка падала с AttributeError.
#
# Поэтому метка одна на всех: не совпала – перезагружаем модуль сами.
# Порядок важен, зависимости идут раньше зависимых, иначе runner останется со
# ссылкой на старый yb_playwright.
from build import BUILD as UI_BUILD


def _same_build(mod) -> bool:
    return getattr(mod, "BUILD", "") == UI_BUILD


# Список ПОЛНЫЙ: любой модуль, оставшийся в памяти старым, ломает своё. Так
# заказчик перезагрузила города из таблицы, а Click опять взял старый лист –
# kp_sheet в списке не было, и правка просто не работала.
_MODULES = ("build", "paths", "repo_store", "projects_data", "kp_sheet", "kp_audit",
            "playwright_worker", "ui_theme", "yb_playwright", "reviews", "llm", "runner")

if not all(_same_build(m) for m in (paths, repo_store, pdata, kp_sheet, kp_audit,
                                    playwright_worker, T, yb, rv, llm, runner)):
    import importlib

    for _name in _MODULES:                # порядок: зависимости раньше зависимых
        try:
            importlib.reload(sys.modules[_name])
        except Exception:  # noqa: BLE001 – без перезагрузки хуже, но падать нельзя
            pass
    import kp_audit  # noqa: F811
    import playwright_worker  # noqa: F811
    import kp_sheet  # noqa: F811
    import llm  # noqa: F811
    import paths  # noqa: F811
    import projects_data as pdata  # noqa: F811
    import repo_store  # noqa: F811
    import reviews as rv  # noqa: F811
    import runner  # noqa: F811
    import ui_theme as T  # noqa: F811
    import yb_playwright as yb  # noqa: F811
    from playwright_worker import PlaywrightWorker  # noqa: F811

ROOT = Path(__file__).parent
USERS_DATA = paths.data_root()

st.set_page_config(page_title="Click – публикация постов", page_icon="📮", layout="wide")

SALT = "click-salt-v1-2026"
SECTIONS = ["🚀 Запуск", "📤 Публикация", "🔄 Актуализация", "🏙 Города", "📊 Отчёт",
            "⚙️ Настройки", "🔎 Сверка"]


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
    # АПС – КП в Google-таблице ещё не доделана, поэтому города зашиты списком.
    # Когда таблица будет готова, она их перекроет («Города» → «Источник городов»).
    "APS": {"id": "APS", "name": "АПС", "fullName": "Авиапромсталь", "color": "#06b6d4", "icon": "✈",
            "yandexEmail": "aviastalru@yandex.ru", "passwordHash": _hash("2727"),
            "presetCities": pdata.APS_CITIES, "endings": pdata.APS_ENDINGS},
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


def _pull_session(project_id: str) -> None:
    """
    Возврат сессии после перезапуска облака.

    Файловая система Streamlit Cloud временная: куки исчезали при каждом
    рестарте, и вход приходилось проходить заново – «выкидывает». Копия
    сессии лежит в приватном хранилище проекта (та же ветка, что города и
    настройки) и восстанавливается, когда локального файла нет.
    """
    if not repo_store.is_configured():
        return
    pairs = ((f"session-{project_id}", yb.session_path(project_id)),
             (f"device-{project_id}", yb.device_path(project_id)))
    for name, path in pairs:
        if path.exists() and path.stat().st_size > 2:
            continue
        try:
            data = repo_store.load(name)
            if data and data.get("cookies"):
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def _push_session(project_id: str) -> None:
    """Свежие куки – в хранилище. Прогон продлевает их, копия не должна отставать."""
    if not repo_store.is_configured():
        return
    pairs = ((f"session-{project_id}", yb.session_path(project_id), True),
             (f"device-{project_id}", yb.device_path(project_id), False))
    for name, path, need_auth in pairs:
        if not path.exists() or (need_auth and not yb.has_saved_session(project_id)):
            continue
        mark = f"_pushed_{name}"
        mtime = path.stat().st_mtime
        if st.session_state.get(mark) == mtime:
            continue
        try:
            repo_store.save(name, json.loads(path.read_text(encoding="utf-8")),
                            f"Click: сессия Яндекса ({project_id})")
            st.session_state[mark] = mtime
        except Exception:  # noqa: BLE001
            pass


def _pull_ledger(project_id: str) -> None:
    """
    Реестр опубликованного – тоже наружу.

    Это вторая защита от дублей: «этот текст уже уходил в этот город». В
    облаке файлы стираются при перезапуске, и после ребута тот же прогон
    публиковал ВСЁ заново. Копия лежит рядом с городами и настройками.
    """
    if not repo_store.is_configured():
        return
    fp = runner.p_ledger(project_id)
    if fp.exists() and fp.stat().st_size > 2:
        return
    try:
        data = repo_store.load(f"ledger-{project_id}")
        lines = (data or {}).get("lines") or []
        if lines:
            fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _push_ledger(project_id: str) -> None:
    if not repo_store.is_configured():
        return
    fp = runner.p_ledger(project_id)
    if not fp.exists():
        return
    mark, mtime = f"_pushed_ledger_{project_id}", fp.stat().st_mtime
    if st.session_state.get(mark) == mtime:
        return
    try:
        lines = [ln for ln in fp.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        repo_store.save(f"ledger-{project_id}", {"lines": lines[-5000:]},
                        f"Click: реестр публикаций ({project_id})")
        st.session_state[mark] = mtime
    except Exception:  # noqa: BLE001
        pass


def _forget_session(project_id: str) -> None:
    """«Войти заново»: стираем сессию и локально, и в хранилище."""
    yb.session_path(project_id).unlink(missing_ok=True)
    if repo_store.is_configured():
        try:
            repo_store.save(f"session-{project_id}", {},
                            f"Click: сессия сброшена ({project_id})")
        except Exception:  # noqa: BLE001
            pass


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
                   with_cities: bool = False, cities: dict | None = None,
                   queued: dict[str, int] | None = None) -> list[dict]:
    """
    Выбор стран карточками, как в оригинале: флаг, название, «N гор.».
    Сама карточка – HTML (Streamlit такого не умеет), клик – кнопкой под ней.

    queued – сколько раз страна уже в очереди: такая плитка получает зелёную
    рамку и «✓ в очереди», ровно как .country-tile.in-queue оригинала.
    """
    countries = config["countries"]
    cities = cities if cities is not None else {}
    queued = queued or {}
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
        def tile_vars(c: dict) -> dict:
            n_q = queued.get(c["id"], 0)
            v = {"--flag": T.flag_data_uri(c["name"]),
                 "--meta": T.css_text(f'{len(c["cities"])} гор.')}
            if n_q:
                mark = "в очереди" + (f" ×{n_q}" if n_q > 1 else "")
                v.update({
                    "--meta": T.css_text(f'{len(c["cities"])} гор. · {mark}'),
                    "--meta-c": "var(--grn)", "--qshow": "flex",
                    "--qbord": "var(--grn)", "--qbg": "var(--grn-bg)",
                })
            return v

        html(T.tile_css([
            (f"tile-cc-{key_prefix}-{n}", tile_vars(c)) for n, c in enumerate(countries)
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

def _open_report(kind: str, run_id: str = "") -> None:
    """Уйти на вкладку «Отчёт» и показать там нужный вид отчёта."""
    st.session_state["report-kind"] = kind
    st.session_state.pop("report-select", None)   # выбор из другого вида не подойдёт
    goto_section(SECTIONS[4])
    st.rerun()


def _render_live_panel(project_id: str, was_running: bool = False) -> None:
    state = runner.read_state(project_id)
    status = state.get("status")
    action = state.get("action")
    kind = "publish" if action == "publish" else "actualize"
    action_ru = {"publish": "Публикация", "collect": "Чтение организаций"}.get(action, "Актуализация")
    running = status == "running"

    # Прогон только что закончился – перерисовываем страницу целиком, чтобы
    # разблокировалась кнопка запуска и обновились счётчики в шапке.
    if was_running and not running:
        st.rerun()

    if running:
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
        st.success(f"{action_ru} завершена." if action != "collect"
                   else "Организации прочитаны.")
    elif status == "stopped":
        st.warning(f"{action_ru} остановлена. Сделанное сохранено в отчёте.")
    elif status == "error":
        st.error(f"{action_ru} завершилась с ошибкой: {state.get('error') or 'см. лог'}")
    elif status == "interrupted":
        st.warning(state.get("error") or "Прогон был прерван.")

    # Прогон закончился – к отчёту. Кнопка нужна и после ОСТАНОВКИ: там тоже
    # есть что смотреть, а раньше она показывалась только после «done».
    # У чтения организаций отчёта нет – результат виден тут же, на «Сверке».
    if action != "collect" and status in ("done", "stopped", "error", "interrupted"):
        with st.container(key="go-report"):
            if st.button("📊 Посмотреть отчёт", key=f"btn-report-{state.get('runId', '')}"):
                _open_report(kind, state.get("runId", ""))

    # Пока идёт прогон лог нужен на виду, после – он только мешает: экран
    # длинный, а под ним отчёт. Сворачиваем.
    log_text = runner.read_live_log(project_id)
    if running or not log_text:
        html(T.log_box(log_text))
    else:
        with st.expander("📄 Показать лог прогона"):
            html(T.log_box(log_text))


def live_panel(project_id: str, running: bool) -> None:
    """
    Живая панель прогона – одна на обе вкладки.

    Пока прогон идёт, панель ОБЯЗАНА обновляться сама. На «Актуализации» она
    рисовалась без авто-обновления, и экран замирал на первом городе, хотя
    прогон шёл дальше: лог отставал на десятки городов, нажатая «Остановить»
    выглядела как «не сработала» (на деле runner останавливался сразу), а
    отчёт «не появлялся». Экран оживал только от постороннего действия –
    заказчик переключила тему, и всё разом обновилось.

    Поэтому обе вкладки ходят сюда: разойтись они больше не могут.
    """
    fragment = getattr(st, "fragment", None)
    if fragment and running:
        @fragment(run_every=2)
        def _live() -> None:
            _render_live_panel(project_id, was_running=True)
        _live()
        return
    _render_live_panel(project_id)
    if running:
        st.caption("Обновите страницу, чтобы увидеть свежий прогресс.")


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

    # ─── Живой лог: обновляется сам, пока идёт прогон ───
    live_panel(project_id, running)

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
    queued_count: dict[str, int] = {}
    for q in queue:
        queued_count[q["countryId"]] = queued_count.get(q["countryId"], 0) + 1
    selected_countries = country_picker("compose", config, title="🌍 Страны для публикации",
                                        with_cities=True, cities=per_country,
                                        queued=queued_count)

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

      # Фото в «Товары» имеют смысл только у отгрузки – так и в оригинале
      # (showProductPhotos = draft.postType === 'shipment'). У остальных типов
      # поле только путало: заливать товары к поздравлению незачем.
      product_photos_raw, goods_files = "", None
      if post_type == "shipment":
          with st.container(key="goods-row"):
              g1, g2 = st.columns([3, 1])
              product_photos_raw = g1.text_area(
                  "Фото в раздел «Товары» (необязательно)", height=118, key="compose-product-photos",
                  placeholder="Ссылки или пути, по одной в строке\n"
                              "Заливаются в карточку после успешной публикации поста",
              )
              goods_files = g2.file_uploader("Фото товаров", type=["jpg", "jpeg", "png", "gif", "webp"],
                                             accept_multiple_files=True,
                                             key=f"compose-goods-files-{st.session_state.get('upl-gen', 0)}",
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
                # Порт addToDraftQueue оригинала: выбор стран сбрасывается,
                # сама выбирается ПЕРВАЯ страна, которой ещё нет в очереди;
                # текст, тип и картинки остаются – пост едет дальше по странам.
                queued_ids = {q["countryId"] for q in queue}
                for c in config["countries"]:
                    st.session_state[f"compose-cb-{c['id']}"] = False
                nxt = next((c for c in config["countries"] if c["id"] not in queued_ids), None)
                if nxt is not None:
                    st.session_state[f"compose-cb-{nxt['id']}"] = True
                    st.session_state["compose-note"] = (
                        f"✓ Добавлено ({cities_word(sum(len(q['cityIds']) for q in queue[-added:]))}) · "
                        f"следующая страна: {nxt['name']}")
                else:
                    st.session_state["compose-note"] = (
                        "🎉 Все страны добавлены! Сохраните очередь – блок «В очереди к сохранению» ниже.")
            st.rerun()
    with c2:
        if not can_add:
            st.caption("Нужно: текст поста + хотя бы один выбранный город.")

    # Заметка живёт до следующего добавления, а не мигает тостом: после
    # перерисовки видно, ЧТО добавилось и какая страна выбралась сама.
    note = st.session_state.get("compose-note")
    if note:
        st.success(note)

    # ─── Очередь ───
    if queue:
        st.divider()
        html(f'<div class="card-title">📦 В очереди к сохранению '
             f'<span class="badge badge-accent">{len(queue)}</span> · '
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
                st.session_state.pop("compose-note", None)
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
                    st.session_state.pop("compose-note", None)
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
    """
    Какие города отмечены для актуализации.

    Набор надо СВЕРЯТЬ с текущим списком городов. После загрузки из КП
    (да и после ручного добавления) у городов новые id, а в наборе лежали
    старые: новые города оказывались невыбранными молча – отсюда «проверяет
    не все города». Правило простое, как в оригинале: город, которого раньше
    не было, считается выбранным; снятые вручную галочки сохраняются;
    исчезнувшие города из набора уходят.
    """
    ids = set(all_ids)
    sel = st.session_state.get("act-selected")
    if sel is None:
        st.session_state["act-selected"] = set(ids)
        st.session_state["act-known"] = set(ids)
        return st.session_state["act-selected"]

    known = st.session_state.get("act-known")
    if known is None:
        known = set(sel)
    fresh = (sel & ids) | (ids - known)        # новые города – сразу выбраны
    if fresh != sel:
        sel.clear()
        sel.update(fresh)
    st.session_state["act-known"] = ids
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


# ════════════════════════════════════════════════════════════════════
#  Очередь ответов на отзывы
# ════════════════════════════════════════════════════════════════════
#
# Отдельного раздела не заводим (решение заказчика: фича живёт внутри
# «Актуализации»), но очередь показываем ПЕРВОЙ – после прогона человеку
# нужна именно она, а не список городов.

_REVIEW_LABELS = {
    rv.DRAFTED: "Черновик готов",
    rv.NEEDS_HUMAN: "Отвечаете сами",
    rv.NO_DRAFT: "Без черновика",
    rv.FAILED: "Не отправилось",
}


def _review_queue_state(project_id: str) -> list[dict]:
    """
    Очередь читаем с диска при каждой перерисовке, а НЕ кэшируем в session_state.

    Первая версия кэшировала – и это была главная поломка: человек открывал
    «Актуализацию» до прогона (очередь пустая, запомнили), запускал прогон,
    прогон писал черновики в файл, а на экране по-прежнему висел запомненный
    пустой список. Черновики были, показать их было некому.

    Файл маленький, читать его на каждый клик дешевле, чем ловить такие
    несоответствия. Правки человека сохраняются сразу же, так что потерять
    их между перерисовками нельзя.
    """
    key = f"_rvq_{project_id}"
    st.session_state[key] = rv.load_queue(project_id)
    return st.session_state[key]


def _review_queue_save(project_id: str, items: list[dict] | None = None,
                       push: bool = True) -> None:
    if items is None:
        items = st.session_state.get(f"_rvq_{project_id}") or []
    try:
        rv.save_queue(project_id, items, push=push)
        st.session_state[f"_rvq_{project_id}"] = items
    except Exception as e:  # noqa: BLE001
        st.session_state["_store_error"] = str(e)


def _review_regenerate(project_id: str, item: dict) -> None:
    prompt = rv.project_prompt(project_id)
    if not prompt.strip():
        item["note"] = "Промпт проекта пуст – заполните его в «Настройках»"
        return
    fake = {"text": item.get("text"), "author": item.get("author"),
            "rating": item.get("rating"), "answered": False}
    try:
        item["draft"] = rv.clean_draft(llm.generate(rv.build_prompt(prompt, fake)))
        item["status"] = rv.DRAFTED
        item["note"] = ""
    except Exception as e:  # noqa: BLE001
        item["note"] = str(e)
        if not item.get("draft"):
            item["status"] = rv.NO_DRAFT


def _apply_send_result(item: dict, status: str, reason: str) -> None:
    """Разложить исход отправки по статусам очереди – одинаково для одного и для всех."""
    item["note"] = reason
    if status == "answered":
        item["status"] = rv.ANSWERED
    elif status == "already":
        item["status"] = rv.ALREADY
    else:
        # И «не подтвердилось» тоже: отзыв остаётся в списке, пока Яндекс
        # сам не покажет ответ. Молча считать успехом нельзя.
        item["status"] = rv.FAILED


# ─── Пачки: «Переписать все» и «Отправить все» ──────────────────────
#
# Пачка обрабатывается ПО ОДНОЙ штуке за перерисовку, а не циклом внутри
# одного прогона скрипта. Цикл выглядел проще, но на время работы страница
# замирала: заказчик видела «2 из 16», рядом «Упёрлись в лимит Gemini» – и
# остановить это было нечем, оставалось ждать или закрывать вкладку.
#
# Теперь после каждой штуки страница перерисовывается: кнопка «Остановить»
# живая, прогресс настоящий, а состояние пачки лежит в session_state и
# переживает перерисовку.
#
# Браузер для отправки держим открытым между перерисовками – поднимать его
# заново на каждый ответ значит вернуть те самые минуты ожидания.

_BATCH_BROWSER: dict[str, tuple] = {}


def _batch_browser(project_id: str):
    """Браузер и поток для пачки отправки. Открывается один раз на пачку."""
    have = _BATCH_BROWSER.get(project_id)
    if have:
        return have
    browser = yb.YbBrowser(project_id, headless=bool(get_settings(project_id)["headless"]))
    worker = PlaywrightWorker()
    worker.call(browser.start)
    _BATCH_BROWSER[project_id] = (worker, browser)
    return worker, browser


def _batch_browser_close(project_id: str) -> None:
    _BATCH_URL.pop(project_id, None)
    _BATCH_COUNT.pop(project_id, None)
    have = _BATCH_BROWSER.pop(project_id, None)
    if not have:
        return
    worker, browser = have
    try:
        worker.call(browser.save_session)
    except Exception:  # noqa: BLE001
        pass
    try:
        worker.call(browser.close)
    except Exception:  # noqa: BLE001
        pass
    worker.stop()


# Через столько ответов браузер перезапускается. Страница отзывов тяжёлая, и
# на четырнадцати подряд Streamlit Cloud выбило по памяти: «This app has gone
# over its resource limits». Перезапуск занимает пару секунд и отдаёт память.
SEND_RESTART_EVERY = 5

_BATCH_URL: dict[str, str] = {}      # какой город сейчас открыт
_BATCH_COUNT: dict[str, int] = {}    # сколько ответов ушло с этого запуска браузера


def _send_one(project_id: str, item: dict, text: str) -> None:
    """Отправить один ответ через браузер пачки и записать исход."""
    url = item.get("reviewsUrl")
    try:
        if _BATCH_COUNT.get(project_id, 0) >= SEND_RESTART_EVERY:
            _batch_browser_close(project_id)          # отдаём память
        worker, browser = _batch_browser(project_id)
        # Страница этого города уже открыта – грузить заново незачем.
        same = _BATCH_URL.get(project_id) == url
        res = worker.call(yb.publish_review_answer, browser.page, url,
                          item.get("reviewId"), text, item.get("text") or "",
                          not same)
        _BATCH_URL[project_id] = url
        _BATCH_COUNT[project_id] = _BATCH_COUNT.get(project_id, 0) + 1
        status, reason = res.get("status", "failed"), res.get("reason", "")
    except Exception as e:  # noqa: BLE001
        status, reason = "failed", str(e)
        _BATCH_URL.pop(project_id, None)
    item["finalText"] = text
    item["sentAt"] = datetime.now(timezone.utc).isoformat()
    _apply_send_result(item, status, reason)


def _review_send(project_id: str, item: dict, text: str) -> tuple[str, str]:
    """Отправка одного ответа кнопкой – та же дорога, что и у пачки."""
    try:
        _send_one(project_id, item, text)
    finally:
        _batch_browser_close(project_id)
    _review_queue_save(project_id)
    status = {rv.ANSWERED: "answered", rv.ALREADY: "already"}.get(item.get("status"), "failed")
    return status, item.get("note") or ""


def _batch_start(kind: str, items: list[dict], project_id: str) -> None:
    # По городам: соседние ответы уходят на одну и ту же открытую страницу.
    items = sorted(items, key=lambda it: (it.get("reviewsUrl") or "", it.get("city") or ""))
    st.session_state["rv-batch"] = {
        "kind": kind, "project": project_id,
        "ids": [it.get("reviewId") for it in items],
        "done": 0, "total": len(items), "stop": False,
        "answered": 0, "already": 0, "failed": 0,
    }


def _batch_stop() -> None:
    batch = st.session_state.get("rv-batch")
    if batch:
        batch["stop"] = True


def _batch_finish(project_id: str, batch: dict) -> None:
    _batch_browser_close(project_id)
    _review_queue_save(project_id)
    st.session_state.pop("rv-batch", None)
    left = batch["total"] - batch["done"]
    if batch["kind"] == "send":
        parts = [f"отправлено {batch['answered']}"]
        if batch["already"]:
            parts.append(f"уже были отвечены {batch['already']}")
        if batch["failed"]:
            parts.append(f"не прошло {batch['failed']} – остались в списке")
        note = " · ".join(parts)
    else:
        note = f"переписано {batch['done']}"
    if left > 0:
        note += f" · остановлено, осталось {left}"
    st.session_state["rv-batch-note"] = note


def _batch_block(project_id: str, items: list[dict]) -> bool:
    """
    Один шаг пачки. Возвращает True, если пачка идёт – тогда остальной
    список рисовать не надо, человек и так смотрит на прогресс.
    """
    note = st.session_state.pop("rv-batch-note", None)
    if note:
        st.success(note)

    batch = st.session_state.get("rv-batch")
    if not batch or batch.get("project") != project_id:
        return False

    if batch["stop"] or batch["done"] >= batch["total"]:
        _batch_finish(project_id, batch)
        st.rerun()

    title = ("Переписываю черновики" if batch["kind"] == "redo" else "Отправляю ответы")
    st.progress(batch["done"] / batch["total"],
                text=f"{title}: {batch['done']} из {batch['total']}")
    st.button("⏹ Остановить", key="rv-batch-stop", use_container_width=True,
              on_click=_batch_stop)

    by_id = {it.get("reviewId"): it for it in items}
    item = by_id.get(batch["ids"][batch["done"]])
    if item is None:                       # отзыв исчез из очереди – просто идём дальше
        batch["done"] += 1
        st.rerun()

    st.caption(f'{item.get("city") or ""} · {item.get("author") or ""}')
    if batch["kind"] == "redo":
        _review_regenerate(project_id, item)
    else:
        stamp = hashlib.md5((item.get("draft") or "").encode("utf-8")).hexdigest()[:8]
        text = st.session_state.get(f"rv-text-{item.get('reviewId')}-{stamp}") or item.get("draft")
        _send_one(project_id, item, text)
        key = {rv.ANSWERED: "answered", rv.ALREADY: "already"}.get(item.get("status"), "failed")
        batch[key] += 1

    batch["done"] += 1
    # Отправку выталкиваем наружу сразу: приложение может упасть, а локальные
    # файлы облако не переживает – и тогда непонятно, что уже опубликовано.
    _review_queue_save(project_id, push=(batch["kind"] == "send"))
    st.rerun()
    return True


_SENT_LABELS = {
    rv.ANSWERED: "✅ отправлен",
    rv.ALREADY: "⊝ уже был ответ",
    rv.SKIPPED: "⏭ пропущен",
    rv.FAILED: "❌ не отправился",
}


def _sent_report_block(project_id: str, done: list[dict], pending: list[dict]) -> None:
    """
    Что уже отправлено – город, автор, когда и чем кончилось.

    Нужен после того, как приложение упало посреди отправки четырнадцати
    ответов: список опустел, а понять, что успело уйти в Яндекс, можно было
    только руками по карточкам. Теперь исход каждого ответа сохраняется
    сразу и виден здесь, даже если приложение перезапустилось.
    """
    if not done:
        return
    c = rv.counters(done)
    rows = sorted(done, key=lambda it: it.get("sentAt") or it.get("collectedAt") or "",
                  reverse=True)

    with st.expander(f"📋 Отчёт по отправке ({len(done)})", expanded=bool(c["failed"])):
        st.caption(f"Отправлено {c['answered']} · уже были отвечены "
                   f"{len([r for r in done if r.get('status') == rv.ALREADY])} · "
                   f"пропущено {c['skipped']} · не отправилось {c['failed']}")
        for it in rows:
            when = local_time(it.get("sentAt")) if it.get("sentAt") else ""
            label = _SENT_LABELS.get(it.get("status"), it.get("status") or "")
            st.markdown(
                f"- **{T.esc(it.get('city') or '?')}** · {T.esc(it.get('author') or 'без имени')} "
                f"– {label}" + (f' · <span class="hint">{when}</span>' if when else "")
                + (f"<br><span class='hint'>{T.esc(it.get('note') or '')}</span>"
                   if it.get("status") == rv.FAILED and it.get("note") else ""),
                unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        c1.download_button("⬇ Отчёт (CSV)", data=_sent_csv(rows),
                           file_name=f"reviews-{project_id}.csv", mime="text/csv",
                           use_container_width=True, key="rv-report-csv")
        if c2.button("Очистить отчёт", key="rv-clear-done", use_container_width=True):
            st.session_state[f"_rvq_{project_id}"] = pending
            _review_queue_save(project_id)
            st.rerun()


def _sent_csv(rows: list[dict]) -> bytes:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Город", "Автор", "Оценка", "Статус", "Когда", "Отзыв", "Ответ", "Причина"])
    for it in rows:
        w.writerow([it.get("city") or "", it.get("author") or "", it.get("rating") or "",
                    _SENT_LABELS.get(it.get("status"), it.get("status") or ""),
                    local_time(it.get("sentAt")) if it.get("sentAt") else "",
                    (it.get("text") or "").replace("\n", " "),
                    (it.get("finalText") or it.get("draft") or "").replace("\n", " "),
                    it.get("note") or ""])
    return buf.getvalue().encode("utf-8-sig")


def _send_all_block(project_id: str, pending: list[dict], running: bool) -> None:
    """
    Отправить все готовые ответы разом.

    В пачку берём только те, где черновик готов и выглядит целым. Отзывы
    «отвечаете сами» и неудачные черновики не трогаем: пачкой уходит то,
    что заведомо можно публиковать, остальное человек разбирает поштучно.

    Отправка публична и необратима – поэтому в два шага, с подтверждением
    и с числом ответов прямо на кнопке.
    """
    ready = [it for it in pending
             if it.get("status") == rv.DRAFTED
             and (it.get("draft") or "").strip()
             and not rv.looks_broken(it.get("draft") or "")]
    if not ready or running:
        return

    if not st.session_state.get("rv-send-all-asked"):
        skipped = len(pending) - len(ready)
        tail = (f" Остальные {skipped} останутся в списке: там либо нужен ваш ответ, "
                "либо черновик не получился." if skipped else "")
        st.caption(f"Готовы к отправке: {len(ready)}.{tail}")
        if st.button(f"📨 Отправить все ({len(ready)})", key="rv-send-all",
                     use_container_width=True):
            st.session_state["rv-send-all-asked"] = True
            st.rerun()
        return

    st.warning(f"Отправить {len(ready)} ответов в Яндекс? Они появятся на карточках "
               "под именем бренда сразу, отменить это можно будет только вручную.")
    yes, no = st.columns(2)
    if no.button("Отмена", key="rv-send-all-no", use_container_width=True):
        st.session_state.pop("rv-send-all-asked", None)
        st.rerun()
    if yes.button(f"Да, отправить {len(ready)}", key="rv-send-all-yes",
                  type="primary", use_container_width=True):
        st.session_state.pop("rv-send-all-asked", None)
        _batch_start("send", ready, project_id)
        st.rerun()


def reviews_queue_block(project_id: str) -> None:
    items = _review_queue_state(project_id)
    pending = rv.open_items(items)

    # Пачку разбираем ДО проверки «список пуст»: отправив всё, список
    # опустеет, и пачка осталась бы недоделанной, а браузер – открытым.
    if st.session_state.get("rv-batch") or st.session_state.get("rv-batch-note"):
        with st.container(border=True):
            html('<div class="card-title">💬 Ответы на отзывы</div>')
            if _batch_block(project_id, items):
                return
        if not pending:
            return

    if not pending:
        return

    running = runner.read_state(project_id).get("status") == "running"
    done = [it for it in items if it.get("status") not in rv.OPEN_STATUSES]

    with st.container(border=True):
        html(f'<div class="card-title">💬 Ответы на отзывы – на подтверждении '
             f'({len(pending)})</div>')
        if running:
            st.info("Идёт прогон. Отправлять ответы можно будет, когда он закончится: "
                    "во время прогона браузер занят, и второй вход в Яндекс его собьёт.")
        else:
            html('<div class="hint" style="margin-bottom:10px">Ничего не уходит в Яндекс само. '
                 'Проверьте текст, при желании поправьте прямо здесь и нажмите '
                 '«Отправить».</div>')

        # Черновики могли остаться от прошлой версии – обрывками на полуслове.
        # Перещёлкивать «Переписать» по каждому вручную дело нудное.
        # Переписать разом. Нужно не только когда черновик неудачный: промпт
        # проекта правится, и после правки прежние ответы устаревают все сразу.
        redo = [it for it in pending if it.get("status") in (rv.DRAFTED, rv.NO_DRAFT)]
        broken = [it for it in redo if rv.looks_broken(it.get("draft") or "")]
        if redo and llm.is_configured() and not running:
            note = (f"Неудачных черновиков: {len(broken)} из {len(redo)}. " if broken
                    else "")
            st.caption(note + "Переписать все разом – примерно "
                       f"{max(1, round(len(redo) * llm.MIN_GAP_S / 60))} мин. "
                       "Пригодится после правки промпта в «Настройках».")
            c_all, c_bad = st.columns(2)
            todo = None
            if c_all.button(f"🔁 Переписать все ({len(redo)})", key="rv-again-all",
                            use_container_width=True):
                todo = redo
            if broken and c_bad.button(f"🔁 Только неудачные ({len(broken)})",
                                       key="rv-again-bad", use_container_width=True):
                todo = broken
            if todo:
                _batch_start("redo", todo, project_id)
                st.rerun()

        _send_all_block(project_id, pending, running)

        for n, item in enumerate(pending):
            label = _REVIEW_LABELS.get(item.get("status"), "–")
            stars = "★" * int(item.get("rating") or 0)
            with st.container(border=True):
                head, badge = st.columns([4, 1])
                head.markdown(
                    f'**{T.esc(item.get("city") or "?")}** · {T.esc(item.get("author") or "без имени")} '
                    f'· <span style="color:var(--yel)">{stars}</span> '
                    f'· <span class="hint">{local_time(item.get("createdAt"))}</span>',
                    unsafe_allow_html=True)
                badge.markdown(f'<div class="hint" style="text-align:right">{label}</div>',
                               unsafe_allow_html=True)
                html(T.preview_box(item.get("text") or ""))

                if item.get("note"):
                    st.caption(f"⚠️ {item['note']}")

                if item.get("status") == rv.NEEDS_HUMAN:
                    st.caption(f"Оценка ниже {rv.GOOD_RATING} – черновик не писали. "
                               "Ответьте сами на карточке:")
                    st.markdown(f"[Открыть отзывы этого города]({item.get('reviewsUrl')})")
                    if st.button("Убрать из списка", key=f"rv-drop-{n}"):
                        item["status"] = rv.SKIPPED
                        _review_queue_save(project_id)
                        st.rerun()
                    continue

                # Ключ поля включает отпечаток черновика. Иначе Streamlit
                # держится за первое, что человек увидел: черновик в файле уже
                # переписан, а на экране остаётся прежний текст – ровно это и
                # выглядело как «нажала „Переписать“, и ничего не происходит».
                # Меняется черновик – меняется ключ – поле берёт новое значение.
                draft = item.get("draft") or ""
                stamp = hashlib.md5(draft.encode("utf-8")).hexdigest()[:8]
                text = st.text_area("Ответ компании", value=draft,
                                    key=f"rv-text-{item.get('reviewId')}-{stamp}",
                                    height=160, disabled=running)
                bad = rv.banned_words(text)
                if bad:
                    st.warning("В ответе есть слова, которые промпт запрещает: "
                               + ", ".join(f"«{w}»" for w in bad))
                if rv.looks_broken(text):
                    st.warning("Черновик не получился – оборван или в нём остались "
                               "служебные заметки модели. Нажмите «Переписать».")

                c1, c2, c3 = st.columns(3)
                if c1.button("✅ Отправить", key=f"rv-send-{n}", type="primary",
                             use_container_width=True, disabled=running or not text.strip()):
                    with st.spinner("Открываю карточку и отправляю ответ…"):
                        status, reason = _review_send(project_id, item, text)
                    if status == "answered":
                        st.success(reason)
                    elif status == "already":
                        st.info(reason)
                    else:
                        # Не подтвердилось – отзыв ОСТАЁТСЯ в списке. Раньше
                        # он отсюда исчезал как отвеченный, а в Яндексе ответа
                        # не было: человек узнавал об этом случайно.
                        st.error(reason)
                    time.sleep(0.8)
                    st.rerun()

                if c2.button("🔁 Переписать", key=f"rv-again-{n}", use_container_width=True,
                             disabled=not llm.is_configured()):
                    with st.spinner("Прошу новый вариант…"):
                        _review_regenerate(project_id, item)
                    _review_queue_save(project_id, push=False)
                    st.rerun()

                if c3.button("⏭ Пропустить", key=f"rv-skip-{n}", use_container_width=True):
                    item["status"] = rv.SKIPPED
                    item["note"] = ""
                    _review_queue_save(project_id)
                    st.rerun()

        _sent_report_block(project_id, done, pending)


def tab_actualize(project_id: str, config: dict) -> None:
    countries = config["countries"]
    if not countries:
        html(T.empty("🏙", "Нет городов", "Добавьте страны и города во вкладке «Города»."))
        return

    # Очередь ответов – первым делом: после прогона именно она и нужна.
    reviews_queue_block(project_id)

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

    if not yb.has_saved_session(project_id) and not running:
        st.warning("Сначала войдите в Яндекс в разделе «⚙️ Настройки».")
        return

    # value не задаём: у виджета есть key, и Streamlit ругается, если состояние
    # приходит и из session_state, и из value одновременно.
    with_reviews = st.checkbox(
        "💬 Заодно проверить отзывы и подготовить ответы", key="act-reviews",
        help="Click зайдёт в раздел «Отзывы» каждой карточки, найдёт отзывы без ответа "
             "и напишет черновики. Ничего не публикуется: ответы уйдут только после "
             "вашего подтверждения, уже после прогона.",
    )
    if with_reviews:
        notes = []
        if not llm.is_configured():
            notes.append("ключ Gemini не задан – отзывы соберутся, но черновиков не будет")
        if not rv.project_prompt(project_id).strip():
            notes.append("промпт проекта пуст – заполните его в «Настройках»")
        if notes:
            st.warning(" · ".join(notes).capitalize())
        else:
            st.caption(f"Черновики пишутся только на отзывы в {rv.GOOD_RATING} звёзд, "
                       f"не больше {rv.MAX_DRAFTS_PER_CITY} на город. "
                       "Всё, что ниже, попадёт в список без ответа – отвечаете сами. "
                       "Прогон станет примерно в полтора раза длиннее.")

    # Порядок как у человека в голове: сначала запускаем, потом смотрим, как
    # идёт. Раньше панель с «Посмотреть отчёт» стояла НАД кнопкой запуска, а
    # ещё ниже висела вторая карточка отчёта – выглядело нацепленным сверху.
    if st.button(f"🔄 Запустить актуализацию ({cities_word(len(selected))})", type="primary",
                 use_container_width=True, disabled=running or not selected, key="btn-actualize"):
        selection = {c["id"]: [ct["id"] for ct in c["cities"] if ct["id"] in chosen]
                     for c in countries}
        save_actualize_tasks(project_id, config, selection)
        ok, msg = runner.start_actualize(project_id, headless=bool(get_settings(project_id)["headless"]),
                                         with_reviews=bool(with_reviews))
        (st.toast if ok else st.error)(msg)
        time.sleep(0.6)
        st.rerun()
    if running:
        st.button("⏹ Остановить", use_container_width=True, key="btn-stop-act",
                  on_click=runner.request_stop, args=(project_id,))

    # Панель показываем, только когда есть что показывать: до первого прогона
    # пустая рамка с пустым логом только мешает.
    if running or runner.read_state(project_id).get("status") not in (None, "idle"):
        with st.container(border=True):
            live_panel(project_id, running)

    # Вторая карточка отчёта убрана: весь отчёт теперь на вкладке «Отчёт»,
    # и туда ведёт кнопка «Посмотреть отчёт» из панели выше.


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
                    skipped: list[str] = []
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
                        # Проверка на дубль была только у добавления по одному,
                        # а списком города падали внутрь как есть – так в проект
                        # и попадали одинаковые карточки. Проверяем и здесь;
                        # заодно ловятся повторы ВНУТРИ самого списка, потому
                        # что добавленное сразу попадает в тот же config.
                        dup = _city_duplicate(config, url_, name_, country["id"])
                        if dup:
                            skipped.append(f"{name_} – {dup}")
                            continue
                        country["cities"].append({
                            "id": f"ct-{_slug(name_)}-{int(time.time() * 1000)}-{added}",
                            "name": name_, "url": url_,
                        })
                        added += 1
                    if skipped:
                        st.session_state[f"bulk-skipped-{country['id']}"] = skipped
                    if added:
                        save_config(project_id)
                        st.toast(f"Добавлено городов: {added}")
                        st.rerun()
                    elif skipped:
                        st.rerun()

                was_skipped = st.session_state.pop(f"bulk-skipped-{country['id']}", None)
                if was_skipped:
                    st.warning(f"Не добавлено (дубли): {len(was_skipped)}")
                    for s in was_skipped[:20]:
                        st.caption(f"• {s}")

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

# Плашка отчёта = фильтр. key – поле в totals, status – статус в строках.
# «always» держит плашку на месте даже при нуле: заказчику нужны ровно эти
# колонки всегда, как в оригинале.
_PUB_TILES = [
    {"key": "ok",       "status": "ok",                "label": "Успешно",       "colour": "ok",    "always": True},
    {"key": "noImage",  "status": "no-image",          "label": "Без картинки",  "colour": "noimg", "always": False},
    {"key": "unknown",  "status": "unknown",           "label": "Проверьте",     "colour": "warn",  "always": False},
    {"key": "failed",   "status": "failed",            "label": "Ошибок",        "colour": "err",   "always": True},
    {"key": "skipped",  "status": "skipped-duplicate", "label": "Пропущено",     "colour": "skip",  "always": False},
]
_ACT_TILES = [
    {"key": "actualized", "status": "actualized", "label": "Актуализировано", "colour": "ok",   "always": True},
    {"key": "notNeeded",  "status": "not-needed", "label": "Не требовалось",  "colour": "skip", "always": True},
    {"key": "failed",     "status": "failed",     "label": "Ошибок",          "colour": "err",  "always": True},
]
# Цвет, фон и рамка плашки – те же, что у не кликабельных .report-stat.
_STAT_COLOUR = {
    "ok":    ("var(--grn)", "var(--grn-bg)", "rgba(16,185,129,.25)"),
    "noimg": ("var(--yel)", "var(--yel-bg)", "rgba(245,158,11,.25)"),
    "warn":  ("var(--yel)", "var(--yel-bg)", "rgba(245,158,11,.4)"),
    "err":   ("var(--red)", "var(--red-bg)", "rgba(239,68,68,.25)"),
    "skip":  ("var(--acc)", "var(--acc-bg)", "rgba(91,124,250,.25)"),
    "dur":   ("var(--text)", "var(--bg-3)", "var(--border)"),
}


def _dur_text(sec: int | None) -> str:
    if sec is None:
        return ""
    return f"{sec} сек" if sec < 90 else f"{sec / 60:.1f} мин"


def _report_notes(data: dict, totals: dict) -> list[str]:
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
    if data.get("withReviews"):
        rt = data.get("reviewTotals") or {}
        notes.append(f'💬 Отзывы: без ответа {rt.get("found", 0)} · '
                     f'черновиков {rt.get("drafted", 0)} · '
                     f'вам на ответ {rt.get("needsHuman", 0)} · '
                     f'без черновика {rt.get("noDraft", 0)}. '
                     "Ответы ждут подтверждения в «Актуализации».")
    return notes


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


_REPORT_KINDS = {"publish": "📤 Публикация", "actualize": "🔄 Актуализация"}


def _last_run_kind(project_id: str) -> str:
    """Какой прогон был последним – его отчёт и показываем по умолчанию."""
    state = runner.read_state(project_id)
    return "actualize" if state.get("action") == "actualize" else "publish"


def tab_report(project_id: str) -> None:
    # Отчёты актуализации лежат в своей папке и на эту вкладку не попадали
    # вовсе: заказчик видела «Отчётов пока нет» и один лог, хотя отчёт был –
    # он показывался только на самой вкладке «Актуализация».
    kind = st.session_state.get("report-kind") or _last_run_kind(project_id)
    if kind not in _REPORT_KINDS:
        kind = "publish"
    k_cols = st.columns(len(_REPORT_KINDS))
    for col, (k, label) in zip(k_cols, _REPORT_KINDS.items()):
        n = len(runner.list_reports(project_id, k, limit=99))
        if col.button(f"{label} ({n})", key=f"rk-{k}", use_container_width=True,
                      type="primary" if k == kind else "secondary"):
            st.session_state["report-kind"] = k
            st.session_state.pop("report-select", None)
            st.rerun()

    is_act = kind == "actualize"
    reports = runner.list_reports(project_id, kind)
    if not reports:
        html(T.empty("📊", "Отчётов пока нет",
                     "Отчёт появится после первой актуализации." if is_act
                     else "Отчёт появится после первой публикации."))
        return _day_logs(project_id)

    names = [r["name"] for r in reports]
    with st.container(border=True):
        # Отчёт – ОДИН блок: шапка с датой справа, выбор, плашки, детали.
        # Раньше всё это лежало вразнобой по странице.
        head = next((r for r in reports
                     if r["name"] == st.session_state.get("report-select")), reports[0])
        html(f'<div class="report-head">'
             f'<span class="report-head-title">📊 Отчёт '
             f'{"актуализации" if is_act else "публикации"}</span>'
             f'<span class="report-head-date">{T.esc(local_time(head.get("finishedAt")))}</span>'
             f'</div>')

        # Выбор нужен только когда отчётов больше одного: иначе одна и та же
        # дата стояла бы и в шапке, и в списке. Список сам обновляется при
        # каждой перерисовке – кнопка «Обновить» не нужна.
        if len(names) > 1:
            selected = st.selectbox("Отчёт", names, key="report-select",
                                    format_func=lambda n: local_time(
                                        next(r for r in reports if r["name"] == n).get("finishedAt")),
                                    label_visibility="collapsed")
        else:
            selected = names[0]

        data = runner.read_report(project_id, kind, selected)
        if not data:
            return _day_logs(project_id)

        totals = data.get("totals") or {}
        results = data.get("results") or []
        current = st.session_state.get("report-filter", "all")

        # ─── Плашки = фильтры ───
        tiles = _ACT_TILES if is_act else _PUB_TILES
        shown = [t for t in tiles if totals.get(t["key"]) or t["always"]]
        dur = data.get("durationSec")
        cells = shown + ([{"key": "__time", "label": "Время", "colour": "dur", "always": True}]
                         if dur is not None else [])

        html(T.tile_css([
            (f"tile-stat-{n}", {
                "--val": T.css_text(_dur_text(dur) if t["key"] == "__time"
                                    else str(int(totals.get(t["key"], 0) or 0))),
                "--stat-c": _STAT_COLOUR[t["colour"]][0],
                "--stat-bg": _STAT_COLOUR[t["colour"]][1],
                "--stat-bd": _STAT_COLOUR[t["colour"]][2],
            }) for n, t in enumerate(cells)
        ]))
        cols = st.columns(len(cells))
        for n, (col, t) in enumerate(zip(cols, cells)):
            with col, st.container(key=f"tile-stat-{n}"):
                is_time = t["key"] == "__time"
                picked = (not is_time) and current == t["status"]
                if st.button(t["label"], key=f"stat-{t['key']}", use_container_width=True,
                             disabled=is_time,
                             type="primary" if picked else "secondary") and not is_time:
                    st.session_state["report-filter"] = "all" if picked else t["status"]
                    st.rerun()

        for n in _report_notes(data, totals):
            st.caption(n)

        # ─── Детали: один плоский список, без разбивки по странам ───
        current = st.session_state.get("report-filter", "all")
        if current not in {t["status"] for t in tiles} | {"all"}:
            current = "all"
        rows = [r for r in results if current == "all" or r.get("status") == current]
        rows = sorted(rows, key=lambda r: ((r.get("country") or r.get("package") or ""),
                                           r.get("cityName") or ""))
        picked_label = next((t["label"] for t in tiles if t["status"] == current), "")
        title = (f"Показать детали ({cities_word(len(rows))})" if current == "all"
                 else f"{picked_label}: {cities_word(len(rows))} – показать")
        with st.expander(title, expanded=current != "all"):
            if not rows:
                st.caption("Ничего не подошло под выбранную плашку.")
            for r in rows:
                html(T.report_row(r, with_country=True))

            # Скачивание – внутри деталей, чтобы не мешалось в шапке.
            reader = getattr(runner, "read_run_log", None)
            run_log = reader(project_id, kind, selected) if reader else ""
            base_name = selected.replace(".json", "")
            d1, d2, _ = st.columns([1, 1, 3])
            d1.download_button("⬇ Отчёт (CSV)", data=_report_csv(data),
                               file_name=base_name + ".csv", mime="text/csv",
                               use_container_width=True, key="btn-csv")
            d2.download_button("⬇ Лог (.txt)", data=(run_log or "Лог этого прогона не сохранён.")
                               .encode("utf-8"),
                               file_name=base_name + ".txt", mime="text/plain",
                               use_container_width=True, disabled=not run_log, key="btn-log")
            if not run_log:
                st.caption("Лог этого прогона не сохранён – он появится у прогонов, "
                           "запущенных начиная с этой версии.")

    _day_logs(project_id)


def _day_logs(project_id: str) -> None:
    html('<div class="card-title">📄 Логи за день</div>')
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
    _reviews_settings_block(project_id)

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

    if repo_store.is_configured():
        st.caption("💾 Сессия Яндекса хранится в приватном хранилище проекта – "
                   "переживает перезапуски облака. Сбросить: «Войти заново». "
                   "Отчёты в облаке по-прежнему живут до перезапуска.")
    else:
        st.caption("⚠️ В облаке файловая система временная: при перезапуске приложения пропадут "
                   "сессия Яндекса и отчёты.")

    with st.container(key="danger-logout"):
        if st.button("Выйти из проекта", key="btn-logout"):
            st.query_params.clear()
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()


def _reviews_settings_block(project_id: str) -> None:
    """Промпт ответов на отзывы – по проекту, рядом с остальными настройками."""
    html('<div class="card-title">💬 Ответы на отзывы</div>')

    keys = llm.api_keys()
    if keys:
        st.caption(f"✅ {llm.where()}.")
        if len(keys) < 2:
            st.caption("💡 Генерация упирается в лимит запросов Gemini, а лимит считается "
                       "на каждый ключ отдельно. Заведите в Google AI Studio ещё один-два "
                       "ключа и положите их в секреты как `gemini_api_key_2` и "
                       "`gemini_api_key_3` – Click распределит запросы между ними, и "
                       "черновики пойдут во столько же раз быстрее.")
    else:
        st.caption("⚠️ Ключ Gemini не задан. Черновики ответов писаться не будут – "
                   "отзывы всё равно соберутся, но отвечать придётся вручную. "
                   "Ключ берётся бесплатно в Google AI Studio и кладётся в секреты "
                   "приложения строкой `gemini_api_key = \"…\"`.")

    current = rv.project_prompt(project_id)
    with st.expander(f"Промпт проекта {PROJECTS[project_id]['name']}"
                     + (" · изменён" if rv.is_custom_prompt(project_id) else " · заводской")):
        st.caption("Это инструкция для Gemini: тон, структура, ассортиментная фраза и запреты. "
                   f"Маркеры **{pdata.REVIEW_TEXT_MARK}** и **{pdata.REVIEW_NAME_MARK}** "
                   "заменяются на текст отзыва и имя автора – не убирайте их.")
        text = st.text_area("Промпт", value=current, height=340, key=f"rv-prompt-{project_id}",
                            label_visibility="collapsed")
        missing = [m for m in (pdata.REVIEW_TEXT_MARK, pdata.REVIEW_NAME_MARK) if m not in text]
        if missing:
            st.warning("Пропали маркеры: " + ", ".join(missing)
                       + ". Без них отзыв и имя в промпт не подставятся.")
        c1, c2 = st.columns([1, 1])
        if c1.button("Сохранить промпт", key=f"rv-prompt-save-{project_id}", type="primary",
                     use_container_width=True):
            try:
                note = rv.save_project_prompt(project_id, text)
                st.success(f"Промпт {note}.")
            except Exception as e:  # noqa: BLE001
                st.error(f"Не сохранилось: {e}")
        if c2.button("Вернуть заводской", key=f"rv-prompt-reset-{project_id}",
                     use_container_width=True):
            try:
                rv.save_project_prompt(project_id, "")
                st.session_state.pop(f"rv-prompt-{project_id}", None)
                st.success("Вернули промпт из документа заказчика.")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"Не сохранилось: {e}")

    st.caption(f"Черновик пишется только на отзывы в {rv.GOOD_RATING} звёзд и не больше "
               f"{rv.MAX_DRAFTS_PER_CITY} на город. Отзывы с оценкой ниже попадают в список "
               "без черновика – их вы отвечаете сами. Отзывы без текста пропускаются.")

    # Проверка на месте: прогонять 60 городов ради того, чтобы понять, жив ли
    # Gemini и не обрывается ли ответ, – слишком дорогое удовольствие.
    if st.button("Проверить генерацию на примере", key=f"rv-try-{project_id}",
                 disabled=not llm.is_configured()):
        sample = {"text": "Продукция отличного качества, все отлично! Поставка без задержек.",
                  "author": "Павел Филиппов", "rating": 5, "answered": False}
        with st.spinner("Прошу у Gemini ответ на пробный отзыв…"):
            try:
                answer = rv.clean_draft(
                    llm.generate(rv.build_prompt(rv.project_prompt(project_id), sample)))
            except Exception as e:  # noqa: BLE001
                answer = None
                st.error(str(e))
        if answer:
            stats = getattr(llm, "last_stats", {}) or {}
            st.caption(f"Отзыв: «{sample['full_text']}» · автор Павел Филиппов "
                       f"(в обращении – {rv.name_for_prompt('Павел Филиппов')}) · "
                       f"модель {stats.get('model') or llm.model_in_use() or '–'} · "
                       f"{stats.get('seconds', '?')} сек, запросов {stats.get('calls', 1)}, "
                       f"ключей {stats.get('keys', len(keys))}")
            html(T.preview_box(answer))
            if rv.looks_cut_off(answer):
                st.warning("Ответ оборван на середине – напишите мне, покажу, куда смотреть.")
            else:
                st.success("Ответ пришёл целиком.")


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
                _forget_session(project_id)
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
                res = worker.call(flow.auto_login, email, password, 10, by_mail)
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
            # Поле и его кнопка идут ПОДРЯД: два действия рядом читались как
            # «либо одно, либо другое», хотя пароль надо сначала написать,
            # а потом нажать. Остальные способы – ниже, отдельным блоком.
            st.caption("Способ 1 – по паролю: впишите пароль и нажмите кнопку под ним.")
            value = st.text_input(field_label, value=password, type="password", key="yb-password")
            if st.button("Войти по паролю", key="yb-submit-password", type="primary") and value:
                with st.spinner("Проверяю пароль…"):
                    new_state = worker.call(flow.submit_password, value)

            st.divider()
            st.caption("Способ 2 – без пароля. Подтверждение придёт письмом или в SMS.")
            c1, c2 = st.columns(2)
            if c1.button("✉️ Отправить письмо для входа", key="yb-login-mail",
                         use_container_width=True):
                with st.spinner("Прошу Яндекс прислать письмо…"):
                    new_state = worker.call(flow.send_login_email)
            if c2.button("Войти с помощью SMS", key="yb-login-sms", use_container_width=True):
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
        _push_session(project_id)
    finally:
        try:
            worker.call(flow.close)
        except Exception:
            pass
    for k in ("yb_flow", "yb_state", "yb_step", "yb_note"):
        st.session_state.pop(k, None)
    st.success("Вход выполнен, сессия сохранена." + (f" Аккаунт: {account}" if account else ""))


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: СВЕРКА С ЯНДЕКСОМ
# ════════════════════════════════════════════════════════════════════

# Плашки сверки – они же фильтры, как в отчёте.
_AUDIT_TILES = [
    {"key": "found",    "filter": "found",    "label": "Найдено",       "colour": "ok"},
    {"key": "several",  "filter": "several",  "label": "Несколько",     "colour": "warn"},
    {"key": "missing",  "filter": "missing",  "label": "Нет в Яндексе", "colour": "err"},
    {"key": "mismatch", "filter": "mismatch", "label": "Расхождения",   "colour": "noimg"},
    {"key": "extra",    "filter": "extra",    "label": "Нет в КП",      "colour": "skip"},
]


def _audit_cache(key: str, make):
    """
    Ответ Google держим в памяти вкладки.

    Streamlit перерисовывает страницу на каждый клик, а один только список
    листов – это три запроса к Google. Без кэша нажатие любой плашки лезло бы
    в сеть и упиралось в квоту.
    """
    cache = st.session_state.get("audit-cache") or {}
    if key not in cache:
        cache[key] = make()
        st.session_state["audit-cache"] = cache
    return cache[key]


def _audit_forget() -> None:
    st.session_state.pop("audit-cache", None)


def _audit_sheet_rows(project_id: str, config: dict, title: str) -> list[list[str]]:
    return _audit_cache(
        f"rows|{project_id}|{title}",
        lambda: kp_sheet.read_sheet(project_id, title, (config.get("kpSheetUrl") or "").strip()))


def _audit_pick_sheet(titles: list[str], prefer: str) -> str:
    """Какой лист предлагать: указанный ссылкой, потом «Лист20», потом «кп»."""
    saved = st.session_state.get("audit-sheet")
    if saved in titles:
        return saved
    for want in ("лист20", "лист 20"):
        for t in titles:
            if kp_audit.norm_text(t).replace(" ", "") == want.replace(" ", ""):
                return t
    if prefer in titles:
        return prefer
    for t in titles:
        if kp_audit.norm_text(t) in ("кп", "карта присутствия", "карта присутсвия"):
            return t
    return titles[0] if titles else ""


def tab_audit(project_id: str, config: dict) -> None:
    state = runner.read_state(project_id)
    running = state.get("status") == "running"
    stored = runner.load_companies(project_id)
    companies = stored.get("companies") or []

    with st.container(border=True):
        html('<div class="card-title">🔎 Сверка КП с организациями Яндекса</div>')
        html('<div class="hint" style="margin-bottom:12px">Click читает список организаций аккаунта, '
             'раскладывает их по городам КП и сравнивает <b>сайт, телефоны и почту</b> с таблицей. '
             'Города, где карточек несколько, помечаются отдельно – это дубли, из-за них посты '
             'уходят в одну карточку, а вторая живёт своей жизнью. На выходе – то же самое КП, '
             'только с колонками из Яндекса.</div>')

        c1, c2 = st.columns([2, 3])
        collected_at = local_time(stored.get("collectedAt")) if stored.get("collectedAt") else ""
        if c1.button("🔄 Прочитать организации в Яндексе", type="primary", key="audit-collect",
                     disabled=running or not yb.has_saved_session(project_id),
                     use_container_width=True):
            ok, msg = runner.start_collect(project_id,
                                           headless=bool(get_settings(project_id)["headless"]),
                                           with_cards=bool(st.session_state.get("audit-cards")))
            (st.toast if ok else st.error)(msg)
            time.sleep(0.6)
            st.rerun()
        c2.caption(f"Собрано организаций: **{len(companies)}**, {collected_at}" if companies
                   else "Организации ещё не читались.")
        st.checkbox("Открывать карточку каждой организации (дольше, но наверняка)",
                    key="audit-cards",
                    help="Список организаций и так отдаёт сайт, телефоны и почту. Открывать "
                         "карточки нужно, только если в списке чего-то не хватает: это "
                         "3-4 секунды на каждую из сотен карточек.")
        if not yb.has_saved_session(project_id):
            st.warning("Сначала войдите в Яндекс в разделе «⚙️ Настройки».")

    if running or runner.read_state(project_id).get("status") not in (None, "idle"):
        with st.container(border=True):
            live_panel(project_id, running)

    # ─── Лист КП ───
    saved_url = (config.get("kpSheetUrl") or "").strip()
    if not kp_sheet.is_configured(project_id, saved_url):
        st.info("Не настроена таблица КП. Ссылка и ключ доступа задаются во вкладке "
                "«🏙 Города» → «Источник городов».")
        return

    try:
        titles, prefer = _audit_cache(f"titles|{project_id}|{saved_url}",
                                      lambda: kp_sheet.sheet_titles(project_id, saved_url))
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    if not titles:
        st.error("В таблице КП нет ни одного листа.")
        return

    with st.container(border=True):
        html('<div class="card-title">📄 Лист КП</div>')
        # Лист из прошлого раза мог пропасть (переименовали, сменили таблицу) –
        # тогда Streamlit упал бы на значении, которого нет в списке.
        if st.session_state.get("audit-sheet") not in titles:
            st.session_state.pop("audit-sheet", None)
        pick = _audit_pick_sheet(titles, prefer)
        title = st.selectbox("Лист таблицы", titles, index=titles.index(pick) if pick in titles else 0,
                             key="audit-sheet", label_visibility="collapsed")
        if st.button("↻ Перечитать таблицу", key="audit-reread"):
            _audit_forget()
            st.rerun()
        try:
            rows = _audit_sheet_rows(project_id, config, title)
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
            return
        if not any(any(str(c).strip() for c in r) for r in rows):
            # «Лист20» заказчик только собирается заполнить – пока он пустой,
            # предлагаем открыть рабочий лист в один клик, а не искать в списке.
            st.warning(f"Лист «{title}» пустой – сверять нечего.")
            others = [t for t in titles
                      if t != title and kp_audit.norm_text(t) in
                      ("кп", "карта присутствия", "карта присутсвия", "выгрузка", "целевая")]
            for n, other in enumerate(others[:3]):
                st.button(f"Открыть лист «{other}»", key=f"audit-goto-{n}",
                          on_click=lambda t=other: st.session_state.__setitem__("audit-sheet", t))
            return

    if not companies:
        html(T.empty("🔎", "Организации ещё не прочитаны",
                     "Нажмите «Прочитать организации в Яндексе» – это займёт около минуты."))
        return

    result = kp_audit.build(rows, companies)
    if result.get("error"):
        st.error(result["error"] + f" (лист «{title}»)")
        return
    totals = result["totals"]

    with st.container(border=True):
        html(f'<div class="report-head"><span class="report-head-title">📋 Сверка · лист «{T.esc(title)}»'
             f'</span><span class="report-head-date">{T.esc(collected_at)}</span></div>')

        current = st.session_state.get("audit-filter", "all")
        html(T.tile_css([
            (f"tile-audit-{n}", {
                "--val": T.css_text(str(totals.get(t["key"], 0))),
                "--stat-c": _STAT_COLOUR[t["colour"]][0],
                "--stat-bg": _STAT_COLOUR[t["colour"]][1],
                "--stat-bd": _STAT_COLOUR[t["colour"]][2],
            }) for n, t in enumerate(_AUDIT_TILES)
        ]))
        cols = st.columns(len(_AUDIT_TILES))
        for n, (col, t) in enumerate(zip(cols, _AUDIT_TILES)):
            with col, st.container(key=f"tile-audit-{n}"):
                picked = current == t["filter"]
                if st.button(t["label"], key=f"audit-tile-{t['key']}", use_container_width=True,
                             type="primary" if picked else "secondary"):
                    st.session_state["audit-filter"] = "all" if picked else t["filter"]
                    st.rerun()

        st.caption(f"Городов в листе: {totals['rows']} · организаций в Яндексе: {totals['companies']}"
                   + (f" · сетевых карточек: {totals['chains']}" if totals.get("chains") else "")
                   + (f" · без ссылки в КП: {totals['noLink']}" if totals.get("noLink") else ""))

        _audit_details(result, current, title, rows, collected_at)


def _audit_details(result: dict, current: str, title: str, rows: list[list[str]],
                   collected_at: str = "") -> None:
    items = result["items"]
    picks = {
        "found":    [i for i in items if i["cmp"]["status"] != "нет"],
        "several":  [i for i in items if i["cmp"]["status"] == "несколько"],
        "missing":  [i for i in items if i["cmp"]["status"] == "нет"],
        "mismatch": [i for i in items if i["cmp"]["status"] != "нет" and i["cmp"]["problems"]],
    }
    shown = items if current == "all" else picks.get(current, [])
    label = next((t["label"] for t in _AUDIT_TILES if t["filter"] == current), "")

    if current == "extra":
        with st.expander(f"Есть в Яндексе, нет в КП: {len(result['extra'])} – показать",
                         expanded=True):
            st.caption("Организации аккаунта, которым не нашлось города в КП. "
                       "У каждой – ссылка на карточку: по ней видно, дописать город "
                       "в КП или удалить дубль. Сетевые карточки показаны отдельно.")
            for co in result["extra"] + result["chains"]:
                html(T.audit_extra_row(co))
            if not result["extra"] and not result["chains"]:
                st.caption("Все организации разошлись по городам КП.")
    else:
        head = (f"Показать детали ({cities_word(len(shown))})" if current == "all"
                else f"{label}: {cities_word(len(shown))} – показать")
        with st.expander(head, expanded=current != "all"):
            if not shown:
                st.caption("Ничего не подошло под выбранную плашку.")
            for it in shown[:400]:
                html(T.audit_row(it))
            if len(shown) > 400:
                st.caption(f"Показаны первые 400 из {len(shown)}. Остальное – в выгрузке.")

    st.markdown("**Выгрузка**")
    d1, d2, _ = st.columns([1, 1, 3])
    stamp = datetime.now().strftime("%Y-%m-%d")
    try:
        blob = kp_audit.to_xlsx(rows, result, title, collected_at=collected_at)
        d1.download_button("⬇ Отчёт (.xlsx)", data=blob,
                           file_name=f"КП-сверка-{stamp}.xlsx", use_container_width=True,
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           key="audit-xlsx")
    except Exception as e:  # noqa: BLE001
        d1.error(f"Excel не собрался: {e}")
    d2.download_button("⬇ Только таблица (CSV)",
                       data=kp_audit.to_csv(kp_audit.to_rows(rows, result)).encode("utf-8-sig"),
                       file_name=f"КП-сверка-{stamp}.csv", mime="text/csv",
                       use_container_width=True, key="audit-csv")
    st.caption("В файле восемь листов, у каждого своя задача: «Дашборд» – цифрами, "
               "«Нет в КП», «Дубли», «Нет в Яндексе» и «Расхождения» – короткие списки "
               "со ссылками, «Сверка» – сетка ✓/✗ по всем городам, «КП с данными» – "
               "ваша таблица без изменений плюс колонки из Яндекса. Как читать значки – "
               "на листе «Как читать».")


# ════════════════════════════════════════════════════════════════════
#  ГЛАВНЫЙ ЭКРАН
# ════════════════════════════════════════════════════════════════════

def show_main(project_id: str) -> None:
    inject_css()
    ensure_dirs(project_id)
    if not st.session_state.get(f"_sess_pulled_{project_id}"):
        _pull_session(project_id)
        _pull_ledger(project_id)
        st.session_state[f"_sess_pulled_{project_id}"] = True
    _push_session(project_id)          # дешёвая проверка по mtime: пуш только при изменении
    _push_ledger(project_id)
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
    # Счётчик на вкладке «Актуализация»: после прогона с отзывами человек
    # иначе не догадается, что где-то ждут готовые ответы. Первый боевой
    # прогон это и показал – черновики были, а найти их было негде.
    waiting = len(rv.open_items(rv.load_queue(project_id)))

    def _section_label(name: str) -> str:
        if name == SECTIONS[2] and waiting:
            return f"{name} · 💬 {waiting}"
        return name

    with st.container(key="click-tabs"):
        section = st.radio("Раздел", SECTIONS, index=SECTIONS.index(current), horizontal=True,
                           label_visibility="collapsed", format_func=_section_label,
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
    elif section == SECTIONS[5]:
        tab_settings(project_id, config)
    else:
        tab_audit(project_id, config)


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
