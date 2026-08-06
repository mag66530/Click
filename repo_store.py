"""
repo_store.py – данные, которые должны пережить перезапуск приложения.

Зачем. В облаке файловая система временная: всё, что приложение записало рядом
с собой, пропадает при перезапуске контейнера. А окончания постов и почта
Яндекса – ровно то, что заказчик правит руками и терять не должен.

Как. Храним такие данные в самом репозитории через GitHub API. Тогда
«сохранить» в приложении = коммит в репозиторий, и при любом перезапуске всё
подтягивается обратно. Заодно видна история: кто, когда и что поменял, и можно
откатиться.

Почему отдельная ветка. Пуш в main заставил бы Streamlit Cloud пересобрать и
перезапустить приложение прямо в момент сохранения. Поэтому пишем в ветку
click-data – её облако не отслеживает, сохранение мгновенное и без перезапуска.

Что нужно один раз настроить: секрет github_token – точечный токен
(fine-grained) с правом Contents: Read and write ТОЛЬКО на этот репозиторий.
Без токена модуль работает по-старому, через локальный файл: на своём
компьютере это полноценно, в облаке – до перезапуска.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

import paths

# Метка сборки: streamlit_app сверяет её и перезагружает модуль при расхождении.
# Метка сборки – одна на всё приложение, лежит в build.py. Держать её
# в каждом модуле своей строкой уже ломалось: у одного файла осталось
# старое значение, и Click принялся перезагружать все модули на каждое
# нажатие, пока не выбило по памяти.
from build import BUILD  # noqa: F401

API = "https://api.github.com"
BRANCH = "click-data"           # ветка под данные; облако её не отслеживает
DIR = "app-data"                # папка внутри ветки
DEFAULT_REPO = "mag66530/Click"
TIMEOUT = 20
CACHE_TTL = 60                  # секунд; правки видны почти сразу, но без спама в API

_cache: dict[str, tuple[float, dict | None, str | None]] = {}


# ─── Настройки доступа ──────────────────────────────────────────────
def _secret(key: str) -> str:
    v = (os.environ.get(key) or os.environ.get(key.upper()) or "").strip()
    if v:
        return v
    try:
        import streamlit as st
        v = str(st.secrets.get(key) or "").strip()
        if v:
            return v
    except Exception:
        pass
    try:                                   # вписанное в «Настройках»
        import secrets_local
        return secrets_local.get(key)
    except Exception:  # noqa: BLE001
        return ""


def token() -> str:
    return _secret("github_token")


def repo() -> str:
    """owner/name. Из секрета, иначе из origin, иначе известный по умолчанию."""
    v = _secret("github_repo")
    if v:
        return v.strip("/")
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"],
                             cwd=Path(__file__).parent, capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if url:
            url = url.removesuffix(".git")
            parts = [p for p in url.replace(":", "/").split("/") if p]
            if len(parts) >= 2:
                return f"{parts[-2]}/{parts[-1]}"
    except Exception:
        pass
    return DEFAULT_REPO


def is_configured() -> bool:
    return bool(token())


def where() -> str:
    """Понятное человеку описание, где лежат данные."""
    return f"репозиторий {repo()}, ветка {BRANCH}" if is_configured() else "локальный файл"


_checked: tuple[bool, str] | None = None


def check() -> tuple[bool, str]:
    """
    Настоящая проверка доступа, а не «токен вписан». Один запрос к GitHub,
    результат запоминаем на время жизни процесса.
    """
    global _checked
    if _checked is not None:
        return _checked
    if not is_configured():
        _checked = (False, "секрет github_token не задан")
        return _checked
    try:
        r = _api("GET", f"/repos/{repo()}")
        if r.status_code != 200:
            _checked = (False, _explain(r.status_code))
        elif not ((r.json() or {}).get("permissions") or {}).get("push"):
            _checked = (False, "токен видит репозиторий, но не может писать – "
                               "нужно право Contents: Read and write")
        else:
            _checked = (True, f"есть доступ на запись в {repo()}")
    except Exception as e:  # noqa: BLE001
        _checked = (False, f"не удалось связаться с GitHub: {e}")
    return _checked


# ─── Локальный запасной путь ────────────────────────────────────────
def _local_path(name: str) -> Path:
    fp = paths.data_root() / "store" / f"{name}.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    return fp


def _local_load(name: str) -> dict | None:
    fp = _local_path(name)
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


def _local_save(name: str, data: dict) -> None:
    tmp = _local_path(name).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_local_path(name))


# ─── GitHub ─────────────────────────────────────────────────────────
def _headers() -> dict:
    return {"Authorization": f"Bearer {token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _api(method: str, path: str, **kw):
    import requests
    return requests.request(method, f"{API}{path}", headers=_headers(), timeout=TIMEOUT, **kw)


def _ensure_branch() -> None:
    """Ветка под данные может ещё не существовать – заводим от ветки по умолчанию."""
    r = _api("GET", f"/repos/{repo()}/git/ref/heads/{BRANCH}")
    if r.status_code == 200:
        return
    if r.status_code != 404:
        raise RuntimeError(f"GitHub вернул HTTP {r.status_code} при проверке ветки {BRANCH}")
    info = _api("GET", f"/repos/{repo()}")
    if info.status_code != 200:
        raise RuntimeError(_explain(info.status_code))
    base = (info.json() or {}).get("default_branch") or "main"
    head = _api("GET", f"/repos/{repo()}/git/ref/heads/{base}")
    if head.status_code != 200:
        raise RuntimeError(f"Не нашли ветку {base}: HTTP {head.status_code}")
    sha = ((head.json() or {}).get("object") or {}).get("sha")
    made = _api("POST", f"/repos/{repo()}/git/refs",
                json={"ref": f"refs/heads/{BRANCH}", "sha": sha})
    if made.status_code not in (200, 201):
        raise RuntimeError(f"Не удалось создать ветку {BRANCH}: HTTP {made.status_code}")


def _explain(code: int) -> str:
    if code == 401:
        return "GitHub не принял токен (401). Проверьте секрет github_token."
    if code == 403:
        return ("GitHub отказал (403). У токена должно быть право Contents: "
                f"Read and write на репозиторий {repo()}.")
    if code == 404:
        return (f"GitHub не нашёл репозиторий {repo()} (404). Точечный токен видит только "
                "те репозитории, которые ему явно разрешили.")
    return f"GitHub вернул HTTP {code}"


def _remote_load(name: str) -> tuple[dict | None, str | None]:
    r = _api("GET", f"/repos/{repo()}/contents/{DIR}/{name}.json", params={"ref": BRANCH})
    if r.status_code == 404:
        return None, None
    if r.status_code != 200:
        raise RuntimeError(_explain(r.status_code))
    body = r.json() or {}
    try:
        raw = base64.b64decode(body.get("content") or "").decode("utf-8")
        return json.loads(raw), body.get("sha")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Файл {name}.json в репозитории испорчен: {e}") from e


def _remote_save(name: str, data: dict, message: str) -> None:
    _ensure_branch()
    _, sha = _remote_load(name)
    payload = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    r = _api("PUT", f"/repos/{repo()}/contents/{DIR}/{name}.json", json=payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(_explain(r.status_code))


# ─── Публичный интерфейс ────────────────────────────────────────────
def load(name: str) -> dict | None:
    """
    Данные по имени. Сначала репозиторий (если настроен), при недоступности –
    локальная копия. None – ничего не сохранено, работают значения по умолчанию.
    """
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    data = None
    if is_configured():
        try:
            data, _ = _remote_load(name)
        except Exception:
            data = None                      # сеть/токен подвели – ниже возьмём локальное
    if data is None:
        data = _local_load(name)
    _cache[name] = (time.time(), data, None)
    return data


def save(name: str, data: dict, message: str = "") -> str:
    """Сохранить. Возвращает человекочитаемое «куда сохранили». Бросает RuntimeError."""
    _local_save(name, data)                  # локально пишем всегда – это и кэш, и запасной путь
    _cache.pop(name, None)
    if not is_configured():
        return ("сохранено в файл на этом компьютере. В облаке такие правки пропадут при "
                "перезапуске – добавьте секрет github_token, чтобы они хранились в репозитории")
    _remote_save(name, data, message or f"Click: обновлены данные {name}")
    return f"сохранено в {where()}"


def forget(name: str) -> None:
    _cache.pop(name, None)
