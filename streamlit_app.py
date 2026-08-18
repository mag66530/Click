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
import functools
import hashlib
import importlib
import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

# ── Свои модули ─────────────────────────────────────────────────────
#
# Сначала прогреваем их по списку, и только потом импортируем обычными
# строками. Выглядит лишним – но именно из-за отсутствия этого шага
# приложение падало страницей «Oh no» каждый раз, когда код обновлялся.
#
# Облако обновляет файлы под работающим приложением, а Streamlit в этот
# момент выселяет изменённые модули из sys.modules – так он заставляет
# страницу подхватить новый код. Если в ту же секунду другой поток (вторая
# вкладка, идущий прогон) импортирует такой модуль, импорт падает на ровном
# месте: KeyError: 'paths', следом 'yb_playwright', 'runner', 'llm'.
# Приложение умирает целиком, а человек видит «Oh no» и не понимает, при
# чём тут он: он всего лишь запустил публикацию.
#
# Лечится ожиданием: выселение занимает мгновение, повтор проходит. А после
# прогрева обычные import'ы – это уже просто взгляд в словарь, они успевают
# проскочить между двумя выселениями.
_OWN_MODULES = ("build", "apptime", "paths", "projects_data", "repo_store", "ui_theme",
                "llm", "reviews", "kp_sheet", "kp_audit", "secrets_local",
                "yb_playwright", "gis_playwright", "playwright_worker", "runner",
                # Кросспостинг. Раньше их тут не было – и правки в них
                # доезжали до облака как придётся: модуль остаётся в памяти
                # от прежней сборки, а по метке его никто не проверяет.
                # Заказчица дважды получила «та же ошибка» на уже
                # исправленном коде, и оба раза мы гадали, дело в правке или
                # в том, что она ещё не доехала. Список меток должен знать
                # обо ВСЕХ модулях, которые мы правим.
                "content_plan", "crosspost_plan", "crosspost_state",
                "crosspost_form", "post_text",
                "scheduler", "social_session", "vk_social", "ok_browser",
                "ok_social", "tg_social", "max_social", "max_browser",
                # Дзен: студия автора (браузер) и разбор статьи из документа.
                "zen_browser", "zen_doc")


def _settle_imports() -> None:
    """Прогреть свои модули, пережив обновление кода на ходу."""
    for attempt in range(5):
        try:
            for name in _OWN_MODULES:
                importlib.import_module(name)
            return
        except KeyError:          # sys.modules подменили посреди импорта
            time.sleep(0.2 * (attempt + 1))
    # Не вышло за секунду с лишним – пусть падает обычный импорт ниже,
    # с настоящей ошибкой, а не с нашей.


_settle_imports()

import apptime  # noqa: E402
import content_plan  # noqa: E402
import crosspost_plan  # noqa: E402
import crosspost_state as cps  # noqa: E402
import gis_playwright as gis  # noqa: E402
import kp_audit  # noqa: E402
import kp_sheet  # noqa: E402
import llm  # noqa: E402
import paths  # noqa: E402
import post_text  # noqa: E402
import projects_data as pdata  # noqa: E402
import repo_store  # noqa: E402
import secrets_local  # noqa: E402
import reviews as rv  # noqa: E402
import runner  # noqa: E402
import ui_theme as T  # noqa: E402
import yb_playwright as yb  # noqa: E402
import playwright_worker  # noqa: E402
from playwright_worker import PlaywrightWorker  # noqa: E402

# Метка сборки. Показывается человеку и служит ключом кэша для CSS.
#
# ЗДЕСЬ БОЛЬШЕ НЕТ САМОДЕЛЬНОЙ ПЕРЕЗАГРУЗКИ МОДУЛЕЙ, и это важно.
#
# Раньше тут стояло: «не совпала метка – перезагружаем модуль сами» через
# importlib.reload. Задумка была честная: облако умеет обновить главный скрипт,
# оставив соседние модули в памяти прежними, и тогда страница зовёт функцию,
# которой в старом модуле ещё нет.
#
# Но это оказалось лечением несуществующей болезни ценой настоящей. Streamlit
# делает ровно ту же работу сам: следит за локальными модулями и выселяет
# изменённые из sys.modules – причём аккуратно, откладывая выселение так, чтобы
# «never mutate sys.modules while user code is running» (его local_sources_watcher).
#
# А наша перезагрузка шла из пользовательского потока, без всякой оглядки на
# соседей. Пока один поток перезагружал модули, другой их импортировал – и
# импорт падал на ровном месте: KeyError: 'runner', KeyError: 'repo_store',
# KeyError: 'reviews'. Приложение умирало целиком. Потоков же стало много:
# два прогона разом, плюс каждая открытая вкладка – это свой поток. Заказчик
# получила это, запустив актуализацию вместе со сверкой КП; и на вопрос «а если
# два человека с разных компов?» ответ был бы такой же – упало бы.
#
# Поэтому: модули не трогаем, метку только показываем.
from build import BUILD as UI_BUILD

ROOT = Path(__file__).parent
USERS_DATA = paths.data_root()

st.set_page_config(page_title="Click – публикация постов", page_icon="📮", layout="wide")

SALT = "click-salt-v1-2026"
SECTIONS = ["🚀 Запуск", "📤 Публикация", "🗓 Кросспостинг", "🔄 Актуализация", "🏙 Города",
            "📊 Отчёт", "⚙️ Настройки", "🔎 Сверка"]

# Разделы адресуются ИМЕНАМИ, а не номерами. Раньше по коду были разбросаны
# SECTIONS[4] и SECTIONS[2]; стоило вставить раздел в середину – и кнопка
# «к отчёту» молча уводила в «Города». Имена от перестановки не страдают.
(SEC_RUN, SEC_COMPOSE, SEC_CROSSPOST, SEC_ACTUALIZE,
 SEC_CITIES, SEC_REPORT, SEC_SETTINGS, SEC_AUDIT) = SECTIONS


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
_KEPT = ("countries", "countriesGis", "email", "kpSheetUrl", "kpSheetTitle",
        "kpSyncedAt", "gisPhotosDefault",
        # Ссылки на площадки и на реестр. Раньше их тут не было – и после
        # каждого перезапуска облака все поля стояли пустыми: файловая
        # система временная, а наружу уезжали только города. Заказчица
        # вбивала два десятка ссылок заново.
        "vkGroupUrl", "okGroupUrl", "tgChannelClient", "tgChannelStaff",
        "maxWebUrl", "maxChatId", "zenUrl", "zenStudioUrl", "planSheetUrl")


def _fill_social(project_id: str, sub: dict) -> dict:
    """
    Дописать зашитые ссылки бренда в ПУСТЫЕ поля.

    Порядок старшинства тут ровно тот же, что у городов: сохранённое
    заказчицей главнее кода. Заготовка лишь избавляет от набора вручную –
    стоило один раз потерять их при перезапуске, и работа встала.
    """
    for key, value in pdata.social_defaults(project_id).items():
        if not (sub.get(key) or "").strip():
            sub[key] = value
    return sub


def load_raw_config(project_id: str) -> dict:
    fp = config_path(project_id)
    if fp.exists():
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            if raw.get("projects"):
                return _merge_kept(project_id, raw)
        except (json.JSONDecodeError, OSError):
            pass
    # Локального конфига нет – собираем из кода. Это НЕ данные заказчика, а
    # заготовка, и _merge_kept обязан знать разницу (см. ниже).
    sub = _default_subproject(project_id)
    return _merge_kept(project_id, {"projects": [sub], "activeProjectId": sub["id"], "settings": {}},
                       from_preset=True)


def _merge_kept(project_id: str, raw: dict, from_preset: bool = False) -> dict:
    """
    Города и настройки проекта поднимаем из репозитория. В облаке файловая
    система временная: без этого после каждого перезапуска список городов
    пришлось бы набирать заново.

    ГЛАВНОЕ ЗДЕСЬ: сохранённые города ВАЖНЕЕ зашитого в код списка.

    Так было не всегда, и это стоило заказчику дня работы. Она загрузила
    города МПЭ из КП, ночью облако перезапустилось, файловая система
    обнулилась – и Click, не найдя локального конфига, собрал его из
    пресета `projects_data.MPE_CITIES`. Пресет не пустой, а внешняя копия
    подставлялась только «если локально пусто» – свежий список из КП молча
    проиграл коду, и утром на экране были вчерашние города.

    Проявлялось это только у проектов с пресетом (СМУ, ИМП, МПЭ, АПС). У МПИ
    пресета нет, поэтому там всё работало, и беда выглядела случайной.

    Пресет теперь – только для проекта, который ещё ни разу ничего не
    сохранял. Правки, сделанные в самом приложении (локальный файл есть),
    по-прежнему главнее: их писал человек, а не код.
    """
    sub = next((x for x in raw["projects"] if x["id"] == raw.get("activeProjectId")), raw["projects"][0])
    saved = repo_store.load(f"project-{project_id}")
    for key in _KEPT:
        if saved and key in saved and (from_preset or not sub.get(key)):
            sub[key] = saved[key]
    # Ссылки – в последнюю очередь и только в пустое: и сохранённое, и
    # набранное руками старше заготовки.
    _fill_social(project_id, sub)
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
    for name, path in _session_files(project_id):
        if path.exists() and path.stat().st_size > 2:
            continue
        try:
            data = repo_store.load(name)
            # У МАКС вход живёт не в куках, а в localStorage (раздел origins) –
            # проверка «есть ли cookies» отбраковала бы живую сессию.
            if data and (data.get("cookies") or data.get("origins")):
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def _session_files(project_id: str) -> tuple[tuple[str, Path], ...]:
    """
    Все файлы входов проекта: что храним снаружи и под каким именем.

    Соцсети сюда добавлены по просьбе заказчицы (12.08.2026): в облаке
    файловая система временная, и после каждого перезапуска вход в ВК, ОК и
    МАКС приходилось проходить заново – вставлять файл сессии руками. Куки
    ложатся в ту же закрытую ветку, где уже лежат сессии Яндекса и 2ГИС.
    """
    import max_browser
    import ok_browser
    import vk_social

    return ((f"session-{project_id}", yb.session_path(project_id)),
            (f"device-{project_id}", yb.device_path(project_id)),
            (f"session-gis-{project_id}", gis.session_path(project_id)),
            (f"session-vk-{project_id}", vk_social.session_path(project_id)),
            (f"session-ok-{project_id}", ok_browser.session_path(project_id)),
            (f"session-max-{project_id}", max_browser.session_path(project_id)))


def _push_session(project_id: str) -> None:
    """Свежие куки – в хранилище. Прогон продлевает их, копия не должна отставать."""
    if not repo_store.is_configured():
        return
    import max_browser
    import ok_browser
    import vk_social

    alive_by_name = {
        f"session-{project_id}": yb.has_saved_session,
        f"session-gis-{project_id}": gis.has_saved_session,
        f"session-vk-{project_id}": vk_social.has_saved_session,
        f"session-ok-{project_id}": ok_browser.has_saved_session,
        f"session-max-{project_id}": max_browser.has_saved_session,
    }
    where_by_name = {"gis": "2ГИС", "vk": "ВК", "ok": "ОК", "max": "МАКС"}
    for name, path in _session_files(project_id):
        alive = alive_by_name.get(name)
        if not path.exists() or (alive and not alive(project_id)):
            continue
        mark = f"_pushed_{name}"
        mtime = path.stat().st_mtime
        if st.session_state.get(mark) == mtime:
            continue
        try:
            kind = name.split("-")[1] if name.count("-") > 1 else ""
            where = where_by_name.get(kind, "Яндекса")
            repo_store.save(name, json.loads(path.read_text(encoding="utf-8")),
                            f"Click: сессия {where} ({project_id})")
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


def _forget_session(project_id: str, kind: str = "") -> None:
    """
    «Войти заново»: стираем сессию и локально, И в хранилище.

    Стереть один локальный файл мало: копия лежит в закрытой ветке, и при
    следующем запуске Click честно вернёт её обратно – кнопка «сбросить»
    выглядела бы сработавшей, а вход остался бы прежним. Особенно больно
    это на соцсетях: заказчица однажды собрала куки по СМУ и пыталась ими
    же зайти в ИМП – пока сессия не стёрта до конца, выхода из этого нет.
    """
    name = f"session-{kind}-{project_id}" if kind else f"session-{project_id}"
    path = dict(_session_files(project_id)).get(name)
    if path is None:
        return
    path.unlink(missing_ok=True)
    st.session_state.pop(f"_pushed_{name}", None)
    if repo_store.is_configured():
        try:
            repo_store.save(name, {}, f"Click: сессия сброшена ({project_id})")
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
    Время отчёта – по Екатеринбургу, а не по часам сервера.

    В файлы время пишется в UTC. Раньше показывалось «время машины»: у
    заказчика на локальной машине это совпадало с её часами, а в облаке –
    нет, там UTC. В логе стояло 11:56, когда в Екатеринбурге было почти
    17:00, и выглядело это как чужой, старый прогон. Часовой пояс задан в
    apptime и один на всё приложение.
    """
    return apptime.human(iso)


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
    if not data:
        return
    if not data.get("countries"):
        # Городов в этом конфиге нет – но это не повод не сохранить ссылки.
        # Пустым списком поверх сохранённых городов не пишем никогда: у МПИ
        # города приходят из КП и в конфиге появляются не сразу.
        old = repo_store.load(f"project-{project_id}") or {}
        data = {**old, **data}
    try:
        repo_store.save(f"project-{project_id}", data, f"Click: города и настройки {project_id}")
    except Exception as e:  # noqa: BLE001
        st.session_state["_store_error"] = str(e)


def country_by_id(config: dict, cid: str) -> dict | None:
    return next((c for c in config["countries"] if c["id"] == cid), None)


# ════════════════════════════════════════════════════════════════════
#  Города из КП: одна дорога для кнопки и для автообновления
# ════════════════════════════════════════════════════════════════════

KP_SYNC_TTL_HOURS = 6      # столько живёт загруженный список, дальше обновляем сами
KP_SYNC_RETRY_S = 600      # не вышло – следующая попытка не раньше


def _hours_since(iso: str | None) -> float:
    """Сколько часов прошло с отметки. Нет отметки или мусор – бесконечность."""
    try:
        t = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return float("inf")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def kp_pull(project_id: str, config: dict) -> tuple[bool, str]:
    """
    Перечитать КП и заменить ею список городов.

    Возвращает (получилось, что сказать человеку). Одна дорога и для кнопки
    «Загрузить», и для автообновления: разъехавшись, они означали бы, что
    город появляется в списке по-разному в зависимости от того, как его
    загрузили.
    """
    saved_title = (config.get("kpSheetTitle") or "").strip()
    cities, diag = kp_sheet.load_cities(project_id, (config.get("kpSheetUrl") or "").strip(),
                                        title=saved_title)
    if diag.get("error"):
        return False, diag["error"]
    if not cities:
        return False, "В таблице не нашлось ни одного города со ссылкой на Яндекс.Бизнес."
    config["countries"] = kp_sheet.to_countries(cities, project_id)
    # Города 2ГИС – из того же прохода по таблице: блок «2ГИС» лежит в тех же
    # строках, отдельного чтения не требуется.
    config["countriesGis"] = kp_sheet.to_countries(cities, project_id, platform=kp_sheet.GIS)
    config["kpSyncedAt"] = datetime.now(timezone.utc).isoformat()
    # Лист ещё не выбирали явно в «⚙️ Настройки» – запоминаем тот, что нашла
    # эвристика. Дальше выбор явный и не меняется сам по себе: заново гадать
    # незачем, а вкладка «Настройки» сразу покажет то, что реально читается.
    if not saved_title and diag.get("usedSheet"):
        config["kpSheetTitle"] = diag["usedSheet"]
    save_config(project_id)
    note = f'Загружено: {cities_word(len(cities))} в {diag.get("countries", 0)} странах.'
    note += f' В 2ГИС карточек: {diag.get("gisCities", 0)}.'
    if diag.get("skippedDeleted"):
        note += f' Пропущено удалённых карточек: {diag["skippedDeleted"]}.'
    # Почему город не попал в 2ГИС – по имени и по причине. Без этого
    # «поставила Шымкент активным, а его нет» разбиралось перепиской: в
    # приложении не было ни числа, ни имени, ни причины.
    skipped = diag.get("gisSkippedRows") or []
    if skipped:
        names = "; ".join(f'{r["name"]} – {r["why"]}' for r in skipped[:6])
        tail = " и ещё…" if len(skipped) > 6 else ""
        note += f" В список 2ГИС не попали: {names}{tail}."
    return True, note


def _kp_autosync(project_id: str, config: dict) -> None:
    """
    Города подтягиваются из КП сами, без кнопки.

    Зачем. Таблица КП – источник правды: её правят руками, в ней же статусы
    карточек. Загрузка кнопкой означала, что достаточно забыть нажать – и
    прогон уходит по вчерашнему списку, а карточка, вчера отмеченная
    «Удалена», получает клики. Плюс облако: файловая система там временная,
    и после перезапуска список должен вернуться сам.

    Раз в KP_SYNC_TTL_HOURS часов и только если КП настроена. Не вышло –
    работаем на прежнем списке и говорим об этом, но не долбимся в таблицу
    на каждое нажатие: следующая попытка не раньше чем через KP_SYNC_RETRY_S.
    """
    if not kp_sheet.is_configured(project_id, (config.get("kpSheetUrl") or "").strip()):
        return
    if _hours_since(config.get("kpSyncedAt")) < KP_SYNC_TTL_HOURS:
        return
    fail_key = f"_kp_sync_failed_{project_id}"
    try:
        if time.time() - float(st.session_state.get(fail_key, 0)) < KP_SYNC_RETRY_S:
            return
    except (TypeError, ValueError):
        pass
    try:
        with st.spinner("Обновляю города из КП…"):
            ok, note = kp_pull(project_id, config)
    except Exception as e:  # noqa: BLE001 – текст ошибки уже человеческий
        ok, note = False, str(e)
    if ok:
        st.session_state.pop(fail_key, None)
        st.toast(f"🏙 {note}")
        return
    st.session_state[fail_key] = time.time()
    st.warning(f"Города из КП не обновились: {note} Работаем на прежнем списке.", icon="📊")


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

def gis_url_for_city(config: dict, country_name: str, city_name: str) -> str | None:
    """
    Ссылка на кабинет 2ГИС для города из списка Яндекса.

    Списки площадок разные – в 2ГИС карточка заведена не везде, – а вот
    названия городов приходят из ОДНОЙ строки КП и потому совпадают.
    Сначала ищем в той же стране, потом по всему списку: страну в таблице
    могли записать по-разному («РФ» и «Россия» сводятся не всегда).
    """
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").strip().lower()).replace("ё", "е")

    want, in_country = norm(city_name), norm(country_name)
    fallback = None
    for country in config.get("countriesGis") or []:
        same = norm(country.get("name")) == in_country
        for city in country.get("cities") or []:
            if norm(city.get("name")) != want:
                continue
            if same:
                return city.get("url")
            fallback = fallback or city.get("url")
    return fallback


def save_queue_to_tasks(project_id: str, config: dict, queue: list[dict]) -> int:
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
            # Ссылку 2ГИС кладём в саму задачу, а не ищем во время прогона:
            # задача уезжает в файл и должна пережить перезапуск облака.
            city_tasks.append({
                "cityName": city["name"],
                "companyUrl": city["url"],
                "companyId": yb.extract_company_id(city["url"]),
                "postText": item["text"],
                "imageUrl": item.get("imageUrl") or None,
                "imagePath": item.get("imagePath") or None,
                "extraImages": item.get("extraImages") or None,
                "productPhotos": item.get("productPhotos") or None,
                "gisPhotos": bool(item.get("gisPhotos")),
                # ВРЕМЕННО: задание «только фото в 2ГИС» – прогон по нему не
                # публикует пост вовсе, а только заливает снимки.
                "gisOnly": bool(item.get("gisOnly")),
                "gisUrl": (gis_url_for_city(config, country["name"], city["name"])
                           if item.get("gisPhotos") else None),
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
        # Задания «только фото в 2ГИС» кладём в ОТДЕЛЬНУЮ папку. Это не про
        # порядок в файлах: прогон прежней сборки про такие задания не знает и,
        # увидев пустой текст, публикует пустой пост – так у заказчика и вышло.
        # В чужую папку он не заглянет, потому что в его коде её нет.
        folder = tasks_dir(project_id, "gis" if item.get("gisOnly") else "tasks")
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{idx + 1:02d}-{safe_filename(country['name'])}-{ts}.json"
        (folder / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        saved += 1
    return saved


def platform_countries(config: dict, platform: str = rv.YANDEX) -> list[dict]:
    """Страны и города площадки. У 2ГИС свой список: карточка заведена не везде."""
    return config["countries"] if platform == rv.YANDEX else (config.get("countriesGis") or [])


def save_actualize_tasks(project_id: str, config: dict, selection: dict[str, list[str]],
                         platform: str = rv.YANDEX) -> int:
    folder = project_base(project_id) / runner.PLATFORMS[platform]["tasks"]
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.json"):          # чистим прошлые – иначе прогон подхватит лишнее
        old.unlink(missing_ok=True)
    ts = int(time.time() * 1000)
    total = 0
    countries = platform_countries(config, platform)
    for idx, (country_id, city_ids) in enumerate(selection.items()):
        country = next((c for c in countries if c["id"] == country_id), None)
        if not country or not city_ids:
            continue
        city_tasks = [
            {"cityName": c["name"], "companyUrl": c["url"], "companyId": yb.extract_company_id(c["url"]),
             # Статус из КП едет вместе с городом – actualize_city сверит его
             # с тем, что реально в кабинете (см. kp_sheet.status_verdict).
             "kpStatus": c.get("kpStatus", "")}
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


def tasks_dir(project_id: str, source: str = "tasks") -> Path:
    """
    Папка очереди. `gis` – задания «только фото в 2ГИС», они лежат ОТДЕЛЬНО.

    Имя папки спрашиваем у runner: очередь читает он, и знать про неё двоим –
    это рано или поздно разные имена в двух файлах.
    """
    return project_base(project_id) / runner.tasks_folder(project_id, source).name


def clear_tasks(project_id: str, source: str = "tasks") -> None:
    for fp in tasks_dir(project_id, source).glob("*.json"):
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
    # Светлая по умолчанию – решение заказчика: тёмная вставала автоматом при
    # каждом заходе, а работает она днём. Переключатель в шапке остаётся,
    # выбор держится в сессии.
    return st.session_state.get("theme", "light")


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


def _persist_css(style_html: str, tag: str) -> None:
    """
    Продублировать наш CSS в <head> страницы.

    Блок <style> из st.markdown живёт среди обычных элементов, а Streamlit при
    перерисовке их пересобирает: на «Выбрать все в стране» стиль пропадал со
    страницы примерно на 450 мс, и всё это время экран выглядел «растянутым» –
    голая вёрстка Streamlit без нашего оформления. Замерено на живой странице,
    не на глаз.

    В <head> Streamlit не заглядывает, поэтому стиль там переживает любую
    перерисовку. Блок в теле страницы остаётся: он рисует самый первый экран,
    пока код ниже ещё не выполнился, и он же спасает, если это не сработает.

    Код выполняется внутри рамки (iframe) – только так Streamlit пускает
    скрипты, из markdown он их вырезает. Рамка своя на каждую перерисовку,
    поэтому вставка идёт с проверкой «такой стиль уже стоит».
    """
    body = re.sub(r"</?style[^>]*>", "", style_html)   # в <head> кладём сами правила
    components.html(
        "<script>(function () {\n"
        "  try {\n"
        "    var d = window.parent.document;\n"
        f"    var id = {json.dumps('click-css-' + tag)};\n"
        "    if (d.getElementById(id)) return;\n"
        "    d.querySelectorAll('style[data-click-css]').forEach(function (s) { s.remove(); });\n"
        "    var s = d.createElement('style');\n"
        "    s.id = id;\n"
        "    s.setAttribute('data-click-css', '1');\n"
        f"    s.textContent = {json.dumps(body)};\n"
        "    d.head.appendChild(s);\n"
        "  } catch (e) { /* чужой домен – останется стиль из тела страницы */ }\n"
        "})();</script>",
        height=0,
    )


def inject_css() -> None:
    style = _css(theme(), UI_BUILD) + _TILE_SAFETY_CSS
    st.markdown(style, unsafe_allow_html=True)
    _persist_css(style, f"{UI_BUILD}-{theme()}")
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
    # Прогонов может идти несколько разом – показываем все, иначе человек видит
    # «идёт публикация» и не понимает, откуда взялся второй браузер.
    live = runner.running_kinds(project_id)
    if live:
        pills += [("warn", "Идёт: " + runner.KIND_RU[k].lower()) for k in live]
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
    # if/else, а не выражение: голое условное выражение Streamlit печатает на
    # экран (см. тест «Ничего лишнего на экран»).
    if st.session_state.get(widget_key):
        cur.add(city_id)
    else:
        cur.discard(city_id)
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
            html(T.project_tile(p, selected == pid))
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
    goto_section(SEC_REPORT)
    st.rerun()


def _render_live_panel(project_id: str, run_kind: str, was_running: bool = False) -> None:
    state = runner.read_state(project_id, run_kind)
    status = state.get("status")
    action = state.get("action") or run_kind
    kind = "publish" if action == "publish" else "actualize"   # какой отчёт открывать
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

    # Лог сворачивается ВСЕГДА – и пока прогон идёт тоже. Он длинный, а под
    # ним ответы на отзывы, и мотать через него каждый раз неудобно. Во время
    # прогона раскрыт по умолчанию: там смотреть и надо.
    log_text = runner.read_live_log(project_id, run_kind)
    if not log_text:
        html(T.log_box(log_text))
    else:
        with st.expander("📄 Лог прогона", expanded=running):
            html(T.log_box(log_text))

    # Рядом идёт ещё один прогон – про него тут же и скажем, чтобы человек не
    # гадал, почему браузер шевелится, когда на этой вкладке всё закончилось.
    others = [k for k in runner.running_kinds(project_id) if k != run_kind]
    if others:
        st.caption("Параллельно идёт: " + ", ".join(runner.KIND_RU[k] for k in others) + ".")


def live_panel(project_id: str, running: bool, run_kind: str) -> None:
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
            _render_live_panel(project_id, run_kind, was_running=True)
        _live()
        return
    _render_live_panel(project_id, run_kind)
    if running:
        st.caption("Обновите страницу, чтобы увидеть свежий прогресс.")


def tab_run(project_id: str, config: dict) -> None:
    settings = get_settings(project_id)
    state = runner.read_state(project_id, "publish")
    running = state.get("status") == "running"
    busy = runner.busy_reason(project_id, "publish")   # пусто – запускать можно

    # Две очереди, и они не смешиваются: посты в своей папке, «только фото в
    # 2ГИС» – в своей. Заказчик просила отдельную функцию, и отдельная она
    # именно здесь: у этих заданий своя очередь, свой запуск и свой смысл.
    files_post, cities_post = runner.count_pending(project_id, "tasks")
    files_gis, cities_gis = runner.count_pending(project_id, "gis")
    both = bool(cities_post and cities_gis)
    gis_only = bool(cities_gis) and not cities_post
    source = "gis" if gis_only else "tasks"
    files = files_gis if gis_only else files_post
    cities = cities_gis if gis_only else cities_post
    has_session = gis.has_saved_session(project_id) if gis_only else yb.has_saved_session(project_id)
    has_creds = bool((config.get("email") or "").strip())

    if both:
        st.warning(f"В очереди сразу два разных задания: посты ({cities_word(cities_post)}) "
                   f"и только фото в 2ГИС ({cities_word(cities_gis)}). Запуск заблокирован, "
                   "чтобы не уехало не то – уберите одно из двух кнопками ниже.")

    # ─── Степпер как в оригинале ───
    html(T.step(1, "Вход в 2ГИС" if gis_only else "Вход в Яндекс",
                "Нужен один раз: Click сохранит сессию, дальше публикация идёт в фоне без 2FA.",
                "done" if has_session else "active",
                "Сессия сохранена" if has_session else ""))
    if not has_session:
        st.info("Перейдите в раздел «⚙️ Настройки» → "
                + ("«Вход в 2ГИС»." if gis_only else "«Вход в Яндекс»."))

    html(T.step(2, "Очередь заданий «только фото в 2ГИС»" if gis_only else "Очередь задач",
                f"Собирается во вкладке «Публикация». "
                f"Сейчас: <b>{plural(files, 'файл', 'файла', 'файлов')}</b>, "
                f"<b>{cities_word(cities)}</b>."
                + (f" Отдельно лежат посты: {cities_word(cities_post)}." if both else ""),
                "done" if cities else ("active" if has_session else "locked"),
                f"{cities_word(cities)} готово" if cities else ""))

    html(T.step(3, "Фото в 2ГИС" if gis_only else "Публикация",
                ("Окно браузера будет видно – галочка в «Настройках» включена."
                 if show_browser_window() else "Браузер работает скрыто.")
                + (" В очереди задания «только фото в 2ГИС»: постов не будет, снимки уйдут "
                   "в раздел «Фото и видео». Успехом считается только появившийся в альбоме снимок."
                   if gis_only else
                   " Каждый город подтверждается ответом API Яндекса – "
                   "в отчёт попадает реальный результат, а не «наверное получилось»."),
                "active" if (cities and has_session and not running) else ("done" if running else "locked")))

    # ─── Кнопки ───
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        disabled = bool(busy) or not cities or not has_session or both
        подпись = "📸 Залить фото в 2ГИС" if gis_only else "▶ Опубликовать"
        if st.button(f"{подпись} ({cities_word(cities)})", type="primary",
                     use_container_width=True, disabled=disabled, key="btn-publish"):
            ok, msg = runner.start_publish(
                project_id,
                headless=bool(settings["headless"]),
                delay_between_posts_s=float(settings["delayBetweenPosts"]),
                expected_email=(config.get("email") or "").strip(),
                strict_account_check=bool(settings["strictAccountCheck"]),
                retry_unknown=bool(settings["retryUnknown"]),
                dedup_window_hours=float(settings["dedupWindowHours"]),
                source=source,
            )
            (st.toast if ok else st.error)(msg)
            time.sleep(0.6)
            st.rerun()
    with c2:
        if st.button("⏹ Остановить", use_container_width=True, disabled=not running, key="btn-stop"):
            runner.request_stop(project_id, "publish")
            st.rerun()
    with c3:
        if st.button("🔄 Обновить", use_container_width=True, key="btn-refresh-run"):
            st.rerun()

    if not has_creds:
        st.warning("В «Настройках» не указан email Яндекс.Бизнеса – без него не работает "
                   "защита от публикации не с того аккаунта.")
    if running:
        st.caption("Кнопка запуска заблокирована, пока идёт публикация – это защита от повторного "
                   "старта и дублей постов.")
    elif busy:
        st.caption(busy)

    st.divider()

    # ─── Живой лог: обновляется сам, пока идёт прогон ───
    live_panel(project_id, running, "publish")

    # ─── Очередь задач ───
    st.divider()
    if cities_post or cities_gis:
        # Кнопки очистки – НА ВИДУ, а не внутри свёрнутого списка файлов.
        # Заказчик: «старая очередь при остановке не сбросилась, и очистить я
        # её не могу». Кнопка была – но лежала внутри «Файлы задач в очереди»,
        # который по умолчанию закрыт, и найти её было нечем. А очередь после
        # остановки и правда остаётся: недоделанные города ждут следующего
        # запуска – это нарочно, но сказать об этом надо здесь же.
        q1, q2 = st.columns([2, 3])
        with q1:
            if cities_post:
                with st.container(key="danger-clear-tasks"):
                    if st.button(f"🗑 Очистить очередь постов ({cities_word(cities_post)})",
                                 disabled=running, use_container_width=True, key="btn-clear-tasks"):
                        clear_tasks(project_id, "tasks")
                        st.rerun()
            if cities_gis:
                with st.container(key="danger-clear-gis"):
                    if st.button(f"🗑 Очистить очередь фото в 2ГИС ({cities_word(cities_gis)})",
                                 disabled=running, use_container_width=True, key="btn-clear-gis"):
                        clear_tasks(project_id, "gis")
                        st.rerun()
        with q2:
            st.caption("После остановки недоделанные города остаются в очереди – следующий "
                       "запуск продолжит с них, а уже опубликованное реестр повторно не отправит. "
                       "Если продолжать не нужно – очистите очередь.")

        with st.expander(f"📋 Файлы задач в очереди ({files_post + files_gis})"):
            for source_id, подпись in (("tasks", ""), ("gis", "📸 только фото в 2ГИС · ")):
                for fp in sorted(tasks_dir(project_id, source_id).glob("*.json")):
                    try:
                        data = json.loads(fp.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        continue
                    tasks = data.get("tasks") or []
                    html(f'<div class="city-row">'
                         f'<span class="city-row-name">{подпись}{T.esc(data.get("country", "–"))}</span>'
                         f'<span class="city-row-url">{T.esc(fp.name)}</span>'
                         f'<span class="badge badge-accent">{len(tasks)} гор.</span></div>')
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
      product_photos_raw, goods_files, gis_photos = "", None, False
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
          # Те же снимки – ещё и в 2ГИС. Галочка помнит прошлый выбор проекта:
          # первый раз выключена, дальше стоит так, как оставили. Ничего не
          # уедет в 2ГИС незамеченным, но и щёлкать каждый раз не придётся.
          gis_photos = st.checkbox(
              "Опубликовать эти фото ещё и в 2ГИС (раздел «Фото и видео»)",
              value=bool(config.get("gisPhotosDefault")), key="compose-gis-photos",
              help="Берутся те же файлы. Город без карточки 2ГИС просто пропускается – "
                   "в отчёте будет «Нет карточки в 2ГИС». 2ГИС не принимает gif.")
          if gis_photos != bool(config.get("gisPhotosDefault")):
              # save_config берёт конфиг из session_state, а config – это его же
              # под-проект: правка выше уже в нём. Лишний второй аргумент ронял
              # всю вкладку с TypeError ровно на щелчке по галочке.
              config["gisPhotosDefault"] = gis_photos
              save_config(project_id)

      # ─── ВРЕМЕННО: только фото в 2ГИС, без поста ───
      # Заказчик проверяет заливку в 2ГИС отдельно: публиковать ради этого
      # шестьдесят постов незачем. Блок нарочно стоит особняком и помечен
      # временным – когда проверка пройдёт, он сольётся с фото Яндекса.
      with st.container(key="gis-only-row"):
          d1, d2 = st.columns([3, 1])
          gis_only_raw = d1.text_area(
              "🟢 ВРЕМЕННО · Только фото в 2ГИС – без поста", height=118,
              key="compose-gis-only",
              placeholder="Ссылки или пути, по одной в строке\n"
                          "Пост НЕ публикуется: снимки уходят только в «Фото и видео» 2ГИС",
          )
          gis_only_files = d2.file_uploader("Фото для 2ГИС", type=["jpg", "jpeg", "png", "webp"],
                                            accept_multiple_files=True,
                                            key=f"compose-gis-only-files-{st.session_state.get('upl-gen', 0)}",
                                            label_visibility="collapsed")
      html('<div class="hint">Заполнили этот блок – прогон не публикует посты вовсе, а только '
           'заливает снимки в «Фото и видео» 2ГИС по выбранным городам. Город без карточки в '
           'списке 2ГИС пропускается с пометкой. Одни и те же снимки в один и тот же город '
           'второй раз не заливаются. gif 2ГИС не принимает.</div>')

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

    # ВРЕМЕННО: снимки для 2ГИС без поста – см. блок выше.
    gis_only_photos = [u.strip() for u in (gis_only_raw or "").splitlines() if u.strip()]
    if gis_only_files:
        uploads = project_base(project_id) / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        for f in gis_only_files:
            digest = hashlib.md5(f.getvalue()).hexdigest()[:10]
            path = uploads / f"{digest}-{safe_filename(f.name)}"
            if not path.exists():
                path.write_bytes(f.getvalue())
            gis_only_photos.append(str(path))
    gis_only_mode = bool(gis_only_photos)

    all_images: list[str] = saved_paths + image_urls
    if len(all_images) > 4:
        st.warning(f"Яндекс берёт максимум 4 фото в пост – лишние {len(all_images) - 4} не отправятся.")

    # Предпросмотр и кнопка «в очередь» нужны, только когда выбрана страна.
    # Раньше тут стоял ранний выход из функции, и вместе с ним со страницы
    # пропадал блок «В очереди к сохранению» – а в нём кнопка «Сохранить
    # очередь в задачи». Ловушка захлопывалась ровно на последней стране:
    # добавили её, выбор стран сбросился, следующей уже нет – и сохранить
    # собранное стало нечем. Плашка при этом бодро советовала «сохраните
    # очередь ниже», а ниже ничего не было.
    # Два режима сразу – это не «и то, и другое», а непонятно что. Говорим
    # прямо и не даём добавить, пока одно из двух не убрано.
    both_filled = gis_only_mode and bool((body or "").strip())
    if both_filled:
        st.warning("Заполнены и текст поста, и блок «Только фото в 2ГИС». Это разные режимы: "
                   "либо публикуем пост, либо только заливаем снимки в 2ГИС. Очистите одно из двух.")
    elif gis_only_mode:
        st.info(f"Режим «только фото в 2ГИС»: постов не будет, уйдут только снимки "
                f"({len(gis_only_photos)} шт.) в «Фото и видео» выбранных городов.")

    if selected_countries:
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
            can_add = (gis_only_mode or bool((body or "").strip())) and total_cities > 0 \
                and not both_filled
            подпись = ("📸 Фото в 2ГИС" if gis_only_mode else "➕ Добавить в очередь")
            if st.button(f"{подпись} ({cities_word(total_cities)})", type="primary",
                         use_container_width=True, disabled=not can_add, key="btn-add-queue"):
                added = 0
                for country in selected_countries:
                    city_ids = per_country.get(country["id"]) or []
                    if not city_ids:
                        continue
                    text = ("" if gis_only_mode
                            else build_final_text(project_id, country["name"], post_type, body))
                    if any(q["countryId"] == country["id"] and q["text"] == text
                           and bool(q.get("gisOnly")) == gis_only_mode for q in queue):
                        st.warning(f"{country['name']}: такое же задание уже в очереди – пропускаю.")
                        continue
                    queue.append({
                        "countryId": country["id"],
                        "countryName": country["name"],
                        "cityIds": list(city_ids),
                        "postType": post_type,
                        "text": text,
                        "imagePath": all_images[0] if all_images and not all_images[0].startswith("http") else None,
                        "imageUrl": all_images[0] if all_images and all_images[0].startswith("http") else None,
                        "extraImages": all_images[1:4] or None,
                        # В режиме «только 2ГИС» снимки едут тем же полем:
                        # прогон различает режимы по gisOnly, а не по полю.
                        "productPhotos": (gis_only_photos if gis_only_mode else product_photos) or None,
                        "gisPhotos": bool(gis_only_mode or (gis_photos and product_photos)),
                        "gisOnly": gis_only_mode,
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
            if not can_add and not both_filled:
                st.caption("Нужно: текст поста (или фото в блоке «Только фото в 2ГИС») + "
                           "хотя бы один выбранный город.")
    elif queue:
        st.info("Все страны уже добавлены в очередь. Осталось сохранить её в задачи – "
                "кнопка «Сохранить очередь в задачи» ниже.")
    else:
        st.info("Выберите хотя бы одну страну выше.")

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
            # У задания «только фото в 2ГИС» текста нет вовсе – показываем, что
            # именно уедет, иначе в очереди висела бы пустая строка.
            if item.get("gisOnly"):
                preview = ("📸 Только фото в 2ГИС, без поста: "
                           + ", ".join(Path(p).name for p in (item.get("productPhotos") or [])[:3]))
            else:
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
                goto_section(SEC_RUN)          # дальше человеку всё равно на «Запуск»
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


def _act_selected(all_ids: list[str], prefix: str = "act") -> set[str]:
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
    # У каждой площадки свой набор: города 2ГИС и Яндекса – разные, и общий
    # набор при переключении вкладки затирал бы соседний.
    sel = st.session_state.get(f"{prefix}-selected")
    if sel is None:
        st.session_state[f"{prefix}-selected"] = set(ids)
        st.session_state[f"{prefix}-known"] = set(ids)
        return st.session_state[f"{prefix}-selected"]

    known = st.session_state.get(f"{prefix}-known")
    if known is None:
        known = set(sel)
    fresh = (sel & ids) | (ids - known)        # новые города – сразу выбраны
    if fresh != sel:
        sel.clear()
        sel.update(fresh)
    st.session_state[f"{prefix}-known"] = ids
    return sel


def _act_set(city_ids: list[str], value: bool, prefix: str = "act") -> None:
    """
    Вызывается ТОЛЬКО из on_click кнопки: в этот момент виджеты ещё не созданы,
    поэтому их состояние можно переписать. Держим в согласии свой набор и сами
    галочки – иначе «Выбрать все» меняло бы счётчик, а галочки нет.

    Но трогаем ТОЛЬКО те галочки, которые сейчас на экране. Живой случай
    заказчика: «Снять все» → раскрыть Казахстан → «Выбрать все в стране» (7 из
    30) → раскрыть Беларусь → отметить Минск – и Казахстан слетал, оставалась
    одна галочка. Причина: «Снять все» писало значение ВСЕМ тридцати городам,
    в том числе свёрнутым, у которых виджета на странице нет. Для Streamlit
    это обычная запись в session_state, она там и оставалась; потом города
    Казахстана рисовались, их значения становились виджетными, а при
    сворачивании страны виджет исчезал – и наружу вылезала та самая старая
    запись False. Подбор ниже честно читал её как «человек снял галочку».
    """
    sel = st.session_state.setdefault(f"{prefix}-selected", set())
    if value:
        sel.update(city_ids)
    else:
        sel.difference_update(city_ids)
    drawn = set(st.session_state.get(f"{prefix}-drawn") or ())
    for cid in city_ids:
        if cid in drawn:
            st.session_state[f"{prefix}-cb-{cid}"] = value


def _act_toggle(city_id: str, widget_key: str, prefix: str = "act") -> None:
    sel = st.session_state.setdefault(f"{prefix}-selected", set())
    if st.session_state.get(widget_key):
        sel.add(city_id)
    else:
        sel.discard(city_id)


def _act_sync_widgets(all_ids: list[str], prefix: str = "act") -> None:
    """
    Подобрать значения галочек, которые браузер прислал, а отрисовать их уже
    не успели.

    Зачем. Снятая галочка запоминается в on_change, а он срабатывает только
    если галочку в этот раз рисуют. Живой случай заказчика: снять галочку и
    тут же свернуть страну – город оставался выбранным, «снять можно только
    кнопкой». Гонка: браузер отправляет снятие, но ответ ещё не пришёл, а
    человек уже жмёт по стране. Второй запрос приходит с обоими изменениями
    сразу, страна сворачивается – и галочки в этот проход не рисуются, значит
    on_change по ним не зовётся, и снятие пропадает. Локально это почти не
    ловится, в облаке с его задержками – запросто.

    Лечится тем, что значение галочки читается НАПРЯМУЮ, до отрисовки: к
    моменту запуска скрипта Streamlit уже положил присланное в session_state.
    """
    sel = st.session_state.setdefault(f"{prefix}-selected", set())
    # Только те города, чьи галочки в прошлый проход РИСОВАЛИСЬ. У остальных
    # в session_state может лежать давняя запись от «Снять все» – читать её
    # как «человек снял галочку» нельзя (см. _act_set).
    drawn = [cid for cid in st.session_state.get(f"{prefix}-drawn") or () if cid in set(all_ids)]
    for cid in drawn:
        key = f"{prefix}-cb-{cid}"
        if key not in st.session_state:
            continue
        # Именно if/else, а не выражение «A if cond else B». Streamlit
        # переписывает главный скрипт: любое голое выражение, кроме вызова
        # функции, он заворачивает в вывод на экран. Условное выражение под
        # это правило попадает, а sel.add/sel.discard возвращают None – и на
        # странице печаталось по «None» на каждый город, сто одна штука.
        if st.session_state[key]:
            sel.add(cid)
        else:
            sel.discard(cid)


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


def _review_queue_state(project_id: str, platform: str = rv.YANDEX) -> list[dict]:
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
    key = f"_rvq_{platform}_{project_id}"
    st.session_state[key] = rv.load_queue(project_id, platform)
    return st.session_state[key]


def _review_queue_save(project_id: str, items: list[dict] | None = None,
                       push: bool = True, platform: str = rv.YANDEX) -> None:
    key = f"_rvq_{platform}_{project_id}"
    if items is None:
        items = st.session_state.get(key) or []
    try:
        rv.save_queue(project_id, items, push=push, platform=platform)
        st.session_state[key] = items
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
        item["draft"] = rv.clean_draft(
            llm.generate(rv.build_prompt(prompt, fake, project_id)),
            project_id, rv.review_text(fake))
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

# ВНИМАНИЕ: держать браузер в переменной модуля НЕЛЬЗЯ. Streamlit на каждую
# перерисовку создаёт для главного скрипта НОВЫЙ модуль (в его исходниках –
# `module = self._new_module("__main__")`) и выполняет файл заново. Любое
# `_X = {}` на верхнем уровне после каждого шага снова становится пустым.
#
# Именно на этом приложение и падало. Пачка обрабатывает ПО ОДНОМУ ответу за
# перерисовку – значит ссылка на открытый браузер терялась ровно между
# ответами. Старый Chromium никто не закрывал, он оставался жить, а на
# следующий ответ поднимался ещё один. Шесть ответов – шесть браузеров, и
# облако убивало приложение по памяти: заказчик видела «еле как 6 штук
# сделал и сломался», а в логе – тишину и мёртвый сервер, без единой
# питоновской ошибки. Счётчик «перезапускать браузер каждые пять» не
# срабатывал по той же причине: он тоже обнулялся каждый шаг.
#
# Всё, что должно пережить перерисовку, живёт в st.session_state.
_BROWSER_BOX = "rv-batch-browser"    # {project_id: (worker, browser)}
_URL_BOX = "rv-batch-url"            # {project_id: какой город сейчас открыт}


def _batch_box(name: str) -> dict:
    """Словарь, переживающий перерисовку. Только через session_state."""
    box = st.session_state.get(name)
    if not isinstance(box, dict):
        box = {}
        st.session_state[name] = box
    return box


def _batch_key(project_id: str, platform: str) -> str:
    """У каждой площадки свой браузер и своя сессия – ключ общий на двоих."""
    return f"{project_id}:{platform}"


def _batch_browser(project_id: str, platform: str = rv.YANDEX):
    """Браузер и поток для пачки отправки. Открывается один раз на пачку."""
    key = _batch_key(project_id, platform)
    have = _batch_box(_BROWSER_BOX).get(key)
    if have and have[0].alive():
        return have
    if have:
        # Поток воркера умер, а браузер мог остаться – убираем за собой,
        # иначе получаем ровно ту утечку, из-за которой всё и падало.
        _batch_browser_close(project_id, platform)
    headless = bool(get_settings(project_id)["headless"])
    if platform == rv.GIS:
        import gis_playwright as gis
        browser = gis.browser(project_id, headless=headless)
    else:
        browser = yb.YbBrowser(project_id, headless=headless)
    worker = PlaywrightWorker()
    worker.call(browser.start)
    _batch_box(_BROWSER_BOX)[key] = (worker, browser)
    return worker, browser


def _batch_browser_close(project_id: str, platform: str | None = None) -> None:
    """Закрыть браузер отправки. Без площадки – все, какие остались открытыми."""
    for pf in ([platform] if platform else [rv.YANDEX, rv.GIS]):
        key = _batch_key(project_id, pf)
        _batch_box(_URL_BOX).pop(key, None)
        have = _batch_box(_BROWSER_BOX).pop(key, None)
        if not have:
            continue
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
def _send_one(project_id: str, item: dict, text: str) -> None:
    """
    Отправить один ответ и записать исход.

    Один браузер на всю пачку, одна вкладка на город. Ответы отсортированы по
    городам, поэтому пять ответов Еревану уходят на ОДНУ открытую страницу –
    карточка грузится один раз, а не пять. Сменился город – закрываем вкладку
    и открываем новую: страница отзывов тяжёлая, и так память освобождается
    сама, ровно по границе города. Вход при этом не теряется – вкладка новая,
    а сессия браузера та же.

    Раньше здесь стоял перезапуск всего браузера каждые пять ответов. Это была
    затычка поверх настоящей причины: ручки браузера и текущего города лежали
    в переменных модуля, которые Streamlit обнуляет на каждой перерисовке.
    Из-за этого «страница уже открыта» не срабатывало НИКОГДА (каждый ответ
    грузил карточку заново), счётчик до пяти не доживал, а старый браузер
    никто не закрывал – он просто оставался висеть. Причина одна, симптомов
    было три; чинить их по отдельности смысла нет.
    """
    url = item.get("reviewsUrl")
    # Площадку берём из самого отзыва: ответ должен уйти тем браузером и той
    # сессией, откуда отзыв пришёл. Перепутать – значит ответить в чужом
    # кабинете, и отменить это уже нельзя.
    platform = item.get("platform") or rv.YANDEX
    key = _batch_key(project_id, platform)
    try:
        worker, browser = _batch_browser(project_id, platform)
        urls = _batch_box(_URL_BOX)
        same = urls.get(key) == url
        if not same:
            worker.call(browser.new_page)     # новый город – новая вкладка
        if platform == rv.GIS:
            import gis_playwright as gis
            res = worker.call(gis.publish_review_answer, browser.page, url,
                              item.get("text") or "", text, not same)
        else:
            res = worker.call(yb.publish_review_answer, browser.page, url,
                              item.get("reviewId"), text, item.get("text") or "",
                              not same)
        urls[key] = url
        status, reason = res.get("status", "failed"), res.get("reason", "")
    except Exception as e:  # noqa: BLE001
        status, reason = "failed", str(e)
        _batch_box(_URL_BOX).pop(key, None)
    item["finalText"] = text
    item["sentAt"] = datetime.now(timezone.utc).isoformat()
    _apply_send_result(item, status, reason)


def _review_send(project_id: str, item: dict, text: str) -> tuple[str, str]:
    """Отправка одного ответа кнопкой – та же дорога, что и у пачки."""
    platform = item.get("platform") or rv.YANDEX
    try:
        _send_one(project_id, item, text)
    finally:
        _batch_browser_close(project_id, platform)
    _review_queue_save(project_id, platform=platform)
    status = {rv.ANSWERED: "answered", rv.ALREADY: "already"}.get(item.get("status"), "failed")
    return status, item.get("note") or ""


def _batch_start(kind: str, items: list[dict], project_id: str,
                 platform: str = rv.YANDEX) -> None:
    # По городам: соседние ответы уходят на одну и ту же открытую страницу.
    items = sorted(items, key=lambda it: (it.get("reviewsUrl") or "", it.get("city") or ""))
    st.session_state["rv-batch"] = {
        "kind": kind, "project": project_id, "platform": platform,
        "ids": [it.get("reviewId") for it in items],
        "done": 0, "total": len(items), "stop": False,
        "answered": 0, "already": 0, "failed": 0,
    }


def _batch_stop() -> None:
    batch = st.session_state.get("rv-batch")
    if batch:
        batch["stop"] = True


def _batch_finish(project_id: str, batch: dict) -> None:
    platform = batch.get("platform") or rv.YANDEX
    _batch_browser_close(project_id, platform)
    _review_queue_save(project_id, push=True, platform=platform)
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


def _batch_block(project_id: str, items: list[dict],
                 platform: str = rv.YANDEX) -> bool:
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
    _review_queue_save(project_id, push=(batch["kind"] == "send"), platform=platform)
    st.rerun()
    return True


_SENT_LABELS = {
    rv.ANSWERED: "✅ отправлен",
    rv.ALREADY: "⊝ уже был ответ",
    rv.SKIPPED: "⏭ пропущен",
    rv.FAILED: "❌ не отправился",
    # Эти три – про отзывы, до которых руки ещё не дошли. В отчёте прогона
    # они тоже нужны: там видно всё найденное, а не только отправленное.
    rv.DRAFTED: "✍ черновик ждёт подтверждения",
    rv.NEEDS_HUMAN: "⚠️ отвечаете сами",
    rv.NO_DRAFT: "– черновика нет",
}


def _sent_report_block(project_id: str, done: list[dict], pending: list[dict],
                       platform: str = rv.YANDEX) -> None:
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

    with st.expander(f"📋 Отчёт по отправке за этот прогон ({len(done)})",
                     expanded=bool(c["failed"])):
        st.caption(f"Отправлено {c['answered']} · уже были отвечены "
                   f"{len([r for r in done if r.get('status') == rv.ALREADY])} · "
                   f"пропущено {c['skipped']} · не отправилось {c['failed']}")
        st.caption("Здесь только текущий прогон. Прошлые – во вкладке «📊 Отчёт»: "
                   "у каждого прогона свой список отзывов и своя выгрузка.")
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
            st.session_state[f"_rvq_{platform}_{project_id}"] = pending
            _review_queue_save(project_id, platform=platform)
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


def _report_reviews(project_id: str, data: dict, platform: str = rv.YANDEX) -> list[dict]:
    """
    Отзывы прогона – с тем, что с ними стало ПОСЛЕ прогона.

    Отчёт знает, какие отзывы нашлись и какие черновики к ним написались.
    Отправка происходит позже, руками, и её исход живёт в очереди. Поэтому
    здесь одно дополняется другим: берём строки прогона и подтягиваем к ним
    свежий статус, время отправки и итоговый текст. Отзыв, разобранный уже
    после прогона, в выгрузке виден разобранным – как оно и есть.
    """
    queue = rv.load_queue(project_id, platform)
    rows = data.get("reviews") or []
    if not rows:
        # Отчёт старый – отзывов в нём не сохраняли. Но у элементов очереди
        # есть метка прогона, так что список всё равно соберётся.
        run_id = data.get("runId") or ""
        rows = [it for it in queue if run_id and it.get("runId") == run_id]
    if not rows:
        return []
    fresh = {it.get("reviewId"): it for it in queue}
    out = []
    for r in rows:
        now = fresh.get(r.get("reviewId")) or {}
        out.append({**r, **{k: v for k, v in now.items() if v not in (None, "")}})
    return sorted(out, key=lambda r: ((r.get("city") or ""), (r.get("author") or "")))


def _report_reviews_block(rows: list[dict]) -> None:
    """
    Отзывы прогона – тем же видом, что и города выше.

    Заказчик просила «общий отчёт об актуализации, если есть отзывы»: чтобы
    не бегать между вкладкой прогона и очередью, а видеть одним экраном, что
    нашлось и чем кончилось, и скачать это одним движением.
    """
    c = rv.counters(rows)
    with st.container(border=True):
        html(f'<div class="report-head">'
             f'<span class="report-head-title">💬 Отзывы этого прогона</span>'
             f'<span class="report-head-date">{len(rows)} шт.</span></div>')
        bits = [f"отвечено {c['answered']}"]
        for label, key in (("ждут подтверждения", "drafted"), ("негативных", "needsHuman"),
                           ("без черновика", "noDraft"), ("пропущено", "skipped"),
                           ("не отправились", "failed")):
            if c.get(key):
                bits.append(f"{label} {c[key]}")
        st.caption(" · ".join(bits))
        with st.expander(f"Показать отзывы ({len(rows)})", expanded=False):
            for r in rows:
                html(T.review_report_row(r))


def _send_all_block(project_id: str, pending: list[dict], running: bool,
                    platform: str = rv.YANDEX) -> None:
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
        # Фиолетовая: это главное действие раздела, а серая кнопка терялась
        # среди «Переписать все» и прочих.
        if st.button(f"📨 Отправить все ({len(ready)})", key="rv-send-all",
                     type="primary", use_container_width=True):
            st.session_state["rv-send-all-asked"] = True
            st.rerun()
        return

    where = "2ГИС" if platform == rv.GIS else "Яндекс"
    st.warning(f"Отправить {len(ready)} ответов в {where}? Они появятся на карточках "
               "под именем бренда, удалить можно будет только вручную.")
    yes, no = st.columns(2)
    if no.button("Отмена", key="rv-send-all-no", use_container_width=True):
        st.session_state.pop("rv-send-all-asked", None)
        st.rerun()
    if yes.button(f"Да, отправить {len(ready)}", key="rv-send-all-yes",
                  type="primary", use_container_width=True):
        st.session_state.pop("rv-send-all-asked", None)
        _batch_start("send", ready, project_id, platform)
        st.rerun()


def reviews_queue_block(project_id: str, platform: str = rv.YANDEX) -> None:
    items = _review_queue_state(project_id, platform)
    pending = rv.open_items(items)

    # Пачку разбираем ДО проверки «список пуст»: отправив всё, список
    # опустеет, и пачка осталась бы недоделанной, а браузер – открытым.
    if st.session_state.get("rv-batch") or st.session_state.get("rv-batch-note"):
        with st.container(border=True):
            html('<div class="card-title">💬 Ответы на отзывы</div>')
            if _batch_block(project_id, items, platform):
                return
        if not pending:
            return

    if not pending:
        return

    # Отправка ответов водит браузер сама, из вкладки. Пускать её рядом с любым
    # прогоном не будем: страница отзывов тяжёлая, и именно на ней приложение
    # уже выбивало по памяти – третий браузер тут лишний.
    live = runner.running_kinds(project_id)
    running = bool(live)

    # Разобранное показываем ТОЛЬКО за текущий прогон. Очередь копится дальше –
    # там лежит вся история, – но на странице от неё была одна путаница:
    # заказчик только собрала новые отзывы, а видела «3 отправленных» с
    # какого-то прошлого раза. История прогонов теперь во вкладке «Отчёт»,
    # у каждого прогона свои отзывы и своя выгрузка.
    run_id = (runner.read_state(project_id, runner.PLATFORMS[platform]["kind"])
              or {}).get("runId") or ""
    done = [it for it in items
            if it.get("status") not in rv.OPEN_STATUSES and it.get("runId") == run_id and run_id]
    # Ждущие ответа показываем ВСЕ, даже со старых прогонов: черновик написан,
    # никуда не делся, и потерять его из виду нельзя.
    shown = pending + done

    with st.container(border=True):
        html(f'<div class="card-title">💬 Ответы на отзывы – на подтверждении '
             f'({len(pending)})</div>')
        # Сводка плашками, как в отчёте: сколько готово, сколько разбирать
        # руками, сколько уже ушло. Раньше это надо было считать глазами по
        # длинному списку.
        c = rv.counters(shown)
        html(T.stat_row([
            ("Черновик готов", c["drafted"], "ok"),
            ("Негативные", c["needsHuman"], "warn"),
            ("Без черновика", c["noDraft"], "noimg"),
            ("Отправлено", c["answered"], "skip"),
        ] + ([("Не отправились", c["failed"], "err")] if c["failed"] else [])))
        if running:
            st.info(f"Идёт {', '.join(runner.KIND_RU[k].lower() for k in live)}. Отправлять ответы "
                    "можно будет, когда прогон закончится: страница отзывов тяжёлая, и третий "
                    "браузер приложению не потянуть.")
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
            # Оценка считается по текущему темпу Gemini (запросов в минуту) –
            # это и есть то, что определяет время: черновик на запрос.
            st.caption(note + "Переписать все разом – примерно "
                       f"{max(1, round(len(redo) / max(1.0, llm.current_pace())))} мин. "
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
                _batch_start("redo", todo, project_id, platform)
                st.rerun()

        _send_all_block(project_id, pending, running, platform)

        # Список можно вычистить целиком. Нужно после неудачного сбора: пока
        # 2ГИС отдавал вперемешку и отвеченные отзывы, список набивался тем,
        # что разбирать не надо, а «Убрать из списка» по одному – это долго.
        if pending and not running:
            if st.session_state.get("rv-drop-all-asked"):
                st.warning(f"Убрать из списка все {len(pending)} отзывов? "
                           "В кабинете ничего не изменится – список просто "
                           "очистится, и следующий прогон соберёт их заново.")
                y, n = st.columns(2)
                if n.button("Отмена", key="rv-drop-all-no", use_container_width=True):
                    st.session_state.pop("rv-drop-all-asked", None)
                    st.rerun()
                if y.button(f"Да, убрать {len(pending)}", key="rv-drop-all-yes",
                            type="primary", use_container_width=True):
                    for it in pending:
                        it["status"] = rv.SKIPPED
                        it["note"] = ""
                    _review_queue_save(project_id, platform=platform)
                    st.session_state.pop("rv-drop-all-asked", None)
                    st.rerun()
            elif st.button("🧹 Очистить список", key="rv-drop-all",
                           use_container_width=True):
                st.session_state["rv-drop-all-asked"] = True
                st.rerun()

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
                        _review_queue_save(project_id, platform=platform)
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
                casual = rv.casual_words(text)
                if casual:
                    st.warning("Разговорные слова в ответе от лица компании: "
                               + ", ".join(f"«{w}»" for w in casual)
                               + " – нажмите «Переписать» или поправьте руками.")
                # Пустое поле и испорченный черновик – разные беды, и путать
                # их нельзя. На пустом поле Click писал «черновик оборван или
                # в нём остались служебные заметки модели» – человек искал в
                # пустоте заметки модели, а черновика попросту не было:
                # причина написана выше, строкой с ⚠️.
                if not text.strip():
                    st.caption("Черновика нет" + (" – причина выше." if item.get("note")
                                                  else ".")
                               + " Напишите ответ сами или нажмите «Переписать».")
                elif rv.looks_broken(text):
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
                    _review_queue_save(project_id, push=False, platform=platform)
                    st.rerun()

                if c3.button("⏭ Пропустить", key=f"rv-skip-{n}", use_container_width=True):
                    item["status"] = rv.SKIPPED
                    item["note"] = ""
                    _review_queue_save(project_id, platform=platform)
                    st.rerun()

        _sent_report_block(project_id, done, pending, platform)


def kp_refresh_row(project_id: str, config: dict, platform: str = rv.YANDEX) -> None:
    """
    «Обновить города из КП» – прямо там, где на города и смотрят.

    Кнопка была и раньше, но жила только в «⚙️ Настройках», рядом с выбором
    таблицы и листа. А человек, поправивший статус города в КП, идёт не в
    настройки: он идёт туда, где выбирает города для прогона. Так и вышло с
    Шымкентом – статус в таблице поставили «Активная», в списке 2ГИС города
    нет, и позвать его туда нечем: «не нашла функционала, который обновляет
    список 2ГИС».

    Теперь список городов обновляется с той же страницы, где он показан, и
    рядом написано, когда он обновлялся в прошлый раз и сколько в нём
    городов ИМЕННО ЭТОЙ площадки.
    """
    where = "2ГИС" if platform == rv.GIS else "Яндекса"
    if not kp_sheet.is_configured(project_id, (config.get("kpSheetUrl") or "").strip()):
        st.caption("Города ведутся вручную: таблица КП у проекта не задана – "
                   "«⚙️ Настройки» → «Источник городов».")
        return

    total = sum(len(c.get("cities") or []) for c in platform_countries(config, platform))
    c1, c2 = st.columns([2, 3], vertical_alignment="center")
    if c1.button("⟳ Обновить города из КП", key=f"kp-refresh-{platform}",
                 use_container_width=True,
                 help="Перечитать таблицу КП и заменить список городов. Город попадает "
                      "в список 2ГИС, если в его строке есть ссылка на кабинет "
                      "account.2gis.com и статус не «Удалена»."):
        try:
            with st.spinner("Читаю таблицу КП…"):
                ok, note = kp_pull(project_id, config)
        except Exception as e:  # noqa: BLE001 – текст ошибки уже человеческий
            ok, note = False, str(e)
        if ok:
            st.success(note)
            time.sleep(1.6)
        else:
            st.error(note)
            time.sleep(2.5)
        st.rerun()

    synced = local_time(config.get("kpSyncedAt"))
    c2.caption(f"Городов {where}: {total}. "
               + (f"Список из КП обновлён: {synced}." if synced
                  else "Из таблицы ещё не загружали."))


def tab_actualize(project_id: str, config: dict) -> None:
    """
    Актуализация и отзывы. Площадок две – Яндекс.Бизнес и 2ГИС; выбор наверху.

    Обе половины устроены одинаково: тот же выбор городов, тот же прогон, та
    же очередь ответов. Отличаются города (в 2ГИС карточка заведена не везде),
    сессия, кнопка в кабинете и файлы прогона. Поэтому здесь один код с
    параметром, а не две вкладки-близнеца.
    """
    # Переключатель рисуем всегда – даже если у площадки нет городов: иначе
    # непонятно, куда делся 2ГИС.
    st.session_state.setdefault("act-platform", "Яндекс.Бизнес")
    label = st.radio("Площадка", ["Яндекс.Бизнес", "2ГИС"], horizontal=True,
                     key="act-platform", label_visibility="collapsed")
    platform = rv.GIS if label == "2ГИС" else rv.YANDEX
    kind = runner.PLATFORMS[platform]["kind"]
    prefix = "act" if platform == rv.YANDEX else "actgis"

    countries = platform_countries(config, platform)
    if not countries:
        if platform == rv.GIS:
            html(T.empty("📍", "Городов 2ГИС нет",
                         "Города берутся из блока «2ГИС» таблицы КП. Город попадает "
                         "сюда, если у него есть ссылка на кабинет и статус не "
                         "«Удалена». Кнопка ниже перечитает таблицу прямо сейчас."))
        else:
            html(T.empty("🏙", "Нет городов", "Добавьте страны и города во вкладке «Города»."))
        kp_refresh_row(project_id, config, platform)
        reviews_queue_block(project_id, platform)
        return

    all_ids = [ct["id"] for c in countries for ct in c["cities"]]
    chosen = _act_selected(all_ids, prefix)
    # Значения галочек читаем ДО отрисовки: те, что браузер прислал, а
    # нарисовать в этот проход не успеем (страну свернули), иначе пропали бы.
    _act_sync_widgets(all_ids, prefix)
    selected = [cid for cid in all_ids if cid in chosen]

    state = runner.read_state(project_id, kind)
    running = state.get("status") == "running"
    busy = runner.busy_reason(project_id, kind)   # пусто – запускать можно

    drawn: list[str] = []          # чьи галочки реально нарисованы в этот проход
    with st.container(border=True):
        if platform == rv.GIS:
            html('<div class="card-title">🔄 Актуализация 2ГИС</div>')
            html('<div class="hint" style="margin-bottom:12px">Click зайдёт в раздел '
                 '<b>«Данные о компании»</b> каждого города и нажмёт <b>«Данные верны»</b>, '
                 'если 2ГИС просит подтвердить, что данные не изменились. Плашки нет – '
                 'подтверждать нечего.</div>')
        else:
            html('<div class="card-title">🔄 Актуализация данных</div>')
            html('<div class="hint" style="margin-bottom:12px">Скрипт зайдёт в раздел «Данные» каждого города '
                 'и нажмёт кнопку <b>«Данные актуальны»</b>, если она там есть. Кнопка появляется на странице '
                 'периодически – Яндекс просит подтверждать, что данные не изменились. '
                 'Если кнопки нет – актуализация не требуется.</div>')

        # Обновление списка – здесь же, над самим списком: искать его в
        # «Настройках» человеку неоткуда (см. kp_refresh_row).
        kp_refresh_row(project_id, config, platform)

        head, act = st.columns([3, 1])
        head.markdown(
            f'<div style="font-size:13px;font-weight:700;color:var(--text)">Выбрано: '
            f'<span style="color:var(--acc)">{len(selected)}</span> / {len(all_ids)} городов</div>',
            unsafe_allow_html=True)
        all_on = len(selected) == len(all_ids)
        act.button("Снять все" if all_on else "Выбрать все", key=f"{prefix}-toggle-all",
                   use_container_width=True, on_click=_act_set, args=(all_ids, not all_on, prefix))

        open_key = f"{prefix}-open"
        html(T.tile_css([
            (f"tile-row-{prefix}-{n}", row_vars(c, chosen,
                                                "свернуть ▾" if st.session_state.get(open_key) == c["id"]
                                                else "изменить ▸"))
            for n, c in enumerate(countries)
        ]))
        for n, c in enumerate(countries):
            ids = [ct["id"] for ct in c["cities"]]
            picked = sum(1 for cid in ids if cid in chosen)
            is_open = st.session_state.get(open_key) == c["id"]
            with st.container(key=f"tile-row-{prefix}-{n}"):
                st.button(c["name"], key=f"{prefix}-row-{c['id']}", use_container_width=True,
                          type="primary" if is_open else "secondary",
                          on_click=_toggle_open, args=(open_key, c["id"]))
            # Города рисуем только для раскрытой страны – иначе 117 чекбоксов
            # строились бы при каждом клике по чему угодно.
            if not is_open:
                continue
            with st.container(border=True):
                st.button("Снять все в стране" if picked == len(ids) else "Выбрать все в стране",
                          key=f"{prefix}-toggle-{c['id']}",
                          on_click=_act_set, args=(ids, picked != len(ids), prefix))
                # Галочка переключается сразу, без кнопки «применить»: on_change
                # правит только набор выбранных, а не пересобирает состояние.
                with st.container(key="city-grid"):
                    per_row = 7
                    for start_i in range(0, len(c["cities"]), per_row):
                        cols = st.columns(per_row)
                        for col, ct in zip(cols, c["cities"][start_i:start_i + per_row]):
                            wkey = f"{prefix}-cb-{ct['id']}"
                            # Источник правды – набор выбранных, галочка лишь
                            # его отражение. Через value= это не сделать: у
                            # виджета есть key, и Streamlit берёт значение из
                            # session_state, а value молча игнорирует.
                            want = ct["id"] in chosen
                            if st.session_state.get(wkey) != want:
                                st.session_state[wkey] = want
                            col.checkbox(ct["name"], key=wkey,
                                         on_change=_act_toggle, args=(ct["id"], wkey, prefix))
                drawn.extend(ids)

    # Что нарисовали – помним до следующего прохода: подбор значений и
    # «Выбрать все» имеют право трогать только эти галочки.
    st.session_state[f"{prefix}-drawn"] = drawn

    logged_in = (gis.has_saved_session(project_id) if platform == rv.GIS
                 else yb.has_saved_session(project_id))
    if not logged_in and not running:
        where = "2ГИС" if platform == rv.GIS else "Яндекс"
        st.warning(f"Сначала войдите в {where} в разделе «⚙️ Настройки».")
        # Очередь ответов показываем и без входа: черновики уже написаны и
        # никуда не делись, а без этой строки они бы просто исчезли с экрана –
        # раздел уходил в выход раньше, чем до них доходило дело.
        reviews_queue_block(project_id, platform)
        return

    # По умолчанию ВКЛЮЧЕНА – прогон почти всегда делают вместе с отзывами.
    # Ставим через session_state, а не через value: у виджета есть key, и
    # Streamlit ругается, когда состояние приходит из обоих мест сразу.
    reviews_key = f"{prefix}-reviews"
    st.session_state.setdefault(reviews_key, True)
    with_reviews = st.checkbox(
        "💬 Заодно проверить отзывы и подготовить ответы", key=reviews_key,
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
            tail = ("" if platform == rv.YANDEX else
                    " Отзывы с Flamp, Otello и прочих площадок 2ГИС показывает вместе со "
                    "своими – отвечать на них оттуда нельзя, они попадут в список со ссылкой.")
            st.caption(f"Черновики пишутся только на отзывы в {rv.GOOD_RATING} звёзд, "
                       f"не больше {rv.MAX_DRAFTS_PER_CITY} на город. "
                       "Всё, что ниже, попадёт в список без ответа – отвечаете сами. "
                       "Прогон станет примерно в полтора раза длиннее." + tail)

    # Порядок как у человека в голове: сначала запускаем, потом смотрим, как
    # идёт. Раньше панель с «Посмотреть отчёт» стояла НАД кнопкой запуска, а
    # ещё ниже висела вторая карточка отчёта – выглядело нацепленным сверху.
    if st.button(f"🔄 Запустить актуализацию ({cities_word(len(selected))})", type="primary",
                 use_container_width=True, disabled=bool(busy) or not selected,
                 key=f"btn-{prefix}-run"):
        selection = {c["id"]: [ct["id"] for ct in c["cities"] if ct["id"] in chosen]
                     for c in countries}
        save_actualize_tasks(project_id, config, selection, platform)
        ok, msg = runner.start_actualize(project_id, headless=bool(get_settings(project_id)["headless"]),
                                         with_reviews=bool(with_reviews), platform=platform)
        (st.toast if ok else st.error)(msg)
        time.sleep(0.6)
        st.rerun()
    if running:
        st.button("⏹ Остановить", use_container_width=True, key=f"btn-stop-{prefix}",
                  on_click=runner.request_stop, args=(project_id, kind))
    elif busy:
        st.caption(busy)

    # Панель показываем, только когда есть что показывать: до первого прогона
    # пустая рамка с пустым логом только мешает.
    if running or state.get("status") not in (None, "idle"):
        with st.container(border=True):
            live_panel(project_id, running, kind)

    # Очередь ответов – В КОНЦЕ, под запуском и прогрессом. Раньше она стояла
    # первой, и во время прогона экран открывался на ней: сколько городов
    # пройдено и что происходит, видно не было – приходилось листать вниз.
    # Заказчик попросила поменять местами: сверху сам прогон, ответы под ним.
    reviews_queue_block(project_id, platform)

    # Вторая карточка отчёта убрана: весь отчёт теперь на вкладке «Отчёт»,
    # и туда ведёт кнопка «Посмотреть отчёт» из панели выше.


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: ГОРОДА
# ════════════════════════════════════════════════════════════════════

def _kp_sheet_settings_block(project_id: str, config: dict) -> None:
    """
    Источник городов – ссылка на таблицу КП и явный выбор листа.

    Раньше лист внутри таблицы подбирался эвристикой в двух разных местах
    («Города» и «Сверка») по-разному, и они могли молча выбрать разные листы.
    Теперь выбор один, здесь, и сохраняется в конфиге – «Города» и «Сверка»
    просто читают то, что тут выбрано.
    """
    html('<div class="card-title">📊 Источник городов – Google-таблица КП</div>')
    saved_url = (config.get("kpSheetUrl") or "").strip()
    saved_title = (config.get("kpSheetTitle") or "").strip()
    effective = kp_sheet.sheet_url(project_id, saved_url)
    has_key = kp_sheet.service_account_info() is not None

    url = st.text_input("Ссылка на таблицу КП этого проекта", value=saved_url,
                        key=f"kp-url-{project_id}",
                        placeholder="https://docs.google.com/spreadsheets/d/…")
    if url.strip() != saved_url:
        config["kpSheetUrl"] = url.strip()
        # Сменили таблицу – старый выбор листа к ней уже не относится.
        config["kpSheetTitle"] = ""
        save_config(project_id)
        _audit_forget()
        st.rerun()

    if not effective:
        st.caption("Ссылку можно задать здесь или секретом `kp_sheet_url_"
                   f"{project_id}` в настройках приложения.")
        return
    if not saved_url:
        st.caption(f"Используется таблица проекта по умолчанию: {effective}")
    if not has_key:
        st.warning(
            "Не найден ключ сервисного аккаунта Google. Добавьте в секреты приложения "
            "`gcp_service_account_b64` – весь JSON-ключ в base64. Таблица должна быть "
            "расшарена на этот аккаунт как Читатель.",
            icon="🔑",
        )
        return

    try:
        titles, prefer = _audit_cache(f"titles|{project_id}|{saved_url}",
                                      lambda: kp_sheet.sheet_titles(project_id, saved_url))
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    if not titles:
        st.error("В таблице нет ни одного листа.")
        return

    # Приоритет подсказки: уже сохранённый выбор → лист по gid из ссылки →
    # угадка по названию («КП», «Карта присутствия» и т.п.). Содержимое
    # листов тут не читаем – это только подсказка, выбирает человек.
    default = saved_title if saved_title in titles else (
        prefer if prefer in titles else kp_sheet.guess_sheet(titles, prefer))

    # bottom – чтобы кнопка встала вровень с самим полем выбора, а не с его
    # подписью: у селектбокса сверху есть заголовок, у кнопки его нет, и без
    # выравнивания она висела выше поля.
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    title = c1.selectbox("Лист таблицы", titles,
                         index=titles.index(default) if default in titles else 0,
                         key=f"kp-title-{project_id}")
    if title != saved_title:
        config["kpSheetTitle"] = title
        save_config(project_id)
    if c2.button("↻ Обновить список листов", key=f"kp-titles-refresh-{project_id}",
                use_container_width=True):
        _audit_forget()
        st.rerun()

    if not saved_title:
        st.caption("Лист подобран по названию – проверьте, что это тот самый, где ведётся "
                   "работа, и при необходимости выберите другой.")
    st.caption("Этот лист читают и «Города», и «Сверка» – выбор общий на весь проект.")

    # Загрузка живёт здесь же, рядом с выбором таблицы и листа: раньше кнопка
    # стояла на «Городах», и человек, сменив лист в настройках, не понимал,
    # куда идти, чтобы список перечитался.
    st.divider()
    c1, c2 = st.columns([2, 3])
    if c1.button("⬇️ Загрузить города из таблицы", type="primary",
                 key=f"kp-pull-{project_id}", use_container_width=True):
        try:
            with st.spinner("Читаю таблицу КП…"):
                ok, note = kp_pull(project_id, config)
        except Exception as e:  # noqa: BLE001
            ok, note = False, str(e)
        if not ok:
            st.error(note)
            return
        st.success(note)
        time.sleep(1.2)
        st.rerun()

    synced = local_time(config.get("kpSyncedAt"))
    total = sum(len(c.get("cities") or []) for c in config.get("countries") or [])
    c2.caption((f"Последняя загрузка: {synced} · сейчас {cities_word(total)}" if synced
                else "Города из таблицы ещё не загружались."))
    st.caption(f"Загружается само: если с последней загрузки прошло больше "
               f"{KP_SYNC_TTL_HOURS} часов, Click перечитает таблицу при открытии проекта. "
               "Кнопка – когда нужно прямо сейчас.")
    st.caption("Загрузка ЗАМЕНЯЕТ список стран и городов данными из таблицы. "
               "Карточки со статусом «Удалена» не попадают.")


def tab_cities(project_id: str, config: dict) -> None:
    html('<div class="card-title">Страны и города проекта</div>')

    # Сколько всего городов и где обновляется список. Блок загрузки из КП
    # переехал в «Настройки», к самой ссылке на таблицу и выбору листа:
    # держать выбор листа в одном месте, а кнопку «загрузить» в другом было
    # неоткуда понять.
    total = sum(len(c.get("cities") or []) for c in config.get("countries") or [])
    countries = len(config.get("countries") or [])
    if total:
        st.caption(f"Всего {cities_word(total)} в "
                   f"{plural(countries, 'стране', 'странах', 'странах')}. "
                   "Списки Яндекса и 2ГИС – из одной таблицы КП, но города в них разные: "
                   "карточка 2ГИС заведена не везде.")
    else:
        st.info("Городов пока нет – загрузите их из таблицы КП.")
    # Кнопка загрузки – и здесь тоже, а не только в «Настройках»: список
    # городов смотрят на этой странице, обновлять его логично отсюда же.
    kp_refresh_row(project_id, config, rv.YANDEX)

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
    # Не сбой Click – статус в КП разошёлся с площадкой (карточки нет, а КП
    # обещал живую, или наоборот). Отдельно от «Ошибок»: одно говорит
    # «поправьте таблицу», другое – «Click не справился», путать нельзя.
    {"key": "statusMismatch", "status": "status-mismatch", "label": "Статус в КП не совпал",
     "colour": "warn", "always": False},
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
    if totals.get("statusMismatch"):
        notes.append(f'🚦 {cities_word(totals["statusMismatch"])} со статусом в КП, который не '
                     "совпал с площадкой (например, в таблице «Активная», а карточки там нет). "
                     "Это не сбой Click – поправьте статус в КП или разберитесь с карточкой.")
    if totals.get("skipped"):
        notes.append(f'⏭ {cities_word(totals["skipped"])} пропущено: этот же текст уже уходил '
                     "в эти карточки недавно (защита от дублей).")
    if totals.get("retried"):
        notes.append(f'⚡ {cities_word(totals["retried"])} удалось со второй попытки.')
    if data.get("withReviews"):
        rt = data.get("reviewTotals") or {}
        notes.append(f'💬 Отзывы: без ответа {rt.get("found", 0)} · '
                     f'черновиков {rt.get("drafted", 0)} · '
                     f'негативных {rt.get("needsHuman", 0)} · '
                     f'без черновика {rt.get("noDraft", 0)}. '
                     "Ответы ждут подтверждения в «Актуализации».")
    return notes


def _report_shots(project_id: str, data: dict) -> None:
    """
    Снимки экрана в момент сбоя – прямо в отчёте.

    Прогон их сохранял и раньше, но добраться до них было нельзя: в логе
    стояло имя файла, а файл лежал в облаке. Отчёт говорил «плашки нет»,
    глаза говорили «есть», и рассудить их было нечем. Показываем по одному:
    два десятка картинок разом – это лишняя память на ровном месте.
    """
    shots = [(r.get("cityName") or "?", r["screenshot"])
             for r in (data.get("results") or []) if r.get("screenshot")]
    if not shots:
        return
    folder = runner.p_screenshots(project_id)
    st.markdown("---")
    st.caption(f"📸 Снимки экрана в момент сбоя ({len(shots)}) – "
               "что приложение видело на странице")
    names = [c for c, _ in shots]
    picked = st.selectbox("Город", names, key="shot-city", label_visibility="collapsed")
    fp = folder / dict(shots)[picked]
    if fp.exists():
        st.image(str(fp), use_container_width=True)
    else:
        st.caption("Снимок не сохранился – облако могло перезапуститься с тех пор.")


def _gis_photo_cell(r: dict) -> str:
    """Колонка «Фото_2ГИС»: счёт или причина, по которой счёта нет."""
    gp = r.get("gisPhotos")
    if not gp:
        return ""
    if gp.get("uploaded"):
        return f'{gp["uploaded"]} из {gp.get("requested", 0)}'
    return re.sub(r"[;\r\n]", " ", gp.get("reason") or "не отправлено")


def _report_csv(data: dict) -> bytes:
    has_gis = any(r.get("gisPhotos") for r in data.get("results") or [])
    head = "Страна;Город;Статус;Причина;Время_сек;URL"
    rows = [head + (";Фото_2ГИС" if has_gis else "")]
    for r in data.get("results") or []:
        reason = re.sub(r"[;\r\n]", " ", (r.get("reason") or ""))
        if r.get("imageError"):
            reason += f' · фото: {r["imageError"]}'
        cells = [
            str(r.get("country") or r.get("package") or ""),
            str(r.get("cityName") or ""),
            str(r.get("status") or ""),
            reason,
            f'{(r.get("durationMs") or 0) / 1000:.1f}',
            str(r.get("companyUrl") or ""),
        ]
        if has_gis:
            cells.append(_gis_photo_cell(r))
        rows.append(";".join(cells))
    return ("﻿" + "\n".join(rows)).encode("utf-8")


_REPORT_KINDS = {"publish": "📤 Публикация", "actualize": "🔄 Актуализация",
                 "actualize-gis": "2ГИС"}


# ════════════════════════════════════════════════════════════════════
#  Кросспостинг: план из реестра
# ════════════════════════════════════════════════════════════════════
# Реестр («контент-план») – Google-таблица, которую заказчик ведёт руками:
# лист на бренд, пост – блок строк (дата, текст, фото, тип и ниже строки по
# соцсетям). Раздел показывает этот план внутри Click и то, что с ним уже
# сделано. Разбор таблицы – content_plan.py, память о сделанном – crosspost_state.py.

CROSSPOST_HORIZON_DAYS = 14      # минимум, сколько дней вперёд показываем план


def _crosspost_horizon(today):
    """
    До какого дня показываем план: до конца ТЕКУЩЕГО месяца, но не меньше
    двух недель вперёд.

    Было ровно 14 дней – и заказчица, перенеся пост с 11-го на 30-е, перестала
    его видеть: 30-е за горизонт уходило. А планируют помесячно, реестр так и
    свёрстан – «Август 2026» отдельным заголовком. В конце месяца две недели
    всё равно остаются: последние числа не должны обрывать план на послезавтра.
    """
    from calendar import monthrange
    end_of_month = today.replace(day=monthrange(today.year, today.month)[1])
    return max(end_of_month, today + timedelta(days=CROSSPOST_HORIZON_DAYS))


def _crosspost_source_block(project_id: str, config: dict) -> bool:
    """
    Источник реестра. Доступ – тот же сервисный аккаунт Google, что читает КП:
    заказчику достаточно расшарить на него таблицу реестра, новых ключей не нужно.
    Возвращает True, если из чего читать.
    """
    saved = (config.get("planSheetUrl") or "").strip()
    has_key = kp_sheet.service_account_info() is not None

    with st.expander("🗓 Источник реестра – Google-таблица контент-плана",
                     expanded=not saved and not st.session_state.get(f"plan-file-{project_id}")):
        url = st.text_input("Ссылка на таблицу реестра", value=saved,
                            key=f"plan-url-{project_id}",
                            placeholder="https://docs.google.com/spreadsheets/d/…")
        if url.strip() != saved:
            config["planSheetUrl"] = url.strip()
            save_config(project_id)
            st.rerun()

        st.caption(f"Лист бренда в таблице – «{project_id}» или его русское имя "
                   f"(СМУ, ИМП, МПЭ, МПИ, АПС). Читаются колонки «Когда выложить», "
                   f"«Соцсеть», «Ссылка», «Формат», «Тип», «Пост», «Фото».")
        if not has_key:
            st.warning(
                "Не найден ключ сервисного аккаунта Google – тот же, что читает КП. "
                "Расшарьте таблицу реестра на него как Читателя. Пока ключа нет, "
                "план можно посмотреть, загрузив файл ниже.", icon="🔑")

        # Файл – запасной путь: посмотреть свой план можно сразу, не дожидаясь
        # доступов. Файл живёт только в этой вкладке и никуда не сохраняется.
        up = st.file_uploader("…или загрузите выгрузку реестра (.xlsx) для просмотра",
                              type=["xlsx"], key=f"plan-file-{project_id}")
        if up is not None:
            st.session_state[f"plan-upload-bytes-{project_id}"] = up.getvalue()

    return bool((config.get("planSheetUrl") or "").strip()
                or st.session_state.get(f"plan-upload-bytes-{project_id}"))


def _crosspost_load_posts(project_id: str, config: dict) -> tuple[list[dict], str]:
    """
    Посты реестра: (список, ошибка). Таблица – источник правды, поэтому читаем
    её заново по кнопке и при первом открытии, а между перерисовками держим в
    session_state: Streamlit перезапускает скрипт на каждое нажатие, и без
    этого раздел ходил бы в Google на каждый клик.
    """
    cache_key = f"plan-posts-{project_id}"
    if st.session_state.get(cache_key) is not None and not st.session_state.pop(f"plan-refresh-{project_id}", False):
        return st.session_state[cache_key], ""

    raw = st.session_state.get(f"plan-upload-bytes-{project_id}")
    try:
        if raw:
            import io
            posts = content_plan.parse_workbook_bytes(io.BytesIO(raw), project_id)
        else:
            url = (config.get("planSheetUrl") or "").strip()
            if not url:
                return [], ""
            posts = content_plan.load_from_google(url, project_id)
    except Exception as e:  # noqa: BLE001 – причину показываем человеку словами
        return [], str(e)

    st.session_state[cache_key] = posts
    return posts, ""


def _crosspost_targets_html(post: dict, state: dict) -> str:
    """Строка площадок поста: «ВК 🕓 · ОК ✅ · ТГ –»."""
    # Видео и статьи Click не формирует. Показываем это прямо в строке, иначе
    # человек будет ждать, что пост «поедет», и не поймёт, почему он стоит.
    fmt = (post.get("format") or "Пост").strip()
    if fmt.lower() != "пост":
        return f'<span class="cp-net cp-net-off">{T.esc(fmt.lower())} – вручную</span>'
    bits = []
    for t in post.get("targets", []):
        net = t.get("network") or ""
        name = cps.network_ru(net)
        if net not in content_plan.SUPPORTED:
            bits.append(f'<span class="cp-net cp-net-off">{T.esc(name)} ✎</span>')
            continue
        link = (t.get("published_link") or "").strip()
        if link:
            bits.append(f'<a class="cp-net cp-net-ok" href="{T.esc(link)}" target="_blank">'
                        f'{T.esc(name)} ✅</a>')
            continue
        status = cps.status_of(state, post, net)
        ico, _, cls = cps.HUMAN[status]
        bits.append(f'<span class="cp-net cp-net-{cls or "wait"}">{T.esc(name)} {ico}</span>')
    return " ".join(bits)


def _crosspost_plan_row(post: dict, state: dict) -> str:
    """Одна строка плана: дата и час, тип, текст, площадки."""
    when = apptime.to_local(post.get("when"))
    day = when.strftime("%d.%m") if when else post.get("date", "")
    weekday = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][when.weekday()] if when else ""
    # В превью – текст без разметки: маркеры **…** здесь только мусорят.
    text = " ".join(post_text.strip_markup(post.get("text") or "").split())
    if len(text) > 90:
        text = text[:90] + "…"
    photos = f'📷 {len(post["images"])}' if post.get("images") else "–"
    return (
        '<div class="report-row">'
        f'<span class="cp-day">{T.esc(day)} <em>{T.esc(weekday)}</em> '
        f'{T.esc(post.get("time", ""))}</span>'
        f'<span class="cp-type">{T.esc(post.get("post_type") or "–")}</span>'
        f'<span class="report-row-reason">{T.esc(text or "нет текста")}</span>'
        f'<span class="cp-photos">{T.esc(photos)}</span>'
        f'<span class="cp-nets">{_crosspost_targets_html(post, state)}</span>'
        '</div>'
    )


def _crosspost_has_source(project_id: str, config: dict) -> bool:
    """Есть ли из чего читать план: ссылка на таблицу или загруженная выгрузка."""
    return bool((config.get("planSheetUrl") or "").strip()
                or st.session_state.get(f"plan-upload-bytes-{project_id}"))


def _crosspost_bar(project_id: str, config: dict, posts: list[dict],
                   upcoming: list[dict], state: dict, horizon: date) -> None:
    """
    Первое, что видно на вкладке, отвечает на главный вопрос: идёт ли всё само.
    Раньше ответ приходилось собирать из галочки внизу страницы, четырёх цифр
    и списка в середине – и «работает ли автопостинг» оставалось непонятным.
    """
    sched = _crosspost_scheduler_state(project_id)
    nearest = crosspost_plan.next_out(posts, state, apptime.now().isoformat())
    if nearest:
        when = apptime.to_local(nearest["when"])
        day = (crosspost_plan.relative_day(when.date(), apptime.now().date())
               or crosspost_plan.human_day(when.date()))
        who = ", ".join(dict.fromkeys(n["name"] for n in nearest["pending"]))
        sub = (f'Ближайший пост – <b>{T.esc(day)} в {T.esc(when.strftime("%H:%M"))}</b>, '
               f'{T.esc(who)}')
    elif upcoming:
        sub = "Ближайшие посты ещё не сформированы – нажмите «Сформировать план»"
    else:
        sub = "Впереди постов нет"

    with st.container(border=True):
        # Состояние и кнопки – одной строкой: слева что происходит, справа что
        # можно нажать. Кнопки формирования здесь нет намеренно: её подпись
        # («ВК: 4, ОК: 4, МАКС: 4, ТГ: 6») в узкую колонку не влезала и ломала
        # строку на три этажа. Она живёт под планом, где ей хватает ширины, –
        # и там же, где человек только что посмотрел, что именно поедет.
        cols = st.columns([6.6, 2, 2], vertical_alignment="center")
        with cols[0]:
            # Ни счётчиков листа, ни устройства планировщика: читать это каждый
            # день не нужно. Час выхода переехал к заголовку плана.
            html(T.crosspost_bar(sched["title"], sub, [], alive=sched["enabled"]))
        with cols[1]:
            _crosspost_scheduler_switch(project_id)
        if cols[2].button("🔄 Обновить план", key=f"plan-refresh-btn-{project_id}",
                          use_container_width=True,
                          help="Перечитать таблицу – после правок в реестре"):
            st.session_state[f"plan-refresh-{project_id}"] = True
            st.session_state.pop(f"plan-posts-{project_id}", None)
            st.rerun()
        if not sched["enabled"]:
            st.caption(sched["note"])


def _crosspost_attention(project_id: str, upcoming: list[dict], state: dict) -> None:
    """Беды – наверх и с датой словами: в календаре жёлтая плитка видна, но молчит."""
    troubles = cps.problems(state, upcoming)
    if not troubles:
        return
    with st.container(border=True):
        html('<div class="card-title">⚠️ Требует внимания '
             f'<span class="badge badge-warn">{len(troubles)}</span></div>')
        for n, tr in enumerate(troubles[:12]):
            post = tr["post"]
            who = f' · {cps.network_ru(tr["network"])}' if tr.get("network") else ""
            d = content_plan.parse_date(post.get("date", ""))
            when = crosspost_plan.human_day(d) if d else post.get("date", "")
            # Ошибка длиной с простыню (лог Playwright) в сводке не читается:
            # показываем первую строку, целиком – в «Отчётах и журналах».
            what = " ".join(str(tr["what"]).split())
            if len(what) > 160:
                what = what[:160] + "… (целиком – в «Отчётах и журналах»)"
            if not tr.get("network"):
                st.write(f'**{when}**{who} – {what}')
                continue
            # У ошибки площадки есть выход прямо здесь: сбросить – и пост по
            # этой площадке снова «ещё не сформирован», без вечной красноты.
            row = st.columns([8, 2], vertical_alignment="center")
            row[0].write(f'**{when}**{who} – {what}')
            if row[1].button("↺ Сбросить", key=f"cp-tr-reset-{project_id}-{n}",
                             use_container_width=True,
                             help="Убрать ошибку: пост по этой площадке снова "
                                  "«ещё не сформировано», можно ставить заново"):
                cps.forget_target(project_id, post, tr["network"])
                st.rerun()
        if len(troubles) > 12:
            st.caption(f"…и ещё {len(troubles) - 12}. Остальное видно в календаре "
                       "жёлтыми и красными плитками.")


def _crosspost_plan_table(project_id: str, config: dict, posts: list[dict],
                          state: dict, today: date, horizon: date,
                          formable: set[str] | None = None) -> list[str]:
    """
    План строками: когда · тип · пост · фото · площадки · что сделать.

    Был календарь недель, и на макете он читался хорошо – но на живом реестре
    постов два-три в неделю, и сетка стояла пустой: две трети экрана «постов
    нет», а текст в узких плитках рвался по слогам. Строки плотнее, а колонки
    площадок дают то же, ради чего затевался календарь: статус ВК, ОК, ТГ и
    МАКС виден сразу и сравнивается по вертикали.

    Слева от таблицы – колонка галочек: что отмечено, то и уедет по кнопке
    «Поставить выбранные». Возвращает ключи отмеченных постов.
    """
    plan = crosspost_plan.rows(posts, state, today, horizon)
    if not plan["rows"]:
        html(T.empty("📭", "Впереди постов нет",
                     "Появятся здесь, как только в реестре будут строки с будущей датой."))
        return []

    # Час выхода стоит у заголовка: даты меняются каждую неделю, а час – нет.
    html(f'<div class="card-title">🗓 План: {T.esc(plan["title"])}'
         f'<span class="hint"> · час выхода '
         f'{T.esc(content_plan.brand_default_time(project_id))} по Екатеринбургу</span></div>')
    html(T.crosspost_legend())

    formable = formable if formable is not None else set()
    if len(formable) < 2:
        # Выбирать не из чего: план сформирован или ждёт формирования ровно
        # один пост. Галочки в этом случае – лишний столбец и лишний щелчок,
        # под таблицей и так стоит одна кнопка на всё.
        html(T.crosspost_table(plan, sheet_url=(config.get("planSheetUrl") or "").strip()))
        return []

    # Что отмечено, известно ДО отрисовки: галочки прошлого прогона лежат в
    # памяти Streamlit, а нажатие на галочку перезапускает страницу. Поэтому
    # подсветка строк в таблице всегда совпадает с тем, что реально отмечено.
    keys = [f'cp-tick-{project_id}-{v["key"]}' for v in plan["rows"]]
    picked = {v["key"] for v, k in zip(plan["rows"], keys)
              if v["key"] in formable and st.session_state.get(k)}
    table = T.crosspost_table(plan, sheet_url=(config.get("planSheetUrl") or "").strip(),
                              picked=picked)

    with st.container(key=f"cp-plan-{project_id}"):
        ticks, table_col = st.columns([1, 32], gap="small", vertical_alignment="top")
        with ticks:
            with st.container(key=f"cp-ticks-{project_id}"):
                html('<div class="cp-ticks-head"></div>')
                for view, key in zip(plan["rows"], keys):
                    if view["key"] not in formable:
                        # Формировать нечего: уже стоит, вышло, публикуется
                        # вручную или в реестре нет текста. Пустая клетка
                        # честнее галочки, которая ничего не сделает.
                        warn = " warn" if view["state"] == "warn" else ""
                        html(f'<div class="cp-tick-off{warn}"></div>')
                        continue
                    st.checkbox(f'{view["date"]} – поставить на отложку', key=key,
                                label_visibility="collapsed",
                                help=f'{view["when_day"]} · {view["kind"] or "пост"}')
        with table_col:
            html(table)
    return sorted(picked)


def _crosspost_login_hint(project_id: str, config: dict) -> None:
    """
    Где включается формирование ВК и ОК. Раньше эта фраза висела у кнопок
    наверху и мозолила глаза каждый день; читают её один раз – при настройке.
    """
    import ok_browser
    import vk_social
    import zen_browser

    not_ready = [name for name, ready in (
        ("ВК", vk_social.has_saved_session(project_id) and (config.get("vkGroupUrl") or "").strip()),
        ("ОК", ok_browser.has_saved_session(project_id) and (config.get("okGroupUrl") or "").strip()),
    ) if not ready]
    if not_ready:
        st.caption(f"Формирование {' и '.join(not_ready)} появится после входа: "
                   f"«Настройки» → «Вход в ВК/ОК (кросспостинг)».")

    # Дзен объясняем отдельно: у него нет своего входа, и это стоит сказать
    # прямо – иначе человек будет искать «Вход в Дзен», которого нет и не будет.
    studio = (config.get("zenStudioUrl") or "").strip()
    if zen_browser.has_saved_session(project_id) and studio:
        st.caption(f"Дзен готов: статьи уходят в студию {studio}. "
                   f"Вход отдельный не нужен – {zen_browser.session_note(project_id)}.")
    elif not studio:
        st.caption("Дзен: укажите студию автора (dzen.ru/profile/editor/…) в блоке "
                   "«Каналы и адреса» – в неё Click и публикует статьи.")
    else:
        st.caption("Дзен ждёт входа в Яндекс – того же, которым публикуется "
                   "Яндекс.Бизнес: «Настройки» → «Вход в Яндекс».")


def _crosspost_form_log_path(project_id: str):
    """Лог последнего формирования – на диске, а не в исчезающем st.status."""
    d = paths.data_root() / project_id / "crosspost"
    d.mkdir(parents=True, exist_ok=True)
    return d / "form-last.log"


def _crosspost_report_block(project_id: str, posts: list[dict], state: dict) -> None:
    """
    Отчёт: что и куда реально ушло – только факты от площадок.

    Каждая строка – пост × площадка: что сделал Click и когда, ссылка на
    вышедшую запись, текст ошибки. «Вышло» здесь пишется в двух случаях, и
    оба – факты: ссылка стоит в реестре (публиковали руками) или площадка
    сама подтвердила выход. Догадок в отчёте нет.
    """
    rows: list[str] = []
    csv_rows: list[list[str]] = []   # то же, но для скачивания одним файлом
    counts = {"set": 0, "live": 0, "bad": 0}
    for post in sorted(posts, key=lambda p: p.get("when") or "", reverse=True):
        for t in post.get("targets", []):
            net = t.get("network") or ""
            saved = cps.target(state, post, net)
            # Отчёт – про работу CLICK, а не про весь реестр. Без этой строки
            # в него попадали все прошлые посты со ссылками из таблицы, и
            # заголовок писал «вышло 439»: пять лет чужой работы, среди
            # которой не найти свои две отложки. Нет записи в памяти Click –
            # значит Click этого не делал, и в отчёте этому места нет.
            if not saved:
                continue
            link = (t.get("published_link") or "").strip()
            at = (saved.get("at") or "")[:16].replace("T", " ")
            # В отчёт попадает только сделанное. «Не тронуто» и «публикуется
            # вручную» – это не событие, а тишина: строк с ней в реестре
            # десятки, и за ними терялось то немногое, что и есть отчёт.
            #
            # И только СДЕЛАННОЕ CLICK'ОМ. Ссылка в реестре без единого
            # действия Click – это история, набитая руками за месяцы: она
            # давала «вышло 439» против «отложек 2», и отчёт становился
            # бесполезным. Заказчица про это и спросила: «зачем мне весь
            # список – и то, что не надо выкладывать, и то, что надо?»
            if link and not saved.get("state"):
                continue
            if link:
                what, cls = f'вышло – <a href="{T.esc(link)}" target="_blank">запись ↗</a>', "ok"
                itog, url_cell = "вышло", link
                counts["live"] += 1
            elif saved.get("state") == cps.SCHEDULED:
                what, cls = f"отложка поставлена {T.esc(at)}", "skip"
                itog, url_cell = f"отложка поставлена {at}", ""
                counts["set"] += 1
            elif saved.get("state") in (cps.SENT, cps.SENT_LATE):
                tail = f' – <a href="{T.esc(saved.get("link", ""))}" target="_blank">запись ↗</a>' \
                    if saved.get("link") else ""
                what, cls = f"вышло {T.esc(at)}{tail}", "ok"
                itog, url_cell = f"вышло {at}", saved.get("link", "")
                counts["live"] += 1
            elif saved.get("state") == cps.FAILED:
                err = " ".join(str(saved.get("error", "")).split())
                what, cls = f'ошибка {T.esc(at)}: {T.esc(err[:140])}', "err"
                itog, url_cell = f"ошибка {at}: {err[:140]}", ""
                counts["bad"] += 1
            elif saved.get("state") == cps.MISSED:
                err = " ".join(str(saved.get("error", "время вышло")).split())
                what, cls = f"пропущено: {T.esc(err[:140])}", "warn"
                itog, url_cell = f"пропущено: {err[:140]}", ""
                counts["bad"] += 1
            else:
                continue
            name = cps.network_ru(net)
            d = content_plan.parse_date(post.get("date", ""))
            day = d.strftime("%d.%m") if d else post.get("date", "")
            head = " ".join(post_text.strip_markup(post.get("text") or "").split())[:60] or "без текста"
            rows.append(
                f'<div class="report-row {cls}">'
                f'<span class="cp-day">{T.esc(day)}</span>'
                f'<span class="cp-type">{T.esc(name)}</span>'
                f'<span class="report-row-reason">{T.esc(head)}</span>'
                f'<span class="report-row-dur">{what}</span></div>')
            csv_rows.append([day, name, head, itog, url_cell])
    title = " · ".join(x for x in (
        f'отложек {counts["set"]}' if counts["set"] else "",
        f'вышло {counts["live"]}' if counts["live"] else "",
        f'ошибок {counts["bad"]}' if counts["bad"] else "") if x) or "пока пусто"
    with st.expander(f"📊 Отчёт: что сделал Click – {title}", expanded=False):
        st.caption("Только работа Click: поставленные отложки, вышедшие посты и "
                   "ошибки. Постов, которых Click не касался, и старых записей "
                   "реестра здесь нет – они видны в плане и в самой таблице.")
        html("".join(rows[:120])
             or T.empty("–", "Click пока ничего не делал",
                        "Здесь появятся поставленные отложки, вышедшие посты и ошибки."))
        if len(rows) > 120:
            st.caption(f"Показаны последние 120 строк из {len(rows)}.")
        # Отчёт скачивается целиком (не только видимые 120 строк) – одним CSV,
        # который открывается в Excel/Google-таблицах.
        if csv_rows:
            head = "Дата;Площадка;Текст;Итог;Ссылка"
            body = [head]
            for cells in csv_rows:
                body.append(";".join(re.sub(r"[;\r\n]+", " ", str(c)) for c in cells))
            blob = ("﻿" + "\n".join(body)).encode("utf-8")
            st.download_button(
                "⬇ Скачать отчёт (CSV)", data=blob,
                file_name=f"otchet-click-{apptime.now().strftime('%Y-%m-%d')}.csv",
                mime="text/csv", key=f"cp-report-dl-{project_id}",
                use_container_width=True)


def _crosspost_form_last_log(project_id: str) -> None:
    """Лог последнего формирования: тот же протокол, что бежал в статусе."""
    fp = _crosspost_form_log_path(project_id)
    if not fp.exists():
        return
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if not text.strip():
        return
    # Время – прямо в заголовке. Иначе непонятно, этого прогона лог или
    # позавчерашнего: оба выглядят одинаково, и разбирают по ошибке чужой.
    head = text.strip().splitlines()[0].strip()
    when = head.replace("Формирование", "").strip()
    with st.expander("📄 Лог последнего формирования"
                     + (f" – {when}" if when else "")):
        st.text_area("Что Click делал по шагам", value=text, height=240,
                     key=f"cp-form-log-{project_id}")
        st.download_button("⬇ Скачать (.txt)", data=text.encode("utf-8"),
                           file_name="formirovanie-poslednee.txt", mime="text/plain",
                           key=f"cp-form-log-dl-{project_id}")


def _crosspost_diag_block(project_id: str) -> None:
    """
    Разметка площадки для разбора – кнопкой, а не поиском по папкам.

    Зачем это вообще есть. Крестик карточки сайта и жирный в ОК Click
    чинил трижды и трижды мимо: вёрстку площадки приходилось угадывать по
    снимкам экрана. Теперь, когда что-то не вышло, прогон сохраняет кусок
    настоящей разметки рядом с логом – а этот блок даёт его скачать и
    прислать. Один прогон вместо трёх кругов догадок.
    """
    d = paths.data_root() / project_id / "crosspost"
    files = [(f, d / f) for f in ("link-card.html", "ok-editor.html",
                                  "ok-linktoolbar.html", "max-attach.html",
                                  "vk-dialog.html")]
    have = [(name, fp) for name, fp in files if fp.exists()]
    if not have:
        return
    with st.expander("🧩 Разметка площадки для разбора"):
        st.caption("Click сохранил кусок настоящей разметки в тот момент, когда "
                   "не смог убрать карточку сайта или выставить жирный. "
                   "Скачайте и пришлите – по ней правка делается точно.")
        for name, fp in have:
            when = apptime.to_local(
                datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc).isoformat())
            st.download_button(
                f"⬇ {name}" + (f" – от {when.strftime('%d.%m %H:%M')}" if when else ""),
                data=fp.read_bytes(), file_name=name, mime="text/html",
                key=f"cp-diag-{name}-{project_id}")


def _crosspost_tools(project_id: str, config: dict, posts: list[dict],
                     state: dict, today: date, upcoming: list[dict]) -> None:
    """
    Настройка и проверка – три группы вместо девяти раскрывашек подряд.
    Сами блоки не переписаны: это те же функции, просто разложены по полкам.
    """
    html('<div class="card-title">🔧 Настройка и проверка'
         '<span class="hint"> – обычно сюда не нужно, всё уже настроено</span></div>')
    t1, t2, t3 = st.tabs(["🔌 Подключения", "🧪 Проверка отложки", "📊 Отчёты и журналы"])

    with t1:
        _crosspost_login_hint(project_id, config)
        _crosspost_source_block(project_id, config)
        _crosspost_channels_block(project_id, config)
        _crosspost_yb_block(project_id, config)

    with t2:
        # Каждая сеть с родной отложкой – своей пробой. Логи зовём ОТСЮДА, а не
        # из конца самой пробы: у той три ранних выхода (нет сессии, нет
        # сообщества, идёт прогон), и лог прошлого раза на них молча пропадал бы.
        for network in PROBE_NETWORKS:
            _crosspost_probe(project_id, config, network)
            _probe_last_log(project_id, network)

    with t3:
        _crosspost_report_block(project_id, posts, state)
        _crosspost_form_last_log(project_id)
        _crosspost_diag_block(project_id)
        _crosspost_scheduler_journal()
        # Прошлые посты – архив, а не рабочий список. Блок живёт ЗДЕСЬ, где
        # есть posts/today/state: в отдельной функции их нет (на этом раздел
        # когда-то падал).
        with st.expander("Показать весь реестр листа (включая прошлые)"):
            st.caption("Прошлые посты со ссылкой в колонке «Ссылка» считаются вышедшими.")
            past = [p for p in posts
                    if (d := content_plan.parse_date(p["date"])) and d < today]
            html("".join(_crosspost_plan_row(p, state) for p in past[-40:])
                 or T.empty("–", "Прошлых постов нет", ""))


def tab_crosspost(project_id: str, config: dict) -> None:
    html(T.crosspost_css())

    if not _crosspost_has_source(project_id, config):
        html(T.empty("🗓", "Реестр не подключён",
                     "Укажите ссылку на таблицу контент-плана – или загрузите "
                     "выгрузку .xlsx, чтобы посмотреть план прямо сейчас."))
        _crosspost_source_block(project_id, config)
        # Выгрузку кладёт в session_state сам загрузчик – уже во время этой
        # отрисовки. Проверка выше её не застала, поэтому перерисовываем сразу:
        # иначе человек загрузил файл и всё равно видит «реестр не подключён».
        if _crosspost_has_source(project_id, config):
            st.rerun()
        return

    posts, err = _crosspost_load_posts(project_id, config)
    if err:
        # Не тупик: рядом с ошибкой оставляем сам источник реестра. Иначе
        # человек видит красную полосу и всё – ни поменять ссылку, ни
        # загрузить выгрузку неоткуда, настройки лежат ниже по странице,
        # которой уже нет.
        st.error(f"Не удалось прочитать реестр: {err}")
        _crosspost_source_block(project_id, config)
        return
    if not posts:
        html(T.empty("🗓", "В листе бренда постов не нашлось",
                     f"Проверьте, что в таблице есть лист «{project_id}» (или СМУ/ИМП/МПЭ/МПИ/АПС) "
                     "с колонками «Когда выложить», «Соцсеть», «Пост»."))
        _crosspost_source_block(project_id, config)
        return

    today = apptime.now().date()
    horizon = _crosspost_horizon(today)
    upcoming = [p for p in posts
                if (d := content_plan.parse_date(p["date"])) and today <= d <= horizon]
    state = cps.load(project_id)

    # Что можно формировать, считаем один раз: по этому же списку в таблице
    # появляются галочки, а под ней – кнопки с числами по площадкам.
    todo = _crosspost_form_todo(project_id, config, upcoming, state)
    formable = {cps.post_key(c["post"]) for c in _crosspost_form_choices(todo)}

    _crosspost_bar(project_id, config, posts, upcoming, state, horizon)
    _crosspost_attention(project_id, upcoming, state)
    picked_keys = _crosspost_plan_table(project_id, config, posts, state, today, horizon,
                                        formable=formable)
    _crosspost_form_block(project_id, config, upcoming, state, hints=False,
                          todo=todo, picked_keys=picked_keys)
    # Сброс памяти – сразу под кнопками, а не в глубине «Настройки и
    # проверки»: нужен он ровно в ту минуту, когда запись удалили в соцсети
    # или отложка встала с ошибкой, – и искать его по вкладкам не должны.
    _crosspost_forget_block(project_id, upcoming, state)
    _crosspost_tools(project_id, config, posts, state, today, upcoming)

    # Честно про границы: что работает и при каких условиях.
    st.info("Click формирует ВК, ОК и МАКС – родными отложками после входа: "
            "запись держит сама площадка, и в час выхода Click не нужен. "
            "**Телеграм пока не формируется** – в плане он помечен «вручную», "
            "и сам Click в час выхода больше ничего не публикует. ЯБ – прогон "
            "по расписанию, как был.", icon="ℹ️")


def _crosspost_channels(config: dict) -> dict[str, str]:
    """
    Кому ставим задание планировщику.

    МАКС попадает сюда ТОЛЬКО как запасной путь – когда ссылки на канал в
    веб-версии нет, а id для бота есть. Есть ссылка – МАКС формируется
    родной отложкой, и задание боту было бы вторым экземпляром того же
    поста.
    """
    max_native = bool((config.get("maxWebUrl") or "").strip())
    return {"tg-client": (config.get("tgChannelClient") or "").strip(),
            "tg-staff": (config.get("tgChannelStaff") or "").strip(),
            "max": "" if max_native else (config.get("maxChatId") or "").strip()}


def _crosspost_forget_block(project_id: str, upcoming: list[dict], state: dict) -> None:
    """
    «Сформировать заново»: забыть, что пост уже формировали.

    Зачем это нужно. Click помнит сделанное, чтобы не наплодить дублей, – и
    правильно делает. Но память живёт у него, а отложки – в соцсети, и они
    расходятся: заказчица удалила записи в ВК, а Click по-прежнему считал их
    поставленными и заново формировать отказывался. Выхода из этого не было
    вовсе. Теперь есть – и он ручной нарочно: забыть пост можно только
    осознанно, глядя на список.
    """
    done = []
    for post in upcoming:
        for t in post.get("targets", []):
            if (t.get("published_link") or "").strip():
                continue
            if cps.status_of(state, post, t["network"]) in (
                    cps.SCHEDULED, cps.SENT, cps.SENT_LATE, cps.FAILED, cps.MISSED):
                done.append(post)
                break
    if not done:
        return

    with st.expander("↺ Сформировать заново – если запись удалили в соцсети "
                     "или отложка встала с ошибкой"):
        st.caption("Click помнит, что уже формировал, и второй раз не делает – так "
                   "не появляются дубли. Если запись из соцсети удалили или отложка "
                   "не встала, сбросьте память по нужным площадкам: пост по ним "
                   "снова станет «ещё не сформировано».")
        titles = {}
        for post in done:
            nets = ", ".join(
                f'{cps.network_ru(t["network"])} – {cps.HUMAN[cps.status_of(state, post, t["network"])][1]}'
                for t in post.get("targets", [])
                if not (t.get("published_link") or "").strip()
                and cps.status_of(state, post, t["network"]) != cps.WAITING)
            head = (post.get("text") or "").strip().split("\n")[0][:60] or "без текста"
            titles[f'{post["date"]} · {head} · {nets}'] = post
        picked = st.selectbox("Какой пост", list(titles),
                              key=f"cp-forget-pick-{project_id}")
        post = titles[picked]
        # Сбрасывать можно не весь пост, а отдельные площадки: заказчица
        # удалила записи в ВК и ОК, а отложка МАКС пусть стоит – переставлять
        # её значило бы получить в МАКС второй экземпляр.
        with_state = [t["network"] for t in post.get("targets", [])
                      if not (t.get("published_link") or "").strip()
                      and cps.status_of(state, post, t["network"]) != cps.WAITING]
        chosen = st.multiselect("Какие площадки сбросить", with_state,
                                default=with_state, format_func=cps.network_ru,
                                key=f"cp-forget-nets-{project_id}-{cps.post_key(post)}")
        if st.button("↺ Сбросить и сформировать заново",
                     key=f"cp-forget-go-{project_id}", disabled=not chosen):
            if set(chosen) == set(with_state):
                cps.forget_post(project_id, post)
            else:
                for net in chosen:
                    cps.forget_target(project_id, post, net)
            st.success("Готово – по сброшенным площадкам пост снова в очереди. "
                       "Проверьте, что в соцсети его правда нет, отметьте пост "
                       "галочкой и поставьте заново.")
            st.rerun()


def _crosspost_channels_block(project_id: str, config: dict) -> None:
    """Каналы мессенджеров бренда. Токены ботов – в «Ключах к веб-сервисам»."""
    import tg_social

    with st.expander("💬 Каналы Телеграма и МАКС"):
        st.warning("Телеграм сейчас выключен: Click его не формирует и в час "
                   "выхода в него ничего не отправляет – в плане он помечен "
                   "«вручную». Каналы храним, чтобы не вводить заново, когда "
                   "Телеграм доделаем.", icon="⏸")
        c1, c2, c3 = st.columns(3)
        vals = {
            "tgChannelClient": c1.text_input("ТГ клиенты", value=config.get("tgChannelClient", ""),
                                             key=f"cp-tgc-{project_id}", placeholder="@stalmetural"),
            "tgChannelStaff": c2.text_input("ТГ сотрудники", value=config.get("tgChannelStaff", ""),
                                            key=f"cp-tgs-{project_id}", placeholder="@SMUdaily"),
            "maxChatId": c3.text_input("МАКС: id канала для бота (не обязательно)",
                                       value=config.get("maxChatId", ""),
                                       key=f"cp-max-{project_id}"),
            # У Дзена два адреса, и публикует Click во ВТОРОЙ. Первый –
            # публичный канал, он остаётся для ссылок и сверки.
            "zenUrl": st.text_input("Дзен: публичный канал",
                                    value=config.get("zenUrl", ""),
                                    key=f"cp-zen-{project_id}",
                                    placeholder="https://dzen.ru/stalmetural",
                                    help="Адрес канала для читателей. Публикация "
                                         "идёт не сюда, а в студию автора – поле рядом."),
            "zenStudioUrl": st.text_input("Дзен: студия автора (сюда публикуем)",
                                          value=config.get("zenStudioUrl", ""),
                                          key=f"cp-zen-studio-{project_id}",
                                          placeholder="https://dzen.ru/profile/editor/stalmetural",
                                          help="Страница, где пишутся статьи. Открывается "
                                               "тем же входом в Яндекс, что и Яндекс.Бизнес."),
        }
        st.caption("МАКС можно вести двумя путями, и id канала нужен только "
                   "первому. **Бот** шлёт «сейчас», а время держит планировщик – "
                   "значит Click должен работать в час выхода; для него и нужен id. "
                   "**Отложка** – родная, её держит сам МАКС, Click при этом может "
                   "быть выключен; для неё нужен не id, а ссылка на канал в "
                   "веб-версии, и она в «Настройках», блок «🔒 МАКС (кросспостинг)». "
                   "Обычно достаточно второго пути – поле id можно оставить пустым.")
        if any(vals[k].strip() != (config.get(k) or "") for k in vals):
            config.update({k: v.strip() for k, v in vals.items()})
            save_config(project_id)
        if not tg_social.is_configured() and (vals["tgChannelClient"] or vals["tgChannelStaff"]):
            st.caption("Токен бота Телеграма не заполнен – «Настройки» → «Ключи к веб-сервисам».")
        _tg_access_check(project_id, config)


def _tg_access_check(project_id: str, config: dict) -> None:
    """
    «Проверит ли бот каналы» – до часа выхода, а не в него.

    Публикация в Телеграм идёт ботом, и для неё мало токена: бот обязан
    быть АДМИНИСТРАТОРОМ канала с правом отправки сообщений. Пока это не
    сделано, пост просто не уйдёт – и узнать об этом в 11:00, когда он
    должен был выйти, поздно. Кнопка спрашивает у Телеграма заранее.
    """
    import tg_social

    channels = [("клиенты", (config.get("tgChannelClient") or "").strip()),
                ("сотрудники", (config.get("tgChannelStaff") or "").strip())]
    channels = [(who, chat) for who, chat in channels if chat]
    if not channels:
        return
    if not st.button("Проверить доступ бота в каналы", key=f"cp-tg-check-{project_id}"):
        return
    if not tg_social.is_configured():
        st.error("Сначала заполните токен бота: «Настройки» → «Ключи к веб-сервисам» "
                 "→ «Телеграм: токен бота». Берётся у @BotFather.")
        return
    for who, chat in channels:
        why = tg_social.access_advice(chat)
        if why:
            st.error(f"{chat} ({who}): {why}")
        else:
            st.success(f"{chat} ({who}): бот на месте, публиковать может.")


def _crosspost_scheduler_state(project_id: str) -> dict:
    """
    Планировщик для строки состояния: жив ли и что это значит словами.

    Раньше и состояние, и выключатель, и журнал жили одним блоком внизу
    страницы. Состояние переехало наверх – это первое, что человек должен
    узнать про вкладку, – а журнал остался внизу, среди служебного.
    """
    import scheduler

    cfg = scheduler.config()
    running = scheduler.ensure_running() if cfg["enabled"] else False
    if not cfg["enabled"]:
        title = "Планировщик выключен"
        note = ("Задания Телеграма не отправляются. ВК, ОК и МАКС выйдут сами – "
                "их отложку держит сама площадка.")
    elif running or scheduler.is_running_here():
        title = "Автопостинг работает"
        note = (f"Проверка заданий каждые {scheduler.TICK_SECONDS} с, окно опоздания "
                f"{cfg['lateWindowHours']:g} ч. Работает, пока открыт Click.")
    else:
        title = "Автопостинг работает"
        note = "Планировщик уже работает в другой копии Click на этой машине."
    return {"enabled": bool(cfg["enabled"]), "title": title, "note": note}


def _crosspost_scheduler_switch(project_id: str) -> None:
    """Выключатель планировщика – один на весь раздел, в строке состояния."""
    import scheduler

    cfg = scheduler.config()
    # Именно checkbox, а не toggle: CSS приложения рисует галочке квадрат
    # 16×16, и широкий тумблер Streamlit налезал на первую букву подписи.
    on = st.checkbox("Планировщик включён", value=cfg["enabled"], key="cp-sched-on")
    if on != cfg["enabled"]:
        scheduler.set_config(enabled=on)
        st.rerun()


def _crosspost_scheduler_journal() -> None:
    """Журнал планировщика – в группе «Журналы и весь реестр»."""
    import scheduler

    journal = scheduler.tail()
    st.code(journal or "Журнал пуст – планировщик ещё ничего не делал.", language=None)


def _crosspost_form_todo(project_id: str, config: dict, upcoming: list[dict],
                         state: dict) -> dict:
    """Что осталось сформировать по площадкам – и готовы ли к этому входы."""
    import crosspost_form
    import max_browser
    import ok_browser
    import vk_social
    import zen_browser

    channels = _crosspost_channels(config)
    vk_ready = bool(vk_social.has_saved_session(project_id)
                    and (config.get("vkGroupUrl") or "").strip())
    ok_ready = bool(ok_browser.has_saved_session(project_id)
                    and (config.get("okGroupUrl") or "").strip())
    # МАКС – третья площадка с родной отложкой. Готовность у всех трёх
    # считается одинаково: есть сессия И есть ссылка на сообщество.
    max_ready = bool(max_browser.has_saved_session(project_id)
                     and (config.get("maxWebUrl") or "").strip())
    # Дзен – четвёртая площадка с родной отложкой. Отдельного входа у неё нет:
    # пускает сессия Яндекса, та же, что публикует Яндекс.Бизнес.
    zen_ready = bool(zen_browser.has_saved_session(project_id)
                     and (config.get("zenStudioUrl") or "").strip())
    msg_by_net = {net: crosspost_form.pending_for(upcoming, state, net)
                  for net, chat in channels.items() if chat}
    return {
        "channels": channels,
        "vk_ready": vk_ready,
        "ok_ready": ok_ready,
        "max_ready": max_ready,
        "zen_ready": zen_ready,
        "vk": crosspost_form.pending_for(upcoming, state, "vk") if vk_ready else [],
        "ok": crosspost_form.pending_for(upcoming, state, "ok") if ok_ready else [],
        "max": crosspost_form.pending_for(upcoming, state, "max") if max_ready else [],
        "zen": crosspost_form.pending_for(upcoming, state, "zen") if zen_ready else [],
        # Плоским списком – для счётчика «ТГ: 6» (пост×площадка), по сетям –
        # чтобы у выбора постов было видно, куда именно поедет этот пост.
        "msg_by_net": msg_by_net,
        "msg": [p for posts in msg_by_net.values() for p in posts],
    }


def _crosspost_form_parts(todo: dict) -> list[str]:
    """Счётчик для подписи кнопки: [«ВК: 4», «ОК: 4», «МАКС: 4», «ТГ: 6»]."""
    parts = []
    for name, key in (("ВК", "vk"), ("ОК", "ok"), ("МАКС", "max"),
                      ("Дзен", "zen"), ("ТГ", "msg")):
        # get, а не [key]: набор площадок растёт (последним пришёл Дзен), и
        # словарь из старого вызова не должен ронять подпись кнопки.
        if todo.get(key):
            parts.append(f"{name}: {len(todo[key])}")
    return parts


def _crosspost_form_choices(todo: dict) -> list[dict]:
    """
    Посты, которым есть что формировать: [{post, nets}] в порядке плана.

    Один пост обычно ждёт сразу нескольких площадок, поэтому собираем его
    один раз и запоминаем, куда он поедет: в списке выбора это важнее, чем
    сам факт «не сформирован».
    """
    picked: dict[str, dict] = {}
    per_net = [("vk", todo["vk"]), ("ok", todo["ok"]), ("max", todo["max"]),
               ("zen", todo.get("zen") or [])]
    per_net += list(todo["msg_by_net"].items())
    for net, posts in per_net:
        for post in posts:
            item = picked.setdefault(cps.post_key(post), {"post": post, "nets": []})
            name = cps.network_ru(net)
            if name not in item["nets"]:
                item["nets"].append(name)
    return sorted(picked.values(),
                  key=lambda i: ((i["post"].get("when") or ""), i["post"].get("date") or ""))


def _crosspost_form_block(project_id: str, config: dict, upcoming: list[dict],
                          state: dict, hints: bool = True, todo: dict | None = None,
                          picked_keys: list[str] | None = None) -> None:
    """
    «Сформировать план»: ВК, ОК, МАКС и Дзен – родные отложки браузером,
    Телеграм – задания планировщику. Каждая площадка независима; исходы
    пишутся в память сразу, повтор кнопки доформирует только несделанное (Д-6).

    Кнопки две: «Поставить выбранные посты на отложку» – те, что отмечены
    галочками в плане, и «Поставить все» – весь план, как было. Формирование
    у них одно и то же, разница только в том, какие посты в него уходят.

    Дзен стоит в том же ряду, хотя материал у него другой: не пост из ячейки,
    а статья по ссылке на документ. Для человека это одна кнопка – разбираться,
    где пост, а где лонгрид, должен Click, а не заказчик.
    """
    import crosspost_form

    todo = todo if todo is not None else _crosspost_form_todo(project_id, config,
                                                              upcoming, state)
    channels = todo["channels"]
    vk_ready, ok_ready, max_ready = todo["vk_ready"], todo["ok_ready"], todo["max_ready"]
    zen_ready = todo["zen_ready"]
    vk_todo, ok_todo, max_todo, msg_todo = todo["vk"], todo["ok"], todo["max"], todo["msg"]
    zen_todo = todo["zen"]
    if not vk_todo and not ok_todo and not max_todo and not zen_todo and not msg_todo:
        # Рядом с кнопками объяснение не живёт: hints=False зовут из строки
        # состояния, где нужна одна кнопка и ничего больше. Подсказка про вход
        # показывается в «Подключениях», где её и ищут.
        if hints and not (vk_ready or ok_ready or max_ready or zen_ready):
            st.caption("Формирование ВК, ОК, МАКС и Дзена появится после входа: "
                       "«Настройки» → блоки входа в соцсети.")
        return

    parts = _crosspost_form_parts(todo)
    # Дзен тоже ходит браузером, поэтому занятость прогоном касается и его.
    busy = (runner.busy_reason(project_id, "publish")
            if (vk_todo or ok_todo or max_todo or zen_todo) else "")
    if busy:
        st.caption(f"Поставить отложенные посты ({', '.join(parts)}): сейчас нельзя – {busy}.")
        return

    # Выбор постов. Раньше было «либо весь план, либо ничего»: посмотреть, как
    # ляжет один пост, не давая уехать остальным шести, было нельзя вовсе.
    # Галочки стоят в самом плане, слева от строк, – здесь только те посты,
    # что отмечены (ключи приходят из таблицы, чтобы не считать их дважды).
    choices = _crosspost_form_choices(todo)
    by_key = {cps.post_key(c["post"]): c["post"] for c in choices}
    picked = [by_key[k] for k in (picked_keys or []) if k in by_key]

    def run(posts: list[dict], what: dict) -> None:
        site = ((project_endings(project_id).get("contacts") or {})
                .get("Россия") or {}).get("site", "")
        # Свёрнутый статус: заголовок меняется по ходу («ВК: пост 2 из 4…»),
        # а простыня шагов не лезет на страницу – развернуть можно всегда,
        # и весь протокол после прогона лежит в «Логе последнего формирования».
        box = st.status("Формирую…", expanded=False)
        headless = bool(get_settings(project_id)["headless"])
        ok = bad = 0

        # Протокол пишется и на экран, и в файл: st.status исчезает при первой
        # же перерисовке, а вопрос «что именно Click делал» встаёт уже после.
        #
        # В файл – СРАЗУ и на каждой строке, а не одним куском в конце.
        # Раньше запись шла последней строчкой прогона, и пока прогон идёт,
        # в разделе висел лог ПРОШЛОГО формирования: заказчица так и
        # написала – «лог вообще старый висит, нового нет». А если прогон
        # обрывался (перерисовка страницы, перезапуск облака), новый лог не
        # появлялся вовсе, и разбирать было нечего.
        lines: list[str] = [f"Формирование {apptime.now().strftime('%d.%m.%Y %H:%M')}"]

        def flush() -> None:
            try:
                _crosspost_form_log_path(project_id).write_text(
                    "\n".join(lines), encoding="utf-8")
            except OSError:
                pass   # лог не должен ронять формирование

        flush()        # старый лог уступает место новому сразу, а не в конце

        def say(m: str) -> None:
            lines.append(f"[{apptime.now().strftime('%H:%M:%S')}] {m}")
            flush()
            box.write(m)
            # Короткие вехи – в заголовок свёрнутого статуса, чтобы было
            # видно, что происходит, не разворачивая.
            if len(m) <= 60:
                box.update(label=f"Формирую… {m}")

        msg_results = crosspost_form.form_messengers(
            project_id, posts, site, channels, progress=say)
        ok += len(msg_results)
        # Дзену дополнительно нужна почта проекта: у человека в Яндексе
        # несколько аккаунтов, и паспорт спрашивает, каким входить.
        zen_former = functools.partial(crosspost_form.form_zen_all,
                                       email=(config.get("email") or "").strip())
        for net_name, net_todo, former, url_key in (
                ("ВК", what["vk"], crosspost_form.form_vk_all, "vkGroupUrl"),
                ("ОК", what["ok"], crosspost_form.form_ok_all, "okGroupUrl"),
                ("МАКС", what["max"], crosspost_form.form_max_all, "maxWebUrl"),
                ("Дзен", what.get("zen") or [], zen_former, "zenStudioUrl")):
            if not net_todo:
                continue
            say(f"— {net_name}: постов к формированию {len(net_todo)}")
            results = former(project_id, (config.get(url_key) or "").strip(), posts, site,
                             progress=say, headless=headless)
            ok += sum(1 for r in results if r["ok"])
            for r in results:
                if not r["ok"]:
                    bad += 1
                    say(f"❌ {net_name} {r['post']['date']}: {r['error']}")
                # Предупреждения – не ошибки: отложка стоит, но что-то
                # заслуживает внимания (таблица не вставилась, Дзен не
                # подтвердил словами). Их видно в логе, а прогон идёт дальше.
                for w in r.get("warnings") or []:
                    say(f"⚠️ {net_name} {r['post']['date']}: {w}")
        lines.append(f"Итог: запланировано {ok}, с ошибками {bad}")
        flush()
        box.update(label=(f"Готово: запланировано {ok}"
                          + (f", с ошибками {bad}" if bad else "")),
                   state="error" if bad else "complete")
        time.sleep(1.2)
        st.rerun()

    # Выбирать не из чего (один несформированный пост) – кнопка одна и во всю
    # ширину: галочка перед единственной кнопкой была бы лишним щелчком.
    if len(choices) <= 1:
        if st.button(f"📌 Поставить отложенные посты ({', '.join(parts)})", type="primary",
                     key=f"cp-form-all-{project_id}", use_container_width=True):
            run(upcoming, todo)
        return

    # Что уедет по выбранным – считаем тем же кодом, что и по всему плану:
    # у поста может быть готова не всякая площадка, и «отметил один пост –
    # поехало три отложки» человек должен видеть ДО нажатия.
    picked_todo = _crosspost_form_todo(project_id, config, picked, state) if picked else None
    picked_parts = _crosspost_form_parts(picked_todo) if picked_todo else []
    html('<div class="cp-picked-note">'
         + (f'Отмечено <b>{crosspost_plan.plural(len(picked), "пост", "поста", "постов")}</b>'
            f' – уедет {T.esc(", ".join(picked_parts))}'
            if picked else
            "Отметьте галочками слева посты, которые нужно поставить сейчас, – "
            "или ставьте весь план целиком.")
         + "</div>")
    left, right = st.columns(2)
    with left:
        if st.button("📌 Поставить выбранные посты на отложку",
                     key=f"cp-form-picked-{project_id}", use_container_width=True,
                     disabled=not picked,
                     help="Отметьте посты галочками в плане" if not picked else None):
            run(picked, picked_todo)
    with right:
        if st.button(f"📌 Поставить все ({', '.join(parts)})", type="primary",
                     key=f"cp-form-all-{project_id}", use_container_width=True):
            run(upcoming, todo)


def _crosspost_yb_block(project_id: str, config: dict) -> None:
    """
    ЯБ по расписанию. ЯБ живёт не в соцреестре, а в своей очереди задач
    («Публикация» → «Сохранить очередь в задачи»). Здесь эта очередь
    ставится на время: в назначенный час планировщик запустит обычный
    прогон публикации; занято другим прогоном – дождётся и запустит следом.
    """
    import scheduler

    with st.expander("🕚 Яндекс.Бизнес по расписанию"):
        n_files = len(list(runner.p_tasks(project_id).glob("*.json")))
        yb_tasks = [t for t in scheduler.load_tasks(project_id)
                    if t.get("network") == "yb" and t.get("state") in ("waiting", "retry")]

        for t in yb_tasks:
            c1, c2 = st.columns([4, 1])
            c1.write(f"⏰ Прогон запланирован на "
                     f"**{apptime.human(t['when'], '%d.%m.%Y %H:%M')}** (Екатеринбург)")
            if c2.button("Отменить", key=f"yb-cancel-{t['id']}"):
                scheduler.cancel_task(project_id, t["id"])
                st.rerun()

        if not n_files:
            st.caption("Очередь ЯБ пуста. Соберите пост во вкладке «Публикация» → "
                       "«Сохранить очередь в задачи», потом планируйте время здесь.")
            return
        st.caption(f"В очереди ЯБ – {n_files} файл(ов) задач. В назначенный час планировщик "
                   "запустит обычный прогон публикации со всеми защитами; если будет идти "
                   "другой прогон – дождётся его и стартует следом. Click в этот момент "
                   "должен работать.")
        c1, c2, c3 = st.columns([2, 2, 3])
        d = c1.date_input("Дата", key=f"yb-when-d-{project_id}",
                          value=apptime.now().date() + timedelta(days=1))
        default_t = content_plan.brand_default_time(project_id)
        t_ = c2.time_input("Время (Екб)", key=f"yb-when-t-{project_id}",
                           value=datetime.strptime(default_t, "%H:%M").time())
        if c3.button("⏰ Запланировать прогон ЯБ", type="primary",
                     key=f"yb-plan-{project_id}", use_container_width=True):
            when = datetime(d.year, d.month, d.day, t_.hour, t_.minute, tzinfo=apptime.TZ)
            if when <= apptime.now():
                st.error("Это время уже прошло – выберите будущее.")
            else:
                scheduler.queue_task(project_id, {
                    "id": f"yb|{project_id}|{when.strftime('%Y-%m-%dT%H:%M')}",
                    "project": project_id, "brand": project_id, "network": "yb",
                    "when": when.isoformat(), "date": when.strftime("%Y-%m-%d"),
                    "headless": bool(get_settings(project_id)["headless"]),
                    "expectedEmail": (config.get("email") or "").strip(),
                })
                scheduler.ensure_running()
                st.success(f"Запланировано: прогон ЯБ {when.strftime('%d.%m.%Y в %H:%M')} "
                           "по Екатеринбургу.")
                time.sleep(1.0)
                st.rerun()


# Пробная отложка одинакова для ВК и ОК: сессия, ссылка на сообщество,
# текст, время, окно браузера, лог. Отличаются только подписи и модуль,
# который умеет ставить отложку. Держим это таблицей, а не двумя почти
# одинаковыми функциями: правка в одной из копий рано или поздно забудется.
PROBE_NETWORKS = {
    "vk": {"ru": "ВК", "module": "vk_social", "url_key": "vkGroupUrl",
           "where": "«Настройки» → «Вход в ВК (кросспостинг)»",
           "shelf": "«Отложенные записи» сообщества"},
    "ok": {"ru": "ОК", "module": "ok_browser", "url_key": "okGroupUrl",
           "where": "«Настройки» → «Вход в ОК (кросспостинг)»",
           "shelf": "«Отложенные» в группе"},
    "max": {"ru": "МАКС", "module": "max_browser", "url_key": "maxWebUrl",
            "where": "«Настройки» → «Файл сессий» и поле «МАКС: ссылка на канал»",
            "shelf": "«Запланированные посты» канала"},
    # Дзен отличается двумя вещами, и обе учтены здесь, а не особым случаем в
    # коде пробы: входа своего у него нет (пускает сессия Яндекса от ЯБ), и
    # вместо текста поста он ждёт СТАТЬЮ – ссылку на документ или готовый
    # текст, первая строка которого станет заголовком.
    "zen": {"ru": "Дзен", "module": "zen_browser", "url_key": "zenStudioUrl",
            "where": "«Настройки» → вход в Яндекс (тот же, что у Яндекс.Бизнеса) "
                     "и поле «Дзен: студия автора»",
            "shelf": "«Отложенные» в студии автора",
            "needs_email": True,
            "probe_text": "Проверка Click\nЭто пробная статья Click – можно удалить.",
            "probe_hint": "Можно вставить ссылку на Google Документ – Click разберёт "
                          "его на заголовок, абзацы, списки и таблицы. Или оставить "
                          "текст: первая строка станет заголовком статьи."},
}


def _crosspost_probe(project_id: str, config: dict, network: str) -> None:
    """
    Пробная отложка: один тестовый пост в сообщество бренда на +N минут.
    Боевой пилот всей механики под сохранённой сессией. Ничего не
    публикуется сразу: запись встаёт в отложенные, оттуда её можно удалить.
    """
    import importlib
    meta = PROBE_NETWORKS[network]
    ru = meta["ru"]
    social = importlib.import_module(meta["module"])

    with st.expander(f"🧪 Пробная отложка {ru} – проверить механику одним постом"):
        if not social.has_saved_session(project_id):
            st.caption(f"Сначала войдите в {ru}: {meta['where']}.")
            return
        group_url = (config.get(meta["url_key"]) or "").strip()
        if not group_url:
            st.caption(f"Укажите ссылку на сообщество бренда: {meta['where']}.")
            return

        st.caption(f"Сообщество: {group_url}. Запись встанет в {meta['shelf']} – "
                   "оттуда её можно удалить.")
        text = st.text_area("Текст пробного поста" if network != "zen" else
                            "Статья: ссылка на документ или текст",
                            key=f"{network}-probe-text-{project_id}",
                            value=meta.get("probe_text",
                                           "Проверка планировщика Click – тестовая "
                                           "отложенная запись, можно удалить."))
        if meta.get("probe_hint"):
            st.caption(meta["probe_hint"])
        minutes = st.number_input("Опубликовать через, минут", 10, 24 * 60, 40, step=5,
                                  key=f"{network}-probe-min-{project_id}",
                                  help=f"{ru} не даёт планировать ближе чем на несколько "
                                       "минут – меньше 10 не ставим.")
        # Смотреть, КАК он это делает. Галочка та же, что у публикации
        # (HEADED_KEY), поэтому включённая здесь она останется включённой и
        # в «Сформировать план» – ходить за ней в «Настройки» больше не надо.
        if can_show_browser():
            st.checkbox(f"Показывать окно браузера – видно каждый шаг в {ru}",
                        value=bool(st.session_state.get(HEADED_KEY)),
                        key=f"show-browser-{network}-probe",
                        on_change=lambda: st.session_state.__setitem__(
                            HEADED_KEY,
                            bool(st.session_state.get(f"show-browser-{network}-probe"))),
                        help="Только на своём компьютере: в облаке экрана нет, "
                             "показывать окно негде.")
        else:
            st.caption("Окно браузера показать негде: в облаке нет экрана. "
                       "Ход отложки виден в логе ниже, а при отказе – на снимке экрана.")

        busy = runner.busy_reason(project_id, "publish")
        if busy:
            st.caption(f"Сейчас нельзя: {busy}. Дождитесь окончания прогона.")
            return
        if st.button("Поставить пробную отложку", type="primary",
                     key=f"{network}-probe-go-{project_id}", disabled=not text.strip()):
            when = apptime.now() + timedelta(minutes=int(minutes))
            # Лог шагов. Копится в списке, а не пишется в виджет на лету:
            # отложку ведёт отдельный поток воркера, а рисовать из чужого
            # потока Streamlit не даёт. Пишем построчно и показываем целиком,
            # когда воркер вернулся.
            steps: list[str] = []

            def note(msg: str) -> None:
                steps.append(f"[{apptime.stamp()}] {msg}")

            note(f"Пробная отложка: {group_url} на "
                 f"{when.strftime('%d.%m.%Y %H:%M')} (Екатеринбург)")
            # Свежий поток, а НЕ постоянный воркер. Отложка сама открывает и
            # закрывает браузер – хранить между вызовами ей нечего, а вот
            # отравиться постоянный поток успел: после неудачной попытки в нём
            # остаётся недоразмотанный цикл Playwright, и дальше КАЖДЫЙ вызов
            # падает с «Sync API inside the asyncio loop» до перезапуска
            # приложения. У нового потока свои локальные данные – отравить
            # его прошлому нечем.
            with st.spinner(f"Открываю {ru} и ставлю отложку – обычно меньше минуты…"):
                extra = ({"email": (config.get("email") or "").strip()}
                         if meta.get("needs_email") else {})
                res = playwright_worker.run_once(
                    social.schedule_postponed_post, project_id, group_url,
                    text.strip(), [], when, log=note,
                    headless=bool(get_settings(project_id)["headless"]), **extra)
            note("ИТОГ: отложка поставлена" if res.get("ok")
                 else f"ИТОГ: не получилось – {res.get('error')}")
            log_text = "\n".join(steps)
            _probe_save_log(project_id, network, log_text)

            if res.get("ok"):
                st.success(f"Готово: отложка на {when.strftime('%H:%M')} стоит. Проверьте "
                           f"{meta['shelf']} – и удалите тестовую запись.")
            else:
                st.error(f"Не получилось: {res.get('error')}")
                # Снимок экрана ВК в момент отказа: по нему сразу видно, что
                # случилось – капча, форма входа или изменившаяся вёрстка.
                if res.get("shot"):
                    st.image(res["shot"], use_container_width=True)
                    st.caption(f"Что Click видел в {ru} в момент отказа.")

            # Лог показываем всегда, и на успехе тоже: по нему видно, где
            # отложка запнулась, и он же нужен, чтобы прислать его на разбор.
            st.text_area("Лог отложки – что Click делал по шагам", value=log_text,
                         height=220, key=f"{network}-probe-log-{project_id}")
            st.download_button(
                "⬇ Лог отложки (.txt)", data=log_text.encode("utf-8"),
                file_name=f"{network}-otlozhka-{apptime.stamp('%Y-%m-%dT%H-%M-%S')}.txt",
                mime="text/plain", key=f"{network}-probe-log-dl-{project_id}")


def _probe_log_path(project_id: str, network: str):
    """Файл с логом последней пробной отложки – рядом с данными проекта."""
    d = paths.data_root() / project_id / "crosspost"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{network}-probe-last.log"


def _probe_save_log(project_id: str, network: str, text: str) -> None:
    try:
        _probe_log_path(project_id, network).write_text(text, encoding="utf-8")
    except OSError:
        pass                      # лог – удобство, ронять из-за него нечего


def _probe_last_log(project_id: str, network: str) -> None:
    """
    Лог прошлой отложки. Нужен потому, что кнопка живёт внутри expander:
    любое следующее нажатие на странице перерисовывает её, и лог, показанный
    сразу после прогона, исчезает вместе с ним. На диске он остаётся.
    """
    fp = _probe_log_path(project_id, network)
    if not fp.exists():
        return
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if not text.strip():
        return
    with st.expander(f"📄 Лог прошлой пробной отложки {PROBE_NETWORKS[network]['ru']}"):
        st.text_area("Что Click делал по шагам", value=text, height=220,
                     key=f"{network}-probe-log-prev-{project_id}")
        st.download_button("⬇ Скачать (.txt)", data=text.encode("utf-8"),
                           file_name=f"{network}-otlozhka-proshlaya.txt",
                           mime="text/plain",
                           key=f"{network}-probe-log-prev-dl-{project_id}")


def _last_run_kind(project_id: str) -> str:
    """
    Какой прогон был последним – его отчёт и показываем по умолчанию.
    Чтение организаций не в счёт: отчёта у него нет.
    """
    kinds = ("publish", "actualize", "actualize-gis")
    when = {k: (runner.read_state(project_id, k).get("startedAt") or "") for k in kinds}
    return max(kinds, key=lambda k: when[k])


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

    is_act = kind in ("actualize", "actualize-gis")
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

        # Чем этот прогон занимался. Заказчик: «пусть будет заголовок – типа
        # публикация с добавлением фото в товары и 2ГИС или публикация
        # информационного поста». По цифрам отчёты не различить.
        if data.get("title"):
            html(f'<div class="hint" style="margin:-4px 0 10px">'
                 f'<b style="color:var(--text)">{T.esc(data["title"])}</b></div>')

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
            # Прогон с отзывами скачивается целиком: города, отзывы, лог.
            review_rows = (_report_reviews(project_id, data,
                                           data.get("platform") or rv.YANDEX)
                           if is_act else [])
            cols = st.columns([1, 1, 1, 2] if review_rows else [1, 1, 3])
            cols[0].download_button("⬇ Города (CSV)", data=_report_csv(data),
                                    file_name=base_name + ".csv", mime="text/csv",
                                    use_container_width=True, key="btn-csv")
            if review_rows:
                cols[1].download_button(f"⬇ Отзывы ({len(review_rows)})",
                                        data=_sent_csv(review_rows),
                                        file_name=base_name + "-отзывы.csv", mime="text/csv",
                                        use_container_width=True, key="btn-csv-reviews")
            log_col = cols[2] if review_rows else cols[1]
            log_col.download_button("⬇ Лог (.txt)",
                                    data=(run_log or "Лог этого прогона не сохранён.")
                                    .encode("utf-8"),
                                    file_name=base_name + ".txt", mime="text/plain",
                                    use_container_width=True, disabled=not run_log, key="btn-log")
            if not run_log:
                st.caption("Лог этого прогона не сохранён – он появится у прогонов, "
                           "запущенных начиная с этой версии.")
            _report_shots(project_id, data)

        # ─── Отзывы того же прогона – отдельным разделом того же отчёта ───
        if review_rows:
            _report_reviews_block(review_rows)

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
    _gis_login_block(project_id, config)

    st.divider()
    _both_sessions_block(project_id)

    st.divider()
    _vk_login_block(project_id, config)

    st.divider()
    _ok_login_block(project_id, config)

    st.divider()
    _max_login_block(project_id, config)

    st.divider()
    _kp_sheet_settings_block(project_id, config)

    st.divider()
    _web_keys_block()

    st.divider()
    _reviews_settings_block(project_id)

    st.divider()
    _free_memory_block()

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


def _free_memory_block() -> None:
    """
    «Освободить память» – вместо перезапуска всего приложения.

    Что она делает на самом деле. Память после упавшего прогона держит не
    Click, а брошенный браузер: прогон умер, а его процесс остался и занял
    свои 400–500 МБ. Раньше это лечилось только перезапуском приложения –
    тяжело и не всегда возможно. Кнопка закрывает именно такие браузеры.

    Пока идёт прогон, кнопка отказывается работать: его браузер выглядит
    точно так же, и закрыть его – значит оборвать работу на середине.
    """
    html('<div class="card-title">🧹 Память приложения</div>')
    used = runner.memory_mb()
    _, _, hard = runner.mem_gates()
    strays = runner.stray_browsers()
    busy = runner.busy_details_ru()

    line = f"Занято **{used} МБ**"
    if hard:
        line += f" (порог остановки прогонов – {hard} МБ)"
    st.caption(line + ".")
    if busy:
        st.caption(f"Сейчас работают: {busy}. Пока прогон идёт, чистить нечего – "
                   "браузер занят делом.")
    elif strays:
        st.caption(f"Брошенных браузеров: **{len(strays)}** – это они и держат память. "
                   "Прогонов при этом не идёт ни одного: значит, остались от упавшего.")
    else:
        st.caption("Брошенных браузеров нет. Если цифра всё равно велика, память "
                   "держит сам Python – тут поможет только перезапуск приложения.")

    said = st.session_state.pop("free-memory-said", None)
    if said:
        (st.success if said[0] else st.warning)(said[1])

    if st.button("🧹 Освободить память", key="btn-free-memory",
                 disabled=bool(busy) or not strays, use_container_width=True):
        with st.spinner("Закрываю брошенные браузеры…"):
            done, msg = runner.free_memory()
        st.session_state["free-memory-said"] = (done, msg)
        st.rerun()


def _web_keys_block() -> None:
    """
    Ключи к веб-сервисам – прямо в приложении.

    В облаке они лежат в секретах Streamlit, и это удобно. Локально того же
    самого нет: надо руками создать `.streamlit/secrets.toml`, знать его
    формат и не ошибиться. Поэтому на своём компьютере не работало ничего,
    что ходит в интернет – черновики отзывов, таблица КП, хранение данных.

    Здесь ключи вписываются полями. Лежат они вне папки Click, рядом с
    сессией Яндекса, поэтому переживают обновление и в репозиторий попасть
    не могут. Секреты и переменные окружения остаются главнее: в облаке
    всё работает как раньше, а поля показывают, что ключ уже задан оттуда.
    """
    html('<div class="card-title">🌐 Ключи к веб-сервисам</div>')

    сервисы = (
        ("Gemini – черновики ответов на отзывы", llm.is_configured(),
         ("gemini_api_key", "gemini_api_key_2", "gemini_api_key_3"),
         "Берётся бесплатно в Google AI Studio. Ключей можно несколько: лимит "
         "запросов считается на каждый отдельно, два ключа – вдвое быстрее."),
        ("Google – таблица КП", kp_sheet.service_account_info() is not None,
         ("gcp_service_account_b64",),
         "JSON-ключ сервисного аккаунта: вставьте его целиком или в base64. "
         "Таблицу нужно расшарить на этот аккаунт как Читателя."),
        ("GitHub – хранение данных между перезапусками", repo_store.is_configured(),
         ("github_token", "github_repo"),
         "Нужен только в облаке: там файловая система временная. На своём "
         "компьютере данные и так лежат на диске."),
    )
    подписи = {n: (t, h) for n, t, h in secrets_local.KNOWN}

    for имя, готов, ключи, пояснение in сервисы:
        значок = "✅" if готов else "⚠️"
        with st.expander(f"{значок} {имя}", expanded=not готов):
            st.caption(пояснение)
            новые = {}
            for ключ in ключи:
                заголовок = подписи.get(ключ, (ключ, ""))[0]
                откуда = secrets_local.source_of(ключ)
                if откуда and откуда != "настройки приложения":
                    st.caption(f"**{заголовок}** – задан через «{откуда}», поле не нужно.")
                    continue
                есть = secrets_local.get(ключ)
                новые[ключ] = st.text_area(
                    заголовок, value=есть, key=f"secret-{ключ}",
                    height=90 if "gcp" in ключ else 70,
                    placeholder="вставьте сюда" + (" JSON или base64" if "gcp" in ключ else ""),
                    help="Пустое поле стирает ключ.")
                if есть:
                    st.caption(f"сейчас: {secrets_local.masked(есть)}")
                    # Значение, которое Google ключом не считает, видно ДО
                    # всякого запроса – по началу строки. Заказчица вписала
                    # «AQ.Ab8…» (это учётные данные OAuth, а не ключ API), и
                    # Click полдня отвечал ей общим «Google не принял ключ».
                    if ключ.startswith("gemini_api_key"):
                        замечание = llm.key_note(есть)
                        if замечание:
                            st.warning(f"Похоже, {замечание}.", icon="🔑")
            if новые and st.button("💾 Сохранить ключи", key=f"secret-save-{ключи[0]}",
                                   type="primary"):
                try:
                    куда = secrets_local.save(новые)
                    _forget_caches()
                    st.success(f"Сохранено: {куда}")
                    time.sleep(0.6)
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"Не сохранилось: {e}")

            # Проверка ключей по одному. Без неё «ключи заданы, а черновиков
            # нет» разбиралось перепиской: какой из трёх ключей плохой и чем
            # именно – приложение не говорило, а Google говорил, просто его
            # слова до экрана не доезжали.
            if ключи[0].startswith("gemini_api_key"):
                _gemini_keys_check()

    st.caption(f"Ключи хранятся в файле `{secrets_local.path()}` – вне папки Click, "
               "поэтому обновление программы их не тронет и в репозиторий они не "
               "попадут. Файл обычный, без шифрования: там же лежит и сохранённая "
               "сессия Яндекса.")


def _gemini_keys_check() -> None:
    """
    «Проверить ключи» – по одному запросу на ключ, с приговором по каждому.

    Зачем отдельная кнопка. Черновик не получается по десятку причин: ключ
    недействителен, ключ вообще не ключ (в поле лежат учётные данные OAuth
    вида «AQ.…»), для проекта не включён Generative Language API, у ключа
    ограничения по адресу, кончилась квота. Раньше всё это сводилось к одной
    строчке «Google не принял ключ Gemini» – по ней непонятно даже, какой из
    трёх ключей чинить. Теперь Click спрашивает у Google про каждый ключ
    отдельно и показывает ЕГО ответ.
    """
    if not llm.is_configured():
        return
    if st.button("🔎 Проверить ключи", key="gemini-keys-check"):
        with st.spinner("Спрашиваю у Google про каждый ключ…"):
            st.session_state["gemini-keys-verdict"] = llm.check_keys()
    for row in st.session_state.get("gemini-keys-verdict") or []:
        head = f"**{row['name']}** · {row['masked']}"
        if row["ok"]:
            st.success(f"{head} – работает", icon="✅")
        else:
            st.error(f"{head} – {row['reason']}", icon="⛔")


def _forget_caches() -> None:
    """Сбросить кэши, которые могли запомнить «ключа нет»."""
    for имя in ("audit-cache",):
        st.session_state.pop(имя, None)
    try:
        import repo_store as _rs
        _rs._cache.clear()          # noqa: SLF001 – данные могли не читаться без токена
    except Exception:  # noqa: BLE001
        pass
    st.cache_data.clear()


def _reviews_settings_block(project_id: str) -> None:
    """Промпт ответов на отзывы – по проекту, рядом с остальными настройками."""
    html('<div class="card-title">💬 Ответы на отзывы</div>')
    st.caption("Промпт один на обе площадки: правила ответа у Яндекса и 2ГИС одинаковые.")

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
                    llm.generate(rv.build_prompt(rv.project_prompt(project_id), sample, project_id)),
                    project_id, rv.review_text(sample))
            except Exception as e:  # noqa: BLE001
                answer = None
                st.error(str(e))
        if answer:
            stats = getattr(llm, "last_stats", {}) or {}
            st.caption(f"Отзыв: «{sample['text']}» · автор Павел Филиппов "
                       f"(в обращении – {rv.name_for_prompt('Павел Филиппов')}) · "
                       f"модель {stats.get('model') or llm.model_in_use() or '–'} · "
                       f"{stats.get('seconds', '?')} сек, запросов {stats.get('calls', 1)}, "
                       f"ключей {stats.get('keys', len(keys))}, "
                       f"темп {stats.get('pace', '?')} запросов/мин")
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


def _gis_login_block(project_id: str, config: dict) -> None:
    """
    Вход в кабинет 2ГИС – почта и пароль, как на самом сайте.

    Проще, чем у Яндекса: 2ГИС при входе с сохранённым устройством кода
    обычно не просит. Если всё-таки попросит – шаг распознаётся, и поле для
    кода появляется здесь же, вместе со снимком экрана.

    Пароль лежит только в этом контейнере: наружу (в репозиторий) уезжают
    города и почта, но не пароли – как и у Яндекса. А вот сессия уезжает,
    поэтому после перезапуска облака вход обычно не нужен.
    """
    worker = get_worker()
    html('<div class="card-title">🔐 Вход в 2ГИС</div>')

    c1, c2 = st.columns(2)
    email = c1.text_input("Почта кабинета 2ГИС", value=config.get("gisEmail", ""),
                          key="set-gis-email",
                          placeholder="та же, что у Яндекса, если аккаунт один")
    password = c2.text_input("Пароль 2ГИС", value=config.get("gisPassword", ""),
                             type="password", key="set-gis-password")
    if email != config.get("gisEmail", "") or password != config.get("gisPassword", ""):
        config["gisEmail"] = email
        config["gisPassword"] = password
        save_config(project_id)

    if gis.has_saved_session(project_id):
        st.success("Сессия 2ГИС сохранена – прогон пойдёт без повторного входа.")
        with st.container(key="danger-reset-gis"):
            if st.button("Войти заново (сбросить сессию)", key="gis-reset"):
                _forget_session(project_id, "gis")
                for k in ("gis_flow", "gis_state"):
                    st.session_state.pop(k, None)
                st.rerun()
        return

    flow = st.session_state.get("gis_flow")
    if flow is None:
        if not (email and password):
            st.caption("Впишите почту и пароль кабинета 2ГИС – и Click войдёт сам. "
                       "Одна учётка на проект: в ней все города.")
            return
        if can_show_browser():
            st.caption("Локально можно смотреть, как Click входит: включите "
                       "«Показывать окно браузера» в блоке входа в Яндекс выше.")
        if st.button("🔑 Войти в 2ГИС", type="primary", key="gis-login",
                     use_container_width=True):
            try:
                flow = gis.GisLoginFlow(project_id, headless=bool(get_settings(project_id)["headless"]))
                with st.spinner("Открываю кабинет 2ГИС…"):
                    worker.call(flow.start)
                    state = worker.call(flow.submit_credentials, email, password)
                st.session_state["gis_flow"] = flow
                st.session_state["gis_state"] = state
                st.rerun()
            except Exception as e:  # noqa: BLE001
                _browser_error(e)
        return

    state = st.session_state.get("gis_state") or {}
    # Сначала словами, что случилось, и только потом снимок: человеку нужен
    # ответ на «почему не пустило», а не картинка без подписи.
    if state.get("note"):
        st.warning(state["note"])
    if state.get("screenshot"):
        st.image(state["screenshot"], use_container_width=True)
        st.caption("Это то, что видит Click в кабинете 2ГИС прямо сейчас.")

    if state.get("step") == "done":
        try:
            worker.call(flow.save_session)
        finally:
            worker.call(flow.close)
        st.session_state.pop("gis_flow", None)
        st.session_state.pop("gis_state", None)
        if gis.has_saved_session(project_id):
            st.success("Вошли в 2ГИС. Сессия сохранена.")
        else:
            st.warning("2ГИС пустил, но куки входа не сохранились – попробуйте ещё раз.")
        time.sleep(1.0)
        st.rerun()

    if state.get("step") == "code":
        st.caption("2ГИС просит код подтверждения – он на почте или в СМС.")
        code = st.text_input("Код", key="gis-code")
        if st.button("Подтвердить", key="gis-code-go", type="primary") and code.strip():
            st.session_state["gis_state"] = worker.call(flow.submit_code, code.strip())
            st.rerun()
    elif not state.get("note"):
        st.warning("Кабинет не пустил с этой парой почта/пароль – проверьте их выше "
                   "и попробуйте снова. На снимке видно, что показывает 2ГИС.")

    if st.button("Отменить вход", key="gis-cancel"):
        try:
            worker.call(flow.close)
        except Exception:  # noqa: BLE001
            pass
        st.session_state.pop("gis_flow", None)
        st.session_state.pop("gis_state", None)
        st.rerun()


def _max_login_block(project_id: str, config: dict) -> None:
    """
    МАКС: ссылка на канал и состояние сессии.

    Живёт рядом с «Вход в ВК» и «Вход в ОК» нарочно. Сначала это поле
    стояло во вкладке «Кросспостинг», среди каналов мессенджеров – и
    заказчица искала его в «Настройках», где лежат ссылки двух других
    сетей. Логично искала: одинаковые вещи должны лежать в одном месте.

    Кнопки «Войти» здесь нет и быть не может: МАКС не пускает
    автоматический браузер – он не рисует проверку «вы не робот» вовсе.
    Вход только через файл сессий, который делает VHOD-VK-i-OK.py.
    """
    import max_browser

    html('<div class="card-title">🔒 МАКС (кросспостинг)</div>')
    url = st.text_input("Ссылка на канал МАКС в веб-версии",
                        value=config.get("maxWebUrl", ""),
                        key=f"set-maxweb-{project_id}",
                        placeholder="https://web.max.ru/-70916890460398",
                        help="Откройте web.max.ru, зайдите в канал и скопируйте адрес "
                             "из строки браузера – он выглядит как web.max.ru/-70916… "
                             "Ссылка-приглашение вида max.ru/join/… НЕ подойдёт: это "
                             "приглашение вступить, а не адрес канала.")
    if url.strip() and "/join/" in url:
        st.warning("Это ссылка-приглашение (max.ru/join/…) – по ней в канал "
                   "вступают, а не публикуют. Откройте канал на web.max.ru и "
                   "скопируйте адрес из строки браузера: web.max.ru/-70916…")
    if url.strip() != (config.get("maxWebUrl") or ""):
        config["maxWebUrl"] = url.strip()
        save_config(project_id)

    if max_browser.has_saved_session(project_id):
        st.success("Сессия МАКС сохранена – отложки будут ставиться без повторного входа.")
        # Сбросить нужно бывает не «когда сломалось», а когда сессия от
        # ЧУЖОГО бренда: на ОК это стоило заказчице половины дня – куки были
        # сняты по СМУ, а отложка ставилась в ИМП.
        with st.container(key="danger-reset-max"):
            if st.button("Войти заново (сбросить сессию)", key="max-reset"):
                _forget_session(project_id, "max")
                st.rerun()
    else:
        st.warning("Сессии МАКС нет. Войти кнопкой отсюда нельзя: МАКС не пускает "
                   "автоматический браузер – он не показывает ему проверку «вы не "
                   "робот». Запустите VHOD-VK-i-OK.py на своём компьютере и "
                   "загрузите файл сессий в блоке выше.")


def _both_sessions_block(project_id: str) -> None:
    """
    Один файл сессий на ВСЕ сети сразу – ВК, ОК и МАКС.

    Мысль заказчицы, и правильная: «может, одним входом оба куки собирать,
    в один файлик, и один раз вставлять». Куки-то снимаются ОДНИМ браузером
    за ОДИН заход – ОК и пускает-то через ВК. Делить их на файлы и
    заставлять человека вставлять по нескольку раз было работой на ровном
    месте. Click раскладывает по сетям сам и говорит, что нашёл.

    Заголовок и подпись перечисляют сети ПОИМЁННО. Когда добавился МАКС,
    приём файла я сделал, а тут оставил «ВК и ОК» – и заказчица искала,
    куда грузить МАКС, глядя прямо на нужное поле. Подпись должна называть
    всё, что блок умеет, иначе она вводит в заблуждение.
    """
    import max_browser
    import ok_browser
    import social_session
    import vk_social

    html('<div class="card-title">🔑 Файл сессий: ВК, ОК и МАКС</div>')
    сети = ((" ВК", vk_social.has_saved_session(project_id)),
            ("ОК", ok_browser.has_saved_session(project_id)),
            ("МАКС", max_browser.has_saved_session(project_id)))
    st.caption(
        "Сейчас: "
        + " · ".join(f"{имя.strip()} – {'вход есть' if есть else 'входа нет'}"
                     for имя, есть in сети)
        + ". Файл делает VHOD-VK-i-OK.py на вашем компьютере: вошли в сети – "
          "получился ОДИН файл. Загрузите его сюда (кнопка «Выбрать файлы» "
          "ниже), и все три сети возьмутся сразу.")
    # Итог прошлой загрузки. Держим в session_state, а не показываем сразу:
    # после успеха страница перерисовывается (чтобы обновились «вход есть»),
    # и сообщение, написанное до перерисовки, стиралось вместе с ней. Со
    # стороны выглядело так, будто не произошло ничего.
    said_before = st.session_state.pop(f"sess-both-said-{project_id}", None)
    if said_before:
        (st.success if said_before[0] else st.error)(said_before[1])

    up = st.file_uploader("Файл сессий VK-i-OK-sessii.json – ВК, ОК и МАКС в одном",
                          type=["json"], key=f"sess-both-up-{project_id}")
    if up is not None and st.button("Загрузить сессии всех сетей", type="primary",
                                    key=f"sess-both-go-{project_id}"):
        took, said = social_session.import_combined(project_id, up.getvalue())
        st.session_state[f"sess-both-said-{project_id}"] = (
            took, ("✅ Готово. " + said) if took else said)
        st.rerun()


def _session_import_block(project_id: str, name: str, importer, key: str) -> None:
    """
    Принести готовый файл сессии вместо входа в приложении.

    Зачем. В облаке окна браузера нет, а ВК защищает вход проверками
    «вы не робот», QR и кодами в МАКС – пройти их вслепую по снимкам
    получается не всегда. Тогда самый быстрый путь: войти там, где браузер
    видно, и принести сюда файл сессии. Click проверит, что в нём есть
    признак входа, и дальше будет работать как после обычного входа.
    """
    with st.expander(f"📥 Загрузить готовый файл сессии {name} (если вход не проходит)"):
        st.caption(
            f"Файл сессии – это storage_state браузера с куками {name}. Его "
            "сохраняет тот, кто уже входил в этот аккаунт автоматизацией. "
            "Click проверит, что внутри есть признак настоящего входа.")
        up = st.file_uploader(f"Файл сессии {name} (.json)", type=["json"],
                              key=f"sess-up-{key}-{project_id}")
        if up is not None and st.button(f"Принять сессию {name}",
                                        key=f"sess-go-{key}-{project_id}"):
            ok, msg = importer(project_id, up.getvalue())
            if ok:
                st.success(msg)
                time.sleep(1.0)
                st.rerun()
            else:
                st.error(msg)


def _vk_login_block(project_id: str, config: dict) -> None:
    """
    Вход в ВК для кросспостинга – тот же порядок, что у Яндекса и 2ГИС:
    скриншот вместо окна, шаги распознаются, сессия сохраняется в файл.
    Телефон и пароль в конфиг НЕ пишутся: телефон достаточно ввести при
    входе, пароль тем более – сессия дальше живёт сама.
    """
    import vk_social

    worker = get_worker()
    html('<div class="card-title">🔐 Вход в ВК (кросспостинг)</div>')

    group_url = st.text_input(
        "Ссылка на сообщество ВК этого бренда", value=config.get("vkGroupUrl", ""),
        key=f"vk-group-{project_id}", placeholder="https://vk.ru/club… или https://vk.ru/название")
    st.caption("Можно писать и vk.com, и vk.ru – Click сам приведёт ссылку к тому "
               "домену, на котором сохранена сессия. Это разные сайты для браузера, "
               "и куки входа у них не общие.")
    if group_url.strip() != (config.get("vkGroupUrl") or ""):
        config["vkGroupUrl"] = group_url.strip()
        save_config(project_id)

    if vk_social.has_saved_session(project_id):
        st.success("Сессия ВК сохранена – отложки будут ставиться без повторного входа.")
        # Проверка до боя: «сессия сохранена» ещё не значит «ВК нас пускает».
        # Кнопка отвечает на это прямо, а не оставляет гадать после отказа.
        if st.button("🔍 Проверить сессию ВК", key="vk-check"):
            # Тоже свежим потоком: проверка открывает и закрывает свой браузер,
            # и незачем ей рисковать общим потоком входа (см. run_once).
            with st.spinner("Открываю ВК и смотрю, пускает ли…"):
                res = playwright_worker.run_once(
                    vk_social.check_session, project_id,
                    (config.get("vkGroupUrl") or "").strip(),
                    bool(get_settings(project_id)["headless"]))
            if res.get("ok"):
                st.success("Сессия жива, сообщество открывается, права на публикацию есть.")
            else:
                st.error(res.get("error", "не удалось проверить"))
                if res.get("shot"):
                    st.image(res["shot"], use_container_width=True)
                    st.caption("Что Click видит в ВК с этой сессией.")
        with st.container(key="danger-reset-vk"):
            if st.button("Войти заново (сбросить сессию)", key="vk-reset"):
                _forget_session(project_id, "vk")
                for k in ("vk_flow", "vk_state"):
                    st.session_state.pop(k, None)
                st.rerun()
        return

    flow = st.session_state.get("vk_flow")
    if flow is None:
        st.caption("Нужен аккаунт-администратор сообщества – тот, кем постят руками. "
                   "Click откроет форму входа ВК: телефон, пароль или код из SMS "
                   "вводятся здесь же, по снимку экрана.")
        _session_import_block(project_id, "ВК", vk_social.import_session, "vk")
        show_window = False
        if can_show_browser():
            show_window = st.checkbox(
                "Показать окно браузера – если ВК просит подтвердить «я не робот»",
                key="vk-show-window",
                help="Откроется настоящее окно: проверку можно пройти руками, "
                     "а Click продолжит вход. Работает только на своём компьютере.")
        if st.button("🔑 Войти в ВК", type="primary", key="vk-login", use_container_width=True):
            try:
                flow = vk_social.VkLoginFlow(
                    project_id,
                    headless=(False if show_window
                              else bool(get_settings(project_id)["headless"])))
                with st.spinner("Открываю форму входа ВК…"):
                    st.session_state["vk_state"] = worker.call(flow.start)
                st.session_state["vk_flow"] = flow
                st.rerun()
            except Exception as e:  # noqa: BLE001
                _browser_error(e)
        return

    state = st.session_state.get("vk_state") or {}
    if state.get("screenshot"):
        st.image(state["screenshot"], use_container_width=True)
        st.caption("Это то, что Click видит на странице входа ВК прямо сейчас.")

    step = state.get("step")
    if step == "done":
        try:
            worker.call(flow.save_session)
        finally:
            worker.call(flow.close)
        st.session_state.pop("vk_flow", None)
        st.session_state.pop("vk_state", None)
        # has_saved_session требует настоящую куку входа, а не любые куки:
        # гостевой заход тоже их ставит, и раньше это выглядело как успех.
        if vk_social.has_saved_session(project_id):
            st.success("Вошли в ВК. Сессия сохранена.")
        else:
            st.warning("ВК показал страницу без формы входа, но признака входа нет – "
                       "сессия не сохранена. Попробуйте войти ещё раз.")
        time.sleep(1.0)
        st.rerun()
    elif step == "phone":
        phone = st.text_input("Телефон аккаунта ВК", key="vk-phone", placeholder="+7…")
        if st.button("Далее", key="vk-phone-go", type="primary") and phone.strip():
            st.session_state["vk_state"] = worker.call(flow.submit_phone, phone)
            st.rerun()
    elif step == "password":
        st.caption("Если пароля у аккаунта нет – входите по коду из SMS, "
                   "это обычный путь для рабочих аккаунтов брендов.")
        pwd = st.text_input("Пароль ВК", type="password", key="vk-pass")
        c1, c2 = st.columns(2)
        if c1.button("Войти по паролю", key="vk-pass-go", type="primary") and pwd:
            st.session_state["vk_state"] = worker.call(flow.submit_password, pwd)
            st.rerun()
        if c2.button("📱 Войти по коду из SMS", key="vk-pass-sms"):
            st.session_state["vk_state"] = worker.call(flow.request_code_instead)
            st.rerun()
    elif step == "code":
        st.caption("ВК просит код подтверждения. Смотрите снимок: код может "
                   "прийти в МАКС, в SMS – либо вместо кода поступит звонок-сброс, "
                   "и тогда нужны последние 6 цифр номера, с которого звонили.")
        code = st.text_input("Код (6 цифр)", key="vk-code")
        c1, c2 = st.columns(2)
        if c1.button("Подтвердить", key="vk-code-go", type="primary") and code.strip():
            st.session_state["vk_state"] = worker.call(flow.submit_code, code)
            st.rerun()
        if c2.button("🔄 Подтвердить другим способом", key="vk-other-confirm"):
            st.session_state["vk_state"] = worker.call(flow.press_other_confirm)
            st.rerun()
    elif step == "qr":
        st.caption("ВК предлагает вход по QR-коду – камеры у нас нет. Переходим "
                   "на вход по телефону.")
        if st.button("➡️ Войти другим способом", key="vk-other-way", type="primary"):
            st.session_state["vk_state"] = worker.call(flow.press_other_way)
            st.rerun()
    elif step == "captcha":
        st.caption("ВК проверяет, что вход делает человек. Обычно хватает одного "
                   "нажатия – жмите кнопку ниже.")
        if st.button("✅ Продолжить (я не робот)", key="vk-captcha-go", type="primary"):
            st.session_state["vk_state"] = worker.call(flow.press_captcha_continue)
            st.rerun()
        if can_show_browser():
            st.caption("Если проверка не проходит с одного нажатия – выключите "
                       "«Скрытый браузер» в блоке входа в Яндекс выше и войдите "
                       "заново: тогда откроется настоящее окно, где проверку можно "
                       "пройти руками.")
        else:
            st.caption("Если проверка не проходит – её нужно пройти руками в окне "
                       "браузера, а это возможно только при локальном запуске Click.")
    elif step == "start-over":
        st.caption("ВК вернул нас на главную страницу – форма входа закрылась. "
                   "Так бывает, если истекло время кода или проверка сорвалась. "
                   "Откроем форму заново.")
        if st.button("🔄 Открыть форму входа заново", key="vk-restart", type="primary"):
            st.session_state["vk_state"] = worker.call(flow.restart_login)
            st.rerun()
    else:
        st.warning("Не разобрал, что за шаг на странице, – смотрите снимок выше.")
        c1, c2 = st.columns(2)
        # Кнопка жмёт «Продолжить» ТОЛЬКО по тексту: раньше в крайнем случае
        # нажималась любая кнопка, и на капче это был крестик – проверка
        # закрывалась, и ВК выбрасывал на главную.
        if c1.button("✅ Нажать «Продолжить» на экране", key="vk-blind-continue"):
            st.session_state["vk_state"] = worker.call(flow.press_captcha_continue)
            st.rerun()
        if c2.button("🔄 Открыть форму входа заново", key="vk-restart-unknown"):
            st.session_state["vk_state"] = worker.call(flow.restart_login)
            st.rerun()
        if can_show_browser():
            st.caption("Не помогает – отмените вход, поставьте галочку «Показать окно "
                       "браузера» и войдите заново: проверку пройдёте руками.")

    if st.button("Отменить вход", key="vk-cancel"):
        try:
            worker.call(flow.close)
        except Exception:  # noqa: BLE001
            pass
        st.session_state.pop("vk_flow", None)
        st.session_state.pop("vk_state", None)
        st.rerun()


def _ok_login_block(project_id: str, config: dict) -> None:
    """
    Вход в ОК для кросспостинга.

    ОСНОВНОЙ ПУТЬ – «Войти через ВК»: у рабочих аккаунтов брендов своего
    пароля ОК нет, и ВК, и ОК открываются одной учёткой ВК. Click подкладывает
    сохранённую сессию ВК, поэтому чаще всего вход в ОК проходит вообще без
    ввода – «вошли в ВК, ОК подтянулся». Если сессии ВК нет, телефон и код
    вводятся в том же всплывающем окне ВК.

    Запасной путь – обычный логин и пароль ОК, если они всё же есть.
    """
    import ok_browser
    import vk_social

    worker = get_worker()
    html('<div class="card-title">🔐 Вход в ОК (кросспостинг)</div>')

    group_url = st.text_input("Ссылка на группу ОК бренда", value=config.get("okGroupUrl", ""),
                              key=f"ok-group-{project_id}",
                              placeholder="https://ok.ru/group/…")
    if group_url.strip() != (config.get("okGroupUrl") or ""):
        config["okGroupUrl"] = group_url.strip()
        save_config(project_id)

    if ok_browser.has_saved_session(project_id):
        st.success("Сессия ОК сохранена – отложки будут ставиться без повторного входа.")
        with st.container(key="danger-reset-ok"):
            if st.button("Войти заново (сбросить сессию)", key="ok-reset"):
                _forget_session(project_id, "ok")
                for k in ("ok_flow", "ok_state"):
                    st.session_state.pop(k, None)
                st.rerun()
        return

    flow = st.session_state.get("ok_flow")
    if flow is None:
        if vk_social.has_saved_session(project_id):
            st.caption("Сессия ВК есть – скорее всего, ОК пустит сразу, без ввода: "
                       "это та самая связка, ВК и ОК под одной учёткой.")
        else:
            st.caption("Сначала лучше войти в ВК (блок выше) – тогда ОК пустит без "
                       "ввода. Можно и сразу сюда: телефон и код спросят в окне ВК.")
        _session_import_block(project_id, "ОК", ok_browser.import_session, "ok")
        if st.button("🔑 Войти в ОК через ВК", type="primary", key="ok-go",
                     use_container_width=True):
            try:
                flow = ok_browser.OkViaVkLoginFlow(
                    project_id, headless=bool(get_settings(project_id)["headless"]))
                with st.spinner("Открываю ОК и жму «Войти через ВК»…"):
                    st.session_state["ok_state"] = worker.call(flow.start)
                st.session_state["ok_flow"] = flow
                st.rerun()
            except Exception as e:  # noqa: BLE001
                _browser_error(e)
        _ok_password_fallback(project_id, config, worker)
        return

    state = st.session_state.get("ok_state") or {}
    if state.get("note"):
        st.warning(state["note"])
    if state.get("screenshot"):
        st.image(state["screenshot"], use_container_width=True)
        st.caption("Это то, что Click видит прямо сейчас.")

    step = state.get("step")
    if step == "done":
        try:
            worker.call(flow.save_session)
        finally:
            worker.call(flow.close)
        st.session_state.pop("ok_flow", None)
        st.session_state.pop("ok_state", None)
        if ok_browser.has_saved_session(project_id):
            st.success("Вошли в ОК. Сессия сохранена.")
        else:
            st.warning("ОК показал вход выполненным, но признака входа в куках нет – "
                       "сессия не сохранена. Попробуйте ещё раз.")
        time.sleep(1.0)
        st.rerun()
    elif step == "phone":
        phone = st.text_input("Телефон аккаунта ВК", key="ok-phone", placeholder="+7…")
        if st.button("Далее", key="ok-phone-go", type="primary") and phone.strip():
            st.session_state["ok_state"] = worker.call(flow.submit_phone, phone)
            st.rerun()
    elif step == "password":
        st.caption("Пароля у аккаунта может не быть – тогда входите по коду из SMS.")
        pwd = st.text_input("Пароль ВК", type="password", key="ok-vkpass")
        c1, c2 = st.columns(2)
        if c1.button("Войти по паролю", key="ok-pass-go", type="primary") and pwd:
            st.session_state["ok_state"] = worker.call(flow.submit_password, pwd)
            st.rerun()
        if c2.button("📱 Войти по коду из SMS", key="ok-pass-sms"):
            st.session_state["ok_state"] = worker.call(flow.request_code_instead)
            st.rerun()
    elif step == "code":
        st.caption("ВК просит код подтверждения – из SMS или приложения.")
        code = st.text_input("Код", key="ok-code")
        if st.button("Подтвердить", key="ok-code-go", type="primary") and code.strip():
            st.session_state["ok_state"] = worker.call(flow.submit_code, code)
            st.rerun()
    elif step == "captcha":
        st.caption("ВК проверяет, что вход делает человек – обычно хватает "
                   "одного нажатия.")
        if st.button("✅ Продолжить (я не робот)", key="ok-captcha-go", type="primary"):
            st.session_state["ok_state"] = worker.call(flow.press_captcha_continue)
            st.rerun()
    elif step == "consent":
        st.caption("ВК узнал аккаунт и спрашивает, входить ли им – это последний "
                   "шаг связки. Нажмите кнопку, и ОК откроется.")
        if st.button("✅ Войти этим аккаунтом", key="ok-consent-go", type="primary"):
            st.session_state["ok_state"] = worker.call(flow.confirm_account)
            st.rerun()
    elif step == "profile":
        st.caption("ОК спрашивает, наш ли это профиль – так он иногда проверяет "
                   "вход с нового места. Click пробовал подтвердить сам и не смог, "
                   "нажмите здесь. Кнопку «Это не мой профиль» в ОК не трогайте: "
                   "это жалоба на угон, после неё аккаунт блокируют.")
        if st.button("✅ Да, это наш профиль", key="ok-profile-go", type="primary"):
            st.session_state["ok_state"] = worker.call(flow.confirm_profile_step)
            st.rerun()
    elif step == "verify":
        st.caption("ОК хочет прислать код по СМС на телефон аккаунта – номер "
                   "виден на снимке. Нажмите «Получить код», дождитесь СМС и "
                   "введите его на следующем шаге.")
        c1, c2 = st.columns(2)
        if c1.button("📱 Получить код по СМС", key="ok-verify-send", type="primary"):
            st.session_state["ok_state"] = worker.call(flow.request_code_step)
            st.rerun()
        if c2.button("✉️ Подтвердить по почте", key="ok-verify-mail"):
            st.session_state["ok_state"] = worker.call(flow.request_mail_step)
            st.rerun()
    elif step == "verify-code":
        st.caption("Введите код, который прислал ОК.")
        vcode = st.text_input("Код из СМС", key="ok-verify-code")
        if st.button("Подтвердить", key="ok-verify-go", type="primary") and vcode.strip():
            st.session_state["ok_state"] = worker.call(flow.submit_verify_step, vcode)
            st.rerun()
    elif step == "no-vk-button":
        st.caption("Значок ВК так и не появился – он в ряду иконок под кнопкой "
                   "«Войти по QR-коду». Войдите логином и паролем ОК ниже.")
    elif step == "no-popup":
        st.caption("Значок ВК нажали, но окно входа ВК не открылось. Попробуйте "
                   "ещё раз – или войдите логином и паролем ОК ниже.")
    else:
        st.warning("Не разобрал, что за шаг на экране – смотрите снимок выше.")
        # Экран «Войти как…» узнаётся не всегда: подписи у ВК разные. Даём
        # нажать вслепую, вместо того чтобы человек упирался в тупик.
        # hasattr – потому что запасной вход по паролю ОК идёт другим
        # классом, у которого окна ВК нет и подтверждать нечего.
        if hasattr(flow, "confirm_account") and \
                st.button("✅ Попробовать подтвердить вход", key="ok-consent-blind"):
            st.session_state["ok_state"] = worker.call(flow.confirm_account)
            st.rerun()

    if st.button("Отменить вход", key="ok-cancel"):
        try:
            worker.call(flow.close)
        except Exception:  # noqa: BLE001
            pass
        st.session_state.pop("ok_flow", None)
        st.session_state.pop("ok_state", None)
        st.rerun()


def _ok_password_fallback(project_id: str, config: dict, worker) -> None:
    """Запасной вход в ОК – своим логином и паролем, если они есть."""
    import ok_browser

    with st.expander("…или войти логином и паролем ОК"):
        c1, c2 = st.columns(2)
        ok_login = c1.text_input("Логин ОК (телефон/почта)", value=config.get("okLogin", ""),
                                 key=f"ok-login-{project_id}")
        password = c2.text_input("Пароль ОК", type="password", key=f"ok-pass-{project_id}")
        if ok_login.strip() != (config.get("okLogin") or ""):
            config["okLogin"] = ok_login.strip()
            save_config(project_id)
        if st.button("Войти логином и паролем", key="ok-plain-go",
                     disabled=not (ok_login.strip() and password)):
            try:
                flow = ok_browser.OkLoginFlow(
                    project_id, headless=bool(get_settings(project_id)["headless"]))
                with st.spinner("Открываю ОК…"):
                    worker.call(flow.start)
                    st.session_state["ok_state"] = worker.call(
                        flow.submit_credentials, ok_login.strip(), password)
                st.session_state["ok_flow"] = flow
                st.rerun()
            except Exception as e:  # noqa: BLE001
                _browser_error(e)


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
                # PROJECTS[project_id], а не project: эта переменная живёт в
                # tab_settings, сюда она никогда не приходила. «Проверить
                # сессию» падало с NameError у всех, кто не вписал почту в
                # «Настройки» руками: при пустом поле код шёл во вторую
                # половину «или» и утыкался в несуществующее имя.
                who = _check_session(project_id,
                                     config.get("email") or PROJECTS[project_id]["yandexEmail"])
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


def _kp_card_ids(project_id: str, config: dict) -> list[str]:
    """
    Номера карточек, на которые ссылается КП.

    Нужны сборщику: список организаций Яндекса отдаёт не всё. У заказчика
    в разделе «Организации» два Красноярска, а список приносит один –
    второй заведён онлайн-организацией и в выдачу не попадает. Сколько ни
    перечитывай, дубль не найдётся. Зато в КП у города записана прямая
    ссылка на карточку: по ней Click откроет её сам.

    Лист берём из настроек проекта (тот же, что и «Города») – не вышло
    (не настроен, таблица недоступна) – возвращаем пусто и просто собираем
    организации как раньше.
    """
    title = (config.get("kpSheetTitle") or "").strip()
    if not title:
        return []
    try:
        rows = _audit_sheet_rows(project_id, config, title)
        sheet = kp_audit.parse_sheet(rows)
        out = []
        for it in sheet.get("items") or []:
            cid = kp_audit.company_id_from_url(it.get("link", ""))
            if cid:
                out.append(cid)
        return list(dict.fromkeys(out))
    except Exception:  # noqa: BLE001 – без таблицы соберём просто список
        return []


def tab_audit(project_id: str, config: dict) -> None:
    state = runner.read_state(project_id, "collect")
    running = state.get("status") == "running"
    busy = runner.busy_reason(project_id, "collect")   # пусто – запускать можно
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
                     disabled=bool(busy) or not yb.has_saved_session(project_id),
                     use_container_width=True):
            ok, msg = runner.start_collect(project_id,
                                           headless=bool(get_settings(project_id)["headless"]),
                                           with_cards=bool(st.session_state.get("audit-cards")),
                                           must_ids=_kp_card_ids(project_id, config))
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
        if running:
            st.button("⏹ Остановить", key="audit-stop", on_click=runner.request_stop,
                      args=(project_id, "collect"))
        elif busy:
            st.caption(busy)
        if not yb.has_saved_session(project_id):
            st.warning("Сначала войдите в Яндекс в разделе «⚙️ Настройки».")

    if running or state.get("status") not in (None, "idle"):
        with st.container(border=True):
            live_panel(project_id, running, "collect")

    # ─── Лист КП ───
    # Источник и лист – общие с «Городами», настраиваются в «⚙️ Настройки»:
    # раньше у «Сверки» был свой отдельный выбор листа, который жил только
    # в сессии и мог разойтись с тем, что читают «Города» – два места видели
    # разные версии одной и той же таблицы.
    saved_url = (config.get("kpSheetUrl") or "").strip()
    title = (config.get("kpSheetTitle") or "").strip()
    if not kp_sheet.is_configured(project_id, saved_url) or not title:
        st.info("Не настроен источник городов. Ссылка на таблицу и лист задаются во вкладке "
                "«⚙️ Настройки» → «Источник городов – Google-таблица КП».")
        return

    with st.container(border=True):
        html('<div class="card-title">📄 Лист КП</div>')
        st.caption(f"Лист «{title}» – выбран в «⚙️ Настройки».")
        if st.button("↻ Перечитать таблицу", key="audit-reread"):
            _audit_forget()
            st.rerun()
        try:
            rows = _audit_sheet_rows(project_id, config, title)
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
            return
        if not any(any(str(c).strip() for c in r) for r in rows):
            st.warning(f"Лист «{title}» пустой – сверять нечего. Выберите другой лист "
                       "во вкладке «⚙️ Настройки» → «Источник городов».")
            return

    if not companies:
        html(T.empty("🔎", "Организации ещё не прочитаны",
                     "Нажмите «Прочитать организации в Яндексе» – это займёт около минуты."))
        return

    result = kp_audit.build(rows, companies,
                            yandex_total=int(stored.get("yandexTotal") or 0))
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
    stamp = apptime.stamp("%Y-%m-%d")
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
    # Города – из КП и сами. Проверка дешёвая (сравнение отметки времени),
    # в таблицу лезем не чаще раза в KP_SYNC_TTL_HOURS часов.
    _kp_autosync(project_id, config)
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
    current = st.session_state.get("section_name", SEC_RUN)
    if current not in SECTIONS:
        current = SEC_RUN
    # Счётчик на вкладке «Актуализация»: после прогона с отзывами человек
    # иначе не догадается, что где-то ждут готовые ответы. Первый боевой
    # прогон это и показал – черновики были, а найти их было негде.
    waiting = sum(len(rv.open_items(rv.load_queue(project_id, pf)))
                  for pf in (rv.YANDEX, rv.GIS))

    def _section_label(name: str) -> str:
        if name == SEC_ACTUALIZE and waiting:
            return f"{name} · 💬 {waiting}"
        return name

    with st.container(key="click-tabs"):
        section = st.radio("Раздел", SECTIONS, index=SECTIONS.index(current), horizontal=True,
                           label_visibility="collapsed", format_func=_section_label,
                           key=f"main-section-{st.session_state.get('nav-gen', 0)}")
    st.session_state["section_name"] = section

    if section == SEC_RUN:
        tab_run(project_id, config)
    elif section == SEC_COMPOSE:
        tab_compose(project_id, config)
    elif section == SEC_CROSSPOST:
        tab_crosspost(project_id, config)
    elif section == SEC_ACTUALIZE:
        tab_actualize(project_id, config)
    elif section == SEC_CITIES:
        tab_cities(project_id, config)
    elif section == SEC_REPORT:
        tab_report(project_id)
    elif section == SEC_SETTINGS:
        tab_settings(project_id, config)
    else:
        tab_audit(project_id, config)


# ─── Сборка должна быть ОДНА на все модули ───────────────────────────
#
# Облако обновляет файлы под работающим приложением, а Streamlit выселяет
# изменённые модули из памяти по одному – по мере того как замечает их на
# диске. В промежутке главный скрипт УЖЕ новый, а сосед ЕЩЁ старый, и это не
# теория: заказчик получила такое дважды за один день. На «Актуализации» –
# «AttributeError: casual_words» (экран новый, reviews старый). На
# «Публикации» хуже: новый экран сложил задачи «только фото в 2ГИС», а прогон
# старым кодом про такие задачи не знал – и отправил в три города ПУСТЫЕ посты.
#
# Модули мы не перезагружаем: самодельная перезагрузка из пользовательского
# потока однажды уже роняла приложение целиком (см. комментарий у импортов).
# Мы просто НЕ РАБОТАЕМ вразнобой – пока метки не сойдутся, страница ничего не
# рисует и ничего не запускает, а сама перерисовывается через секунду.
# Выселение занимает мгновение: человек видит короткую плашку и работает
# дальше уже на одной сборке.
_REFRESH_LOCK = threading.Lock()


def disk_build() -> str:
    """
    Метка сборки, прочитанная С ДИСКА, а не из памяти.

    Сравнивать модули с меткой ИЗ ПАМЯТИ бессмысленно, и это стоило заказчику
    ещё двух пустых постов. Streamlit перечитывает с диска только главный
    скрипт; `build` он держит в памяти наравне с остальными. Значит после
    обновления старыми оказываются И модули, И метка – всё «сходится», хотя
    код разный, и проверка молчит. Правда только на диске.
    """
    try:
        text = (Path(__file__).parent / "build.py").read_text(encoding="utf-8")
    except OSError:
        return UI_BUILD
    found = re.search(r'BUILD\s*=\s*["\']([^"\']+)["\']', text)
    return found.group(1) if found else UI_BUILD


def stale_modules() -> list[str]:
    """Части приложения, оставшиеся в памяти от прежней сборки."""
    want = disk_build()
    stale = [name for name in _OWN_MODULES
             if getattr(sys.modules.get(name), "BUILD", want) != want]
    if UI_BUILD != want:
        stale.append("build")
    return sorted(set(stale))


def refresh_stale_modules() -> list[str]:
    """
    Перечитать с диска модули, оставшиеся от прежней сборки.

    Зачем. Облако обновляет файлы под работающим приложением. Главный скрипт
    Streamlit читает с диска на каждой перерисовке, а соседние модули берёт из
    памяти – и до перезапуска приложения экран новый, а прогон старый. Это не
    теория: заказчик получила «AttributeError: casual_words», а потом старый
    прогон принял задания «только фото в 2ГИС» за обычные посты и опубликовал
    пустые посты в пяти городах. Просить человека нажать «Reboot app» – не
    решение: он этого не увидит и не должен.

    Почему это безопасно теперь, хотя раньше самодельная перезагрузка роняла
    приложение целиком. Тогда она шла на КАЖДОЙ перерисовке и из любого потока
    без оглядки на соседей. Теперь – только когда метки и правда разошлись
    (раз за сборку), только когда НЕ ИДЁТ ни один прогон (иначе живой прогон
    остался бы со старым модулем) и под общим замком, чтобы два потока не
    перезагружали разом. Плюс importlib.reload не выкидывает модуль из
    sys.modules – читатель в соседнем потоке видит его всё время.
    """
    if any(runner.is_running(pid) for pid in PROJECTS):
        return stale_modules()
    with _REFRESH_LOCK:
        stale = stale_modules()
        if not stale:
            return []
        # Сперва метка, потом всё остальное: модули берут BUILD из неё.
        for name in ["build"] + [n for n in _OWN_MODULES if n != "build"]:
            module = sys.modules.get(name)
            if module is None:
                continue
            for attempt in range(3):
                try:
                    importlib.reload(module)
                    break
                except KeyError:            # сосед выселил модуль – подождём
                    time.sleep(0.2 * (attempt + 1))
                except Exception:           # noqa: BLE001 – не вышло, скажем человеку
                    break
        return stale_modules()


def main() -> None:
    if stale_modules():
        # Перечитываем сами. Не вышло (идёт прогон или модуль не поддался) –
        # НИЧЕГО не рисуем и ничего не запускаем: смешанный код опаснее
        # неудобства. Экран уже умел «только фото в 2ГИС», а прогон из памяти
        # про них не знал – и посты ушли пустыми.
        left = refresh_stale_modules()
        if not left:
            st.rerun()
        busy = [pid for pid in PROJECTS if runner.is_running(pid)]
        if busy:
            st.warning("⏳ Вышла новая версия Click, но сейчас идёт прогон – обновимся, "
                       "когда он закончится. Пока работает прежняя версия; новые прогоны "
                       "лучше не запускать.")
            return
        st.error("Приложение обновилось, но часть его осталась от прежней сборки: "
                 + ", ".join(left) + ". Перезагрузите страницу (F5), а если не поможет – "
                 "«Reboot app» в меню Streamlit справа внизу. Запускать прогоны сейчас "
                 "нельзя: новый экран и старый прогон понимают задачи по-разному.")
        return

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
