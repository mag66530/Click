"""
tests_ui_click.py – проверка, что по кнопкам действительно попадает мышь.

Зачем отдельный файл: обычные тесты жмут кнопку через Playwright по её роли,
то есть напрямую по элементу. Так проверка проходит, даже если кнопка лежит
под чужим слоем или её рамка не совпадает с тем, что нарисовано, – а человек
в этот момент кликает и ничего не происходит (или срабатывает соседняя).

Здесь проверяем ровно то, что видит человек:
  • у каждой плитки ненулевой размер (иначе интерфейс «пропал»);
  • в центре плитки под курсором лежит ЕЁ ЖЕ кнопка, а не соседняя и не div;
  • настоящий клик мышью переключает именно ту плитку, по которой кликнули;
  • промах мимо плитки ничего не включает.

Запуск:  python3 tests_ui_click.py [порт]
Поднимает приложение сам, если на порту никого нет.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8611
URL = f"http://localhost:{PORT}/"
PASSWORD = "1501"

OK = FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ❌ {name}{(' – ' + extra) if extra else ''}")


def wait_up(timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(URL, timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.5)
    return False


# ── Разметка тестов ────────────────────────────────────────────────
# Для каждой плитки: в центре её кнопки должна лежать она сама.
PROBE_JS = """
(prefix) => {
  const out = [];
  document.querySelectorAll('[class*="st-key-' + prefix + '"]').forEach(box => {
    const btn = box.querySelector('button');
    const key = (box.className.match(new RegExp('st-key-' + prefix + '[a-z0-9-]*')) || [''])[0];
    if (!btn) { out.push({key, error: 'кнопки нет'}); return; }
    const r = btn.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) {
      out.push({key, error: `рамка ${Math.round(r.width)}x${Math.round(r.height)}`});
      return;
    }
    const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    const owner = el && el.closest('[class*="st-key-' + prefix + '"]');
    const ownerKey = owner
      ? (owner.className.match(new RegExp('st-key-' + prefix + '[a-z0-9-]*')) || [''])[0]
      : '';
    out.push({key, text: btn.innerText.trim().split('\\n')[0],
              hit: ownerKey, tag: el ? el.tagName : 'нет'});
  });
  return out;
}
"""


def probe(page, prefix: str, title: str) -> list[dict]:
    rows = page.evaluate(PROBE_JS, prefix)
    check(f"{title}: плитки найдены", bool(rows), "ни одной")
    bad_size = [r for r in rows if r.get("error")]
    check(f"{title}: у всех плиток живая рамка",
          not bad_size, "; ".join(f'{r["key"]}: {r["error"]}' for r in bad_size))
    misses = [r for r in rows if not r.get("error") and r.get("hit") != r.get("key")]
    check(f"{title}: центр плитки принадлежит ей самой",
          not misses,
          "; ".join(f'{r["text"]} → {r["hit"] or r["tag"]}' for r in misses))
    return rows


def main() -> int:
    from playwright.sync_api import sync_playwright

    proc = None
    if not wait_up(2):
        print(f"▸ Поднимаю приложение на порту {PORT}")
        proc = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", "streamlit_app.py",
             "--server.headless", "true", "--server.port", str(PORT),
             "--browser.gatherUsageStats", "false"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_up():
            print("Приложение не поднялось")
            return 1

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/opt/pw-browsers/chromium", args=["--no-sandbox"])
            page = browser.new_context(viewport={"width": 1294, "height": 900}).new_page()
            page.goto(URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)
            page.get_by_role("button", name="Выбрать СМУ").click()
            page.wait_for_timeout(1500)
            page.get_by_label("Пароль доступа").fill(PASSWORD)
            page.get_by_role("button", name="Войти").click()
            page.wait_for_timeout(6000)

            print("\n▸ Публикация: типы поста и страны")
            page.get_by_text("📤 Публикация", exact=True).first.click()
            page.wait_for_timeout(4000)
            probe(page, "tile-pt-", "Тип поста")
            countries = probe(page, "tile-cc-", "Страны")

            # Настоящий клик по последней стране – именно там раньше промахивались
            last = countries[-1]
            el = page.locator(f'[class*="{last["key"]}"] button').first
            r = el.bounding_box()
            page.mouse.click(r["x"] + r["width"] / 2, r["y"] + r["height"] / 2)
            page.wait_for_timeout(2500)
            active = page.evaluate(
                """() => [...document.querySelectorAll('[class*="st-key-tile-cc-"] button')]
                     .filter(b => b.getAttribute('kind') === 'primary')
                     .map(b => b.innerText.trim().split('\\n')[0])""")
            check(f"клик мышью по «{last['text']}» включил именно её",
                  active == [last["text"]], f"включилось {active}")

            # ── Фото в «Товары» – только у «Отгрузки» (как в оригинале) ──
            def goods_visible() -> bool:
                return page.get_by_text("Фото в раздел", exact=False).count() > 0

            page.locator('[class*="st-key-tile-pt-shipment"] button').first.click()
            page.wait_for_timeout(3000)
            check("у «Отгрузки» поле фото товаров есть", goods_visible())
            page.locator('[class*="st-key-tile-pt-arrival"] button').first.click()
            page.wait_for_timeout(3000)
            check("у «Поступления» поля фото товаров нет", not goods_visible())
            page.locator('[class*="st-key-tile-pt-greeting"] button').first.click()
            page.wait_for_timeout(3000)
            check("у «Поздравления» поля фото товаров нет", not goods_visible())
            page.locator('[class*="st-key-tile-pt-arrival"] button').first.click()
            page.wait_for_timeout(3000)

            # Промах: точка между карточками не должна ничего включать
            box = page.evaluate(
                """() => { const b = document.querySelector('[class*="st-key-tile-pt-"] button');
                   const r = b.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.bottom + 26}; }""")
            before = page.evaluate(
                """() => document.querySelector('[class*="st-key-tile-pt-"] button[kind=primary]')
                     ?.innerText.trim().split('\\n')[0] || ''""")
            page.mouse.click(box["x"], box["y"])
            page.wait_for_timeout(2000)
            after = page.evaluate(
                """() => document.querySelector('[class*="st-key-tile-pt-"] button[kind=primary]')
                     ?.innerText.trim().split('\\n')[0] || ''""")
            check("клик мимо плиток ничего не переключает", before == after, f"{before} → {after}")

            # ── Прикреплённые файлы не вылезают за рамку карточки ──
            # Заказчик: «немного выходят за пределы». У зоны загрузки была
            # жёсткая высота 118px – ровно по полю со ссылками слева. Список
            # файлов в неё не влезал: на четырёх файлах последняя плитка
            # уходила на 14 пикселей ниже рамки, а кнопка «+» – на 54.
            print("\n▸ Прикреплённые файлы держатся внутри карточки")
            page.locator('[class*="st-key-tile-pt-shipment"] button').first.click()
            page.wait_for_timeout(3000)

            import tempfile as _tf
            _dir = Path(_tf.mkdtemp(prefix="click-upl-"))
            _png = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
                "de0000000c4944415408d7636060600000000400012734270a0000000049454e44ae426082")
            _names = ["отгрузка АПС.jpg", "отгрузка АПС 2.jpg", "отгрузка АПС 3.jpg",
                      "отгрузка АПС на объект Северный комплект номер четыре.jpg"]
            _files = []
            for _n in _names:
                _fp = _dir / _n
                _fp.write_bytes(_png)
                _files.append(str(_fp))

            # Пустая зона обязана остаться вровень с полем слева – ради этого
            # высота и задавалась. Меряем ДО загрузки файлов.
            _empty = page.evaluate("""() => {
              const dz = document.querySelector('.st-key-img-row [data-testid="stFileUploaderDropzone"]');
              const ta = document.querySelector('.st-key-img-row textarea');
              return dz && ta ? {dz: Math.round(dz.getBoundingClientRect().height),
                                 ta: Math.round(ta.getBoundingClientRect().height)} : null;
            }""")
            check("пустая зона загрузки вровень с полем ссылок",
                  bool(_empty) and abs(_empty["dz"] - _empty["ta"]) <= 2, str(_empty))

            _inputs = page.locator('input[type="file"]')
            check("загрузчиков на «Отгрузке» два", _inputs.count() == 2, str(_inputs.count()))
            for _i in range(_inputs.count()):
                _inputs.nth(_i).set_input_files(_files)
                page.wait_for_timeout(2500)
            page.wait_for_timeout(1500)

            _over = page.evaluate("""() => {
              const bad = [];
              document.querySelectorAll('[data-testid="stFileUploader"]').forEach((up, i) => {
                const card = up.closest('[data-testid="stVerticalBlockBorderWrapper"]')
                          || up.closest('.stVerticalBlock');
                if (!card) return;
                const cr = card.getBoundingClientRect();
                const parts = [...up.querySelectorAll('[data-testid="stFileChip"]')];
                const plus = up.querySelector('[aria-label="Add files"]');
                if (plus) parts.push(plus);
                parts.forEach(el => {
                  const b = el.getBoundingClientRect();
                  const down = Math.round(b.bottom - cr.bottom);
                  const right = Math.round(b.right - cr.right);
                  if (down > 0 || right > 0)
                    bad.push(`загрузчик ${i}: ${el.getAttribute('aria-label') || '+'} `
                             + `ниже на ${down}, правее на ${right}`);
                });
              });
              return bad;
            }""")
            check("ни один файл и «+» не вылезли за рамку карточки",
                  not _over, "; ".join(_over[:4]))

            _grew = page.evaluate("""() => {
              const dz = document.querySelector('.st-key-img-row [data-testid="stFileUploaderDropzone"]');
              return dz ? Math.round(dz.getBoundingClientRect().height) : 0;
            }""")
            check("зона загрузки растянулась под список файлов", _grew > 118 + 40, f"высота {_grew}")

            # Растёт правый блок – левый обязан идти следом, иначе слева дыра
            # высотой в весь список файлов.
            _pairs = page.evaluate("""() => {
              const out = [];
              ['img-row', 'goods-row'].forEach(k => {
                const row = document.querySelector('.st-key-' + k);
                if (!row) return;
                const ta = row.querySelector('textarea');
                const dz = row.querySelector('[data-testid="stFileUploaderDropzone"]');
                if (!ta || !dz) return;
                out.push({row: k,
                          diff: Math.round(ta.getBoundingClientRect().bottom
                                         - dz.getBoundingClientRect().bottom)});
              });
              return out;
            }""")
            _skew = [x for x in _pairs if abs(x["diff"]) > 4]
            check("поле ссылок и загрузчик кончаются на одной высоте",
                  bool(_pairs) and not _skew,
                  "; ".join(f'{x["row"]}: разница {x["diff"]}px' for x in _skew) or "рядов не нашлось")

            shutil.rmtree(_dir, ignore_errors=True)
            page.locator('[class*="st-key-tile-pt-arrival"] button').first.click()
            page.wait_for_timeout(2500)

            print("\n▸ Актуализация: строки стран")
            page.get_by_text("🔄 Актуализация", exact=True).first.click()
            page.wait_for_timeout(4000)
            rows = probe(page, "tile-row-act-", "Строки стран")
            el = page.locator(f'[class*="{rows[-1]["key"]}"] button').first
            r = el.bounding_box()
            page.mouse.click(r["x"] + r["width"] / 2, r["y"] + r["height"] / 2)
            page.wait_for_function(
                "() => document.querySelectorAll('.st-key-city-grid .stCheckbox').length > 0",
                timeout=20000)
            opened = page.evaluate(
                """() => [...document.querySelectorAll('[class*="st-key-tile-row-act-"] button')]
                     .filter(b => b.getAttribute('kind') === 'primary')
                     .map(b => b.innerText.trim().split('\\n')[0])""")
            check(f"раскрылась именно «{rows[-1]['text']}»",
                  opened == [rows[-1]["text"]], f"раскрыто {opened}")

            widths = page.evaluate(
                """() => [...new Set([...document.querySelectorAll('.st-key-city-grid label')]
                     .map(l => Math.round(l.getBoundingClientRect().width)))]""")
            check("рамки городов одной ширины", len(widths) == 1, str(widths))

            # ── Оформление не пропадает во время перерисовки ──
            # Заказчик: «при нажатии «Выбрать все в стране» всё растягивается,
            # секунду, потом возвращается». Наш <style> лежал среди обычных
            # элементов, а Streamlit при перерисовке их пересобирает: стиля не
            # было на странице ~450 мс, и всё это время экран показывал голую
            # вёрстку Streamlit. Следим не за тегом, а за тем, ДЕЙСТВУЕТ ли CSS:
            # переменную --acc объявляют только наши правила.
            print("\n▸ Оформление не мигает при перерисовке")
            page.evaluate("""() => {
              window.__css = [];
              const on = () => !!getComputedStyle(document.documentElement)
                  .getPropertyValue('--acc').trim();
              let last = null;
              window.__t0 = performance.now();
              window.__timer = setInterval(() => {
                const now = on();
                if (now !== last) {
                  window.__css.push({t: Math.round(performance.now() - window.__t0), on: now});
                  last = now;
                }
              }, 20);
            }""")
            _toggle = page.get_by_role("button", name="Выбрать все в стране").first
            if _toggle.count() == 0:
                _toggle = page.get_by_role("button", name="Снять все в стране").first
            check("кнопка «выбрать/снять все в стране» на месте", _toggle.count() > 0)
            _toggle.click()
            page.wait_for_timeout(4000)
            _log = page.evaluate("() => { clearInterval(window.__timer); return window.__css; }")
            _gone = [e for e in _log if not e["on"]]
            check("CSS не пропадал при «выбрать все в стране»",
                  not _gone, f"пропадал на {[e['t'] for e in _gone]} мс от клика")
            check("стиль продублирован в <head> – его перерисовка не трогает",
                  page.evaluate(
                      """() => !!document.head.querySelector('style[data-click-css]')"""))

            print("\n▸ Города: строки стран")
            page.get_by_text("🏙 Города", exact=True).first.click()
            page.wait_for_timeout(4000)
            probe(page, "tile-row-city-", "Строки стран")

            # ── Сохранение очереди уводит на «Запуск» ──
            # Именно здесь приложение падало: раздел переключали записью в ключ
            # уже отрисованного виджета, а Streamlit это запрещает.
            print("\n▸ Очередь → переход на «Запуск»")
            page.get_by_text("📤 Публикация", exact=True).first.click()
            page.wait_for_timeout(4000)
            page.locator('[class*="st-key-tile-cc-compose-2"] button').first.click()
            page.wait_for_timeout(2500)
            ta = page.locator(".stTextArea textarea").first
            ta.fill("Проверочный текст поста")
            ta.press("Tab")
            page.wait_for_timeout(3000)
            page.get_by_role("button", name="Добавить в очередь", exact=False).first.click()
            page.wait_for_timeout(3000)

            # ── Поведение оригинала после добавления ──
            # Заказчик: «не видно что снизу что-то добавилось», «следующая
            # страна выбралась сама», «та что в очереди помечена галочкой».
            note_ok = (page.get_by_text("следующая страна", exact=False).count() > 0
                       or page.get_by_text("Все страны добавлены", exact=False).count() > 0)
            check("после добавления видно подтверждение и следующую страну", note_ok)
            mark = page.evaluate("""() => {
                const box = document.querySelector('[class*="st-key-tile-cc-compose-2"] .stButton');
                if (!box) return {};
                const btn = box.querySelector('button');
                return { meta: getComputedStyle(btn, '::after').content,
                         badge: getComputedStyle(box, '::after').display };
            }""")
            check("плитка страны помечена «в очереди»",
                  "в очереди" in (mark.get("meta") or ""), str(mark))
            check("зелёная галочка на плитке видна", mark.get("badge") == "flex", str(mark))
            primary = page.evaluate(
                """() => [...document.querySelectorAll('[class*="st-key-tile-cc-compose-"] button')]
                     .filter(b => b.getAttribute('kind') === 'primary')
                     .map(b => b.innerText.trim().split('\\n')[0])""")
            check("следующая страна выбралась сама",
                  len(primary) == 1 and primary[0] != rows[-1]["text"], str(primary))

            # ── Добавили ВСЕ страны – кнопка сохранения обязана остаться ──
            # Заказчик: «загрузила всю очередь на публикацию, а кнопки нет».
            # После последней страны выбор стран сбрасывался, следующей уже не
            # было – и ранний выход из функции уносил со страницы весь блок
            # «В очереди к сохранению» вместе с кнопкой «Сохранить очередь в
            # задачи». Собранное сохранить было нечем, а «Запуск» показывал
            # пустую очередь, хотя плитки стран стояли помеченные «в очереди».
            for _ in range(20):
                add = page.get_by_role("button", name="Добавить в очередь", exact=False)
                if add.count() == 0 or not add.first.is_enabled():
                    break
                add.first.click()
                page.wait_for_timeout(2500)
            left = page.get_by_role("button", name="Добавить в очередь", exact=False).count()
            check("после последней страны кнопки «в очередь» уже нет", left == 0, str(left))
            save = page.get_by_role("button", name="Сохранить очередь в задачи", exact=False)
            check("но кнопка «Сохранить очередь в задачи» на месте", save.count() > 0,
                  "пропала вместе с блоком очереди")
            check("и человеку сказано, что делать дальше",
                  page.get_by_text("Осталось сохранить", exact=False).count() > 0)

            page.get_by_role("button", name="Сохранить очередь в задачи").click()
            page.wait_for_timeout(5000)
            check("приложение не упало", page.get_by_text("StreamlitAPIException").count() == 0,
                  "на странице исключение Streamlit")
            active = page.evaluate(
                """() => { const e = document.querySelector(
                     '.st-key-click-tabs [aria-checked=true], .st-key-click-tabs [data-selected=true]');
                   return e ? e.innerText.trim() : ''; }""")
            check("открылся раздел «Запуск»", "Запуск" in active, f"активно: {active!r}")
            import re as _re
            _body = page.evaluate("() => document.body.innerText")
            _m = _re.search(r"Сейчас:\s*(\d+)\s*файл\w*,\s*(\d+)\s*город\w*", _body)
            check("очередь доехала до «Запуска», а не осталась пустой",
                  bool(_m) and _m.group(1) != "0", _m.group(0) if _m else "строку не нашли")

            # ── Галочка «показывать окно браузера» переживает смену раздела ──
            # Streamlit выбрасывает состояние виджетов, которых не было на
            # текущем экране. Галочка жила в ключе виджета и гасла при уходе
            # с «Настроек» – публикация после этого шла скрыто, и заказчик
            # видел «галочка сбрасывается» и «прогресса всё равно не видно».
            print("\n▸ Галочка «показывать окно браузера»")
            box = "Показывать окно браузера"

            def go(tab: str) -> None:
                page.get_by_text(tab, exact=True).first.click()
                page.wait_for_timeout(4000)

            go("⚙️ Настройки")
            cb = page.get_by_role("checkbox", name=box, exact=False)
            if cb.count() == 0:
                check("галочка есть в «Настройках»", False, "чекбокс не найден")
            else:
                # Сам input у Streamlit спрятан под оформлением – жмём подпись,
                # ровно как это делает человек.
                page.locator("label").filter(has_text=box).first.click()
                page.wait_for_timeout(2500)
                check("галочка встала", cb.first.is_checked())
                go("📤 Публикация")
                go("⚙️ Настройки")
                cb = page.get_by_role("checkbox", name=box, exact=False)
                check("галочка пережила переход на «Публикацию»",
                      cb.count() > 0 and cb.first.is_checked(), "сбросилась")

            # ── ГЛАВНОЕ: живая панель обновляется САМА ──
            # Заказчик: «нажала актуализацию, а там как будто зависло на первом
            # городе. Нажала остановить, а там оказывается 18 городов уже
            # прошёл». Вкладка «Актуализация» рисовала панель без
            # авто-обновления, и экран замирал, хотя прогон шёл дальше.
            print("\n▸ Живая панель обновляется сама")
            import json as _json
            import paths as _paths
            data = _paths.data_root() / "SMU"
            data.mkdir(parents=True, exist_ok=True)

            # Файлы у каждого вида прогона свои: прогоны идут параллельно, и
            # вкладка обязана показывать СВОЙ прогон, а не соседний.
            def files_of(action: str):
                return (data / f".run-log-{action}.txt", data / f".run-state-{action}.json")

            # Прогон «чужого» процесса приложение честно считает прерванным
            # (ownerPid), поэтому подделываем состояние от имени сервера.
            app_pid = proc.pid if proc is not None else None

            def fake_run(action: str, city: str) -> None:
                files_of(action)[1].write_text(_json.dumps({
                    "runId": "ui-test", "action": action, "status": "running",
                    "ownerPid": app_pid, "total": 3, "current": 1,
                    "currentCity": city, "totals": {"total": 1},
                }, ensure_ascii=False), encoding="utf-8")

            tabs = (("🚀 Запуск", "publish"), ("🔄 Актуализация", "actualize"))
            if app_pid is None:
                check("живая панель: приложение подняли не мы – пропускаю", True)
                tabs = ()
            for tab, action in tabs:
                fake_run(action, "Барнаул")
                live_log = files_of(action)[0]
                live_log.write_text("[10:00:00] [INFO] СТРОКА-ОДИН\n", encoding="utf-8")
                page.get_by_text(tab, exact=True).first.click()
                page.wait_for_timeout(4000)
                check(f"{tab}: панель прогона видна",
                      page.get_by_text("СТРОКА-ОДИН", exact=False).count() > 0)
                # Дописываем лог и НИЧЕГО не нажимаем – панель обязана обновиться
                live_log.write_text("[10:00:00] [INFO] СТРОКА-ОДИН\n"
                                    "[10:00:05] [INFO] СТРОКА-ДВА\n", encoding="utf-8")
                appeared = False
                for _ in range(8):
                    page.wait_for_timeout(1500)
                    if page.get_by_text("СТРОКА-ДВА", exact=False).count() > 0:
                        appeared = True
                        break
                check(f"{tab}: новая строка появилась БЕЗ нажатий", appeared)

            for _act in ("publish", "actualize"):
                for _fp in files_of(_act):
                    _fp.unlink(missing_ok=True)

            # ── Отчёт: плашки И ЕСТЬ фильтры ──
            # Заказчик: «сделаем плашки кликабельными, чтобы не дублировались
            # кнопки». Раньше под цветными плашками стоял второй ряд кнопок с
            # теми же названиями.
            print("\n▸ Отчёт: цветные плашки работают как фильтры")
            import json as _js
            rep = _paths.data_root() / "SMU" / "reports-actualize"
            rep.mkdir(parents=True, exist_ok=True)
            (rep / "actualize-2026-08-04T09-00-00.json").write_text(_js.dumps({
                "type": "actualize", "startedAt": "2026-08-04T09:00:00+00:00",
                "finishedAt": "2026-08-04T09:04:00+00:00", "durationSec": 240,
                "state": "finished", "runId": "ui",
                "totals": {"total": 3, "actualized": 2, "notNeeded": 0, "failed": 1},
                "results": [
                    {"cityName": "Барнаул", "country": "Россия", "status": "actualized",
                     "reason": "Клик прошёл", "durationMs": 11000},
                    {"cityName": "Воронеж", "country": "Россия", "status": "actualized",
                     "reason": "Клик прошёл", "durationMs": 12000},
                    {"cityName": "Волгоград", "country": "Россия", "status": "failed",
                     "reason": "Не удалось определить URL", "durationMs": 3000},
                ]}, ensure_ascii=False), encoding="utf-8")

            page.get_by_text("📊 Отчёт", exact=True).first.click()
            page.wait_for_timeout(4000)
            page.get_by_role("button", name="Актуализация", exact=False).first.click()
            page.wait_for_timeout(4000)

            check("страница отчёта не упала",
                  page.get_by_text("Traceback", exact=False).count() == 0)
            check("кнопки «Обновить список» больше нет",
                  page.get_by_role("button", name="Обновить список").count() == 0)
            tiles = page.evaluate("""() =>
                [...document.querySelectorAll('[class*="st-key-tile-stat-"] button')].map(b => ({
                  label: b.innerText.trim(),
                  val: getComputedStyle(b, '::after').content.replace(/"/g, ''),
                  off: b.disabled }))""")
            check("плашки собраны", len(tiles) == 4, str(tiles))
            check("значения на плашках верные",
                  [t["val"] for t in tiles] == ["2", "0", "1", "4.0 мин"], str(tiles))
            check("«Время» не кликается", tiles and tiles[-1]["off"], str(tiles))
            # Кнопка с такой надписью должна быть РОВНО одна – сама плашка.
            same = page.evaluate("""() =>
                [...document.querySelectorAll('button')]
                  .filter(b => b.innerText.trim().toLowerCase() === 'не требовалось').length""")
            check("второго ряда кнопок-фильтров нет", same == 1, f"кнопок с этой надписью: {same}")

            page.locator('[class*="st-key-tile-stat-"] button').nth(2).click()
            page.wait_for_timeout(3500)
            check("нажатая плашка подсветилась",
                  page.evaluate("""() =>
                      [...document.querySelectorAll('[class*="st-key-tile-stat-"] button')]
                        .filter(b => b.getAttribute('kind') === 'primary')
                        .map(b => b.innerText.trim())""") == ["ОШИБОК"])
            check("нажатая плашка осталась своего цвета, а не фиолетовой",
                  page.evaluate("""() => {
                      const b = document.querySelector(
                        '[class*="st-key-tile-stat-"] button[kind="primary"]');
                      const bg = getComputedStyle(b).backgroundImage;
                      return !b || bg === 'none';
                  }"""))
            rows = page.evaluate(
                """() => [...document.querySelectorAll('.report-row')].map(r => r.className)""")
            check("в деталях остались только ошибки",
                  rows == ["report-row err"], str(rows))
            check("детали не разбиты по странам – один список",
                  page.get_by_text("успешно", exact=False).count() == 0)

            # ── Сверка с Яндексом ──
            # Вкладка ходит и в Google, и в Яндекс; на тестовой машине нет ни
            # ключа таблицы, ни сессии. Проверяем, что она из-за этого не падает,
            # а честно объясняет, чего не хватает.
            print("\n▸ Сверка: вкладка открывается")
            page.get_by_text("🔎 Сверка", exact=True).first.click()
            page.wait_for_timeout(4000)
            check("вкладка «Сверка» не упала",
                  page.get_by_text("Traceback", exact=False).count() == 0
                  and page.get_by_text("StreamlitAPIException", exact=False).count() == 0)
            check("заголовок сверки на месте",
                  page.get_by_text("Сверка КП с организациями Яндекса", exact=False).count() > 0)
            check("кнопка чтения организаций есть",
                  page.get_by_role("button", name="Прочитать организации", exact=False).count() > 0)
            check("без таблицы КП сказано, где её настроить",
                  page.get_by_text("Источник городов", exact=False).count() > 0
                  or page.get_by_text("Лист КП", exact=False).count() > 0)
            check("раздел «Сверка» идёт после «Настроек»",
                  page.evaluate("""() => {
                      const t = [...document.querySelectorAll('.st-key-click-tabs label')]
                        .map(l => l.innerText.trim());
                      return t.indexOf('🔎 Сверка') === t.length - 1;
                  }"""))

            browser.close()
    finally:
        if proc is not None:
            proc.terminate()

    print("\n" + "═" * 60)
    if FAIL:
        print(f"  ПРОВАЛЕНО {FAIL} из {OK + FAIL}:")
        for name in FAILED:
            print(f"    • {name}")
        print("═" * 60)
        return 1
    print(f"  ВСЁ ХОРОШО – {OK} проверок пройдено")
    print("═" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
