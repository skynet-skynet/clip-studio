"""Реестр публикаторов."""
from __future__ import annotations

from ..config import Config
from .base import Publisher
from .pack import PackPublisher
from .telegram import TelegramPublisher
from .vk import VKPublisher
from .youtube import YouTubePublisher

PUBLISHERS: dict[str, Publisher] = {
    "vk": VKPublisher(),
    "youtube": YouTubePublisher(),
    "telegram": TelegramPublisher(),
    "pack": PackPublisher(),
}


def get_publisher(name: str) -> Publisher | None:
    return PUBLISHERS.get(name)


def publisher_statuses(cfg: Config) -> list[dict]:
    out = []
    for name, pub in PUBLISHERS.items():
        ok, msg = pub.is_configured(cfg)
        out.append({"name": name, "title": pub.title, "enabled": bool(cfg.platform(name).get("enabled", False)),
                    "configured": ok, "message": msg})
    return out
