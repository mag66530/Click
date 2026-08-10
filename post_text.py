"""
post_text.py – форматирование текста поста под каждую площадку.

Откуда берётся форматирование. Заказчик выделяет жирным прямо в реестре
(Google-таблице) – и это выделение ДОЕЗЖАЕТ: и Sheets API, и выгрузка .xlsx
хранят куски текста с их шрифтом. content_plan вытаскивает эти куски и
собирает текст с разметкой; этот модуль превращает разметку в то, что
понимает каждая площадка.

Разметка внутри Click (одна на всё):
    **жирный кусок**            – из жирного в реестре
    [текст ссылки](https://…)   – ссылка, зашитая в текст

Ссылки в реестре не зашиты («нашем сайте» написано словами) – поэтому есть
автоссылки: правила «фраза → адрес» бренда. Первая встреченная фраза
(«нашем сайте», «наш сайт», «на сайте») превращается в ссылку на сайт
бренда. Работает по разметке, внутрь уже готовых ссылок не лезет.

Что умеет каждая площадка (проверено 2026-08-10):
    Телеграм  – HTML: <b>жирный</b>, <a href>ссылка</a>. Полная поддержка.
    МАКС      – Bot API принимает format=html (POST /messages). Полная.
    ВК (пост) – форматирование заявлено в ЧАТАХ, в постах сообществ – под
                вопросом; проверим на живом редакторе при пилоте. До тех
                пор – простой текст.
    ОК        – простой текст, без вариантов.
    ЯБ        – простой текст.

Простой текст – это не «выбросить»: жирный снимается без следа (голые
звёздочки в посте – брак), а ссылка становится «текст (адрес)»; если текст
ссылки сам по себе адрес – остаётся просто адрес, площадки подсвечивают
URL сами.
"""

from __future__ import annotations

import re

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

BOLD_RX = re.compile(r"\*\*(.+?)\*\*", re.S)
ANCHOR_RX = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
URLISH_RX = re.compile(r"^(https?://)?[\w\-]+(\.[\w\-]+)+(/\S*)?$", re.I)

# Заводские фразы-автоссылки. Порядок важен: более длинная фраза первее,
# иначе «наш сайт» съест начало «нашем сайте». Сравнение без регистра.
AUTOLINK_PHRASES = ("нашем сайте", "нашем интернет-магазине", "наш сайт", "на сайте")


# ─── Сборка разметки из кусков реестра ──────────────────────────────
def runs_to_markup(runs: list[tuple[str, bool]]) -> str:
    """
    Куски текста (текст, жирный?) → строка с разметкой.

    Пробелы и переводы строк по краям жирного куска выносятся НАРУЖУ маркеров:
    «**жирный **» ломает и вид, и разбор, а в реестре хвостовой пробел в
    выделении – сплошь и рядом (выделяют мышкой, зацепив пробел).
    Смежные жирные куски сливаются в один – иначе «**а****б**».
    """
    # Сначала склеиваем соседние куски с одинаковой жирностью.
    merged: list[tuple[str, bool]] = []
    for text, bold in runs:
        if text == "":
            continue
        if merged and merged[-1][1] == bold:
            merged[-1] = (merged[-1][0] + text, bold)
        else:
            merged.append((text, bold))

    out: list[str] = []
    for text, bold in merged:
        if not bold or not text.strip():
            out.append(text)
            continue
        head = text[:len(text) - len(text.lstrip())]
        tail = text[len(text.rstrip()):]
        out.append(f"{head}**{text.strip()}**{tail}")
    return "".join(out)


# ─── Автоссылки ─────────────────────────────────────────────────────
def autolink(markup: str, site_url: str, phrases: tuple[str, ...] = AUTOLINK_PHRASES) -> str:
    """
    Первое вхождение каждой фразы → ссылка на сайт бренда. Только первое:
    пост, где «нашем сайте» трижды и все три – ссылки, выглядит спамом.

    Уже готовые ссылки не трогаем: разбираем разметку на куски «анкор/не
    анкор» и подменяем только вне анкоров. Жирность фразы сохраняется:
    «**нашем сайте**» станет «**[нашем сайте](https://…)**» – жирная ссылка.
    """
    if not site_url or not markup:
        return markup
    url = site_url if site_url.startswith("http") else f"https://{site_url}"

    for phrase in phrases:
        parts = ANCHOR_RX.split(markup)
        # split с 2 группами даёт [текст, анкор-текст, анкор-url, текст, …]
        done = False
        for i in range(0, len(parts), 3):
            low = parts[i].lower()
            pos = low.find(phrase)
            if pos < 0:
                continue
            parts[i] = parts[i][:pos] + f"[{parts[i][pos:pos + len(phrase)]}]({url})" + parts[i][pos + len(phrase):]
            done = True
            break
        if done:
            markup = "".join(
                parts[i] if i % 3 == 0 else (f"[{parts[i]}]({parts[i + 1]})" if i % 3 == 1 else "")
                for i in range(len(parts))
            )
            break   # одна автоссылка на пост: первая же найденная фраза
    return markup


# ─── Рендер под площадку ────────────────────────────────────────────
def _esc_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_html(markup: str) -> str:
    """Телеграм и МАКС: <b> и <a>. Всё остальное экранируется – иначе
    случайный «<» в тексте валит отправку («can't parse entities»)."""
    out: list[str] = []
    pos = 0
    for m in ANCHOR_RX.finditer(markup):
        out.append(_bold_html(markup[pos:m.start()]))
        out.append(f'<a href="{_esc_html(m.group(2))}">{_bold_html(m.group(1))}</a>')
        pos = m.end()
    out.append(_bold_html(markup[pos:]))
    return "".join(out)


def _bold_html(chunk: str) -> str:
    parts: list[str] = []
    pos = 0
    for m in BOLD_RX.finditer(chunk):
        parts.append(_esc_html(chunk[pos:m.start()]))
        parts.append(f"<b>{_esc_html(m.group(1))}</b>")
        pos = m.end()
    parts.append(_esc_html(chunk[pos:]))
    return "".join(parts)


def to_plain(markup: str) -> str:
    """ВК/ОК/ЯБ: жирный снимается, ссылка – «текст (адрес)». Если текст
    ссылки сам адрес (stalmetural.ru) – второй раз адрес не пишем."""
    def anchor(m: re.Match) -> str:
        text, url = m.group(1), m.group(2)
        bare = re.sub(r"^https?://", "", url).rstrip("/")
        if URLISH_RX.match(text.strip()) or text.strip().rstrip("/") in (url, bare):
            return text
        return f"{text} ({url})"
    s = ANCHOR_RX.sub(anchor, markup)
    return BOLD_RX.sub(r"\1", s)


def render(markup: str, mode: str) -> str:
    """
    mode: 'html'  – Телеграм, МАКС;
          'plain' – ВК, ОК, ЯБ (и всё, что не умеет форматирование).
    """
    return to_html(markup) if mode == "html" else to_plain(markup)


def strip_markup(markup: str) -> str:
    """Текст без разметки – для превью и подсчёта длины (лимит подписи ТГ
    считается по видимым знакам, а не по разметке)."""
    return to_plain(markup)
