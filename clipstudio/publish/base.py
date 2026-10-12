"""Контракт публикаторов. Каждая площадка — модуль с классом, реализующим Publisher.

Правила для всех публикаторов:
- никогда не публиковать публично, если privacy не равен "public" явно;
- атрибуция (req.attribution) всегда попадает в описание;
- ошибки не бросать наружу: возвращать PublishResult(ok=False, status="error", message=...);
- секреты брать только через cfg.secret("ИМЯ") и никогда не логировать.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..config import Config


@dataclass
class PublishRequest:
    clip_id: int
    clip_path: Path
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    cover_path: Path | None = None
    vertical: bool = True
    duration: float = 0.0
    privacy: str = "private"          # private | unlisted | public
    attribution: str = ""             # «Источник: …» — обязательно в описании
    monetization_allowed: bool = False


@dataclass
class PublishResult:
    platform: str
    ok: bool
    status: str                        # published | draft | pack_ready | error | skipped
    url: str | None = None
    external_id: str | None = None
    message: str = ""


class Publisher(Protocol):
    name: str          # "vk" | "youtube" | "telegram" | "pack"
    title: str         # человекочитаемое имя для GUI

    def is_configured(self, cfg: Config) -> tuple[bool, str]:
        """(готов ли, пояснение что не так / что настроено)."""
        ...

    def publish(self, req: PublishRequest, cfg: Config) -> PublishResult:
        ...


def full_description(req: PublishRequest) -> str:
    """Описание + атрибуция, если её ещё нет в тексте."""
    desc = (req.description or "").strip()
    attr = (req.attribution or "").strip()
    if attr and attr not in desc:
        desc = f"{desc}\n\n{attr}" if desc else attr
    return desc
