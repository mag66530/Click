"""
VHOD-VK-i-OK.py – получить файл сессий ВК, ОК и МАКС, войдя руками.

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
  5а. Третьей вкладкой откроется МАКС – вход по номеру телефона (кнопка
     под QR-кодом), галочка «я не робот» и код из СМС.
  6. Рядом появится ОДИН файл `VK-i-OK-sessii.json` – в нём обе сети.
     Загрузите его в Click: «Настройки» → «Файл сессий ВК и ОК». Вставлять
     дважды не нужно, Click разложит куки по сетям сам.

ПОЧЕМУ ОК ОТКРЫВАЕТСЯ САМ. Раньше адрес ok.ru нужно было набрать руками, и
это подводило: браузер принимал слово «одноклассники» за поисковый запрос и
уходил в Google вместо сайта. Теперь вкладку открывает скрипт – ошибиться
негде.

ПРО ПРОФИЛЬ. Окно открывается со СВОИМ постоянным профилем (папка рядом с
данными Click, ваш обычный Chrome не трогается). Так надо: одноразовое
«стерильное» окно МАКС узнаёт и не рисует проверку «вы не робот» вовсе –
заголовок есть, а галочки под ним нет. Профиль живёт между запусками, так
что во второй раз входить в ВК и ОК уже не придётся.

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

# Маскировка автоматизации. Без неё сайты видят, что окном управляет
# программа, и ведут себя иначе: МАКС показал заголовок «Проверяем, что вы
# не робот», а сам виджет с галочкой не нарисовал вовсе – проверять стало
# нечего, и вход встал. Те же две строки давно стоят в самом Click.
ANTIBOT_ARGS = ["--disable-blink-features=AutomationControlled"]
ANTIBOT_INIT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"


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


def _has_max(state: dict) -> bool:
    """
    Есть ли в снимке вход в МАКС.

    У МАКС он живёт не только в куках, но и в localStorage – это
    одностраничное приложение. Поэтому смотрим и раздел origins, иначе
    живая сессия выглядела бы пустой.
    """
    for c in state.get("cookies") or []:
        if "max.ru" in str(c.get("domain", "")) and c.get("value"):
            if str(c.get("name", "")) not in OK_GUEST:
                return True
    for o in state.get("origins") or []:
        if "max.ru" in str(o.get("origin", "")) and o.get("localStorage"):
            return True
    return False


def _has_ok(cookies: list) -> bool:
    """Та же проверка, что делает Click в ok_browser.looks_logged_in."""
    names = set(_names(cookies))
    return bool(names & set(OK_AUTH) or names - OK_GUEST)


def _has_tg(state: dict) -> bool:
    """
    Есть ли в снимке вход в Телеграм (Web A).

    Как у МАКС: вход живёт в localStorage домена web.telegram.org (ключи
    dcN_auth_key, user_auth), а не в куках. Смотрим и раздел origins.
    """
    for o in state.get("origins") or []:
        if "web.telegram.org" in str(o.get("origin", "")) and o.get("localStorage"):
            return True
    for c in state.get("cookies") or []:
        if "telegram.org" in str(c.get("domain", "")) and c.get("value"):
            if str(c.get("name", "")) not in ("stel_ln", "stel_dt"):
                return True
    return False


def _open_like_human(context, url: str):
    """
    Открыть адрес КЛИКОМ ПО ССЫЛКЕ, а не командой перехода.

    Разница неочевидная, но решающая. Заказчица: «я нажала сама на ссылку
    из сохранённого ярлыка – и отобразилось, а когда в поиске вставляла,
    не грузилось». Так и есть: у перехода по клику есть источник и
    пользовательский жест, у программного goto – нет, и МАКС на это
    смотрит: заголовок «Проверяем, что вы не робот» рисует, а сам виджет
    с галочкой – нет.

    Поэтому кладём на пустую страницу обычную ссылку и щёлкаем по ней.
    Для сайта это неотличимо от нажатия на ярлык.
    """
    page = context.new_page()
    page.goto("about:blank")
    page.set_content(
        '<html><body style="font:16px sans-serif;padding:40px">'
        '<p>Открываю МАКС…</p>'
        f'<a id="go" href="{url}">Открыть МАКС</a></body></html>')
    page.wait_for_timeout(300)
    page.click("#go")                      # настоящий клик, с жестом
    page.wait_for_timeout(3_000)
    return page


def _profile_dir() -> Path:
    """
    Своя папка профиля браузера – рядом с данными Click, не в проекте.

    Профиль ЖИВЁТ между запусками: второй раз входить уже не придётся, а
    сайты видят браузер, которым пользуются, а не одноразовый.
    """
    import os
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        d = base / "Click" / "vhod-profile"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "Click" / "vhod-profile"
    else:
        d = Path.home() / ".local" / "share" / "click" / "vhod-profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _open_context(pw):
    """
    Открыть окно с ПОСТОЯННЫМ профилем.

    Почему не обычный запуск. Одноразовое окно Playwright – «стерильное»:
    без истории, без настроек, без профиля. МАКС такое узнаёт и не рисует
    виджет проверки «вы не робот» вовсе: заголовок есть, галочки нет,
    пройти нечего (проверено заказчицей 12.08.2026, и маскировка флага
    автоматизации тут не помогла). Постоянный профиль выглядит как
    браузер, которым пользуются.

    Заодно приятное: профиль сохраняется, и во второй раз входить в ВК и
    ОК уже не придётся.
    """
    profile = _profile_dir()
    common = dict(user_data_dir=str(profile), headless=False, args=ANTIBOT_ARGS,
                  viewport={"width": 1280, "height": 900},
                  locale="ru-RU", timezone_id="Asia/Yekaterinburg")
    last = None
    for channel in CHANNELS:
        try:
            kw = dict(common)
            if channel:
                kw["channel"] = channel
            context = pw.chromium.launch_persistent_context(**kw)
            context.add_init_script(ANTIBOT_INIT)
            return context
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
    print("ЕСЛИ ВЫ УЖЕ ВХОДИЛИ РАНЬШЕ в этом окне – профиль всё помнит.")
    print("Тогда просто трижды нажмите Enter: сессии соберутся сами.")
    print("─" * 62)
    print("ШАГ 1 из 3. Открываю окно браузера. Войдите в ВК как обычно.")
    print("Проверку «я не робот», если появится, пройдите мышкой.")
    print("Одноклассники откроются сами, вторым шагом – набирать ничего")
    print("не надо.")
    print("ОКНО НЕ ЗАКРЫВАЙТЕ – я закрою его сам, когда сохраню сессию.")
    print("─" * 62)

    with sync_playwright() as pw:
        # ГЛАВНОЕ ОТЛИЧИЕ ОТ CLICK: окно настоящее, видимое. Проверку
        # проходит человек, а не программа – поэтому она и проходится.
        try:
            context = _open_context(pw)
        except RuntimeError as exc:
            print(f"\n{exc}")
            input("\nНажмите Enter, чтобы закрыть… ")
            return 1

        # У постоянного профиля вкладка уже есть – берём её, а не заводим
        # вторую: лишняя пустая вкладка только путает.
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://vk.ru/login", wait_until="domcontentloaded")

        input("\nВошли в ВК? Нажмите Enter здесь, в этом окне… ")

        # Вторую вкладку открываем САМИ. Набирать адрес руками – лишний шаг,
        # на котором уже спотыкались: браузер принял «одноклассники» за
        # поисковый запрос и ушёл в Google вместо сайта.
        print("\n─" * 1 + "─" * 61)
        print("ШАГ 2 из 3. Открываю Одноклассники во второй вкладке.")
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

        # ШАГ 3 – МАКС. Отложка там родная (у бота её нет вовсе), поэтому
        # сессия нужна такая же, как у ВК и ОК.
        print("\n" + "─" * 62)
        print("ШАГ 3 из 4. Открываю МАКС в третьей вкладке.")
        print("Войдите по номеру телефона: кнопка под QR-кодом, галочка")
        print("«я не робот», код из СМС. Не нужен МАКС? Просто Enter.")
        print()
        print("Если галочка «я не робот» не появилась и окно пустое:")
        print("  • нажмите F5;")
        print("  • или откройте МАКС с ПЛИТКИ на новой вкладке (Ctrl+T) –")
        print("    у заказчицы сработало именно так: переход по ярлыку МАКС")
        print("    принимает охотнее, чем набранный адрес.")
        print("─" * 62)
        try:
            _open_like_human(context, "https://web.max.ru/")
        except Exception as exc:          # noqa: BLE001 – ВК и ОК уже добыты
            print(f"\n⚠️  Вкладку с МАКС открыть не вышло ({exc}).")
            print("   Откройте web.max.ru сами в этом же окне.")

        input("\nВошли в МАКС (или он не нужен)? Нажмите Enter… ")

        # ШАГ 4 – ТЕЛЕГРАМ. Отложка родная (у бота её нет), пишем во ВСЕ каналы
        # с одного аккаунта-админа. Важно: именно веб-версия /a/ – заказчица
        # входит по QR с телефона. Вход Телеграма живёт в localStorage, и
        # storage_state его заберёт, как у МАКС.
        print("\n" + "─" * 62)
        print("ШАГ 4 из 4. Открываю Телеграм (веб-версия /a/) в четвёртой вкладке.")
        print("Войдите по QR-коду: в приложении на телефоне «Настройки» →")
        print("«Устройства» → «Подключить устройство» и наведите на QR.")
        print("Аккаунт должен быть АДМИНОМ каналов, куда планируем посты.")
        print("Не нужен Телеграм? Просто Enter.")
        print("─" * 62)
        try:
            _open_like_human(context, "https://web.telegram.org/a/")
        except Exception as exc:          # noqa: BLE001 – ВК/ОК/МАКС уже добыты
            print(f"\n⚠️  Вкладку с Телеграмом открыть не вышло ({exc}).")
            print("   Откройте web.telegram.org/a/ сами в этом же окне.")

        input("\nВошли в Телеграм (или он не нужен)? Нажмите Enter… ")

        try:
            state = context.storage_state()
        except Exception:
            print("\n❌ Окно браузера закрыто – сохранять уже нечего. "
                  "Запустите файл ещё раз и не закрывайте окно сами.")
            input("\nНажмите Enter, чтобы закрыть… ")
            return 1
        context.close()

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

    if _has_max(state):
        print("   МАКС: вход есть")
    else:
        print("   ℹ️  МАКС: входа не видно. Если проверка «я не робот» так и не")
        print("      показала галочку – обновите страницу МАКС (F5) и повторите:")
        print("      виджет проверки иногда не рисуется с первого раза.")

    if _has_tg(state):
        print("   Телеграм: вход есть")
    else:
        print("   ℹ️  Телеграм: входа не видно. Убедитесь, что открыли именно")
        print("      web.telegram.org/a/ и увидели свои чаты после QR. Если")
        print("      осталась страница с QR – вход не завершился, повторите.")

    print("\nТеперь загрузите ЭТОТ ОДИН файл в Click:")
    print("  «Настройки» → «Файл сессий ВК и ОК» → «Загрузить»")
    print("Обе сети возьмутся из него сразу, вставлять дважды не нужно.")
    input("\nНажмите Enter, чтобы закрыть… ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
