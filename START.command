#!/bin/bash
# ============================================================
#  Click launcher for macOS / Linux (Streamlit version)
#
#  Virtualenv and user data live outside the project folder,
#  so re-downloading the zip keeps the Yandex session,
#  cities and reports intact.
# ============================================================

cd "$(dirname "$0")" || exit 1

if [ "$(uname)" = "Darwin" ]; then
  CLICK_HOME="$HOME/Library/Application Support/Click"
else
  CLICK_HOME="$HOME/.local/share/click"
fi
VENV="$CLICK_HOME/venv"
PY="$VENV/bin/python"

echo
echo "  ==============================================="
echo "    Click - Yandex.Business poster"
echo "  ==============================================="
echo

BASEPY=""
command -v python3 >/dev/null 2>&1 && BASEPY=python3
[ -z "$BASEPY" ] && command -v python >/dev/null 2>&1 && BASEPY=python
if [ -z "$BASEPY" ]; then
  echo "[ERROR] Python 3 not found. Install it from https://www.python.org/downloads/"
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "[OK] Python detected: $($BASEPY --version 2>&1)"
echo

if [ ! -x "$PY" ]; then
  echo "[INFO] First run - creating environment in $VENV"
  mkdir -p "$CLICK_HOME"
  "$BASEPY" -m venv "$VENV" || { echo "[ERROR] Failed to create virtualenv."; read -r -p "Enter..."; exit 1; }
fi

# Re-install when a package is missing OR requirements.txt changed since the
# last install (a hash stamp is kept next to the venv). Without the stamp a
# package added to requirements.txt later (google-auth) was never installed.
STAMP="$CLICK_HOME/requirements.stamp"
NEED=""
"$PY" -c "import streamlit, playwright, google.auth" >/dev/null 2>&1 || NEED=1
if [ -z "$NEED" ]; then
  CUR=$("$PY" -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())" 2>/dev/null)
  [ -n "$CUR" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$CUR" ] || NEED=1
fi
if [ -n "$NEED" ]; then
  echo "[INFO] Installing/updating dependencies - this takes 1-3 minutes."
  "$PY" -m pip install --upgrade pip --quiet
  "$PY" -m pip install -r requirements.txt || { echo "[ERROR] Install failed."; read -r -p "Enter..."; exit 1; }
  "$PY" -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())" > "$STAMP"
  echo "[OK] Dependencies installed."
  echo
fi

if ! "$PY" -m playwright install --dry-run chromium >/dev/null 2>&1; then
  :
fi
echo "[INFO] Checking Playwright browser..."
"$PY" -m playwright install chromium >/dev/null 2>&1 || {
  echo "[WARN] Could not install Chromium automatically."
  echo "       Run manually: \"$PY\" -m playwright install chromium"
}

echo
echo "[INFO] Starting Click on http://localhost:8501"
echo "[INFO] Data folder: $CLICK_HOME/users-data"
echo "[INFO] To stop - press Ctrl+C."
echo
echo "--------------------------------------------------"
echo

"$PY" -m streamlit run streamlit_app.py --browser.gatherUsageStats false

echo
echo "[INFO] Click has stopped."
read -r -p "Press Enter to close..."
