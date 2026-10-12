#!/usr/bin/env bash
# Clip Studio — запуск на macOS и Linux
set -e
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "Python не найден. macOS: brew install python   Ubuntu: sudo apt install python3 python3-venv"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Первый запуск: создаю окружение..."
  "$PY" -m venv .venv
fi

if [ ! -f ".venv/installed.flag" ]; then
  echo "Ставлю зависимости, это займёт несколько минут..."
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/installed.flag
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo
  echo "[!] ffmpeg не найден. macOS: brew install ffmpeg   Ubuntu: sudo apt install ffmpeg"
  echo
fi

exec .venv/bin/python -m clipstudio "$@"
