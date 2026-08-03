"""
streamlit_app.py — Click на Streamlit. Интерфейс повторяет оригинальное
приложение (app.js + _ui.js): те же 6 разделов, тот же дизайн, та же логика
черновик → очередь → задачи → прогон → отчёт.

Публикацией занимается runner.py (фоновый поток + защита от дублей),
браузером — yb_playwright.py (порт publish.js/actualize.js на Playwright).
Здесь только интерфейс и работа с конфигом проекта.

Запуск:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

import paths
import projects_data as pdata
import runner
import ui_theme as T
import yb_playwright as yb
from playwright_worker import PlaywrightWorker

ROOT = Path(__file__).parent
USERS_DATA = paths.data_root()

st.set_page_config(page_title="Click — публикация постов", page_icon="📮", layout="wide")

SALT = "click-salt-v1-2026"
SECTIONS = ["🚀 Запуск", "📤 Публикация", "🔄 Актуализация", "🏙 Города", "📊 Отчёт", "⚙️ Настройки"]


def _hash(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha512", password.encode(), SALT.encode(), 100_000, dklen=64).hex()


PROJECTS: dict[str, dict] = {
    "SMU": {"id": "SMU", "name": "СМУ", "fullName": "Стальметгрупп", "color": "#3b82f6", "icon": "🏗",
            "yandexEmail": "stalmetural19@yandex.ru", "passwordHash": _hash("1501"),
            "presetCities": pdata.SMU_CITIES, "endings": None},
    "IMP": {"id": "IMP", "name": "ИМП", "fullName": "Инметпром", "color": "#10b981", "icon": "🔩",
            "yandexEmail": "inmetprom77@yandex.ru", "passwordHash": _hash("2205"),
            "presetCities": pdata.IMP_CITIES, "endings": pdata.IMP_ENDINGS},
    "MPE": {"id": "MPE", "name": "МПЭ", "fullName": "МетПромЭнерго", "color": "#f59e0b", "icon": "⚡",
            "yandexEmail": "mepen88@yandex.ru", "passwordHash": _hash("1101"),
            "presetCities": pdata.MPE_CITIES, "endings": pdata.MPE_ENDINGS},
}

COUNTRY_FLAGS = {
    "Россия": "🇷🇺", "Казахстан": "🇰🇿", "Беларусь": "🇧🇾", "Киргизия": "🇰🇬", "Кыргызстан": "🇰🇬",
    "Узбекистан": "🇺🇿", "Азербайджан": "🇦🇿", "Армения": "🇦🇲", "Грузия": "🇬🇪", "Таджикистан": "🇹🇯",
}


def flag(name: str) -> str:
    return COUNTRY_FLAGS.get(name, "🏳️")


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
    Идентификатор из названия. ВАЖНО: кириллицу оставляем — если её вырезать,
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
        # Индекс в id — страховка от совпадения slug'ов у разных названий.
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


def load_raw_config(project_id: str) -> dict:
    fp = config_path(project_id)
    if fp.exists():
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            if raw.get("projects"):
                return raw
        except (json.JSONDecodeError, OSError):
            pass
    sub = _default_subproject(project_id)
    return {"projects": [sub], "activeProjectId": sub["id"], "settings": {}}


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


def get_settings(project_id: str) -> dict:
    raw = st.session_state.get(f"_cfg_{project_id}") or load_raw_config(project_id)
    settings = raw.setdefault("settings", {})
    settings.setdefault("headless", True)
    settings.setdefault("delayBetweenPosts", 3)
    settings.setdefault("strictAccountCheck", True)
    settings.setdefault("retryUnknown", False)
    settings.setdefault("dedupWindowHours", runner.DEDUP_WINDOW_HOURS)
    return settings


def save_config(project_id: str) -> None:
    raw = st.session_state.get(f"_cfg_{project_id}")
    if not raw:
        return
    fp = config_path(project_id)
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(fp)


def country_by_id(config: dict, cid: str) -> dict | None:
    return next((c for c in config["countries"] if c["id"] == cid), None)


# ════════════════════════════════════════════════════════════════════
#  Текст поста — построчный порт buildFinalText из _ui.js
# ════════════════════════════════════════════════════════════════════

def build_final_text(project_id: str, country_name: str, post_type: str, body: str) -> str:
    lines: list[str] = []
    if (body or "").strip():
        lines.append(body.strip())

    endings = PROJECTS[project_id]["endings"]

    # ── ИМП / МПЭ: динамические окончания, контакты подставляются по стране ──
    if endings and endings.get("__dynamic"):
        contacts = (endings.get("contacts") or {}).get(country_name)
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

    # ── СМУ: старая логика по COUNTRY_TEMPLATES ──
    tpl = pdata.COUNTRY_TEMPLATES.get(country_name) or pdata.COUNTRY_TEMPLATES["Россия"]
    type_def = next((t for t in pdata.POST_TYPES if t["id"] == post_type), pdata.POST_TYPES[0])
    if type_def["hasContact"]:
        lines += [
            "",
            "Ознакомиться с наличием металлопроката в вашем городе, оформить заказ "
            "и проконсультироваться с менеджерами можно на нашем сайте:",
            f"🌐 {tpl['site']}",
            f"📩 {tpl['email']}",
            f"📞 {tpl['phone']}",
            "",
            f"{type_def['hashtag']} {pdata.COMMON_HASHTAGS_SMU}".strip(),
        ]
    elif type_def["isInfo"]:
        lines += ["", f"Ознакомиться с ассортиментом трубного проката и техническими "
                      f"параметрами можно на нашем сайте {tpl['site']}"]
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
    for old in folder.glob("*.json"):          # чистим прошлые — иначе прогон подхватит лишнее
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
    """Один постоянный поток для Playwright: sync API нельзя дёргать из разных потоков."""
    if "pw_worker" not in st.session_state:
        st.session_state.pw_worker = PlaywrightWorker()
    return st.session_state.pw_worker


def theme() -> str:
    return st.session_state.get("theme", "dark")


def inject_css() -> None:
    st.markdown(T.css(theme()), unsafe_allow_html=True)


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def status_pills(project_id: str) -> list[tuple[str, str]]:
    files, cities = runner.count_pending(project_id)
    state = runner.read_state(project_id)
    pills = [
        ("ok", "Сессия Яндекса") if yb.has_saved_session(project_id) else ("err", "Нет входа в Яндекс"),
        ("info", f"Задач: {files} · городов: {cities}"),
    ]
    if state.get("status") == "running":
        pills.append(("warn", f"Идёт {'публикация' if state.get('action') == 'publish' else 'актуализация'}"))
    elif state.get("status") == "error":
        pills.append(("err", "Последний прогон с ошибкой"))
    return pills


def city_selector(key_prefix: str, country: dict, default_all: bool = False) -> list[str]:
    """
    Выбор городов страны. По умолчанию НИЧЕГО не выбрано — так в оригинале,
    чтобы случайно не отправить пост во все 137 городов.
    """
    state_key = f"{key_prefix}-cities-{country['id']}"
    options = [c["id"] for c in country["cities"]]
    names = {c["id"]: c["name"] for c in country["cities"]}
    if state_key not in st.session_state:
        st.session_state[state_key] = list(options) if default_all else []

    c1, c2, c3 = st.columns([1, 1, 2])
    if c1.button("Выбрать все", key=f"{key_prefix}-all-{country['id']}", use_container_width=True):
        st.session_state[state_key] = list(options)
        st.rerun()
    if c2.button("Снять все", key=f"{key_prefix}-none-{country['id']}", use_container_width=True):
        st.session_state[state_key] = []
        st.rerun()
    selected = st.session_state.get(state_key) or []
    c3.markdown(
        f'<div style="padding-top:8px"><span class="badge badge-{"accent" if selected else "muted"}">'
        f'выбрано {len(selected)} из {len(options)}</span></div>',
        unsafe_allow_html=True,
    )
    return st.multiselect("Города", options=options, format_func=lambda cid: names.get(cid, cid),
                          key=state_key, label_visibility="collapsed")


def country_picker(key_prefix: str, config: dict) -> list[dict]:
    """Чекбоксы стран + «выбрать все» / «снять все» (в оригинале — пилюли стран)."""
    countries = config["countries"]
    if not countries:
        return []

    def cb_key(country_id: str) -> str:
        return f"{key_prefix}-cb-{country_id}"

    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("Все страны", key=f"{key_prefix}-all-countries", use_container_width=True):
        for c in countries:
            st.session_state[cb_key(c["id"])] = True
        st.rerun()
    if c2.button("Снять страны", key=f"{key_prefix}-none-countries", use_container_width=True):
        for c in countries:
            st.session_state[cb_key(c["id"])] = False
            st.session_state[f"{key_prefix}-{c['id']}-cities-{c['id']}"] = []
        st.rerun()

    cols = st.columns(min(4, max(1, len(countries))))
    for i, c in enumerate(countries):
        with cols[i % len(cols)]:
            st.checkbox(f"{flag(c['name'])} {c['name']} ({len(c['cities'])})", key=cb_key(c["id"]))
    return [c for c in countries if st.session_state.get(cb_key(c["id"]))]


# ════════════════════════════════════════════════════════════════════
#  ЭКРАН ВХОДА
# ════════════════════════════════════════════════════════════════════

def show_login() -> None:
    inject_css()
    html('<div class="auth-wrap"><div class="auth-logo">➤</div>'
         '<div class="auth-title">Click</div>'
         '<div class="auth-sub">Пакетная публикация в Яндекс.Бизнес — выберите проект</div></div>')

    selected = st.session_state.get("selected_project_id")
    cols = st.columns(3)
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

    # Прогон только что закончился — перерисовываем страницу целиком, чтобы
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
        st.success(f"{action_ru} завершена. Отчёт — во вкладке «Отчёт».")
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
                "Браузер работает скрыто. Каждый город подтверждается ответом API Яндекса — "
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
        st.warning("В «Настройках» не указан email Яндекс.Бизнеса — без него не работает "
                   "защита от публикации не с того аккаунта.")
    if running:
        st.caption("Кнопка запуска заблокирована, пока идёт прогон — это защита от повторного старта "
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
                html(f'<div class="city-row"><span class="city-row-name">{T.esc(data.get("country", "—"))}</span>'
                     f'<span class="city-row-url">{T.esc(fp.name)}</span>'
                     f'<span class="badge badge-accent">{len(data.get("tasks") or [])} гор.</span></div>')
            with st.container(key="danger-clear-tasks"):
                if st.button("Очистить очередь", disabled=running, key="btn-clear-tasks"):
                    clear_tasks(project_id)
                    st.rerun()
    else:
        html(T.empty("📭", "Очередь пуста", "Соберите пост во вкладке «Публикация» и добавьте города в очередь."))


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: ПУБЛИКАЦИЯ
# ════════════════════════════════════════════════════════════════════

def tab_compose(project_id: str, config: dict) -> None:
    if not config["countries"]:
        html(T.empty("🏙", "Нет городов", "Добавьте страны и города во вкладке «Города»."))
        return

    queue: list[dict] = st.session_state.setdefault("queue", [])

    # ─── Шаг 1: тип поста ───
    html('<div class="card-title">1 · Тип поста</div>')
    types = pdata.POST_TYPES
    post_type = st.radio(
        "Тип поста",
        options=[t["id"] for t in types],
        format_func=lambda tid: next(f"{t['icon']} {t['title']}" for t in types if t["id"] == tid),
        horizontal=True, label_visibility="collapsed", key="compose-type",
    )

    # ─── Шаг 2: текст ───
    html('<div class="card-title">2 · Текст поста</div>')
    body = st.text_area(
        "Основной текст (контакты и хэштеги добавятся автоматически)",
        height=200, key="compose-body",
        placeholder="Например: Поступление на склад — балка двутавровая 20Б1, ГОСТ Р 57837-2017…",
    )

    # ─── Шаг 3: картинки ───
    html('<div class="card-title">3 · Картинки (необязательно)</div>')
    c1, c2 = st.columns(2)
    with c1:
        uploaded = st.file_uploader("Файлы с компьютера (до 4 на пост)",
                                    type=["jpg", "jpeg", "png", "gif", "webp"],
                                    accept_multiple_files=True, key="compose-images")
    with c2:
        image_urls_raw = st.text_area(
            "Или ссылки — по одной в строке (ImgBB / Imgur / Я.Диск / прямые)",
            height=110, key="compose-image-urls",
        )
        product_photos_raw = st.text_area(
            "Фото в раздел «Товары» — ссылки или пути, по одной в строке",
            height=90, key="compose-product-photos",
            help="Заливаются в карточку после успешной публикации поста. На статус поста не влияют.",
        )

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
        st.image([f.getvalue() for f in uploaded[:4]], width=120)

    all_images: list[str] = saved_paths + image_urls
    if len(all_images) > 4:
        st.warning(f"Яндекс берёт максимум 4 фото в пост — лишние {len(all_images) - 4} не отправятся.")

    # ─── Шаг 4: страны и города ───
    html('<div class="card-title">4 · Куда публикуем</div>')
    selected_countries = country_picker("compose", config)
    if not selected_countries:
        st.info("Выберите хотя бы одну страну.")
        return

    per_country: dict[str, list[str]] = {}
    for country in selected_countries:
        with st.expander(f"{flag(country['name'])} {country['name']}", expanded=len(selected_countries) <= 2):
            per_country[country["id"]] = city_selector(f"compose-{country['id']}", country)
            if (body or "").strip():
                st.caption("Так пост уйдёт в Яндекс:")
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
                    st.warning(f"{country['name']}: такой же пост уже в очереди — пропускаю.")
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
        html(f'<div class="card-title">📦 Очередь на публикацию — '
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
                st.toast(f"Сохранено файлов задач: {saved}. Откройте «Запуск».")
                time.sleep(0.8)
                st.rerun()
        with c2:
            with st.container(key="danger-clear-queue"):
                if st.button("Очистить очередь", use_container_width=True, key="btn-drop-queue"):
                    st.session_state["queue"] = []
                    st.rerun()


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: АКТУАЛИЗАЦИЯ
# ════════════════════════════════════════════════════════════════════

def tab_actualize(project_id: str, config: dict) -> None:
    if not config["countries"]:
        html(T.empty("🏙", "Нет городов", "Добавьте страны и города во вкладке «Города»."))
        return

    st.caption("Click зайдёт в раздел «Данные» каждой карточки и нажмёт «Данные актуальны», если кнопка "
               "там есть. Кнопки нет — значит актуализация не требуется, это не ошибка.")

    state = runner.read_state(project_id)
    running = state.get("status") == "running"

    selected_countries = country_picker("act", config)
    selection: dict[str, list[str]] = {}
    for country in selected_countries:
        with st.expander(f"{flag(country['name'])} {country['name']}", expanded=len(selected_countries) <= 2):
            selection[country["id"]] = city_selector(f"act-{country['id']}", country, default_all=True)

    total = sum(len(v) for v in selection.values())
    if selected_countries:
        st.write(f"Выбрано городов: **{total}**")

    if not yb.has_saved_session(project_id):
        st.warning("Сначала войдите в Яндекс в разделе «⚙️ Настройки».")
        return

    c1, c2 = st.columns([2, 1])
    with c1:
        if st.button(f"🔄 Запустить актуализацию ({cities_word(total)})", type="primary",
                     use_container_width=True, disabled=running or total == 0, key="btn-actualize"):
            save_actualize_tasks(project_id, config, selection)
            ok, msg = runner.start_actualize(project_id, headless=bool(get_settings(project_id)["headless"]))
            (st.toast if ok else st.error)(msg)
            time.sleep(0.6)
            st.rerun()
    with c2:
        if st.button("⏹ Остановить", use_container_width=True, disabled=not running, key="btn-stop-act"):
            runner.request_stop(project_id)
            st.rerun()

    st.divider()
    fragment = getattr(st, "fragment", None)
    if fragment and running:
        @fragment(run_every=2)
        def _live():
            _render_live_panel(project_id, was_running=True)
        _live()
    else:
        _render_live_panel(project_id)

    reports = runner.list_reports(project_id, "actualize", limit=1)
    if reports:
        data = runner.read_report(project_id, "actualize", reports[0]["name"])
        if data:
            st.divider()
            html('<div class="card-title">Последний отчёт актуализации</div>')
            html(T.report_summary(data.get("totals") or {}, data.get("durationSec"),
                                  keys=["actualized", "notNeeded", "failed"]))
            with st.expander(f"Детали ({cities_word(len(data.get('results') or []))})"):
                for r in data.get("results") or []:
                    html(T.report_row(r))


# ════════════════════════════════════════════════════════════════════
#  РАЗДЕЛ: ГОРОДА
# ════════════════════════════════════════════════════════════════════

def tab_cities(project_id: str, config: dict) -> None:
    html('<div class="card-title">Страны и города проекта</div>')
    st.caption("Ссылка города — адрес карточки Яндекс.Бизнеса. Подойдёт любой вид "
               "(/edit/, /edit/photos/, /p/edit/posts/) — Click сам приведёт его к разделу «Посты».")

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

    for country in list(config["countries"]):
        with st.expander(f"{flag(country['name'])} {country['name']} — {cities_word(len(country['cities']))}"):
            tab_add, tab_bulk = st.tabs(["Один город", "Списком"])

            with tab_add:
                c1, c2, c3 = st.columns([2, 4, 1])
                name = c1.text_input("Город", key=f"add-city-name-{country['id']}")
                url = c2.text_input("Ссылка на карточку", key=f"add-city-url-{country['id']}")
                c3.write("")
                if c3.button("＋", key=f"add-city-btn-{country['id']}", use_container_width=True):
                    if name.strip() and url.strip():
                        country["cities"].append({
                            "id": f"ct-{_slug(name)}-{int(time.time() * 1000)}",
                            "name": name.strip(), "url": url.strip(),
                        })
                        save_config(project_id)
                        st.rerun()
                    else:
                        st.warning("Нужны и название, и ссылка")

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
                when = (r.get("finishedAt") or "")[:19].replace("T", " ")
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
                notes.append("⏹ Прогон был остановлен вручную — часть городов не обработана.")
            if data.get("state") == "crashed":
                notes.append("💥 Прогон упал: отчёт содержит всё, что успели сделать до падения.")
            if data.get("state") == "in-progress":
                notes.append("⏳ Прогон ещё идёт — отчёт обновляется после каждого города.")
            if totals.get("unknown"):
                notes.append(f'⚠️ {cities_word(totals["unknown"])} с неподтверждённой публикацией. '
                             "Клик «Создать» был сделан, но Яндекс не подтвердил. "
                             "Проверьте вручную — повторять автоматически опасно (дубль).")
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
                by_country.setdefault(r.get("country") or r.get("package") or "—", []).append(r)

            for country, rows in by_country.items():
                ok = sum(1 for r in rows if r.get("status") == "ok")
                bad = sum(1 for r in rows if r.get("status") == "failed")
                with st.expander(f"{flag(country)} {country} — {ok}/{len(rows)} успешно"
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

    st.divider()
    _yandex_login_block(project_id)

    st.divider()
    html('<div class="card-title">⚙️ Параметры прогона</div>')
    c1, c2 = st.columns(2)
    with c1:
        headless = st.toggle("Скрытый браузер (headless)", value=bool(settings["headless"]),
                             key="set-headless",
                             help="Выключите, если хотите видеть окно браузера — работает только "
                                  "при локальном запуске, не в облаке.")
        delay = st.number_input("Пауза между городами, сек", min_value=0.0, max_value=60.0,
                                value=float(settings["delayBetweenPosts"]), step=0.5, key="set-delay",
                                help="Слишком маленькая пауза ловит антифлуд Яндекса.")
    with c2:
        strict = st.toggle("Строгая проверка аккаунта", value=bool(settings["strictAccountCheck"]),
                           key="set-strict",
                           help="Останавливать прогон, если в Яндексе залогинен другой аккаунт.")
        dedup = st.number_input("Не повторять тот же пост, часов", min_value=0.0, max_value=168.0,
                                value=float(settings["dedupWindowHours"]), step=1.0, key="set-dedup",
                                help="Защита от дубля: тот же текст в тот же город в этом окне "
                                     "повторно не отправляется.")
    retry_unknown = st.toggle(
        "Повторять неопределённые публикации (опасно)", value=bool(settings["retryUnknown"]),
        key="set-retry-unknown",
        help="Если Яндекс не подтвердил публикацию, но клик «Создать» был — повторить попытку. "
             "Может создать дубль. По умолчанию выключено: такие города помечаются «проверьте вручную».",
    )
    if retry_unknown:
        st.warning("Повтор после неподтверждённого клика — самая частая причина двойных постов. "
                   "Включайте только осознанно.")

    changed = (headless != settings["headless"] or float(delay) != float(settings["delayBetweenPosts"])
               or strict != settings["strictAccountCheck"] or retry_unknown != settings["retryUnknown"]
               or float(dedup) != float(settings["dedupWindowHours"]))
    if changed:
        settings.update({"headless": headless, "delayBetweenPosts": float(delay),
                         "strictAccountCheck": strict, "retryUnknown": retry_unknown,
                         "dedupWindowHours": float(dedup)})
        save_config(project_id)

    st.divider()
    html('<div class="card-title">🧹 Обслуживание</div>')
    st.caption(f"📁 Данные проекта: `{paths.describe()}`")
    ledger = runner._read_ledger(project_id)  # noqa: SLF001 — служебный просмотр реестра
    st.caption(f"Реестр отправленных постов: {len(ledger)} записей. "
               "Именно он не даёт опубликовать один и тот же текст в город дважды.")
    c1, c2 = st.columns(2)
    with c1:
        with st.container(key="danger-clear-ledger"):
            if st.button("Очистить реестр публикаций", key="btn-clear-ledger"):
                runner.clear_ledger(project_id)
                st.toast("Реестр очищен — защита от дублей начнёт отсчёт заново.")
                st.rerun()
    with c2:
        with st.container(key="danger-logout"):
            if st.button("Выйти из проекта", key="btn-logout"):
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
        if "libgbm" in text or "shared libraries" in text:
            st.info("Не хватает системной библиотеки. В облаке добавьте её в `packages.txt` "
                    "и перезапустите приложение, либо задайте переменную `CLICK_BROWSER=firefox`.")
    with st.expander("Технические подробности"):
        st.code(text[:6000])


def _yandex_login_block(project_id: str) -> None:
    """Пошаговый вход в Яндекс: браузер headless, вместо окна — скриншот."""
    worker = get_worker()
    html('<div class="card-title">🔐 Вход в Яндекс</div>')
    headless_login = bool(get_settings(project_id)["headless"])

    if yb.has_saved_session(project_id):
        st.success("Сессия Яндекса сохранена — публикация пойдёт в фоне без повторного входа.")
        with st.container(key="danger-reset-session"):
            if st.button("Войти заново (сбросить сессию)", key="yb-reset"):
                yb.session_path(project_id).unlink(missing_ok=True)
                for k in ("yb_flow", "yb_screenshot", "yb_step"):
                    st.session_state.pop(k, None)
                st.rerun()
        return

    step = st.session_state.get("yb_step", "idle")

    if step == "idle":
        st.caption("Откроется скрытый браузер на странице входа Яндекса. Дальше вы вводите логин, "
                   "пароль и код — по картинке, которую Click показывает после каждого шага.")
        if st.button("Начать вход", type="primary", key="yb-start"):
            old = st.session_state.get("yb_flow")
            if old is not None:
                try:
                    worker.call(old.close)
                except Exception:
                    pass
            try:
                with st.spinner("Открываю браузер…"):
                    flow = yb.YbLoginFlow(project_id, headless=headless_login)
                    shot = worker.call(flow.start)
            except Exception as e:  # noqa: BLE001
                _browser_error(e)
                return
            st.session_state.yb_flow = flow
            st.session_state.yb_screenshot = shot
            st.session_state.yb_step = "first"
            st.rerun()
        return

    st.image(st.session_state.yb_screenshot, caption="Что сейчас на экране Яндекса")
    flow: yb.YbLoginFlow = st.session_state.yb_flow
    shot = None

    if step == "first":
        st.caption("Если логин уже подставлен правильно — просто нажмите «Продолжить».")
        if st.button("Продолжить (ничего не менять)", key="yb-just-next"):
            with st.spinner("Жму «Далее»…"):
                shot = worker.call(flow.click_next)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Если просит логин / e-mail**")
            login_value = st.text_input("Логин или e-mail", key="yb-login")
            if st.button("Отправить логин", key="yb-submit-login") and login_value:
                with st.spinner("Отправляю логин…"):
                    shot = worker.call(flow.submit_login, login_value)
        with c2:
            st.markdown("**Если просит телефон**")
            phone = st.text_input("Номер телефона", key="yb-phone")
            if st.button("Отправить телефон", key="yb-submit-phone") and phone:
                with st.spinner("Отправляю номер…"):
                    shot = worker.call(flow.submit_phone, phone)
        if shot is not None:
            st.session_state.yb_screenshot = shot
            st.session_state.yb_step = "next"
            st.rerun()
        return

    # step == 'next'
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Если просит пароль**")
        password = st.text_input("Пароль", type="password", key="yb-password")
        pw_clicked = st.button("Войти по паролю", key="yb-submit-password")
    with c2:
        st.markdown("**Если просит код (SMS / почта / приложение)**")
        code = st.text_input("Код", key="yb-code")
        code_clicked = st.button("Подтвердить код", key="yb-submit-code")

    st.caption("Подтвердили вход в приложении Яндекса? Нажмите «Проверить вход».")
    check_clicked = st.button("Проверить вход", key="yb-check")

    try:
        if pw_clicked and password:
            with st.spinner("Проверяю пароль…"):
                shot = worker.call(flow.submit_password, password)
        elif code_clicked and code:
            with st.spinner("Проверяю код…"):
                shot = worker.call(flow.submit_code, code)
        elif check_clicked:
            shot = worker.call(flow.screenshot)
    except Exception as e:  # noqa: BLE001
        st.error(f"Ошибка: {type(e).__name__}: {e}")
        return

    if shot is None:
        return

    st.session_state.yb_screenshot = shot
    try:
        with st.spinner("Проверяю, выполнен ли вход…"):
            logged_in = worker.call(flow.is_logged_in)
    except Exception as e:  # noqa: BLE001
        st.error(f"Ошибка: {type(e).__name__}: {e}")
        return

    if logged_in:
        account = None
        try:
            account = worker.call(flow.current_account)
        except Exception:
            pass
        worker.call(flow.save_session)
        worker.call(flow.close)
        for k in ("yb_flow", "yb_screenshot", "yb_step"):
            st.session_state.pop(k, None)
        st.success("Вход выполнен, сессия сохранена!" + (f" Аккаунт: {account}" if account else ""))
    else:
        st.warning("Похоже, вход ещё не завершён — посмотрите на новый снимок выше.")
    st.rerun()


# ════════════════════════════════════════════════════════════════════
#  ГЛАВНЫЙ ЭКРАН
# ════════════════════════════════════════════════════════════════════

def show_main(project_id: str) -> None:
    inject_css()
    ensure_dirs(project_id)
    config = get_config(project_id)
    project = PROJECTS[project_id]

    head, ctrl = st.columns([14, 1])
    with head:
        html(T.topbar(project, status_pills(project_id)))
    with ctrl:
        if st.button("🌙" if theme() == "dark" else "☀️", key="btn-theme", use_container_width=True,
                     help="Переключить тему"):
            st.session_state["theme"] = "light" if theme() == "dark" else "dark"
            st.rerun()

    with st.container(key="click-tabs"):
        section = st.radio("Раздел", SECTIONS, horizontal=True,
                           label_visibility="collapsed", key="main-section")

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
    if project_id:
        show_main(project_id)
    else:
        show_login()


main()
