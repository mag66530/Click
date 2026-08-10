"""
ВХОД-ВК.py – получить файл сессии ВК и ОК, войдя руками.

ЗАЧЕМ ЭТО НУЖНО. Click в облаке работает скрытым браузером, а ВК показывает
ему проверку «Подтвердите, что вы не робот». Пройти её программой нельзя –
она для того и сделана. Зато можно войти РУКАМИ один раз и отдать Click
готовую сессию: дальше он публикует сам, никаких проверок больше не будет.

КАК ПОЛЬЗОВАТЬСЯ:
  1. Запустите этот файл на своём компьютере (двойной клик или
     `python ВХОД-ВК.py` в командной строке).
  2. Откроется обычное окно браузера с формой входа ВК.
  3. Войдите как всегда: «Войти другим способом» → телефон → код.
     Проверку «я не робот», если появится, пройдите мышкой – вы человек,
     у вас получится.
  4. Когда окажетесь в своей ленте, вернитесь сюда и нажмите Enter.
  5. Рядом появится файл `vk-session.json`. Загрузите его в Click:
     «Настройки» → «Вход в ВК» → «Загрузить готовый файл сессии».

ОДНОКЛАССНИКИ. После входа в ВК окно НЕ закрывается: перейдите на ok.ru и
войдите там кнопкой «Войти через ВК» – он пустит почти без вопросов, вы уже
в ВК. Тогда сохранится и `ok-session.json` – его тоже загрузите в Click.

БЕЗОПАСНОСТЬ. Файл сессии – это доступ к аккаунту. Передавайте его так же
бережно, как пароль: не через открытые чаты. В репозиторий он не попадает.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
VK_FILE = HERE / "vk-session.json"
OK_FILE = HERE / "ok-session.json"

VK_AUTH = ("remixsid", "remixsid6", "remixnsid")
# JSESSIONID сюда НЕ входит, хотя Click его и принимает: ОК выдаёт эту куку
# и гостю тоже. Файл с одним только ей – пустышка, и лучше сказать об этом
# сейчас, чем в день публикации.
OK_AUTH = ("AUTH_ID", "auth_id", "AUTH_SIG", "OK_LOGIN")

# Первым пробуем ваш настоящий Chrome: он уже стоит, ничего качать не надо,
# и ВК видит обычный браузер, а не «стерильный» тестовый. Если Chrome нет –
# берём тот, что идёт с playwright.
CHANNELS = ("chrome", "msedge", None)


def _split(state: dict) -> tuple[dict, dict]:
    """Разделить куки на ВК-шные и ОК-шные: Click хранит сессии отдельно."""
    vk = {"cookies": [], "origins": []}
    ok = {"cookies": [], "origins": []}
    for c in state.get("cookies") or []:
        domain = str(c.get("domain", ""))
        if "vk.ru" in domain or "vk.com" in domain or "vkontakte" in domain:
            vk["cookies"].append(c)
        if "ok.ru" in domain or "odnoklassniki" in domain:
            ok["cookies"].append(c)
        # Куки VK ID нужны обеим: через них ОК и пускает.
        if "id.vk" in domain or "login.vk" in domain:
            ok["cookies"].append(c)
    for o in state.get("origins") or []:
        origin = str(o.get("origin", ""))
        (ok if "ok.ru" in origin else vk)["origins"].append(o)
    return vk, ok


def _has_vk(cookies: list) -> bool:
    """Та же проверка, что делает Click в vk_social.import_session."""
    return any(str(c.get("name", "")).startswith(VK_AUTH) and c.get("value")
               for c in cookies)


def _has_ok(cookies: list) -> bool:
    """Та же проверка, что делает Click в ok_browser.import_session (точное имя)."""
    return any(str(c.get("name", "")) in OK_AUTH and c.get("value")
               for c in cookies)


def _open_browser(pw):
    """Открыть окно браузера, перебрав что есть на компьютере."""
    last = None
    for channel in CHANNELS:
        try:
            if channel:
                return pw.chromium.launch(headless=False, channel=channel)
            return pw.chromium.launch(headless=False)
        except Exception as exc:          # нет такого браузера – пробуем следующий
            last = exc
    raise RuntimeError(
        "Не нашёл ни одного браузера. Установите Google Chrome либо выполните: "
        f"python -m playwright install chromium\n({last})")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Не установлен playwright. Выполните:  pip install playwright")
        print("и потом:  python -m playwright install chromium")
        return 1

    print("─" * 62)
    print("Открываю окно браузера. Войдите в ВК как обычно.")
    print("Проверку «я не робот», если появится, пройдите мышкой.")
    print("Хотите заодно Одноклассники – перейдите на ok.ru и войдите")
    print("кнопкой «Войти через ВК» (он пустит почти без вопросов).")
    print("ОКНО НЕ ЗАКРЫВАЙТЕ – я закрою его сам, когда сохраню сессию.")
    print("─" * 62)

    with sync_playwright() as pw:
        # ГЛАВНОЕ ОТЛИЧИЕ ОТ CLICK: окно настоящее, видимое. Проверку
        # проходит человек, а не программа – поэтому она и проходится.
        try:
            browser = _open_browser(pw)
        except RuntimeError as exc:
            print(f"\n{exc}")
            input("\nНажмите Enter, чтобы закрыть… ")
            return 1

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="ru-RU", timezone_id="Asia/Yekaterinburg")
        page = context.new_page()
        page.goto("https://vk.ru/login", wait_until="domcontentloaded")

        input("\nВошли? Нажмите Enter здесь, в этом окне… ")

        try:
            state = context.storage_state()
        except Exception:
            print("\n❌ Окно браузера закрыто – сохранять уже нечего. "
                  "Запустите файл ещё раз и не закрывайте окно сами.")
            input("\nНажмите Enter, чтобы закрыть… ")
            return 1
        browser.close()

    vk, ok = _split(state)

    if _has_vk(vk["cookies"]):
        VK_FILE.write_text(json.dumps(vk, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n✅ Сессия ВК сохранена: {VK_FILE}")
    else:
        print("\n❌ Признака входа в ВК нет – похоже, войти не удалось. "
              "Запустите ещё раз и убедитесь, что видите свою ленту.")

    if _has_ok(ok["cookies"]):
        OK_FILE.write_text(json.dumps(ok, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✅ Сессия ОК сохранена: {OK_FILE}")
    else:
        print("ℹ️  Сессии ОК нет – вы в него не входили. Это нормально, "
              "если Одноклассники пока не нужны.")

    print("\nТеперь загрузите файл(ы) в Click:")
    print("  «Настройки» → «Вход в ВК (кросспостинг)» → «Загрузить готовый файл сессии»")
    print("  и то же самое в блоке «Вход в ОК».")
    input("\nНажмите Enter, чтобы закрыть… ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
