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

# Метка сборки – одна на всё приложение, лежит в build.py. Держать её
# в каждом модуле своей строкой уже ломалось: у одного файла осталось
# старое значение, и Click принялся перезагружать все модули на каждое
# нажатие, пока не выбило по памяти.
from build import BUILD  # noqa: F401

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
    "cbBg": "#10131d", "cbBorder": "#3d4763",   # пустой квадратик галочки
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
    "cbBg": "#ffffff", "cbBorder": "#8b91a6",   # белый квадратик с тёмной рамкой
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
/* ВАЖНО: здесь НЕТ @import шрифтов Google.
   CSS вставляется заново при каждой перерисовке Streamlit, а @import блокирует
   отрисовку до ответа сервера. Из России fonts.googleapis.com часто отвечает
   долго или не отвечает вовсе – и каждый клик подвисал на секунды.
   Берём системные шрифты: на Windows это Segoe UI, визуально почти то же самое. */
:root {{
  --font: 'Inter', 'Segoe UI', -apple-system, Roboto, system-ui, sans-serif;
  --mono: ui-monospace, 'Cascadia Mono', 'Segoe UI Mono', Consolas, monospace;
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
/* Панель Streamlit («Deploy», «⋮») – прозрачный блок во всю ширину: он
   перехватывал клики по нашей кнопке темы. Пропускаем клики сквозь пустое
   место, оставляя кликабельными сами кнопки Streamlit. */
[data-testid="stHeader"], [data-testid="stHeader"] * {{ pointer-events: none; }}
[data-testid="stToolbar"] button, [data-testid="stToolbar"] a,
[data-testid="stToolbar"] [role="button"], [data-testid="stStatusWidget"],
[data-testid="stStatusWidget"] * {{ pointer-events: auto; }}
[data-testid="stToolbar"] {{ right: 8px; }}
[data-testid="stDecoration"] {{ display: none; }}
/* Подсказка (help у кнопок). Внутри у неё обычный markdown, а ему общее правило
   даёт приглушённый цвет текста – на светлой теме получалось тёмное на тёмном. */
[data-testid="stTooltipContent"], [role="tooltip"] {{
  background: #10131d !important; border: 1px solid #323a52 !important;
  border-radius: var(--r-sm) !important; box-shadow: var(--shadow-md);
}}
[data-testid="stTooltipContent"] *, [role="tooltip"] [data-testid="stMarkdownContainer"] *,
[data-testid="stTooltipContent"] p, [role="tooltip"] p {{
  color: #e8eaf3 !important; font-size: 12.5px !important;
}}
/* Блоки с <style> Streamlit кладёт в обычный элемент страницы. Сами они нулевой
   высоты, но между элементами стоит отступ 16px – два таких блока и давали
   пустую полосу над шапкой. Убираем их из потока; на сами стили это не влияет. */
[data-testid="stElementContainer"]:has(style) {{ display: none !important; }}
[data-testid="stAppViewBlockContainer"],
.block-container {{ padding-top: 0 !important; padding-bottom: 4rem !important; max-width: 1240px; }}
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
  margin-left: calc(-50vw + 50%); padding: 0 18px;
}}
/* Справа сверху висит панель Streamlit (Share, звёздочка, меню) – оставляем ей место,
   иначе плашка проекта и переключатель темы уезжают под неё. */
.st-key-click-topbar {{
  padding: 8px 210px 8px 18px;          /* справа – место под панель Streamlit */
  background: var(--bg-1); border-bottom: 1px solid var(--border);
}}
.st-key-click-topbar .click-topbar {{
  background: transparent; border: none; box-shadow: none; padding: 0; margin: 0;
}}
.st-key-click-topbar [data-testid="stColumn"] {{ display: flex; align-items: center; }}
.st-key-click-topbar [data-testid="stHorizontalBlock"] {{ gap: 8px; }}
/* Ноль по вертикали: своё поле полосе задаёт контейнер выше. Иначе содержимое
   шапки оказывается на 10px ниже кнопки темы – видно по несовпадению центров. */
.st-key-click-topbar .click-topbar {{ border-radius: 0; border-left: none; border-right: none;
  margin: 0; padding: 0; min-height: 46px; align-items: center; }}
.st-key-click-topbar [data-testid="stHorizontalBlock"] {{ align-items: center; }}
/* Левая часть шапки – обычный markdown-блок, и Streamlit вешает на его
   обёртки свои отступы. Из-за них логотип со статусами стоял ниже плашки
   проекта и кнопки темы: справа настоящие виджеты, слева – разметка с
   чужим полем. Обнуляем отступы обёрток и слегка приподнимаем блок,
   чтобы центры совпали. */
.st-key-click-topbar [data-testid="stColumn"]:first-child [data-testid="stElementContainer"],
.st-key-click-topbar [data-testid="stColumn"]:first-child [data-testid="stMarkdown"],
.st-key-click-topbar [data-testid="stColumn"]:first-child [data-testid="stMarkdownContainer"] {{
  margin: 0; padding: 0; width: 100%;
}}
.st-key-click-topbar [data-testid="stColumn"]:first-child .click-topbar {{ margin-top: -4px; }}
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
  height: 46px; width: 46px; min-width: 46px; font-size: 18px; padding: 0;
  background: var(--bg-2); border-color: var(--border); border-radius: 10px;
}}
/* Ключ стоит на самом виджете, поэтому класс висит на элементе, а не на
   родителе. Прижимаем влево – иначе между плашкой проекта и кнопкой темы
   остаётся пустая полоса в полсотни пикселей. */
.st-key-btn-theme {{ width: fit-content !important; margin-right: auto !important; }}

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
/* Разметка полей в свежем Streamlit – не baseweb, а свои root-элементы, и цвет
   рамки он берёт из своей темы: на светлой выходила толстая чёрная рамка. */
[data-testid="stTextInputRootElement"], [data-testid="stTextAreaRootElement"],
[data-testid="stNumberInputContainer"] {{
  background: var(--bg-2) !important; border: 1px solid var(--border) !important;
  border-radius: var(--r-sm) !important; box-shadow: none !important;
}}
[data-testid="stTextInputRootElement"]:focus-within,
[data-testid="stTextAreaRootElement"]:focus-within,
[data-testid="stNumberInputContainer"]:focus-within {{
  border-color: var(--acc) !important;
}}
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
/* Лог всегда тёмный – и на светлой теме тоже: цветные строки читаются только
   на тёмном фоне. !important обязателен: у Streamlit своя тема поверх нашей,
   и внутри раскрывашек она пробовала красить содержимое в свой цвет текста. */
.log-box {{
  background: var(--log-bg) !important; border: 1px solid var(--border);
  border-radius: var(--r-sm);
  padding: 14px; font-family: var(--mono); font-size: 12px; line-height: 1.7;
  color: var(--log-fg) !important; max-height: 460px; overflow-y: auto;
  /* column-reverse у контейнера с ОДНИМ ребёнком не меняет порядок текста,
     зато прокрутка стартует С НИЗА – свежие строки видны без скролла руками.
     Скриптом это не сделать: st.markdown вырезает <script>. */
  display: flex; flex-direction: column-reverse;
}}
.log-box .log-lines {{
  white-space: pre-wrap; word-break: break-word;
  background: transparent !important; color: var(--log-fg) !important;
}}
.log-box p, .log-box span:not([class]) {{ color: var(--log-fg) !important; }}
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

/* Кликабельная плашка отчёта: она же и фильтр. Раньше под цветными плашками
   стоял второй ряд кнопок с теми же названиями – одно и то же дважды. */
[class*="st-key-tile-stat-"] .stButton > button {{
  flex-direction: column; align-items: flex-start; justify-content: center; gap: 4px;
  padding: 12px 15px; min-height: 78px; text-align: left;
  border: 1px solid var(--stat-bd, var(--border));
  background-color: var(--stat-bg, var(--bg-3)) !important;
}}
[class*="st-key-tile-stat-"] .stButton > button p,
[class*="st-key-tile-stat-"] .stButton > button[kind="primary"] p {{
  font-size: 10.5px !important; font-weight: 700 !important; letter-spacing: .05em;
  text-transform: uppercase; line-height: 1.2 !important;
  color: var(--stat-c, var(--muted)) !important; opacity: .85;
}}
[class*="st-key-tile-stat-"] .stButton > button::after {{
  content: var(--val, ""); font-family: var(--mono); font-size: 24px; font-weight: 800;
  line-height: 1.05; color: var(--stat-c, var(--text));
}}
/* Выбранная плашка: Streamlit красит primary-кнопку СВОИМ градиентом через
   сокращение background – background-color его не перебивает, и плашка
   становилась фиолетовой, а подпись на ней пропадала. */
[class*="st-key-tile-stat-"] .stButton > button[kind="primary"] {{
  background: var(--stat-bg, var(--bg-3)) !important;
  background-image: none !important;
  box-shadow: inset 0 0 0 2px var(--stat-c, var(--acc)) !important;
  border-color: var(--stat-c, var(--acc)) !important;
}}
[class*="st-key-tile-stat-"] .stButton > button[kind="primary"] p {{ opacity: 1; }}
/* Плашка «Время» кликать нечего – но выглядеть должна как остальные. */
[class*="st-key-tile-stat-"] .stButton > button:disabled {{
  opacity: 1 !important; cursor: default; transform: none !important;
}}
[class*="st-key-tile-stat-"] .stButton > button:disabled:hover {{
  border-color: var(--stat-bd, var(--border)); transform: none !important;
}}

/* Отчёт – один блок: карточка, дата справа в шапке. */
.report-head {{ display: flex; align-items: baseline; gap: 12px; margin-bottom: 10px; }}
.report-head-title {{ font-size: 14px; font-weight: 700; color: var(--text); }}
.report-head-date {{ margin-left: auto; font-size: 12px; color: var(--dim); font-family: var(--mono); }}
.report-row-country {{ font-size: 11px; color: var(--dim); flex: 0 0 96px; }}

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

/* ─── Сверка с Яндексом ────────────────────────────────────────── */
.audit-row {{
  display: block; padding: 9px 13px; border-radius: var(--r-sm);
  background: var(--bg-2); border: 1px solid var(--border);
  border-left: 3px solid var(--border); margin-bottom: 6px;
}}
.audit-row.ok {{ border-left-color: var(--grn); }}
.audit-row.warn {{ border-left-color: var(--yel); }}
.audit-row.err {{ border-left-color: var(--red); }}
.audit-row.skip {{ border-left-color: var(--acc); }}
.audit-head {{ display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }}
.audit-city {{ font-size: 13px; font-weight: 700; color: var(--text); }}
.audit-country {{ font-size: 11px; color: var(--dim); }}
.audit-note {{ margin-left: auto; font-size: 12px; color: var(--yel); }}
.audit-fields {{ margin-top: 5px; font-size: 12.5px; color: var(--text-2); line-height: 1.65; }}
.audit-key {{ color: var(--dim); }}
.audit-same {{ color: var(--grn); }}
.audit-diff {{ color: var(--red); }}
.audit-none {{ color: var(--dim); }}
.audit-link a {{ color: var(--acc); text-decoration: none; font-family: var(--mono); font-size: 11.5px; }}
.audit-link a:hover {{ text-decoration: underline; }}

/* ─── Пустое состояние ─────────────────────────────────────────── */
.empty {{ text-align: center; padding: 40px 20px; color: var(--muted); }}
.empty-icon {{ font-size: 42px; opacity: .35; margin-bottom: 12px; }}
.empty-title {{ font-size: 15px; font-weight: 700; color: var(--text); margin-bottom: 6px; }}
.empty-desc {{ font-size: 13px; max-width: 420px; margin: 0 auto; }}

/* ─── Плитки типа поста и карточки стран (1:1 с _ui.js) ────────── */
/* Плитка – это САМА кнопка Streamlit, а не картинка с невидимой кнопкой поверх.
   Наложение не работало: в центре карточки оказывался её собственный div, и клик
   мышью просто не доходил до кнопки – с виду «тормоза», на деле клик в пустоту.

   Флаг рисуем фоном самой кнопки, а ::before / ::after оставляем под подписи
   («76 гор.», «✓ все 76», «изменить ▸») – ровно как в .country-tile оригинала.
   Значения подставляются переменными --flag / --meta / --mark / --act. */
[class*="st-key-tile-"] .stButton {{ width: 100%; }}
[class*="st-key-tile-"] .stButton > button {{
  width: 100%; display: flex; align-items: center; justify-content: center;
  background-color: var(--bg-2); border: 1.5px solid var(--border);
  border-radius: var(--r-md); color: var(--text) !important;
  transition: all .15s var(--ease);
}}
[class*="st-key-tile-"] .stButton > button:hover {{
  border-color: var(--border-hi); background-color: var(--bg-3); transform: translateY(-1px);
}}
[class*="st-key-tile-"] .stButton > button[kind="primary"] {{
  border-color: var(--acc); background-color: var(--acc-bg);
  color: var(--acc) !important; box-shadow: 0 0 0 2px var(--acc-bg);
}}
[class*="st-key-tile-"] .stButton > button[kind="primary"] p {{ color: var(--acc) !important; }}
[class*="st-key-tile-"] .stButton > button [data-testid="stMarkdownContainer"] {{ min-width: 0; }}
/* Подпись Streamlit кладёт в два своих div/span – без этого текст встаёт по центру. */
[class*="st-key-tile-cc-"] .stButton > button > div,
[class*="st-key-tile-row-"] .stButton > button > div {{
  min-width: 0; max-width: 100%; display: flex; justify-content: flex-start;
}}
[class*="st-key-tile-cc-"] .stButton > button > div > span,
[class*="st-key-tile-row-"] .stButton > button > div > span {{
  min-width: 0; max-width: 100%; display: flex; justify-content: flex-start;
}}

/* Тип поста: иконка над названием (.post-type-card) */
[class*="st-key-tile-pt-"] .stButton > button {{
  flex-direction: column; gap: 8px; padding: 16px 10px; min-height: 92px; text-align: center;
  background-image: none !important;
}}
[class*="st-key-tile-pt-"] .stButton > button::before {{
  content: var(--ico, ""); font-size: 26px; line-height: 1; filter: grayscale(.2);
}}
[class*="st-key-tile-pt-"] .stButton > button[kind="primary"]::before {{ filter: none; }}
[class*="st-key-tile-pt-"] .stButton > button p {{
  font-size: 12.5px !important; font-weight: 600 !important; line-height: 1.25 !important;
}}

/* Страна карточкой (.country-tile): флаг слева, под названием – «N гор.» */
[class*="st-key-tile-cc-"] .stButton > button {{
  flex-direction: column; align-items: flex-start; justify-content: center; gap: 1px;
  padding: 11px 13px 11px 44px; min-height: 56px; text-align: left;
  background-image: var(--flag) !important; background-repeat: no-repeat;
  background-position: 13px center; background-size: 24px 16px;
}}
[class*="st-key-tile-cc-"] .stButton > button p {{
  font-size: 13px !important; font-weight: 700 !important; line-height: 1.3 !important;
  letter-spacing: -.01em; text-align: left;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
[class*="st-key-tile-cc-"] .stButton > button::after {{
  content: var(--meta, ""); font-size: 11px; font-weight: 500;
  color: var(--meta-c, var(--muted));
}}
/* Страна уже в очереди (.country-tile.in-queue оригинала): зелёная рамка и
   галочка в углу. Управляется переменными --qshow/--qbord/--qbg из tile_css. */
[class*="st-key-tile-cc-"] .stButton {{ position: relative; }}
[class*="st-key-tile-cc-"] .stButton::after {{
  content: "✓"; display: var(--qshow, none); position: absolute; top: 6px; right: 8px;
  width: 18px; height: 18px; border-radius: 50%; background: var(--grn); color: #fff;
  align-items: center; justify-content: center; font-size: 11px; font-weight: 700;
  pointer-events: none; z-index: 1;
}}
[class*="st-key-tile-cc-"] .stButton > button {{
  border-color: var(--qbord, var(--border));
  background-color: var(--qbg, var(--bg-2));
}}
[class*="st-key-tile-cc-"] .stButton > button[kind="primary"]::after {{
  color: var(--acc); font-weight: 700;
}}

/* Страна строкой (.country-row): флаг · название · отметка · действие.
   Порядок во flex задаём order, потому что ::before по умолчанию идёт первым. */
[class*="st-key-tile-row-"] .stButton > button {{
  justify-content: flex-start; gap: 10px; padding: 9px 14px 9px 44px; min-height: 40px;
  border-radius: var(--r-sm); border-width: 1px;
  background-image: var(--flag) !important; background-repeat: no-repeat;
  background-position: 14px center; background-size: 24px 16px;
}}
[class*="st-key-tile-row-"] .stButton > button > div {{ order: 1; flex: 1 1 auto; }}
[class*="st-key-tile-row-"] .stButton > button p {{
  font-size: 13.5px !important; font-weight: 700 !important; text-align: left;
}}
[class*="st-key-tile-row-"] .stButton > button::before {{
  content: var(--mark, ""); order: 2; font-size: 12px; font-weight: 700;
  color: var(--mark-c, var(--muted)); white-space: nowrap;
}}
[class*="st-key-tile-row-"] .stButton > button::after {{
  content: var(--act, ""); order: 3; font-size: 11.5px; font-weight: 500;
  color: var(--muted); white-space: nowrap;
}}
[class*="st-key-tile-row-"] .stButton > button[kind="primary"] {{
  border-color: var(--border-hi); background-color: var(--bg-3); box-shadow: none;
}}
[class*="st-key-tile-row-"] .stButton > button[kind="primary"] p {{ color: var(--text) !important; }}

/* Шестерёнка правки окончаний – ровно по строке заголовка карточки */
/* Кнопка с подсказкой обёрнута Streamlit в хост подсказки, поэтому «> button»
   до неё не достаёт. И !important обязателен: у Streamlit своя тема, всегда
   тёмная, иначе на светлой шестерёнка выходит чёрным квадратом. */
.st-key-endings-gear button {{
  padding: 2px 0 !important; min-height: 30px !important; height: 30px !important;
  font-size: 15px; line-height: 1; box-shadow: none !important;
  background: var(--bg-2) !important; border: 1px solid var(--border) !important;
  color: var(--muted) !important;
}}
.st-key-endings-gear button:hover {{
  background: var(--bg-3) !important; border-color: var(--acc) !important;
  color: var(--acc) !important;
}}

/* Кнопка «Посмотреть отчёт» под сообщением об окончании прогона */
.st-key-go-report .stButton > button {{
  background: var(--grn-bg) !important; border: 1px solid transparent !important;
  color: var(--grn) !important; font-weight: 700 !important; box-shadow: none !important;
}}
.st-key-go-report .stButton > button:hover {{ border-color: var(--grn) !important; }}
.st-key-go-report .stButton > button p {{ color: var(--grn) !important; }}

/* Плашка проекта в шапке – кнопка, открывающая «Сменить проект» */
.st-key-projbadge {{ display: flex; align-items: flex-end; }}
.st-key-projbadge [data-testid="stElementContainer"], .st-key-projbadge .stPopover {{
  width: fit-content !important; margin-left: auto !important;
}}
.st-key-projbadge button {{
  display: inline-flex !important; align-items: center; gap: 9px; width: auto !important;
  padding: 7px 14px !important; border-radius: 999px !important;
  background: var(--bg-2) !important; border: 1px solid var(--border) !important;
  color: var(--text) !important; min-height: 36px !important; box-shadow: none !important;
}}
.st-key-projbadge button:hover {{ border-color: var(--border-hi) !important; }}
.st-key-projbadge button::before {{
  content: ""; width: 10px; height: 10px; border-radius: 50%;
  background: var(--dot, var(--acc)); flex: 0 0 10px;
}}
.st-key-projbadge button p {{
  font-size: 13px !important; font-weight: 600 !important; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; color: var(--text) !important;
}}
.st-key-projbadge [data-testid="stIconMaterial"] {{ display: none !important; }}

/* Список подстановок под полем шаблона – в две колонки, а не строкой в кашу */
.ph-list {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 2px 18px; margin: 6px 0 2px;
}}
.ph-list > div {{ display: flex; align-items: baseline; gap: 8px; font-size: 12px; }}
.ph-list code {{
  font-family: var(--mono); font-size: 11.5px; color: var(--acc);
  background: var(--acc-bg); padding: 1px 6px; border-radius: var(--r-xs); white-space: nowrap;
}}
.ph-list span {{ color: var(--muted); }}

/* Окно правки окончаний рисуется ВНЕ .stApp, и цвета Streamlit берёт из своей
   темы – а она у него всегда тёмная. На светлой выходил тёмный блок с тёмным
   текстом, поэтому здесь всё задано явно. */
[data-testid="stDialog"] > div {{
  background: var(--bg-1) !important; border: 1px solid var(--border) !important;
  border-radius: var(--r-md) !important; box-shadow: var(--shadow-lg);
}}
[data-testid="stDialog"] h2, [data-testid="stDialog"] h3 {{
  font-size: 16px !important; color: var(--text) !important;
}}
[data-testid="stDialog"] p, [data-testid="stDialog"] label,
[data-testid="stDialog"] li, [data-testid="stDialog"] small {{ color: var(--text-2) !important; }}
[data-testid="stDialog"] [data-testid="stCaptionContainer"] p,
[data-testid="stDialog"] .hint, [data-testid="stDialog"] .ph-list span {{
  color: var(--muted) !important;
}}
[data-testid="stDialog"] [data-testid="stWidgetLabel"] p {{ color: var(--muted) !important; }}
[data-testid="stDialog"] .card-title {{ color: var(--text) !important; }}
[data-testid="stDialog"] > div > button {{
  background: transparent !important; border: none !important; color: var(--muted) !important;
  box-shadow: none !important;
}}
[data-testid="stDialog"] > div > button:hover {{ color: var(--text) !important; }}

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
/* Кнопка внутри зоны загрузки: у Streamlit она тёмная и подписана «Upload» –
   на светлой теме это чёрный прямоугольник с нечитаемым текстом. */
[data-testid="stFileUploaderDropzone"] button {{
  background: var(--bg-3) !important; color: var(--text) !important;
  border: 1px solid var(--border) !important; padding: 7px 14px !important;
  min-height: 0 !important; height: auto !important; box-shadow: none !important;
}}
[data-testid="stFileUploaderDropzone"] button p {{ font-size: 0 !important; }}
[data-testid="stFileUploaderDropzone"] button p::after {{
  content: "Выбрать файлы"; font-size: 12px; font-weight: 600;
}}
[data-testid="stFileUploaderDropzone"] button:hover {{
  border-color: var(--acc) !important; color: var(--acc) !important;
}}
[data-testid="stFileUploaderDropzone"] button:hover::after {{ color: var(--acc); }}

/* Строка страны в «Актуализации»: заголовок экспандера – одна строка,
   как ряд в оригинале, а не толстый блок. */
.st-key-act-rows [data-testid="stExpander"] {{ margin-bottom: 6px; }}
.st-key-act-rows [data-testid="stExpander"] summary {{
  background: var(--bg-2); padding: 9px 14px !important; font-size: 13px;
}}
.st-key-act-rows [data-testid="stExpander"] summary p {{ font-weight: 700; }}
.st-key-act-rows .stCheckbox {{ margin-bottom: 2px; }}
.st-key-act-rows .stCheckbox p {{ font-size: 12px !important; }}

.country-flag-svg {{ display: inline-flex; align-items: center; vertical-align: -2px;
  border-radius: 2px; overflow: hidden; box-shadow: 0 0 0 1px rgba(0,0,0,.15); }}

/* Чекбоксы городов – в рамочках и плотно, как .city-check в оригинале:
   между рядами и колонками почти нет воздуха. */
.st-key-city-grid, .st-key-city-grid > div,
.st-key-city-grid [data-testid="stVerticalBlock"],
.st-key-city-grid [data-testid="stHorizontalBlock"],
.st-key-city-grid [data-testid="stColumn"],
.st-key-city-grid [data-testid="stColumn"] > div,
.st-key-city-grid [data-testid="stElementContainer"] {{
  gap: 5px !important; row-gap: 5px !important; column-gap: 5px !important;
}}
.st-key-city-grid [data-testid="stElementContainer"] {{ margin: 0 !important; }}
.st-key-city-grid .stCheckbox, .st-key-city-grid [data-testid="stElementContainer"] {{
  margin-bottom: 0; width: 100% !important;
}}
.st-key-city-grid label[data-baseweb="checkbox"] {{
  width: 100% !important; box-sizing: border-box; display: flex !important;
}}
.st-key-city-grid [data-testid="stColumn"] {{ min-width: 0; }}
.st-key-city-grid .stCheckbox > label {{
  width: 100%; padding: 5px 9px; background: var(--bg-2);
  border: 1px solid var(--border); border-radius: var(--r-xs);
  transition: all .1s var(--ease); min-height: 30px; align-items: center;
  overflow: hidden;                      /* длинные названия не должны вылезать за рамку */
}}
.st-key-city-grid .stCheckbox > label > div:last-child {{ min-width: 0; overflow: hidden; }}
.st-key-city-grid .stCheckbox > label:hover {{ border-color: var(--border-hi); background: var(--bg-3); }}
.st-key-city-grid .stCheckbox > label:has(input:checked) {{
  background: var(--acc-bg); border-color: var(--acc);
}}
.st-key-city-grid .stCheckbox p {{ font-size: 11.5px !important; font-weight: 600 !important;
  line-height: 1.2 !important; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.st-key-city-grid [data-baseweb="checkbox"] > span:first-child {{ transform: scale(.85); }}

/* Поле выбора Streamlit рисует по своей теме – на светлой оно выходило чёрным.
   Разметка у него react-aria, поэтому целимся в группу и её поле ввода. */
[data-testid="stSelectbox"] [role="group"], [data-baseweb="select"] > div {{
  background: var(--bg-2) !important; border-color: var(--border) !important;
  color: var(--text) !important;
}}
[data-testid="stSelectbox"] input {{ background: transparent !important; color: var(--text) !important; }}
[data-testid="stSelectbox"] svg, [data-baseweb="select"] svg {{
  fill: var(--muted) !important; color: var(--muted) !important;
}}
[role="listbox"], [data-baseweb="menu"], .react-aria-Popover {{
  background: var(--bg-1) !important; border: 1px solid var(--border) !important;
}}
[role="option"], [data-baseweb="menu"] li {{ color: var(--text) !important; }}
[role="option"]:hover, [data-baseweb="menu"] li:hover {{ background: var(--bg-3) !important; }}

/* Сам квадратик галочки. Красим оба состояния явно: у Streamlit невыбранный
   квадрат берёт цвет из своей темы, и на светлой он получался чёрным. */
.stCheckbox label > div:first-of-type {{
  background: {c['cbBg']} !important;
  border: 1.5px solid {c['cbBorder']} !important;
  border-radius: 4px !important;
  width: 16px !important; height: 16px !important; flex: 0 0 16px !important;
  display: flex !important; align-items: center; justify-content: center;
  transition: all .12s var(--ease);
}}
.stCheckbox label:hover > div:first-of-type {{ border-color: var(--acc) !important; }}
.stCheckbox label > div:first-of-type svg {{
  width: 10px; height: 8px; stroke: #fff; stroke-width: 2;
  fill: none; stroke-linecap: round; stroke-linejoin: round;
}}
/* Выбрано – фиолетово-синий градиент акцента и белая птичка */
.stCheckbox label[data-selected="true"] > div:first-of-type,
.stCheckbox label:has(input:checked) > div:first-of-type {{
  background: var(--gradient) !important; border-color: transparent !important;
}}

/* Рамка, через которую CSS попадает в <head> (см. _persist_css). Своего вида
   у неё нет и быть не должно: пустая рамка нулевой высоты всё равно тянула бы
   за собой отступ между элементами. */
[data-testid="stIFrame"], [data-testid="stCustomComponentV1"] {{
  display: none !important; height: 0 !important;
}}

/* Загрузчик файлов ровно по высоте соседнего поля со ссылками – но ТОЛЬКО
   пока он пуст. Высота была жёсткой (118px), и список прикреплённых файлов
   в неё не влезал: на четырёх файлах последняя плитка и кнопка «+» вылезали
   на 14 и 54 пикселя ниже рамки карточки. Теперь 118px – это минимум, а
   дальше зона растёт под содержимое. */
.st-key-img-row [data-testid="stFileUploaderDropzone"],
.st-key-goods-row [data-testid="stFileUploaderDropzone"] {{
  min-height: 118px; height: auto; padding: 8px 12px;
  align-items: center; justify-content: center; flex-direction: column; gap: 6px;
}}
/* Плитка файла не должна вылезать вбок при длинном имени: имя ужимается
   с многоточием, а крестик остаётся на месте. Без min-width:0 длинное имя
   распирает плитку изнутри – flex-элемент по умолчанию не ужимается меньше
   своего содержимого. */
[data-testid="stFileChip"] {{ min-width: 0 !important; max-width: 100%; }}
[data-testid="stFileChip"] > div {{ min-width: 0 !important; }}
[data-testid="stFileChipName"] {{
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
/* Подпись поля слева занимает место – у загрузчика подписи нет, компенсируем,
   чтобы обе рамки начинались и заканчивались на одной высоте. */
.st-key-img-row [data-testid="stFileUploader"],
.st-key-goods-row [data-testid="stFileUploader"] {{ margin-top: 30px; }}

/* Поле со ссылками тянется вслед за загрузчиком: тот растёт под список файлов,
   и без этого слева оставалась пустая дыра высотой в этот список. Колонки
   Streamlit и так одной высоты (align-items: stretch), поэтому тянуть надо всю
   цепочку до самой textarea – каждое звено по отдельности высоту не наследует. */
.st-key-img-row [data-testid="stColumn"] > [data-testid="stVerticalBlock"],
.st-key-goods-row [data-testid="stColumn"] > [data-testid="stVerticalBlock"],
.st-key-img-row [data-testid="stColumn"] [data-testid="stElementContainer"]:has(.stTextArea),
.st-key-goods-row [data-testid="stColumn"] [data-testid="stElementContainer"]:has(.stTextArea) {{
  height: 100%;
}}
.st-key-img-row .stTextArea,
.st-key-goods-row .stTextArea {{ height: 100%; display: flex; flex-direction: column; }}
.st-key-img-row [data-testid="stTextAreaRootElement"],
.st-key-goods-row [data-testid="stTextAreaRootElement"] {{ flex: 1 1 auto; }}
.st-key-img-row .stTextArea textarea,
.st-key-goods-row .stTextArea textarea {{ height: 100% !important; min-height: 118px; }}

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


# ─── Флаги ──────────────────────────────────────────────────────────
# Именно inline-SVG, а не эмодзи: на Windows эмодзи-флаги не отрисовываются –
# вместо флага видно буквы «RU», «KZ». Так же сделано в оригинальном _ui.js.
_FLAG_SVG = {
    "Россия": '<rect width="9" height="2" fill="#fff"/><rect y="2" width="9" height="2" fill="#0039A6"/>'
              '<rect y="4" width="9" height="2" fill="#D52B1E"/>',
    "Казахстан": '<rect width="9" height="6" fill="#00AFCA"/><circle cx="4.5" cy="3" r="1.2" fill="#FEC50C"/>',
    "Беларусь": '<rect width="9" height="4" fill="#D22730"/><rect y="4" width="9" height="2" fill="#007C30"/>'
                '<rect width="0.9" height="6" fill="#fff"/>',
    "Кыргызстан": '<rect width="9" height="6" fill="#E8112D"/><circle cx="4.5" cy="3" r="1.4" fill="#FFCD00"/>',
    "Киргизия": '<rect width="9" height="6" fill="#E8112D"/><circle cx="4.5" cy="3" r="1.4" fill="#FFCD00"/>',
    "Узбекистан": '<rect width="9" height="2" fill="#1EB53A"/><rect y="2" width="9" height="2" fill="#fff"/>'
                  '<rect y="4" width="9" height="2" fill="#0099B5"/>',
    "Азербайджан": '<rect width="9" height="2" fill="#00B5E2"/><rect y="2" width="9" height="2" fill="#EF3340"/>'
                   '<rect y="4" width="9" height="2" fill="#509E2F"/>',
    "Армения": '<rect width="9" height="2" fill="#D90012"/><rect y="2" width="9" height="2" fill="#0033A0"/>'
               '<rect y="4" width="9" height="2" fill="#F2A800"/>',
    "Украина": '<rect width="9" height="3" fill="#005BBB"/><rect y="3" width="9" height="3" fill="#FFD500"/>',
    "Грузия": '<rect width="9" height="6" fill="#fff"/><rect x="4" width="1" height="6" fill="#FF0000"/>'
              '<rect y="2.5" width="9" height="1" fill="#FF0000"/>',
    "Молдова": '<rect width="3" height="6" fill="#003DA5"/><rect x="3" width="3" height="6" fill="#FFD200"/>'
               '<rect x="6" width="3" height="6" fill="#CE1126"/>',
    "Турция": '<rect width="9" height="6" fill="#E30A17"/><circle cx="3.5" cy="3" r="1.3" fill="#fff"/>'
              '<circle cx="3.8" cy="3" r="1.05" fill="#E30A17"/>',
    "Таджикистан": '<rect width="9" height="2" fill="#CC0000"/><rect y="2" width="9" height="2" fill="#fff"/>'
                   '<rect y="4" width="9" height="2" fill="#006600"/>',
}


def flag_svg(name: str, height: int = 14) -> str:
    body = _FLAG_SVG.get(_norm_country(name), '<rect width="9" height="6" fill="#888"/>')
    w = round(height * 1.5)
    return (f'<span class="country-flag-svg"><svg viewBox="0 0 9 6" width="{w}" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg">{body}</svg></span>')


def flag_data_uri(name: str) -> str:
    """Тот же флаг, но как data-URI – чтобы поставить фоном настоящей кнопке."""
    import urllib.parse as _u
    body = _FLAG_SVG.get(_norm_country(name), '<rect width="9" height="6" fill="#888"/>')
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 9 6">{body}</svg>'
    return "url(\"data:image/svg+xml," + _u.quote(svg, safe="") + "\")"


def _norm_country(name: str) -> str:
    import re as _re
    return _re.sub(r"\s*\([^)]*\)\s*", " ", name or "").strip()


def css_text(value: str) -> str:
    """Строка для CSS-свойства content: в кавычках и с экранированием."""
    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def tile_css(rules: list[tuple[str, dict[str, str]]]) -> str:
    """
    Один <style> на весь список плиток: подписи и флаги идут переменными.

    ВАЖНО: ключ контейнера должен быть чисто латинским. Streamlit заменяет в
    классе `st-key-…` любой символ вне [A-Za-z0-9_-] на дефис, поэтому ключ с
    кириллицей («…-россия») превращается в «…-------» и селектор не совпадает.
    Отсюда – нумерация вместо названий.
    """
    out = []
    for key, vars_ in rules:
        body = ";".join(f"{k}:{v}" for k, v in vars_.items() if v)
        if body:
            # Переменные ставим на контейнер: CSS-переменные наследуются вниз,
            # так их видит и сама кнопка, и бейдж «✓ в очереди» на контейнере.
            out.append(f'.st-key-{key} .stButton{{{body}}}')
    return "<style>" + "".join(out) + "</style>" if out else ""


def esc(s: object) -> str:
    return _html.escape(str(s if s is not None else ""))


def topbar(project: dict | None, pills: list[tuple[str, str]] | None = None) -> str:
    """Топбар оригинала: логотип + статус-пилюли + плашка активного проекта."""
    pills_html = "".join(
        f'<span class="pill pill-{kind}">{esc(text)}</span>' for kind, text in (pills or [])
    )
    badge = ""
    if project:
        # Без эмодзи: на Windows иконка проекта рисуется квадратиком.
        badge = (
            f'<span class="click-projbadge">'
            f'<span class="click-projbadge-dot" style="background:{esc(project["color"])}"></span>'
            f'{esc(project["name"])} –<span class="click-projbadge-sub">'
            f'{esc(project["fullName"])}</span></span>'
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
    return '<div class="log-box"><div class="log-lines">' + "\n".join(lines) + "</div></div>"


_STAT_META = {
    "ok": ("ok", "Успешно"),
    "noImage": ("noimg", "Без картинки"),
    "unknown": ("warn", "Проверьте"),
    "failed": ("err", "Ошибок"),
    "skipped": ("skip", "Пропущено"),
    "actualized": ("ok", "Актуализировано"),
    "notNeeded": ("skip", "Не требовалось"),
}


_ROW_STYLE = {
    "ok": ("ok", "✅"),
    "no-image": ("noimg", "🟡"),
    "unknown": ("warn", "⚠️"),
    "failed": ("err", "🔴"),
    "skipped-duplicate": ("skip", "⏭"),
    "no-session": ("err", "🔒"),
    "actualized": ("ok", "✅"),
    "not-needed": ("skip", "⊝"),
    # Не сбой Click – статус в КП разошёлся с тем, что на площадке. Жёлтый,
    # не красный: это сигнал «поправьте таблицу», а не «приложение сломалось».
    "status-mismatch": ("warn", "🚦"),
}


def report_row(item: dict, with_country: bool = False) -> str:
    cls, ico = _ROW_STYLE.get(item.get("status", ""), ("err", "🔴"))
    if item.get("status") == "ok" and item.get("retried"):
        ico = "⚡"
    reason = item.get("reason") or ""
    if item.get("imageError"):
        reason += f' · фото: {item["imageError"]}'
    if item.get("reviews"):
        reason += f' · 💬 {item["reviews"]}'
    pp = item.get("productPhotos")
    if pp:
        reason += f' · 📸 товары: {pp.get("uploaded", 0)}/{pp.get("requested", 0)}'
    gp = item.get("gisPhotos")
    if gp:
        # Причина важнее счёта: «нет карточки в 2ГИС» объясняет ноль, а голый
        # «0 из 3» выглядит поломкой.
        reason += (f' · 📸 2ГИС: {gp["reason"]}' if gp.get("reason") and not gp.get("uploaded")
                   else f' · 📸 2ГИС: {gp.get("uploaded", 0)}/{gp.get("requested", 0)}')
    dur = f'{item.get("durationMs", 0) / 1000:.1f} сек' if item.get("durationMs") else ""
    country = ""
    if with_country:
        name = item.get("country") or item.get("package") or ""
        country = f'<span class="report-row-country">{esc(name)}</span>'
    return (
        f'<div class="report-row {cls}"><span class="report-row-ico">{ico}</span>'
        f'{country}'
        f'<span class="report-row-city">{esc(item.get("cityName", "–"))}</span>'
        f'<span class="report-row-reason">{esc(reason)}</span>'
        f'<span class="report-row-dur">{esc(dur)}</span></div>'
    )


# Отзывы в отчёте прогона рисуются теми же строками, что и города, – чтобы
# «общий отчёт» читался как одно целое, а не как две разные таблицы.
_REVIEW_ROW_STYLE = {
    "answered": ("ok", "✅"),
    "already-answered": ("skip", "⊝"),
    "skipped": ("skip", "⏭"),
    "failed": ("err", "🔴"),
    "drafted": ("warn", "✍"),
    "needs-human": ("warn", "⚠️"),
    "no-draft": ("noimg", "🟡"),
}


def stat_row(cells: list[tuple[str, str, str]]) -> str:
    """
    Ряд плашек «подпись – число» тем же видом, что в отчёте.

    Нужен там, где плашки НЕ кликаются: в отчёте они работают фильтрами и
    рисуются кнопками Streamlit, а здесь достаточно показать цифры. Заказчик
    просила отделять сводку карточками-блоками, «так же как в обычном отчёте».

    cells: [(подпись, значение, цвет)], цвет – ok/warn/err/skip/noimg/dur.
    """
    if not cells:
        return ""
    boxes = "".join(
        f'<div class="report-stat {esc(colour)}">'
        f'<div class="report-stat-label">{esc(label)}</div>'
        f'<div class="report-stat-value">{esc(str(value))}</div></div>'
        for label, value, colour in cells
    )
    return f'<div class="report-summary">{boxes}</div>'


def review_report_row(item: dict) -> str:
    cls, ico = _REVIEW_ROW_STYLE.get(item.get("status", ""), ("err", "🔴"))
    stars = "★" * int(item.get("rating") or 0)
    tail = item.get("note") or (item.get("text") or "")
    return (
        f'<div class="report-row {cls}"><span class="report-row-ico">{ico}</span>'
        f'<span class="report-row-country">{esc(item.get("city") or "–")}</span>'
        f'<span class="report-row-city">{esc(item.get("author") or "–")}</span>'
        f'<span class="report-row-reason">{esc(tail)}</span>'
        f'<span class="report-row-dur">{esc(stars)}</span></div>'
    )


_AUDIT_STYLE = {"найдена": ("ok", "✅"), "несколько": ("warn", "❗"), "нет": ("err", "❌")}
_VERDICT_CLS = {"совпадает": "audit-same", "совпадает частично": "audit-same",
                "расходится": "audit-diff", "нет в Яндексе": "audit-diff",
                "нет в КП": "audit-none", "–": "audit-none"}


def audit_row(item: dict) -> str:
    """
    Строка сверки: город, что нашлось в Яндексе, где расходится с КП.

    Показываем ОБА значения – из КП и из Яндекса: «расходится» без цифр
    заставляет лезть в таблицу, а так сразу видно, кто из них прав.
    """
    cmp = item.get("cmp") or {}
    cls, ico = _AUDIT_STYLE.get(cmp.get("status", ""), ("skip", "•"))
    if cmp.get("status") == "найдена" and cmp.get("problems"):
        cls = "warn"

    def line(label: str, kp: str, ya: str, verdict: str) -> str:
        if not kp and not ya:
            return ""
        vcls = _VERDICT_CLS.get(verdict, "audit-none")
        return (f'<div><span class="audit-key">{esc(label)}:</span> '
                f'КП <b>{esc(kp or "–")}</b> · Яндекс <b>{esc(ya or "–")}</b> '
                f'<span class="{vcls}">{esc(verdict)}</span></div>')

    cos = item.get("companies") or []
    fields = "".join([
        line("Сайт", item.get("site", ""), " / ".join(cmp.get("yaSites") or []), cmp.get("site", "–")),
        line("Телефон", " / ".join(item.get("phones") or []),
             " / ".join(cmp.get("yaPhones") or []), cmp.get("phone", "–")),
        line("Почта", item.get("email", ""), " / ".join(cmp.get("yaEmails") or []), cmp.get("email", "–")),
    ])
    if cos:
        links = " · ".join(
            f'<a href="https://yandex.ru/sprav/{esc(c.get("id"))}/p/edit/" target="_blank">'
            f'{esc(c.get("name") or c.get("id"))}</a>' for c in cos)
        addr = "; ".join(c.get("address", "") for c in cos if c.get("address"))
        fields += (f'<div class="audit-link">{links}</div>'
                   f'<div><span class="audit-key">Адрес в Яндексе:</span> {esc(addr)}</div>')

    notes = "; ".join(cmp.get("notes") or [])
    return (
        f'<div class="audit-row {cls}">'
        f'<div class="audit-head"><span>{ico}</span>'
        f'<span class="audit-city">{esc(item.get("city", "–"))}</span>'
        f'<span class="audit-country">{esc(item.get("country", ""))}</span>'
        f'<span class="audit-note">{esc(notes)}</span></div>'
        f'<div class="audit-fields">{fields}</div></div>'
    )


def audit_extra_row(co: dict) -> str:
    """Организация Яндекса, которой не нашлось города в КП."""
    bits = [co.get("address", ""), " / ".join(co.get("sites") or []),
            " / ".join(co.get("phones") or []), " / ".join(co.get("emails") or [])]
    return (
        f'<div class="audit-row skip">'
        f'<div class="audit-head"><span>➕</span>'
        f'<span class="audit-city">{esc(co.get("name") or co.get("id"))}</span>'
        f'<span class="audit-country">{"сеть" if co.get("type") == "chain" else ""}</span></div>'
        f'<div class="audit-fields">{esc(" · ".join(b for b in bits if b))}'
        f'<div class="audit-link"><a href="https://yandex.ru/sprav/{esc(co.get("id"))}/p/edit/" '
        f'target="_blank">открыть карточку</a></div></div></div>'
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


# Карточки типа поста и стран больше не рисуются отдельным HTML: плитка – это
# сама кнопка Streamlit (см. tile_css). Прежние post_type_card / country_card /
# country_row удалены, чтобы не осталось двух источников правды для вёрстки.


def preview_box(text: str) -> str:
    """
    Переносы строк – числовой ссылкой &#10;. В разметке тогда нет ни одного
    настоящего перевода строки, и markdown Streamlit не может слепить из текста
    абзацы со своими отступами (из-за них в предпросмотре зияли дыры) и списки.
    Браузер разворачивает ссылку обратно, а white-space: pre-wrap её рисует.
    """
    body = esc(text).replace("\r\n", "\n").replace("\n", "&#10;")
    return f'<div class="preview-box">{body}</div>'


def crosspost_css() -> str:
    """
    Стили раздела «Кросспостинг». Строка плана – та же `report-row`, что в
    отчёте: разделы должны выглядеть одним приложением, а не набором разных.
    Своё здесь только колонки даты/типа/площадок.
    """
    return (
        "<style>"
        ".cp-day{font-size:12.5px;font-weight:700;color:var(--text);"
        "flex:0 0 108px;font-family:var(--mono)}"
        ".cp-day em{font-style:normal;color:var(--dim);font-weight:500}"
        ".cp-type{font-size:11.5px;color:var(--dim);flex:0 0 120px}"
        ".cp-photos{font-size:11.5px;color:var(--dim);flex:0 0 46px;text-align:right}"
        ".cp-nets{display:flex;gap:5px;flex-wrap:wrap;flex:0 0 auto;justify-content:flex-end}"
        ".cp-net{font-size:11px;padding:2px 7px;border-radius:999px;white-space:nowrap;"
        "background:var(--bg-3,var(--bg-2));border:1px solid var(--border);color:var(--text-2)}"
        "a.cp-net{text-decoration:none}"
        ".cp-net-ok{border-color:var(--grn);color:var(--grn)}"
        ".cp-net-skip{border-color:var(--acc);color:var(--acc)}"
        ".cp-net-warn{border-color:var(--yel);color:var(--yel)}"
        ".cp-net-err{border-color:var(--red);color:var(--red)}"
        ".cp-net-off{opacity:.45}"
        "@media(max-width:900px){.cp-type,.cp-photos{display:none}"
        ".cp-nets{flex:1 0 100%;justify-content:flex-start;margin-top:4px}}"
        + _CROSSPOST_TABLE_CSS +
        "</style>"
    )


# ─── Таблица плана кросспостинга ────────────────────────────────────
# Строки, а не сетка недель: на живом реестре (два-три поста в неделю)
# календарь стоял пустым, а текст в узких плитках рвался по слогам. Здесь
# строка — это пост, колонки справа — площадки: статус сравнивается по
# вертикали, а «что сделать» собрано в одну колонку, чтобы взгляд шёл по ней.
_CROSSPOST_TABLE_CSS = (
    ".cp-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:var(--r-sm);"
    "background:var(--bg-1);margin-bottom:24px;box-shadow:none}"
    # Карточки состояния и «требует внимания» – ровные прямоугольники без тени.
    '[data-testid="stVerticalBlockBorderWrapper"]{box-shadow:none!important;'
    "background:var(--bg-1);border-color:var(--border)!important}"
    "table.cp-plan{border-collapse:collapse;width:100%;min-width:720px;font-size:12.5px;table-layout:fixed}"
    "table.cp-plan th{position:sticky;top:0;z-index:2;background:var(--bg-3);text-align:left;"
    "font-size:12.5px;font-weight:700;color:var(--text-2);padding:10px 12px;"
    "border-bottom:1px solid var(--border);white-space:nowrap}"
    "table.cp-plan th.c,table.cp-plan td.c{text-align:center}"
    # Все ячейки прижаты к верху и имеют одинаковый отступ, а первая строка
    # внутри каждой — один и тот же бокс в 20px (дата, плашка типа, текст,
    # значок площадки, «что сделать»). Только так они лежат на одной линии:
    # vertical-align:middle разъезжался, как только строка становилась выше.
    "table.cp-plan td{padding:11px 12px;border-bottom:1px solid var(--border);"
    "vertical-align:top;text-align:left}"
    "table.cp-plan th.c,table.cp-plan td.c{padding-left:4px;padding-right:4px}"
    "table.cp-plan tr:last-child td{border-bottom:none}"
    "table.cp-plan tbody tr:hover td{background:var(--bg-3)}"
    "table.cp-plan tr.cp-group td{background:var(--bg-3);color:var(--muted);padding:7px 12px;"
    "font-size:12px;font-weight:600}"
    "table.cp-plan tr.cp-group:hover td{background:var(--bg-4)}"
    "table.cp-plan tr.cp-warn td{background:var(--yel-bg)}"
    "table.cp-plan tr.cp-warn:hover td{background:var(--yel-bg)}"
    # Левая полоска — состояние строки: она же цвет в легенде.
    ".cp-flag{width:3px;padding:0 !important}"
    ".cp-flag.set,.cp-flag.live{background:var(--grn)}"
    ".cp-flag.wait{background:var(--acc)}"
    ".cp-flag.warn{background:var(--yel)}"
    ".cp-flag.err{background:var(--red)}"
    ".cp-flag.todo,.cp-flag.manual{background:var(--border-2)}"
    # line-height без display:block — это классы на самой <td>, и блочный
    # display развалил бы табличную раскладку.
    ".cp-when{line-height:20px;font-family:var(--mono);font-size:12.5px;"
    "font-weight:700;color:var(--text);white-space:nowrap}"
    ".cp-kind-cell{white-space:nowrap}"
    # Текст поста — ровно две строки: начало и продолжение идут одним потоком,
    # лишнее срезает многоточие. min-height держит высоту строк одинаковой даже
    # там, где текста нет совсем, иначе таблица «прыгает».
    ".cp-post{min-width:0}"
    ".cp-text{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;"
    "overflow:hidden;line-height:20px;min-height:40px;word-break:break-word}"
    ".cp-text b{color:var(--text);font-weight:600;font-size:12.5px}"
    ".cp-text b.warn{color:var(--yel)}"
    ".cp-text span{color:var(--text-2);font-size:12px}"
    ".cp-kind{display:inline-flex;align-items:center;height:20px;font-size:11px;"
    "padding:0 9px;border-radius:20px;white-space:nowrap;background:var(--bg-4);"
    "color:var(--muted)}"
    ".cp-photo{line-height:20px;font-family:var(--mono);font-size:11.5px;"
    "color:var(--muted);white-space:nowrap}"
    # Ячейка площадки — тот же значок, что и в легенде.
    ".cp-mark{width:24px;height:20px;border-radius:5px;display:inline-flex;"
    "align-items:center;justify-content:center;font-size:11px;font-weight:800;"
    "border:none;vertical-align:top;line-height:20px}"
    ".cp-mark.set{background:var(--grn-bg);color:var(--grn)}"
    ".cp-mark.wait{background:var(--acc-bg);color:var(--acc)}"
    ".cp-mark.off{color:var(--dim);background:transparent}"
    ".cp-mark.err{background:var(--red-bg);color:var(--red)}"
    ".cp-mark.live{background:var(--grn);color:#fff}"
    ".cp-todo{font-size:11.5px;color:var(--dim);line-height:20px;display:block}"
    ".cp-todo.fix{color:var(--yel);font-weight:600}"
    ".cp-todo.err{color:var(--red);font-weight:600}"
    ".cp-todo a{color:var(--acc);text-decoration:none;font-weight:600}"
    ".cp-todo a:hover{text-decoration:underline}"
    # Строка состояния наверху раздела.
    ".cp-bar{display:flex;align-items:center;gap:14px;min-width:0}"
    ".cp-bar-dot{width:9px;height:9px;border-radius:50%;background:var(--grn);"
    "box-shadow:0 0 0 4px var(--grn-bg);flex-shrink:0}"
    ".cp-bar-dot.off{background:var(--yel);box-shadow:0 0 0 4px var(--yel-bg)}"
    ".cp-bar-state{font-size:14px;font-weight:700;color:var(--text);white-space:nowrap}"
    ".cp-bar-next{font-size:13.5px;color:var(--text-2);padding-left:14px;"
    "border-left:1px solid var(--border);overflow:hidden;text-overflow:ellipsis;"
    "white-space:nowrap;min-width:0}"
    ".cp-bar-next b{font-family:var(--mono);color:var(--text)}"
    ".cp-bar-meta{display:flex;gap:6px 18px;flex-wrap:wrap;font-size:12.5px;"
    "color:var(--muted);margin-top:10px;padding-top:10px;border-top:1px solid var(--border)}"
    ".cp-bar-meta b{color:var(--text-2);font-family:var(--mono);font-weight:700}"
    # Легенда значков — одной строкой над таблицей.
    ".cp-legend{display:flex;align-items:center;gap:16px;flex-wrap:wrap;font-size:12px;"
    "color:var(--muted);padding:2px 0 8px}"
    ".cp-legend span{display:inline-flex;align-items:center;gap:7px}"
    ".cp-legend .cp-mark{width:20px;height:20px;vertical-align:middle}"
    "@media(max-width:900px){table.cp-plan{table-layout:auto}}"
)


def crosspost_legend() -> str:
    """
    Легенда всегда на виду: без неё галочка и часы читаются одинаково, и
    раздел выглядит шифром. Пять состояний, словами.
    """
    items = (
        ("set", "✓", "отложка стоит в соцсети"),
        ("wait", "⏱", "отправит Click в час выхода"),
        ("off", "·", "ещё не сформировано"),
        ("err", "✕", "ошибка — пост туда не ушёл"),
        ("live", "✓", "уже вышло"),
    )
    body = "".join(
        f'<span><i class="cp-mark {cls}">{esc(mark)}</i> {esc(text)}</span>'
        for cls, mark, text in items
    )
    return f'<div class="cp-legend">{body}</div>'


def crosspost_bar(state_text: str, sub_html: str, meta: list[str],
                  alive: bool = True) -> str:
    """Строка состояния: работает ли автопостинг и что выйдет ближайшим."""
    dot = "cp-bar-dot" if alive else "cp-bar-dot off"
    next_html = f'<span class="cp-bar-next">{sub_html}</span>' if sub_html else ""
    meta_html = ""
    if meta:
        meta_html = ('<div class="cp-bar-meta">'
                     + "".join(f"<span>{m}</span>" for m in meta) + "</div>")
    return (f'<div class="cp-bar"><span class="{dot}"></span>'
            f'<span class="cp-bar-state">{esc(state_text)}</span>{next_html}</div>{meta_html}')


def _crosspost_todo_html(todo: dict, sheet_url: str = "") -> str:
    """Колонка «что сделать»: тихая подпись или ссылка в таблицу — по делу."""
    kind = todo.get("kind", "quiet")
    text = esc(todo.get("text", ""))
    if todo.get("sheet") and sheet_url:
        text = f'<a href="{esc(sheet_url)}" target="_blank">{text} ↗</a>'
    return f'<span class="cp-todo {esc(kind)}">{text}</span>'


def crosspost_table(plan: dict, sheet_url: str = "", group_title: str = "") -> str:
    """
    План строками: когда · тип · пост · фото · площадки · что сделать.

    Колонки площадок приходят из самого реестра (crosspost_plan.columns) —
    жёсткий набор врал бы: у одного бренда есть Дзен, у другого нет ОК.
    """
    cols = plan["columns"]
    widths = ('<colgroup><col style="width:3px"><col style="width:60px">'
              '<col style="width:132px"><col>'
              '<col style="width:44px">'
              + f'<col style="width:{56 if len(cols) < 5 else 48}px">' * len(cols)
              + '<col style="width:148px"></colgroup>')
    head = (
        '<tr><th class="cp-flag"></th><th>Когда</th><th>Тип</th><th>Пост</th>'
        '<th class="c">Фото</th>'
        + "".join(f'<th class="c" title="{esc(c["name"])}">{esc(c.get("short") or c["name"])}</th>'
                  for c in cols)
        + '<th>Что сделать</th></tr>'
    )
    body = []
    if group_title:
        body.append(f'<tr class="cp-group"><td colspan="{5 + len(cols) + 1}">'
                    f'{esc(group_title)}</td></tr>')
    for view in plan["rows"]:
        marks = {n["code"]: n for n in view["nets"]}
        cells = []
        for col in cols:
            net = marks.get(col["code"])
            if not net:
                # Площадка есть у других постов, а у этого её в реестре нет:
                # пустая клетка честнее серого значка «не сформировано».
                cells.append('<td class="c"></td>')
                continue
            cells.append(f'<td class="c"><span class="cp-mark {esc(net["cls"])}" '
                         f'title="{esc(net["name"])} — {esc(net["note"])}">'
                         f'{esc(net["mark"])}</span></td>')
        head_cls = ' class="warn"' if view["state"] == "warn" else ""
        # Начало и продолжение идут одним потоком внутри .cp-text: это один и
        # тот же текст поста, разорванный по предложению. Так он занимает ровно
        # две строки, а не три, и низ строки не пляшет от поста к посту.
        text_html = (f'<div class="cp-text"><b{head_cls}>'
                     f'{esc(view["head"] or "нет текста")}</b>'
                     + (f' <span>{esc(view["tail"])}</span>' if view["tail"] else "")
                     + '</div>')
        body.append(
            f'<tr class="{"cp-warn" if view["state"] == "warn" else ""}">'
            f'<td class="cp-flag {esc(view["state"])}"></td>'
            f'<td class="cp-when">{esc(view["when_day"])}</td>'
            f'<td class="cp-kind-cell"><span class="cp-kind">'
            f'{esc(view["kind"] or "—")}</span></td>'
            f'<td class="cp-post">{text_html}</td>'
            f'<td class="c cp-photo">{view["photos"] or "—"}</td>'
            + "".join(cells)
            + f'<td>{_crosspost_todo_html(view["todo"], sheet_url)}</td></tr>'
        )
    return ('<div class="cp-wrap"><table class="cp-plan">' + widths + '<thead>' + head
            + '</thead><tbody>' + "".join(body) + '</tbody></table></div>')
