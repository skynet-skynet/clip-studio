@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Clip Studio

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python не найден. Установите Python 3.10+ с https://www.python.org/downloads/
  echo В установщике отметьте "Add python.exe to PATH", затем запустите start.bat снова.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск: создаю окружение...
  %PY% -m venv .venv || goto :fail
)

if not exist ".venv\installed.flag" (
  echo Ставлю зависимости, это займёт несколько минут...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
  echo ok> ".venv\installed.flag"
)

where ffmpeg >nul 2>nul || (
  echo.
  echo [!] ffmpeg не найден. Установите его командой в PowerShell:  winget install Gyan.FFmpeg
  echo     и перезапустите start.bat
  echo.
)

".venv\Scripts\python.exe" -m clipstudio
pause
exit /b 0

:fail
echo Что-то пошло не так. Пришлите текст ошибки выше в Claude Code или откройте docs\GUIDE.md.
pause
exit /b 1
