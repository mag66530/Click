"""
social_session.py – один файл сессий на обе сети, ВК и ОК.

Зачем. Куки ВК и ОК снимаются ОДНИМ браузером за ОДИН заход: вошли в ВК,
следом в ОК (он и пускает-то через ВК) – и всё лежит в одном storage_state.
Делить это на два файла и заставлять человека вставлять их в два разных
окошка – работа на ровном месте. Предложение заказчицы: «может, одним
входом оба куки собирать, в один файлик, и один раз вставлять».

Так и делаем. Файл принимается один, а Click сам раскладывает куки по
сетям и говорит, что нашёл: «ВК принят, ОК принят» либо честно – чего в
файле не оказалось.

Разбор по домену, а не по именам кук: имена площадки меняют, домены – нет.
Куки VK ID (id.vk, login.vk, connect.vk) кладём В ОБА набора: через них ОК
и пускает, и без них его сессия живёт недолго.
"""

from __future__ import annotations

import json

# Метка сборки – одна на всё приложение (см. build.py).
from build import BUILD  # noqa: F401

VK_DOMAINS = ("vk.ru", "vk.com", "vkontakte", "userapi.com")
OK_DOMAINS = ("ok.ru", "odnoklassniki")
# Общий вход: VK ID. Нужен обеим сетям – ОК входит через него.
SHARED_DOMAINS = ("id.vk", "login.vk", "connect.vk", "oauth.vk")


def _has(domain: str, marks) -> bool:
    return any(m in domain for m in marks)


def split_state(state: dict) -> tuple[dict, dict]:
    """
    Общий storage_state → (сессия ВК, сессия ОК).

    Куки, не относящиеся ни к одной сети (счётчики, реклама), отбрасываем:
    в файле сессии им делать нечего, а размер они раздувают.
    """
    vk: dict = {"cookies": [], "origins": []}
    ok: dict = {"cookies": [], "origins": []}
    for cookie in state.get("cookies") or []:
        domain = str(cookie.get("domain", "")).lower()
        if _has(domain, SHARED_DOMAINS):
            vk["cookies"].append(cookie)
            ok["cookies"].append(cookie)
            continue
        if _has(domain, VK_DOMAINS):
            vk["cookies"].append(cookie)
        if _has(domain, OK_DOMAINS):
            ok["cookies"].append(cookie)
    for origin in state.get("origins") or []:
        where = str(origin.get("origin", "")).lower()
        if _has(where, OK_DOMAINS):
            ok["origins"].append(origin)
        elif _has(where, VK_DOMAINS + SHARED_DOMAINS):
            vk["origins"].append(origin)
    return vk, ok


def import_combined(project_id: str, raw: bytes) -> tuple[bool, str]:
    """
    Принять один файл и разложить по сетям. (получилось?, что вышло словами).

    «Получилось» – если принялась ХОТЯ БЫ одна сеть: файл, где есть только
    ВК, тоже полезен, и отвергать его целиком было бы вредно. А чего не
    хватило, сказано прямо, чтобы не гадать.
    """
    import ok_browser
    import vk_social

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"Это не файл сессии: {e}"
    if not isinstance(data, dict) or "cookies" not in data:
        return False, ("В файле нет раздела cookies. Нужен storage_state "
                       "Playwright – его сохраняет VHOD-VK-i-OK.py.")

    vk_state, ok_state = split_state(data)
    said = []
    took = False

    for label, state, importer in (("ВК", vk_state, vk_social.import_session),
                                   ("ОК", ok_state, ok_browser.import_session)):
        if not state["cookies"]:
            said.append(f"{label}: куки этой сети в файле не найдены")
            continue
        accepted, why = importer(project_id, json.dumps(state).encode("utf-8"))
        took = took or accepted
        said.append(f"{label}: " + ("принят" if accepted else why))

    return took, " · ".join(said)
