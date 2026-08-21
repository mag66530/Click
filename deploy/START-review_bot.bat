@echo off
REM Держим бота живым на своём Windows-компьютере: если процесс упал —
REM перезапускаем через 10 секунд, вместо того чтобы бот просто исчез.
REM
REM Использование: положите этот файл в корень репозитория (рядом с requirements.txt)
REM и запустите. Для автозапуска при включении компьютера — см.
REM deploy/README-деплой-бота-Windows.md, раздел про планировщик задач.

cd /d "%~dp0"

:loop
echo [%date% %time%] Запускаем review_bot...
call venv\Scripts\activate.bat
python -m bot.main

echo [%date% %time%] Бот остановился (код выхода %errorlevel%). Перезапуск через 10 секунд...
timeout /t 10 /nobreak
goto loop
