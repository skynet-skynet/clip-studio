"""Публикация в Telegram-канал через Bot API (sendVideo). Бот должен быть админом канала."""
from __future__ import annotations

import requests

from ..config import Config
from .base import PublishRequest, PublishResult

LIMIT_MB = 50       # лимит загрузки файла через стандартный Bot API
CAPTION = 1024


def build_caption(req: PublishRequest, limit: int = CAPTION) -> str:
    """Подпись: название, описание, атрибуция. Атрибуция не обрезается никогда."""
    attr = (req.attribution or "").strip()
    body_parts = [req.title.strip()] if req.title else []
    desc = (req.description or "").replace(attr, "").strip() if attr else (req.description or "").strip()
    if desc:
        body_parts.append(desc)
    body = "\n\n".join(body_parts)
    tail = f"\n\n{attr}" if attr else ""
    room = limit - len(tail)
    if len(body) > room:
        body = body[: max(room - 1, 0)].rstrip() + "…"
    return (body + tail).strip()


class TelegramPublisher:
    name = "telegram"
    title = "Telegram"

    def is_configured(self, cfg: Config) -> tuple[bool, str]:
        if not cfg.secret("TELEGRAM_BOT_TOKEN"):
            return False, "Нет TELEGRAM_BOT_TOKEN в .env (создайте бота у @BotFather)"
        pc = cfg.platform("telegram")
        if not pc.get("chat_id") and not pc.get("review_chat_id"):
            return False, "Укажите publish.platforms.telegram.chat_id (канал) в настройках"
        return True, f"Готово: канал {pc.get('chat_id') or '—'}"

    def publish(self, req: PublishRequest, cfg: Config) -> PublishResult:
        pc = cfg.platform("telegram")
        size_mb = req.clip_path.stat().st_size / 1024 / 1024
        if size_mb > LIMIT_MB:
            return PublishResult(self.name, False, "error", message=f"Файл {size_mb:.0f} МБ, а бот может отправить до {LIMIT_MB} МБ. Сделайте клип короче или увеличьте clips.crf.")
        public = req.privacy == "public"
        chat = pc.get("chat_id") if public else pc.get("review_chat_id")
        if not chat:
            return PublishResult(self.name, True, "skipped", message="Telegram публикует сразу. Для черновика укажите review_chat_id (ваш чат с ботом) или выберите «публично».")
        token = cfg.secret("TELEGRAM_BOT_TOKEN")
        data = {"chat_id": chat, "caption": build_caption(req), "supports_streaming": "true"}
        if req.duration:
            data["duration"] = int(round(req.duration))
        files = {}
        try:
            fh = open(req.clip_path, "rb")
            files["video"] = (req.clip_path.name, fh, "video/mp4")
            th = None
            if req.cover_path and req.cover_path.exists():
                th = open(req.cover_path, "rb")
                files["thumbnail"] = (req.cover_path.name, th, "image/jpeg")
            try:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendVideo", data=data, files=files, timeout=600)
                resp = r.json()
            finally:
                fh.close()
                if th:
                    th.close()
        except (requests.RequestException, ValueError, OSError) as e:
            return PublishResult(self.name, False, "error", message=f"Telegram недоступен: {type(e).__name__}")
        if not resp.get("ok"):
            return PublishResult(self.name, False, "error", message=f"Telegram: {resp.get('description', 'ошибка')}")
        msg_id = (resp.get("result") or {}).get("message_id")
        url = f"https://t.me/{str(chat).lstrip('@')}/{msg_id}" if str(chat).startswith("@") and msg_id else None
        return PublishResult(self.name, True, "published" if public else "draft", url=url, external_id=str(msg_id or ""),
                             message="Опубликовано в канал" if public else "Отправлено вам на просмотр")
