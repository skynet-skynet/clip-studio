# Локальный HTTP API Clip Studio

Сервер: FastAPI на `http://127.0.0.1:8765`. GUI (`web/`) — статические файлы, которые сервер отдаёт по `/`.
Все ответы — JSON, ошибки — `{"detail": "текст ошибки"}` с кодом 4xx/5xx.
Медиа-файлы (клипы, превью, исходники) отдаются по `/media/<путь относительно data_dir>`.

## Статус

`GET /api/status`
```json
{
  "version": "0.1.0",
  "llm": {"mode": "claude_cli", "ready": true, "message": "Claude Code 2.1.296 найден", "model_moments": "sonnet", "model_review": "opus"},
  "ffmpeg": {"ready": true, "message": "ffmpeg 6.1"},
  "transcribe": {"ready": true, "message": "faster-whisper, модель small"},
  "publishers": [
    {"name": "vk", "title": "VK Видео", "enabled": true, "configured": false, "message": "Нет VK_ACCESS_TOKEN в .env"}
  ]
}
```

## Источники (белый список, sources.yaml)

- `GET /api/sources` → `{"sources": [Source]}`
- `POST /api/sources` (тело — Source без лишних полей) → `{"source": Source}`; если `id` существует — обновление
- `DELETE /api/sources/{id}` → `{"ok": true}`

Source:
```json
{"id": "me", "name": "Мои видео", "platform": "local|vk|youtube|other", "channel_url": "", "vk_owner_id": "",
 "youtube_channel_id": "", "license": "own|permission|cc-by|public-domain", "proof": "", "proof_date": "",
 "monetization_allowed": true, "attribution": "", "rules": "", "enabled": true}
```

## Видео (библиотека)

- `GET /api/videos` → `{"videos": [Video]}`
- `POST /api/videos/upload` — multipart: `file` (видео), `source_id`, необязательно `subs` (.srt/.vtt), `title` → `{"video": Video}`
- `POST /api/videos/url` — `{"url": "...", "source_id": "..."}` → `{"video": Video}` или 403 с причиной блокировки шлюзом. Загрузка идёт в фоне: статус в `Video.status`.
- `DELETE /api/videos/{id}` → `{"ok": true}`

Video:
```json
{"id": 3, "source_id": "me", "title": "Стрим 12.10", "origin_url": "", "media_url": "/media/videos/3.mp4",
 "duration": 3725.4, "width": 1920, "height": 1080,
 "rights_status": "cleared|blocked|pending", "rights_reason": "own: Я автор",
 "status": "downloading|ready|transcribing|transcribed|error", "status_message": "",
 "has_transcript": true, "created_at": "2026-10-12T14:00:00"}
```

## Поиск видео

`POST /api/discover` — `{"query": "смешные моменты", "source_ids": ["cc_by_youtube"], "limit": 15}` →
```json
{"results": [{"platform": "youtube", "video_id": "abc", "url": "https://...", "title": "...", "channel_title": "...",
  "duration_s": 600, "max_height": 1080, "views": 12000, "published_at": "2026-09-01", "license": "creativeCommon",
  "thumbnail": "https://...", "source_id": "cc_by_youtube", "gate": {"allowed": true, "reason": "CC BY по API"}}],
 "warnings": ["YOUTUBE_API_KEY не задан — поиск по YouTube пропущен"]}
```
Добавить найденное в библиотеку — тем же `POST /api/videos/url`.

## Задачи (нарезка по запросу)

`POST /api/jobs`
```json
{"request": "10 клипов, где он срывается на чат",
 "video_ids": [3, 4],
 "count": 10, "min_seconds": 12, "max_seconds": 60,
 "format": "vertical|horizontal|square", "reframe": "blur|center|face",
 "subtitle_style": "karaoke|plain|none",
 "compilation": false,
 "auto": false}
```
→ `{"job": Job}`. Задача сама проходит этапы: транскрибация → сигналы → поиск моментов (Claude) → `review` (ждёт одобрения) → рендер.
При `auto: true` кандидаты с оценкой ≥ 7 одобряются сами и сразу рендерятся (публикация всё равно ручная/черновиком).

- `GET /api/jobs` → `{"jobs": [Job]}` (без кандидатов)
- `GET /api/jobs/{id}` → `{"job": Job, "candidates": [Candidate], "clips": [Clip]}`
- `POST /api/jobs/{id}/render` → запускает рендер всех `approved` кандидатов (или склейку при `compilation`)
- `POST /api/jobs/{id}/cancel` → `{"ok": true}`

Job:
```json
{"id": 7, "request": "...", "params": {...}, "status": "queued|transcribing|analyzing|waiting_manual|review|rendering|done|error|cancelled",
 "progress": 0.42, "stage": "Поиск моментов: кусок 3 из 8", "log": ["..."], "created_at": "...", "error": ""}
```

Candidate:
```json
{"id": 51, "job_id": 7, "video_id": 3, "start": 812.4, "end": 841.9, "quote": "…", "why": "…",
 "scores": {"editor": 8, "comedian": 9, "retention": 7, "compliance": 10}, "total": 8.4,
 "title": "Когда чат знал раньше него", "hook": "ОН НЕ ОЖИДАЛ", "tags": ["стрим", "фейл"],
 "risk": "", "status": "proposed|approved|rejected|rendered", "preview_url": "/media/videos/3.mp4#t=812.4,841.9"}
```

- `POST /api/candidates/{id}` — `{"status": "approved|rejected", "start": 812.0, "end": 842.5, "title": "...", "hook": "..."}` (любые поля опциональны) → `{"candidate": Candidate}`

## Ручной режим LLM (manual: claude.ai / Claude Code web)

Когда `llm.mode = manual`, задача останавливается в статусе `waiting_manual`.
- `GET /api/jobs/{id}/manual` → `{"prompt": "полный текст для вставки в Claude", "chunk": 1, "chunks_total": 3}`
- `POST /api/jobs/{id}/manual` — `{"answer": "JSON-ответ Claude целиком"}` → `{"ok": true, "next": true}` (если кусков больше — отдаётся следующий)

## Клипы

- `GET /api/clips` → `{"clips": [Clip]}`
- `GET /api/clips/{id}` → `{"clip": Clip, "publications": [Publication]}`
- `POST /api/clips/{id}` — правка `title`, `description`, `tags` → `{"clip": Clip}`
- `POST /api/clips/{id}/review` → разбор готового клипа менторами (Opus): `{"review": {"verdict": "publish|fix|drop", "notes": ["…"], "title_options": ["…"]}}`
- `DELETE /api/clips/{id}` → `{"ok": true}`

Clip:
```json
{"id": 12, "job_id": 7, "candidate_ids": [51], "media_url": "/media/clips/12.mp4", "thumb_url": "/media/clips/12.jpg",
 "duration": 29.5, "width": 1080, "height": 1920, "title": "...", "description": "...", "tags": ["..."],
 "attribution": "Источник: …", "monetization_allowed": false, "created_at": "..."}
```

## Публикация

`POST /api/clips/{id}/publish` — `{"platforms": ["vk", "youtube", "telegram", "pack"], "privacy": "private|unlisted|public"}` →
`{"results": [Publication]}` (выполняется синхронно по очереди; для больших файлов может занять минуты)

- `GET /api/publications` → `{"publications": [Publication]}`

Publication:
```json
{"id": 4, "clip_id": 12, "platform": "vk", "status": "published|draft|pack_ready|error|skipped",
 "url": "https://vkvideo.ru/video-1_2", "external_id": "-1_2", "message": "", "created_at": "..."}
```

## Авторизация площадок

- `GET /api/auth/youtube/start` → `{"url": "https://accounts.google.com/..."}` — открыть в браузере; после входа токен сохранится сам
- `GET /api/auth/youtube/callback?...` — обрабатывает редирект Google
- `GET /api/auth/vk/start` → `{"url": "https://id.vk.ru/authorize?..."}` (нужен VK_CLIENT_ID)
- `GET /api/auth/vk/callback?...` — обрабатывает редирект VK ID и сохраняет токен в .env
- `POST /api/settings/secret` — `{"name": "TELEGRAM_BOT_TOKEN", "value": "..."}` → сохраняет в `.env` (только имена из белого списка)

## Настройки и гайд

- `GET /api/config` → `{"config": {...}}` (без секретов)
- `POST /api/config` — частичное обновление (тот же формат) → `{"config": {...}}`
- `GET /api/guide` → `{"markdown": "..."}` — содержимое docs/GUIDE.md
- `GET /api/ledger` → `{"rows": [...]}` — реестр прав (для экспорта и ответов на жалобы)
