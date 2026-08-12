"""
VHOD-VK-i-OK.py – получить файлы сессии ВК и ОК, войдя руками.

ЗАЧЕМ ЭТО НУЖНО. Click в облаке работает скрытым браузером, а ВК показывает
ему проверку «Подтвердите, что вы не робот». Пройти её программой нельзя –
она для того и сделана. Зато можно войти РУКАМИ один раз и отдать Click
готовую сессию: дальше он публикует сам, никаких проверок больше не будет.

КАК ПОЛЬЗОВАТЬСЯ:
  1. Запустите этот файл на своём компьютере (двойной клик или
     `python VHOD-VK-i-OK.py` в командной строке).
  2. Откроется обычное окно браузера с формой входа ВК.
  3. Войдите как всегда: «Войти другим способом» → телефон → код.
     Проверку «я не робот», если появится, пройдите мышкой – вы человек,
     у вас получится.
  4. Когда окажетесь в своей ленте, вернитесь сюда и нажмите Enter.
  5. Тогда САМ ОТКРОЕТСЯ вторая вкладка с Одноклассниками – войдите и там
     (проще всего кнопкой «Войти через ВК»: вы уже в ВК, он пустит почти
     без вопросов). Не нужны ОК – просто нажмите Enter ещё раз.
  6. Рядом появится ОДИН файл `VK-i-OK-sessii.json` – в нём обе сети.
     Загрузите его в Click: «Настройки» → «Файл сессий ВК и ОК». Вставлять
     дважды не нужно, Click разложит куки по сетям сам.

ПОЧЕМУ ОК ОТКРЫВАЕТСЯ САМ. Раньше адрес ok.ru нужно было набрать руками, и
это подводило: браузер принимал слово «одноклассники» за поисковый запрос и
уходил в Google вместо сайта. Теперь вкладку открывает скрипт – ошибиться
негде.

БЕЗОПАСНОСТЬ. Файл сессии – это доступ к аккаунту. Передавайте его так же
бережно, как пароль: не через открытые чаты. В репозиторий он не попадает.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
VK_FILE = HERE / "vk-session.json"          # оставлены для совместимости
OK_FILE = HERE / "ok-session.json"
BOTH_FILE = HERE / "VK-i-OK-sessii.json"   # основной: один файл на обе сети

VK_AUTH = ("remixsid", "remixsid6", "remixnsid")

# Вход в ОК определяем ОТ ОБРАТНОГО – по тому, чего у гостя быть не может.
# Список «правильных» имён (AUTH_ID, AUTH_SIG, OK_LOGIN) уже подвёл: человек
# вошёл в ОК руками, а файл не сохранился – ни одного имени из списка среди
# его кук не оказалось, ОК зовёт их иначе. Гостевые же куки снимаются с живой
# ok.ru за секунду (сделано 11.08.2026) и меняются куда реже.
OK_GUEST = frozenset({
    "bci", "_statid", "JSESSIONID", "cookieChoice", "ss_wb",
    "TZ", "TZO", "_flashVersion", "tmr_lvid", "tmr_lvidTS",
    "_ym_uid", "_ym_d", "_ym_isad", "_ga", "_gid",
})
OK_AUTH = ("AUTH_ID", "auth_id", "AUTH_SIG", "OK_LOGIN", "AUTHCODE")

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


def _names(cookies: list) -> list[str]:
    return sorted({str(c.get("name", "")) for c in cookies if c.get("value")})


def _has_ok(cookies: list) -> bool:
    """Та же проверка, что делает Click в ok_browser.looks_logged_in."""
    names = set(_names(cookies))
    return bool(names & set(OK_AUTH) or names - OK_GUEST)


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
    print("ШАГ 1 из 2. Открываю окно браузера. Войдите в ВК как обычно.")
    print("Проверку «я не робот», если появится, пройдите мышкой.")
    print("Одноклассники откроются сами, вторым шагом – набирать ничего")
    print("не надо.")
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

        input("\nВошли в ВК? Нажмите Enter здесь, в этом окне… ")

        # Вторую вкладку открываем САМИ. Набирать адрес руками – лишний шаг,
        # на котором уже спотыкались: браузер принял «одноклассники» за
        # поисковый запрос и ушёл в Google вместо сайта.
        print("\n─" * 1 + "─" * 61)
        print("ШАГ 2 из 2. Открываю Одноклассники во второй вкладке.")
        print("Проще всего войти кнопкой «Войти через ВК» – вы уже в ВК.")
        print("Не нужны ОК? Просто нажмите Enter, ВК всё равно сохранится.")
        print("─" * 62)
        try:
            ok_page = context.new_page()
            ok_page.goto("https://ok.ru/", wait_until="domcontentloaded")
        except Exception as exc:          # noqa: BLE001 – ВК уже добыт, не теряем его
            print(f"\n⚠️  Вкладку с ОК открыть не вышло ({exc}).")
            print("   Откройте ok.ru сами в этом же окне – ВК от этого не пострадает.")

        input("\nВошли в ОК (или он не нужен)? Нажмите Enter… ")

        try:
            state = context.storage_state()
        except Exception:
            print("\n❌ Окно браузера закрыто – сохранять уже нечего. "
                  "Запустите файл ещё раз и не закрывайте окно сами.")
            input("\nНажмите Enter, чтобы закрыть… ")
            return 1
        browser.close()

    # ОДИН файл на обе сети. Раньше их было два, и вставлять их приходилось
    # в два разных окошка – работа на ровном месте: куки-то снимаются одним
    # браузером за один заход. Click сам разложит их по сетям.
    vk, ok = _split(state)
    BOTH_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✅ Файл сессий сохранён: {BOTH_FILE}")

    if _has_vk(vk["cookies"]):
        print("   ВК: вход есть")
    else:
        print("   ❌ ВК: признака входа нет – похоже, войти не удалось. "
              "Запустите ещё раз и убедитесь, что видите свою ленту.")

    if _has_ok(ok["cookies"]):
        print("   ОК: вход есть")
    else:
        # Показываем, ЧТО нашли. Без этого «сессии ОК нет» – тупик: человек
        # входил своими глазами, а файла нет, и почему – непонятно.
        found = ", ".join(_names(ok["cookies"])[:12]) or "ни одной куки ОК"
        print("   ℹ️  ОК: входа не видно. Что нашлось: " + found)
        print("      Если вы точно вошли и видели свою страницу ОК – пришлите")
        print("      эту строку разработчику: ОК назвал куки по-новому.")

    print("\nТеперь загрузите ЭТОТ ОДИН файл в Click:")
    print("  «Настройки» → «Файл сессий ВК и ОК» → «Загрузить»")
    print("Обе сети возьмутся из него сразу, вставлять дважды не нужно.")
    input("\nНажмите Enter, чтобы закрыть… ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
