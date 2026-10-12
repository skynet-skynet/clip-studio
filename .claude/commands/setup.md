---
description: Проверить и донастроить Clip Studio на этом компьютере
---

Помоги пользователю настроить Clip Studio. Иди по шагам, после каждого проверяй результат.

1. Проверь Python (3.10+) и ffmpeg: `python --version`, `ffmpeg -version`. Если ffmpeg нет — дай команду установки для его ОС
   (Windows: `winget install Gyan.FFmpeg`; macOS: `brew install ffmpeg`; Ubuntu: `sudo apt install ffmpeg`).
2. Зависимости: `python -m pip install -r requirements.txt`. Для распознавания речи: `python -m pip install faster-whisper`.
   Для YouTube: `python -m pip install google-api-python-client google-auth-oauthlib`.
3. `python -m clipstudio status` — разбери каждый пункт, что не готово.
4. Ключи площадок сохраняй только так: `python -m clipstudio secret VK_ACCESS_TOKEN` (ввод скрыт). Сам `.env` не открывай.
   Как получить каждый ключ — в docs/GUIDE.md (разделы «VK», «YouTube», «Telegram»); перескажи нужный раздел своими словами.
5. Предложи запустить GUI: `python -m clipstudio` (откроется http://127.0.0.1:8765).
