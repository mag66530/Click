@echo off
rem ============================================================
rem  Click launcher for Windows (Streamlit version)
rem  Only ASCII in control flow to avoid encoding issues.
rem
rem  Virtualenv and user data live in %LOCALAPPDATA%\Click,
rem  NOT inside the project folder. So re-downloading the zip
rem  keeps the Yandex session, cities and reports intact,
rem  and does not reinstall dependencies every time.
rem ============================================================

title Click

chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "CLICK_HOME=%LOCALAPPDATA%\Click"
set "VENV=%CLICK_HOME%\venv"
set "PY=%VENV%\Scripts\python.exe"

echo.
echo   ===============================================
echo     Click - Yandex.Business poster
echo   ===============================================
echo.

rem ---------- Find Python ----------
set "BASEPY="
where python >nul 2>&1
if not errorlevel 1 set "BASEPY=python"
if not defined BASEPY (
    where py >nul 2>&1
    if not errorlevel 1 set "BASEPY=py -3"
)
if not defined BASEPY (
    echo [ERROR] Python is not installed or not in PATH.
    echo.
    echo   Install Python 3.10 or newer from:
    echo   https://www.python.org/downloads/
    echo.
    echo   IMPORTANT: tick "Add Python to PATH" during install,
    echo   then RESTART this window and try again.
    echo.
    pause
    exit /b 1
)

echo [OK] Python detected:
%BASEPY% --version
echo.

rem ---------- Create virtualenv if missing ----------
if not exist "%PY%" (
    echo [INFO] First run - creating environment in %VENV%
    if not exist "%CLICK_HOME%" mkdir "%CLICK_HOME%"
    %BASEPY% -m venv "%VENV%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtualenv.
        pause
        exit /b 1
    )
)

rem ---------- Install dependencies if missing ----------
"%PY%" -c "import streamlit, playwright" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dependencies - this takes 1-3 minutes.
    echo        Please wait, do NOT close this window.
    echo.
    "%PY%" -m pip install --upgrade pip --quiet
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install dependencies.
        echo        Check your internet connection and try again.
        pause
        exit /b 1
    )
    echo.
    echo [OK] Dependencies installed.
    echo.
)

rem ---------- Install browser for Playwright if missing ----------
set "PWCACHE=%USERPROFILE%\AppData\Local\ms-playwright"
if not exist "%PWCACHE%" goto install_browser
dir /b "%PWCACHE%\chromium*" >nul 2>&1
if errorlevel 1 goto install_browser
goto browser_ok

:install_browser
echo [INFO] Installing Chromium for Playwright.
echo        Downloading about 150 MB, takes 1-3 minutes.
echo.
"%PY%" -m playwright install chromium
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install Chromium.
    echo          Possible reasons: no internet, corporate firewall, antivirus.
    pause
    exit /b 1
)
echo [OK] Chromium installed.
echo.

:browser_ok

rem ---------- Start ----------
echo [INFO] Starting Click on http://localhost:8501
echo [INFO] Data folder: %CLICK_HOME%\users-data
echo [INFO] To stop - close this window or press Ctrl+C.
echo.
echo --------------------------------------------------
echo.

"%PY%" -m streamlit run streamlit_app.py --browser.gatherUsageStats false

echo.
echo --------------------------------------------------
echo [INFO] Click has stopped.
pause
