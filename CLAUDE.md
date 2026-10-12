# Clip Studio — инструкции для Claude Code

Локальное приложение: находит смешные моменты в видео, на которые у пользователя есть права, режет клипы и публикует их.
Язык общения с пользователем — русский.

## Жёсткие правила
1. **Юридический шлюз не обходится.** Видео берётся только из источника в `sources.yaml` с основанием
   own / permission / cc-by / public-domain. Никогда не скачивай видео мимо `python -m clipstudio fetch`
   и не предлагай добавить в белый список источник без реального разрешения. Если основания нет — так и скажи
   и предложи шаблон письма автору (см. docs/GUIDE.md, раздел «Права»).
2. **Публикация по умолчанию приватная.** `--privacy public` — только если пользователь прямо попросил опубликовать публично
   в этом разговоре.
3. **Секреты не читать и не выводить.** Не открывай `.env` и `secrets/`. Ключи сохраняются командой
   `python -m clipstudio secret ИМЯ` (ввод скрыт) или в GUI.
4. Если источник запретил монетизацию (`monetization_allowed: false`), не советуй её включать для этого клипа.

## Команды (всё через CLI, вывод — JSON или таблица)
```
python -m clipstudio status                       # что настроено
python -m clipstudio sources | videos             # белый список и библиотека
python -m clipstudio import ФАЙЛ --source ID [--subs файл.srt] [--title ...]
python -m clipstudio fetch ССЫЛКА --source ID     # проверка прав → загрузка
python -m clipstudio discover "запрос" [--source ID]
python -m clipstudio job "запрос" --videos 1,2 --count 5 [--format vertical] [--compilation] [--auto]
python -m clipstudio candidates JOB | approve ID... [--reject] | render JOB
python -m clipstudio review CLIP                  # разбор менторами (Opus)
python -m clipstudio publish CLIP --to vk,youtube,telegram,pack [--privacy private]
python -m clipstudio manual JOB [--answer файл]   # ручной режим LLM
python -m clipstudio cut VIDEO START END [--title ..] [--hook ..] [--jumpcut]   # ручной клип
python -m clipstudio export JOB --format edl|csv  # в DaVinci Resolve / Premiere или LosslessCut
python -m clipstudio mcp                          # MCP-сервер (stdio)
python -m clipstudio connect-claude [--desktop]   # подключить MCP к Claude Code / Desktop
python -m clipstudio serve                        # GUI на http://127.0.0.1:8765
```
Если подключён MCP-сервер `clip-studio`, удобнее работать его инструментами: `look` и `clip_preview` дают кадры,
то есть видео можно проверять глазами.
На Windows вместо `python` может быть `py`.

## Как устроено
- `clipstudio/rights.py` — шлюз и реестр прав; `ingest.py` — импорт; `transcribe.py` — faster-whisper или .srt/.vtt
- `moments.py` + `prompts/mentors.md` — поиск моментов «редакцией из четырёх менторов» (Sonnet); `prompts/review.md` — разбор готового клипа (Opus)
- `render.py` + `subtitles.py` — ffmpeg: кадрирование 9:16, субтитры с подсветкой, хук, атрибуция, громкость −14 LUFS
- `publish/` — VK, YouTube, Telegram и пакет для ручной загрузки (Rutube, Дзен, VK Клипы, OK)
- `server.py` + `web/` — локальный GUI (`web/editor.js` — монтажный стол); `docs/API.md` — контракт API
- `mcp_server.py` — MCP-инструменты; `integrations.py` — регистрация в Claude Code / Desktop; `visual.py` — кадры и волна
- `export.py` — EDL и CSV; `plugin/` — плагин Claude Code со скиллами; `docs/manual.html` — методичка
- `packaging/` и `.github/workflows/` — сборка ClipStudio.exe
- Тесты: `python -m pytest -q`

## При доработке кода
- Сообщения для пользователя — по-русски, идентификаторы — по-английски.
- Не добавляй тяжёлых зависимостей в обязательные: всё необязательное импортируется лениво.
- После изменений запускай `python -m pytest -q`.
