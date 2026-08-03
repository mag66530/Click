"""
ui_theme.py – дизайн оригинального Click, перенесённый на Streamlit.

CSS взят из _ui.js (переменные, палитра, карточки, кнопки, пилюли, степпер,
лог-бокс, строки отчёта) и адаптирован под DOM Streamlit. Цель – чтобы вкладки,
кнопки, карточки и отчёт выглядели так же, как в оригинальном приложении,
а не как дефолтная форма Streamlit.

Здесь же – маленькие HTML-хелперы (топбар, карточка, пилюли, лог), чтобы
streamlit_app.py оставался читаемым.
"""

from __future__ import annotations

import html as _html

# ─── Палитра 1:1 из :root в _ui.js ──────────────────────────────────
DARK = {
    "bg": "#0a0c14", "bg1": "#10131d", "bg2": "#171b28", "bg3": "#1e2333", "bg4": "#272d40",
    "border": "#252b3c", "border2": "#323a52", "borderHi": "#4a5578",
    "text": "#e8eaf3", "text2": "#b4bacf", "muted": "#7078a0", "dim": "#4a5070",
    "accBg": "rgba(91,124,250,0.12)", "accBg2": "rgba(91,124,250,0.2)",
    "grnBg": "rgba(16,185,129,0.12)", "redBg": "rgba(239,68,68,0.12)",
    "yelBg": "rgba(245,158,11,0.12)", "pinkBg": "rgba(236,72,153,0.12)",
    "shadowSm": "0 2px 8px rgba(0,0,0,0.25)", "shadowMd": "0 8px 24px rgba(0,0,0,0.35)",
    "shadowLg": "0 16px 48px rgba(0,0,0,0.5)",
    "logBg": "#06080e", "logFg": "#c5cce0",
    "scheme": "dark",
}

LIGHT = {
    "bg": "#f7f8fb", "bg1": "#ffffff", "bg2": "#ffffff", "bg3": "#f1f3f8", "bg4": "#e6e9f2",
    "border": "#e3e6ef", "border2": "#d1d6e3", "borderHi": "#aeb4c7",
    "text": "#141824", "text2": "#353d55", "muted": "#6b7189", "dim": "#9aa0b5",
    "accBg": "rgba(91,124,250,0.10)", "accBg2": "rgba(91,124,250,0.18)",
    "grnBg": "rgba(16,185,129,0.10)", "redBg": "rgba(239,68,68,0.10)",
    "yelBg": "rgba(245,158,11,0.12)", "pinkBg": "rgba(236,72,153,0.10)",
    "shadowSm": "0 1px 3px rgba(20,24,36,0.08)", "shadowMd": "0 6px 20px rgba(20,24,36,0.08)",
    "shadowLg": "0 12px 40px rgba(20,24,36,0.14)",
    "logBg": "#0d1018", "logFg": "#e4e7f1",
    "scheme": "light",
}

ACC = "#5b7cfa"
ACC2 = "#8b5cf6"
GRN = "#10b981"
RED = "#ef4444"
YEL = "#f59e0b"
GRADIENT = f"linear-gradient(135deg, {ACC} 0%, {ACC2} 100%)"


def css(theme: str = "dark") -> str:
    """Полный <style> для страницы. theme: 'dark' | 'light'."""
    c = DARK if theme != "light" else LIGHT
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {{
  --font: 'Inter', -apple-system, 'Segoe UI', Roboto, system-ui, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, 'SF Mono', Consolas, monospace;
  --r-xs: 4px; --r-sm: 8px; --r-md: 12px; --r-lg: 16px;
  --ease: cubic-bezier(0.16, 1, 0.3, 1);
  --acc: {ACC}; --acc-2: {ACC2}; --grn: {GRN}; --red: {RED}; --yel: {YEL};
  --bg: {c['bg']}; --bg-1: {c['bg1']}; --bg-2: {c['bg2']}; --bg-3: {c['bg3']}; --bg-4: {c['bg4']};
  --border: {c['border']}; --border-2: {c['border2']}; --border-hi: {c['borderHi']};
  --text: {c['text']}; --text-2: {c['text2']}; --muted: {c['muted']}; --dim: {c['dim']};
  --acc-bg: {c['accBg']}; --acc-bg-2: {c['accBg2']};
  --grn-bg: {c['grnBg']}; --red-bg: {c['redBg']}; --yel-bg: {c['yelBg']};
  --shadow-sm: {c['shadowSm']}; --shadow-md: {c['shadowMd']}; --shadow-lg: {c['shadowLg']};
  --gradient: {GRADIENT};
  --gradient-subtle: linear-gradient(135deg, rgba(91,124,250,0.08), rgba(139,92,246,0.08));
  --log-bg: {c['logBg']}; --log-fg: {c['logFg']};
  color-scheme: {c['scheme']};
}}

/* ─── Каркас Streamlit ─────────────────────────────────────────── */
.stApp {{ background: var(--bg); color: var(--text); font-family: var(--font); }}
[data-testid="stHeader"] {{ background: transparent; height: 0; }}
[data-testid="stToolbar"] {{ right: 8px; }}
[data-testid="stDecoration"] {{ display: none; }}
[data-testid="stAppViewBlockContainer"],
.block-container {{ padding-top: 1.1rem !important; padding-bottom: 4rem !important; max-width: 1240px; }}
[data-testid="stSidebar"] {{ background: var(--bg-1); border-right: 1px solid var(--border); }}
/* ВАЖНО: иконки Streamlit – лигатурный шрифт Material Symbols. Если накрыть их
   своим font-family, вместо стрелки экспандера рисуется текст «arrow_right».
   Поэтому иконки исключаем из общего правила и явно возвращаем им их шрифт. */
.stApp, .stApp p, .stApp label, .stApp li,
.stApp span:not([data-testid="stIconMaterial"]):not([class*="aterial"]):not([class*="icon"]) {{
  font-family: var(--font);
}}
[data-testid="stIconMaterial"], .material-icons, .material-symbols-rounded,
span[class*="aterial"], [data-testid="stExpanderIcon"] {{
  font-family: 'Material Symbols Rounded', 'Material Icons', 'Material Symbols Outlined' !important;
}}
h1, h2, h3, h4, h5 {{ font-family: var(--font) !important; letter-spacing: -0.02em; color: var(--text) !important; }}
h1 {{ font-size: 24px !important; font-weight: 800 !important; }}
h2 {{ font-size: 18px !important; font-weight: 700 !important; }}
h3 {{ font-size: 15px !important; font-weight: 700 !important; }}
hr {{ border-color: var(--border) !important; }}
[data-testid="stMarkdownContainer"] p {{ color: var(--text-2); }}
::-webkit-scrollbar {{ width: 10px; height: 10px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: var(--border-2); border-radius: 5px; border: 2px solid var(--bg); }}
::-webkit-scrollbar-thumb:hover {{ background: var(--border-hi); }}

/* Шапка и вкладки – во всю ширину окна, содержимое остаётся по центру.
   В оригинале .topbar и .tabs-bar это полосы на всю ширину, а main – 1240px. */
/* Это flex-элемент внутри контейнера Streamlit: без min-width его ужимает
   обратно до ширины колонки, и 100vw не срабатывает. */
.st-key-click-topbar, .st-key-click-tabs {{
  width: 100vw !important; min-width: 100vw; flex: 0 0 auto;
  margin-left: calc(-50vw + 50%); padding: 0 22px;
}}
.st-key-click-topbar .click-topbar {{ border-radius: 0; border-left: none; border-right: none;
  margin-bottom: 0; padding: 12px 22px; }}
.st-key-click-tabs {{ background: var(--bg-1); border-bottom: 1px solid var(--border);
  margin-bottom: 18px; }}
.st-key-click-tabs [role="radiogroup"] {{ border-bottom: none; margin-bottom: 0; }}

/* ─── Топбар ───────────────────────────────────────────────────── */
.click-topbar {{
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  padding: 12px 18px; margin: 0 0 14px;
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: var(--r-md); box-shadow: var(--shadow-sm);
}}
.click-logo {{ display: flex; align-items: center; gap: 12px; }}
.click-logo-icon {{
  width: 38px; height: 38px; border-radius: 10px; background: var(--gradient);
  display: flex; align-items: center; justify-content: center; color: #fff;
  box-shadow: 0 4px 14px rgba(91,124,250,0.35); font-size: 18px;
}}
.click-logo-title {{ font-size: 15px; font-weight: 700; letter-spacing: -0.02em; color: var(--text); }}
.click-logo-sub {{ font-size: 11px; color: var(--muted); font-weight: 500; }}
.click-topbar-spacer {{ flex: 1; }}
.click-projbadge {{
  display: inline-flex; align-items: center; gap: 9px;
  padding: 7px 14px; border-radius: 999px;
  background: var(--bg-2); border: 1px solid var(--border); font-size: 13px; font-weight: 600;
}}
.click-projbadge-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
.click-projbadge-sub {{ color: var(--muted); font-weight: 500; font-size: 12px; }}

.pills {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.pill {{
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 11px; border-radius: 20px; font-size: 11px; font-weight: 600;
}}
.pill::before {{ content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }}
.pill-ok {{ background: var(--grn-bg); color: var(--grn); }}
.pill-err {{ background: var(--red-bg); color: var(--red); }}
.pill-warn {{ background: var(--yel-bg); color: var(--yel); }}
.pill-info {{ background: var(--acc-bg); color: var(--acc); }}

/* ─── Карточки ─────────────────────────────────────────────────── */
.card {{
  background: var(--bg-1); border: 1px solid var(--border);
  border-radius: var(--r-md); padding: 20px; margin-bottom: 14px; box-shadow: var(--shadow-sm);
}}
.card-title {{
  display: inline-flex; align-items: center; gap: 9px;
  font-size: 14px; font-weight: 700; color: var(--text); letter-spacing: -0.01em; margin-bottom: 12px;
}}
.hint {{ font-size: 11.5px; color: var(--dim); }}
.label {{
  font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--muted);
}}
.section-label {{
  font-size: 11px; font-weight: 800; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--yel); display: inline-flex; align-items: center; gap: 6px;
}}
.badge {{ display: inline-flex; align-items: center; padding: 2px 10px;
          border-radius: 20px; font-size: 11px; font-weight: 600; }}
.badge-muted {{ background: var(--bg-4); color: var(--muted); }}
.badge-accent {{ background: var(--acc-bg); color: var(--acc); }}
.badge-success {{ background: var(--grn-bg); color: var(--grn); }}
.badge-danger {{ background: var(--red-bg); color: var(--red); }}
.badge-warn {{ background: var(--yel-bg); color: var(--yel); }}

/* ─── Кнопки Streamlit → .btn оригинала ────────────────────────── */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  font-family: var(--font); font-size: 13px; font-weight: 600;
  padding: 10px 18px; border-radius: var(--r-sm);
  background: var(--bg-3); color: var(--text); border: 1px solid var(--border);
  transition: all .15s var(--ease); box-shadow: none;
}}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {{
  background: var(--bg-4); border-color: var(--border-hi); color: var(--text); transform: translateY(-1px);
}}
.stButton > button:active {{ transform: translateY(0); }}
.stButton > button:focus, .stButton > button:focus-visible {{
  box-shadow: 0 0 0 3px var(--acc-bg) !important; outline: none !important; color: var(--text);
}}
.stButton > button:disabled {{ opacity: .45; transform: none !important; }}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"],
.stDownloadButton > button[kind="primary"] {{
  background: var(--gradient); color: #fff; border-color: transparent;
  box-shadow: 0 4px 14px rgba(91,124,250,.30);
}}
.stButton > button[kind="primary"]:hover {{ box-shadow: 0 6px 20px rgba(91,124,250,.45); color: #fff; }}
.stButton > button[kind="primary"]:disabled, .stFormSubmitButton > button[kind="primary"]:disabled {{
  background: var(--bg-3); color: var(--muted); box-shadow: none; border-color: var(--border);
}}

/* Кнопка темы в шапке – компактная, вровень с топбаром */
.st-key-btn-theme .stButton > button, .st-key-btn-theme button {{
  height: 62px; font-size: 20px; padding: 0; background: var(--bg-1); border-color: var(--border);
}}

/* Опасные действия – красные (по ключу виджета) */
[class*="st-key-danger-"] .stButton > button,
[class*="st-key-danger-"] button {{ background: var(--red-bg); color: var(--red); border-color: transparent; }}
[class*="st-key-danger-"] .stButton > button:hover,
[class*="st-key-danger-"] button:hover {{ background: var(--red); color: #fff; }}

/* Цвет подписи кнопки задаём явно: внутри неё markdown-контейнер, которому
   общее правило даёт приглушённый цвет – на светлой теме это нечитаемо. */
.stButton > button p, .stButton > button div, .stButton > button span,
.stDownloadButton > button p, .stFormSubmitButton > button p {{ color: inherit !important; }}
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  color: var(--text) !important;
}}
.stButton > button[kind="primary"], .stButton > button[kind="primary"] p {{ color: #fff !important; }}
.stButton > button:disabled, .stButton > button:disabled p {{ color: var(--muted) !important; }}

/* ─── Поля ввода ───────────────────────────────────────────────── */
.stTextInput input, .stTextArea textarea, .stNumberInput input,
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="input"] * {{
  background: var(--bg-2) !important; color: var(--text) !important;
  border-color: var(--border) !important;
  border-radius: var(--r-sm) !important; font-family: var(--font) !important; font-size: 13.5px !important;
}}
.stTextInput input::placeholder, .stTextArea textarea::placeholder {{ color: var(--dim) !important; }}
[data-baseweb="select"] *, [data-baseweb="popover"] li {{ color: var(--text) !important; }}
.stCheckbox p, .stCheckbox label span {{ color: var(--text) !important; }}
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div {{
  border: 1px solid var(--border) !important; background: var(--bg-2) !important;
}}
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within {{ border-color: var(--acc) !important; }}
.stTextInput label, .stTextArea label, .stSelectbox label, .stMultiSelect label,
.stNumberInput label, .stFileUploader label, .stCheckbox label, .stRadio label {{
  font-size: 11px !important; font-weight: 700 !important; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--muted) !important;
}}
[data-testid="stWidgetLabel"] p {{ color: var(--muted) !important; font-size: 11px !important;
  font-weight: 700 !important; text-transform: uppercase; letter-spacing: .04em; }}
[data-baseweb="popover"] li {{ font-family: var(--font); }}
[data-testid="stFileUploaderDropzone"] {{
  background: var(--bg-2); border: 1px dashed var(--border-2); border-radius: var(--r-sm);
}}
.stCheckbox p, .stRadio p {{ text-transform: none !important; font-size: 13px !important;
  font-weight: 500 !important; letter-spacing: 0 !important; color: var(--text) !important; }}

/* Мультиселект: теги – как .badge-accent */
[data-baseweb="tag"] {{ background: var(--acc-bg) !important; color: var(--acc) !important;
  border-radius: 20px !important; font-weight: 600; }}

/* ─── Вкладки: radio → .tabs-bar оригинала ─────────────────────── */
/* Streamlit вешает класс st-key-<key> на контейнер с key – это наш якорь.
   .click-tabs оставлен как запасной вариант для старых версий. */
.click-tabs [role="radiogroup"], .st-key-click-tabs [role="radiogroup"] {{
  display: flex; gap: 2px; flex-wrap: wrap;
  border-bottom: 1px solid var(--border); padding: 0 4px; margin-bottom: 18px;
}}
.click-tabs [role="radiogroup"] label, .st-key-click-tabs [role="radiogroup"] label {{
  padding: 10px 16px !important; margin: 0 !important;
  border: 1px solid transparent; border-bottom: 2px solid transparent;
  border-radius: var(--r-sm) var(--r-sm) 0 0; margin-bottom: -1px !important;
  background: transparent; cursor: pointer; transition: all .15s var(--ease);
}}
.click-tabs [role="radiogroup"] label p, .st-key-click-tabs [role="radiogroup"] label p {{
  font-size: 13px !important; font-weight: 600 !important; color: var(--muted) !important;
  text-transform: none !important; letter-spacing: 0 !important; white-space: nowrap;
}}
.click-tabs [role="radiogroup"] label:hover p,
.st-key-click-tabs [role="radiogroup"] label:hover p {{ color: var(--text) !important; }}
.click-tabs [role="radiogroup"] label:has(input:checked),
.st-key-click-tabs [role="radiogroup"] label:has(input:checked) {{
  background: var(--bg); border-color: var(--border); border-bottom-color: var(--bg);
}}
.click-tabs [role="radiogroup"] label:has(input:checked) p,
.st-key-click-tabs [role="radiogroup"] label:has(input:checked) p {{ color: var(--text) !important; }}
/* Прячем кружок радио: внутри label[stRadioOption] это первый div строки,
   второй – stMarkdownContainer с текстом. */
.click-tabs [data-testid="stRadioOption"] > div > div > div:first-child,
.st-key-click-tabs [data-testid="stRadioOption"] > div > div > div:first-child {{ display: none !important; }}

/* Тип поста в «Публикации» – радио-плитки вместо кружочков */
.st-key-compose-type [role="radiogroup"] {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.st-key-compose-type [role="radiogroup"] label {{
  padding: 10px 16px !important; margin: 0 !important; border-radius: var(--r-sm);
  background: var(--bg-2); border: 1px solid var(--border); transition: all .15s var(--ease);
}}
.st-key-compose-type [role="radiogroup"] label p {{
  font-size: 13px !important; font-weight: 600 !important;
  color: var(--text-2) !important; text-transform: none !important; letter-spacing: 0 !important;
}}
.st-key-compose-type [role="radiogroup"] label:has(input:checked) {{
  background: var(--acc-bg); border-color: var(--acc);
}}
.st-key-compose-type [role="radiogroup"] label:has(input:checked) p {{ color: var(--acc) !important; }}
.st-key-compose-type [data-testid="stRadioOption"] > div > div > div:first-child {{ display: none !important; }}

/* ─── Экспандеры → .country-section ────────────────────────────── */
[data-testid="stExpander"] {{
  border: 1px solid var(--border) !important; border-radius: var(--r-sm) !important;
  background: var(--bg-1) !important; margin-bottom: 8px; overflow: hidden;
}}
[data-testid="stExpander"] summary {{
  background: var(--bg-2); padding: 10px 14px !important;
  font-size: 13.5px; font-weight: 700; color: var(--text);
}}
[data-testid="stExpander"] summary:hover {{ background: var(--bg-3); }}
[data-testid="stExpander"] [data-testid="stExpanderDetails"] {{ padding-top: 6px; }}

/* ─── Контейнеры с рамкой → .card ──────────────────────────────── */
[data-testid="stVerticalBlockBorderWrapper"] > div > [data-testid="stVerticalBlock"] {{ gap: .6rem; }}
div[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {{
  border-radius: var(--r-md);
}}

/* ─── Метрики → .stat-card ─────────────────────────────────────── */
[data-testid="stMetric"] {{
  background: var(--bg-2); border: 1px solid var(--border);
  border-radius: var(--r-sm); padding: 14px;
}}
[data-testid="stMetricLabel"] p {{ font-size: 11px !important; font-weight: 700 !important;
  color: var(--muted) !important; text-transform: uppercase; letter-spacing: .04em; }}
[data-testid="stMetricValue"] {{ font-family: var(--mono); font-size: 26px !important;
  font-weight: 700 !important; color: var(--text); }}

/* ─── Прогресс ─────────────────────────────────────────────────── */
.stProgress > div > div > div > div {{ background: var(--gradient); }}
.stProgress > div > div > div {{ background: var(--bg-3); }}

/* ─── Алерты ───────────────────────────────────────────────────── */
[data-testid="stAlert"] {{ border-radius: var(--r-sm); border: 1px solid var(--border); font-size: 13px; }}

/* ─── Плитки проектов (экран входа) ────────────────────────────── */
.project-tile {{
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 24px 14px 18px; background: var(--bg-2); border: 2px solid var(--border);
  border-radius: var(--r-md); position: relative; overflow: hidden;
  transition: all .2s var(--ease); text-align: center;
}}
.project-tile::before {{
  content: ''; position: absolute; left: 0; right: 0; top: 0; height: 4px;
  background: var(--proj-color); transition: height .2s;
}}
.project-tile:hover {{ border-color: var(--proj-color); transform: translateY(-3px); box-shadow: var(--shadow-md); }}
.project-tile:hover::before {{ height: 8px; }}
.project-tile.selected {{ border-color: var(--proj-color); box-shadow: 0 0 0 3px color-mix(in srgb, var(--proj-color) 22%, transparent); }}
.project-tile-icon {{
  font-size: 34px; line-height: 1; width: 70px; height: 70px;
  display: flex; align-items: center; justify-content: center; border-radius: 50%;
  background: var(--proj-color); color: #fff;
  box-shadow: 0 4px 12px color-mix(in srgb, var(--proj-color) 40%, transparent);
}}
.project-tile-name {{ font-size: 18px; font-weight: 700; margin-top: 4px; color: var(--text); }}
.project-tile-fullname {{ font-size: 12px; color: var(--muted); }}
.project-tile-cities {{ font-size: 11px; color: var(--dim); font-family: var(--mono); }}

.auth-wrap {{ max-width: 760px; margin: 6vh auto 0; }}
.auth-logo {{
  width: 56px; height: 56px; border-radius: 16px; background: var(--gradient);
  display: flex; align-items: center; justify-content: center;
  font-size: 26px; color: #fff; margin: 0 auto 14px; box-shadow: 0 8px 24px rgba(91,124,250,.35);
}}
.auth-title {{ font-size: 24px; font-weight: 800; text-align: center; color: var(--text); }}
.auth-sub {{ font-size: 13px; color: var(--muted); text-align: center; margin-bottom: 22px; }}

/* ─── Степпер ──────────────────────────────────────────────────── */
.step {{
  display: flex; gap: 16px; padding: 16px 18px; margin-bottom: 10px;
  background: var(--bg-1); border: 1px solid var(--border); border-radius: var(--r-md);
}}
.step.done {{ border-color: rgba(16,185,129,0.3); background: linear-gradient(135deg, var(--grn-bg), transparent); }}
.step.active {{ border-color: var(--acc); box-shadow: 0 0 0 3px var(--acc-bg); }}
.step.locked {{ opacity: .5; }}
.step-num {{
  flex-shrink: 0; width: 40px; height: 40px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 16px; background: var(--bg-3);
  color: var(--muted); border: 2px solid var(--border);
}}
.step.done .step-num {{ background: var(--grn); color: #fff; border-color: var(--grn); }}
.step.active .step-num {{ background: var(--gradient); color: #fff; border-color: transparent;
  box-shadow: 0 4px 14px rgba(91,124,250,0.4); }}
.step-title {{ font-size: 15px; font-weight: 700; color: var(--text); margin-bottom: 3px; }}
.step-sub {{ font-size: 12.5px; color: var(--muted); line-height: 1.5; }}
.step-mark {{
  display: inline-flex; align-items: center; gap: 6px; margin-top: 8px;
  padding: 4px 11px; border-radius: 20px; background: var(--grn-bg); color: var(--grn);
  font-size: 12px; font-weight: 600;
}}

/* ─── Прогресс прогона ─────────────────────────────────────────── */
.run-progress {{
  display: flex; align-items: center; gap: 14px; padding: 14px 18px; margin-bottom: 12px;
  background: var(--gradient-subtle); border: 1px solid var(--acc); border-radius: var(--r-sm);
}}
.run-progress-ico {{
  width: 36px; height: 36px; border-radius: 50%; background: var(--gradient); color: #fff;
  display: flex; align-items: center; justify-content: center; font-size: 18px;
  animation: clickpulse 1.4s ease-in-out infinite;
}}
@keyframes clickpulse {{
  0%, 100% {{ transform: scale(1); box-shadow: 0 0 0 0 rgba(91,124,250,0.4); }}
  50% {{ transform: scale(1.05); box-shadow: 0 0 0 8px rgba(91,124,250,0); }}
}}
.run-progress-title {{ font-size: 14px; font-weight: 700; color: var(--text); }}
.run-progress-sub {{ font-size: 12px; color: var(--muted); }}

/* ─── Лог ──────────────────────────────────────────────────────── */
.log-box {{
  background: var(--log-bg); border: 1px solid var(--border); border-radius: var(--r-sm);
  padding: 14px; font-family: var(--mono); font-size: 12px; line-height: 1.7;
  color: var(--log-fg); white-space: pre-wrap; word-break: break-word;
  max-height: 460px; overflow-y: auto;
}}
.log-ok {{ color: #33d298; }} .log-err {{ color: #ff7c7c; }}
.log-warn {{ color: #fabe23; }} .log-info {{ color: #8fa8ff; }}
.log-dim {{ color: #5b6485; }}
.log-placeholder {{ color: var(--dim); text-align: center; padding: 22px 0;
  font-family: var(--font); font-size: 12.5px; }}

/* ─── Отчёт ────────────────────────────────────────────────────── */
.report-summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin-bottom: 14px; }}
.report-stat {{ padding: 13px 15px; border-radius: var(--r-sm); border: 1px solid var(--border); }}
.report-stat-label {{ font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .04em; opacity: .8; margin-bottom: 4px; }}
.report-stat-value {{ font-size: 25px; font-weight: 800; font-family: var(--mono); line-height: 1.1; }}
.report-stat.ok {{ background: var(--grn-bg); color: var(--grn); border-color: rgba(16,185,129,.25); }}
.report-stat.noimg {{ background: var(--yel-bg); color: var(--yel); border-color: rgba(245,158,11,.25); }}
.report-stat.warn {{ background: var(--yel-bg); color: var(--yel); border-color: rgba(245,158,11,.4); }}
.report-stat.err {{ background: var(--red-bg); color: var(--red); border-color: rgba(239,68,68,.25); }}
.report-stat.skip {{ background: var(--acc-bg); color: var(--acc); border-color: rgba(91,124,250,.25); }}
.report-stat.dur {{ background: var(--bg-3); color: var(--text); }}

.report-row {{
  display: flex; align-items: center; gap: 12px; padding: 9px 13px;
  border-radius: var(--r-sm); background: var(--bg-2); border: 1px solid var(--border);
  margin-bottom: 6px;
}}
.report-row.ok {{ border-left: 3px solid var(--grn); }}
.report-row.noimg {{ border-left: 3px solid var(--yel); }}
.report-row.warn {{ border-left: 3px solid var(--yel); }}
.report-row.err {{ border-left: 3px solid var(--red); }}
.report-row.skip {{ border-left: 3px solid var(--acc); }}
.report-row-ico {{ font-size: 16px; flex-shrink: 0; }}
.report-row-city {{ font-size: 13px; font-weight: 600; flex: 0 0 170px; color: var(--text); }}
.report-row-reason {{ flex: 1; font-size: 12.5px; color: var(--text-2); }}
.report-row-dur {{ font-size: 11.5px; color: var(--dim); font-family: var(--mono); flex-shrink: 0; }}
@media (max-width: 720px) {{
  .report-row-city {{ flex: 1; }}
  .report-row-dur {{ display: none; }}
}}

/* ─── Пустое состояние ─────────────────────────────────────────── */
.empty {{ text-align: center; padding: 40px 20px; color: var(--muted); }}
.empty-icon {{ font-size: 42px; opacity: .35; margin-bottom: 12px; }}
.empty-title {{ font-size: 15px; font-weight: 700; color: var(--text); margin-bottom: 6px; }}
.empty-desc {{ font-size: 13px; max-width: 420px; margin: 0 auto; }}

/* ─── Плитки типа поста и карточки стран (1:1 с _ui.js) ────────── */
.post-type-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px;
}}
.post-type-card {{
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px;
  padding: 16px 10px; background: var(--bg-2); border: 1.5px solid var(--border);
  border-radius: var(--r-md); color: var(--text); text-align: center;
  transition: all .15s var(--ease);
}}
.post-type-card.active {{
  border-color: var(--acc); background: var(--acc-bg); color: var(--acc);
  box-shadow: 0 0 0 3px var(--acc-bg);
}}
.post-type-ico {{ font-size: 26px; line-height: 1; filter: grayscale(0.2); }}
.post-type-card.active .post-type-ico {{ filter: none; }}
.post-type-title {{ font-size: 12.5px; font-weight: 600; line-height: 1.25; }}

.country-card {{
  display: flex; align-items: center; gap: 10px;
  padding: 11px 13px; background: var(--bg-2); border: 1.5px solid var(--border);
  border-radius: var(--r-md); transition: all .15s var(--ease);
}}
.country-card.active {{ border-color: var(--acc); background: var(--acc-bg); }}
.country-card-flag {{ font-size: 20px; line-height: 1; }}
.country-card-body {{ display: flex; flex-direction: column; min-width: 0; }}
.country-card-name {{ font-size: 13.5px; font-weight: 700; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.country-card-count {{ font-size: 11px; color: var(--muted); font-family: var(--mono); }}

/* Кликабельная плитка: HTML-карточка + невидимая кнопка Streamlit поверх неё.
   Иначе под каждой карточкой болталась бы вторая, настоящая кнопка. */
[class*="st-key-tile-"] {{ position: relative; }}
[class*="st-key-tile-"] .stButton {{ position: absolute; inset: 0; margin: 0; z-index: 3; }}
[class*="st-key-tile-"] .stButton > button {{
  width: 100%; height: 100%; opacity: 0; padding: 0; border: none; background: transparent;
}}
[class*="st-key-tile-"] .stButton > button:hover {{ transform: none; }}
[class*="st-key-tile-"] [data-testid="stMarkdownContainer"] {{ cursor: pointer; }}

/* Секции-карточки: st.container(border=True) → .card оригинала */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {{
  background: var(--bg-1); border: 1px solid var(--border) !important;
  border-radius: var(--r-md) !important; box-shadow: var(--shadow-sm);
}}
[data-testid="stVerticalBlockBorderWrapper"] > div {{ padding: 4px 2px; }}

/* Загрузка файла: подписи Streamlit английские, подменяем на свои */
[data-testid="stFileUploaderDropzoneInstructions"] > div > span {{ display: none; }}
[data-testid="stFileUploaderDropzoneInstructions"] > div::before {{
  content: "Файлы с компьютера"; font-size: 12.5px; font-weight: 600; color: var(--text);
}}
[data-testid="stFileUploaderDropzoneInstructions"] > div > small {{ visibility: hidden; position: relative; }}
[data-testid="stFileUploaderDropzoneInstructions"] > div > small::after {{
  content: "JPG, PNG, GIF, WEBP · до 20 МБ"; visibility: visible;
  position: absolute; left: 0; white-space: nowrap; color: var(--muted);
}}
[data-testid="stFileUploaderDropzone"] button {{ font-size: 12px; }}

/* Строка страны в «Актуализации»: заголовок экспандера – одна строка,
   как ряд в оригинале, а не толстый блок. */
.st-key-act-rows [data-testid="stExpander"] {{ margin-bottom: 6px; }}
.st-key-act-rows [data-testid="stExpander"] summary {{
  background: var(--bg-2); padding: 9px 14px !important; font-size: 13px;
}}
.st-key-act-rows [data-testid="stExpander"] summary p {{ font-weight: 700; }}
.st-key-act-rows .stCheckbox {{ margin-bottom: 2px; }}
.st-key-act-rows .stCheckbox p {{ font-size: 12px !important; }}

/* ─── Превью текста поста ──────────────────────────────────────── */
.preview-box {{
  background: var(--bg-2); border: 1px solid var(--border); border-radius: var(--r-sm);
  padding: 14px 16px; font-size: 13px; line-height: 1.6; color: var(--text-2);
  white-space: pre-wrap; word-break: break-word; max-height: 340px; overflow-y: auto;
}}
.queue-item {{
  padding: 12px 14px; margin-bottom: 8px;
  background: var(--bg-2); border: 1px solid var(--border); border-radius: var(--r-sm);
}}
.queue-item-title {{ display: inline-flex; align-items: center; gap: 8px;
  font-size: 13.5px; font-weight: 700; color: var(--text); }}
.queue-item-text {{ font-size: 12.5px; color: var(--text-2); line-height: 1.5;
  white-space: pre-wrap; margin: 6px 0; word-break: break-word; }}
.city-row {{
  display: flex; align-items: center; gap: 12px; padding: 8px 12px; margin-bottom: 6px;
  background: var(--bg-2); border: 1px solid var(--border); border-radius: var(--r-sm);
}}
.city-row-num {{ width: 26px; font-family: var(--mono); font-size: 11px; color: var(--dim); font-weight: 700; }}
.city-row-name {{ flex: 0 0 170px; font-size: 13px; font-weight: 600; color: var(--text); }}
.city-row-url {{ flex: 1; font-size: 11.5px; color: var(--muted); font-family: var(--mono);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
</style>
"""


# ════════════════════════════════════════════════════════════════════
#  HTML-хелперы
# ════════════════════════════════════════════════════════════════════

def esc(s: object) -> str:
    return _html.escape(str(s if s is not None else ""))


def topbar(project: dict | None, pills: list[tuple[str, str]] | None = None) -> str:
    """Топбар оригинала: логотип + статус-пилюли + плашка активного проекта."""
    pills_html = "".join(
        f'<span class="pill pill-{kind}">{esc(text)}</span>' for kind, text in (pills or [])
    )
    badge = ""
    if project:
        badge = (
            f'<span class="click-projbadge">'
            f'<span class="click-projbadge-dot" style="background:{esc(project["color"])}"></span>'
            f'{esc(project["icon"])} {esc(project["name"])}'
            f'<span class="click-projbadge-sub">{esc(project["fullName"])}</span></span>'
        )
    return (
        '<div class="click-topbar">'
        '<div class="click-logo">'
        '<div class="click-logo-icon">➤</div>'
        '<div><div class="click-logo-title">Click</div>'
        '<div class="click-logo-sub">публикация постов</div></div></div>'
        f'<div class="pills">{pills_html}</div>'
        '<div class="click-topbar-spacer"></div>'
        f'{badge}</div>'
    )


def project_tile(project: dict, cities: int, selected: bool = False) -> str:
    return (
        f'<div class="project-tile{" selected" if selected else ""}" style="--proj-color:{esc(project["color"])}">'
        f'<div class="project-tile-icon">{esc(project["icon"])}</div>'
        f'<div class="project-tile-name">{esc(project["name"])}</div>'
        f'<div class="project-tile-fullname">{esc(project["fullName"])}</div>'
        f'<div class="project-tile-cities">{cities} городов</div>'
        f'</div>'
    )


def step(num: int, title: str, sub: str, state: str = "", mark: str = "") -> str:
    """state: '' | 'done' | 'active' | 'locked'"""
    mark_html = f'<div class="step-mark">{esc(mark)}</div>' if mark else ""
    num_html = "✓" if state == "done" else str(num)
    return (
        f'<div class="step {state}"><div class="step-num">{num_html}</div>'
        f'<div><div class="step-title">{esc(title)}</div>'
        f'<div class="step-sub">{sub}</div>{mark_html}</div></div>'
    )


def run_progress(title: str, sub: str, icon: str = "⚙️") -> str:
    return (
        f'<div class="run-progress"><div class="run-progress-ico">{esc(icon)}</div>'
        f'<div><div class="run-progress-title">{esc(title)}</div>'
        f'<div class="run-progress-sub">{esc(sub)}</div></div></div>'
    )


_LOG_CLASS_RULES = (
    ("log-err", ("[ERROR]", "❌", "💥")),
    ("log-warn", ("[WARN]", "⚠️", "🟡", "⏭", "🐢")),
    ("log-ok", ("✅", "✓ ")),
    ("log-info", ("📍", "📦", "🔁", "🌐", "🎯", "📡")),
)


def log_box(text: str, placeholder: str = "Лог пуст – запустите публикацию") -> str:
    if not text.strip():
        return f'<div class="log-box"><div class="log-placeholder">{esc(placeholder)}</div></div>'
    lines = []
    for raw in text.split("\n"):
        cls = "log-dim"
        for name, markers in _LOG_CLASS_RULES:
            if any(m in raw for m in markers):
                cls = name
                break
        else:
            if raw.strip():
                cls = ""
        lines.append(f'<span class="{cls}">{esc(raw)}</span>' if cls else esc(raw))
    return '<div class="log-box">' + "\n".join(lines) + "</div>"


_STAT_META = {
    "ok": ("ok", "Успешно"),
    "noImage": ("noimg", "Без картинки"),
    "unknown": ("warn", "Проверьте"),
    "failed": ("err", "Ошибок"),
    "skipped": ("skip", "Пропущено"),
    "actualized": ("ok", "Актуализировано"),
    "notNeeded": ("skip", "Не требовалось"),
}


def report_summary(totals: dict, duration_sec: int | None = None, keys: list[str] | None = None) -> str:
    keys = keys or ["ok", "noImage", "unknown", "failed", "skipped"]
    cells = [
        f'<div class="report-stat dur"><div class="report-stat-label">Всего</div>'
        f'<div class="report-stat-value">{int(totals.get("total", 0))}</div></div>'
    ]
    for k in keys:
        if k not in _STAT_META:
            continue
        value = int(totals.get(k, 0) or 0)
        if k in ("skipped", "unknown", "noImage", "notNeeded") and value == 0:
            continue
        cls, label = _STAT_META[k]
        cells.append(
            f'<div class="report-stat {cls}"><div class="report-stat-label">{label}</div>'
            f'<div class="report-stat-value">{value}</div></div>'
        )
    if duration_sec is not None:
        mins = duration_sec / 60
        dur = f"{duration_sec} сек" if duration_sec < 90 else f"{mins:.1f} мин"
        cells.append(
            f'<div class="report-stat dur"><div class="report-stat-label">Время</div>'
            f'<div class="report-stat-value" style="font-size:19px">{dur}</div></div>'
        )
    return '<div class="report-summary">' + "".join(cells) + "</div>"


_ROW_STYLE = {
    "ok": ("ok", "✅"),
    "no-image": ("noimg", "🟡"),
    "unknown": ("warn", "⚠️"),
    "failed": ("err", "🔴"),
    "skipped-duplicate": ("skip", "⏭"),
    "actualized": ("ok", "✅"),
    "not-needed": ("skip", "⊝"),
}


def report_row(item: dict) -> str:
    cls, ico = _ROW_STYLE.get(item.get("status", ""), ("err", "🔴"))
    if item.get("status") == "ok" and item.get("retried"):
        ico = "⚡"
    reason = item.get("reason") or ""
    if item.get("imageError"):
        reason += f' · фото: {item["imageError"]}'
    pp = item.get("productPhotos")
    if pp:
        reason += f' · 📸 товары: {pp.get("uploaded", 0)}/{pp.get("requested", 0)}'
    dur = f'{item.get("durationMs", 0) / 1000:.1f} сек' if item.get("durationMs") else ""
    return (
        f'<div class="report-row {cls}"><span class="report-row-ico">{ico}</span>'
        f'<span class="report-row-city">{esc(item.get("cityName", "–"))}</span>'
        f'<span class="report-row-reason">{esc(reason)}</span>'
        f'<span class="report-row-dur">{esc(dur)}</span></div>'
    )


def empty(icon: str, title: str, desc: str = "") -> str:
    return (
        f'<div class="empty"><div class="empty-icon">{esc(icon)}</div>'
        f'<div class="empty-title">{esc(title)}</div>'
        f'<div class="empty-desc">{esc(desc)}</div></div>'
    )


def card(title: str, body_html: str = "") -> str:
    head = f'<div class="card-title">{esc(title)}</div>' if title else ""
    return f'<div class="card">{head}{body_html}</div>'


def post_type_card(t: dict, active: bool) -> str:
    return (
        f'<div class="post-type-card{" active" if active else ""}">'
        f'<div class="post-type-ico">{esc(t["icon"])}</div>'
        f'<div class="post-type-title">{esc(t["title"])}</div></div>'
    )


def post_type_grid(types: list[dict], active_id: str) -> str:
    """Плитки типов поста – .post-type-card из _ui.js."""
    cells = "".join(
        f'<div class="post-type-card{" active" if t["id"] == active_id else ""}">'
        f'<div class="post-type-ico">{esc(t["icon"])}</div>'
        f'<div class="post-type-title">{esc(t["title"])}</div></div>'
        for t in types
    )
    return f'<div class="post-type-grid">{cells}</div>'


def country_card(flag_emoji: str, name: str, cities: int, active: bool = False) -> str:
    """Карточка страны – флаг, название, «N гор.»."""
    return (
        f'<div class="country-card{" active" if active else ""}">'
        f'<span class="country-card-flag">{esc(flag_emoji)}</span>'
        f'<span class="country-card-body"><span class="country-card-name">{esc(name)}</span>'
        f'<span class="country-card-count">{cities} гор.</span></span></div>'
    )


def preview_box(text: str) -> str:
    return f'<div class="preview-box">{esc(text)}</div>'
