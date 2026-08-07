"""
crosspost_probe.py — проба API ВКонтакте. СПРАВОЧНО, в текущем плане НЕ используется.

ВАЖНО (2026-08-07). ВК примерно с августа 2025 перестал регистрировать новые
приложения с доступом к постингу (`wall.post` требует типа «Standalone», а его
больше не выдают — в новой панели VK ID такого типа нет). Поэтому в проекте
принято решение вести ВК не через API, а через ВХОД С СОХРАНЕНИЕМ СЕССИИ, как
Яндекс/2ГИС в Click, с родной отложкой через «Таймер» на сайте (см.
ПОСТАНОВКА-Кросспостинг.md, редакция 7, метод «Сформировать отложку ВК»).

Этот файл оставлен как справочник и живой пример вызова API — на случай, если
ВК снова откроет регистрацию приложений. Тогда он делает ровно ОДИН отложенный
пост в сообществе (нужен пользовательский токен админа с правами
wall,photos,groups,offline).

КАК ПОЛЬЗОВАТЬСЯ (на СМУ):
  1. Получите токен ВК (пошагово — в ПОСТАНОВКА-Кросспостинг.md, «Приложение А»):
     Standalone-приложение на vk.com/apps?act=manage → права
     wall,photos,groups,offline → токен из адресной строки браузера.
  2. Запустите одной строкой (Windows — в том же окне, где стартует Click):
        set VK_TOKEN=vk1.a...ваш токен...
        python crosspost_probe.py
     macOS/Linux:
        VK_TOKEN="vk1.a...ваш токен..." python crosspost_probe.py
     По умолчанию — сообщество СМУ (id 217668235), текст-заглушка, без фото,
     время публикации через 15 минут.
  3. Откройте сообщество ВК → «Управление» → «Отложенные записи».
     Там должна появиться запись. Значит API работает — можно встраивать в Click.

ПЕРЕОПРЕДЕЛИТЬ УМОЛЧАНИЯ (необязательно) — тоже через переменные окружения:
  VK_GROUP_ID=217668235                      id сообщества (без минуса)
  VK_MINUTES=15                              через сколько минут опубликовать
  VK_TEXT="Проверка"                         текст поста
  VK_IMAGE_URL="https://i.ibb.co/....jpg"    если задано — прикрепит фото

Токен читается только из окружения и уходит только на api.vk.com. Скрипт
ничего не хранит и ничего в Click не меняет.
"""

from __future__ import annotations

import io
import os
import time

import requests

VK_API = "https://api.vk.com/method"
VK_VER = "5.199"          # версия API ВК; фиксируем, чтобы поведение не «поплыло»
TIMEOUT = 60


def _call(method: str, token: str, **params):
    """Один вызов метода ВК. Ошибку ВК превращаем в понятное сообщение и выходим."""
    params["access_token"] = token
    params["v"] = VK_VER
    data = requests.post(f"{VK_API}/{method}", data=params, timeout=TIMEOUT).json()
    if "error" in data:
        e = data["error"]
        raise SystemExit(
            f"ВК отказал в методе {method}: код {e.get('error_code')} — {e.get('error_msg')}\n"
            f"Частые причины: токен без нужных прав (нужны wall, photos, groups, offline), "
            f"или аккаунт токена не администратор этого сообщества."
        )
    return data["response"]


def _upload_photo(token: str, group_id: str, image_url: str) -> str:
    """
    Три шага загрузки фото на стену сообщества (иначе к посту его не прикрепить):
      1) спросить у ВК адрес для загрузки, 2) залить туда файл, 3) сохранить фото.
    Возвращает строку-вложение вида photo<owner>_<id> для wall.post.
    """
    srv = _call("photos.getWallUploadServer", token, group_id=group_id)
    img = requests.get(image_url, timeout=TIMEOUT)
    img.raise_for_status()
    up = requests.post(
        srv["upload_url"],
        files={"photo": ("photo.jpg", io.BytesIO(img.content))},
        timeout=TIMEOUT,
    ).json()
    if not up.get("photo") or up.get("photo") == "[]":
        raise SystemExit("ВК не принял файл (пустой ответ загрузки). Это точно картинка по ссылке?")
    saved = _call("photos.saveWallPhoto", token, group_id=group_id,
                  server=up["server"], photo=up["photo"], hash=up["hash"])
    ph = saved[0]
    return f"photo{ph['owner_id']}_{ph['id']}"


def main() -> None:
    token = os.environ.get("VK_TOKEN", "").strip()
    if not token:
        raise SystemExit('Нет токена. Запустите:  VK_TOKEN="ваш_токен" python crosspost_probe.py')

    group_id = os.environ.get("VK_GROUP_ID", "217668235").strip()   # СМУ по умолчанию
    minutes = int(os.environ.get("VK_MINUTES", "15"))
    text = os.environ.get("VK_TEXT", "Проверка кросспостинга Click — тестовая отложенная запись.")
    image_url = os.environ.get("VK_IMAGE_URL", "").strip()

    publish_ts = int(time.time()) + minutes * 60

    attachments = None
    if image_url:
        print(f"Гружу фото из {image_url} …")
        attachments = _upload_photo(token, group_id, image_url)
        print(f"  фото прикреплено: {attachments}")

    print(f"Создаю отложку в сообществе {group_id} на время +{minutes} мин …")
    extra = {"attachments": attachments} if attachments else {}
    resp = _call("wall.post", token, owner_id=f"-{group_id}", from_group=1,
                 message=text, publish_date=publish_ts, **extra)

    when = time.strftime("%H:%M:%S", time.localtime(publish_ts))
    print("\n✅ Готово. ВК принял отложенную запись.")
    print(f"   post_id = {resp.get('post_id')}, выйдет примерно в {when} "
          f"(время этого компьютера).")
    print("   Проверьте: сообщество → «Управление» → «Отложенные записи».")


if __name__ == "__main__":
    main()
