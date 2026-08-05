"""
paths.py – где Click хранит данные.

Папка проекта может пересоздаваться (скачали новый zip – новая папка), поэтому
личные данные там держать нельзя: пропадут сессия Яндекса, города и отчёты.
Поэтому по умолчанию данные лежат ВНЕ проекта:

    Windows  %LOCALAPPDATA%\\Click\\users-data
    macOS    ~/Library/Application Support/Click/users-data
    Linux    ~/.local/share/click/users-data

Исключения (в порядке приоритета):
  1. переменная окружения CLICK_DATA_DIR – если задана, побеждает всё;
  2. папка users-data ВНУТРИ проекта – если она уже есть, работаем с ней
     (старые установки продолжают работать как раньше, ничего не переезжает).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Метка сборки: streamlit_app сверяет её и перезагружает модуль при расхождении.
BUILD = "2026-08-06-kp-audit"

PROJECT_ROOT = Path(__file__).parent


def data_root() -> Path:
    env = (os.environ.get("CLICK_DATA_DIR") or "").strip()
    if env:
        return Path(env).expanduser()

    local = PROJECT_ROOT / "users-data"
    if local.exists():
        return local

    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "Click" / "users-data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Click" / "users-data"
    return Path.home() / ".local" / "share" / "click" / "users-data"


def describe() -> str:
    """Человеческое описание – показываем в «Настройках», чтобы было понятно, где данные."""
    root = data_root()
    if (os.environ.get("CLICK_DATA_DIR") or "").strip():
        return f"{root}  (задано переменной CLICK_DATA_DIR)"
    if root == PROJECT_ROOT / "users-data":
        return f"{root}  (внутри папки проекта – старая установка)"
    return f"{root}  (вне папки проекта: обновление Click данные не затронет)"
