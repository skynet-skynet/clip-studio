"""Юридический шлюз. Решает, можно ли брать видео в работу, и пишет решение в реестр прав.

Принцип: по умолчанию — запрет. Разрешено только то, что прямо покрыто белым списком sources.yaml:
  own            — вы автор;
  permission     — письменное разрешение автора (обязательны proof и proof_date);
  cc-by          — лицензия Creative Commons Attribution, подтверждённая площадкой или вами;
  public-domain  — общественное достояние.
Шлюз не заменяет юриста: он гарантирует, что у каждого клипа есть записанное основание.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .db import DB, now

ALLOWED_LICENSES = {"own", "permission", "cc-by", "public-domain"}
CC_BY_MARKERS = ("creativecommon", "creative commons", "cc-by", "cc by", "reuse allowed")


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    license: str = ""


def find_source(sources: list[dict], source_id: str) -> dict | None:
    return next((s for s in sources if str(s.get("id")) == str(source_id)), None)


def url_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(("youtube.com", "youtu.be")):
        return "youtube"
    if host.endswith(("vk.com", "vkvideo.ru", "vk.ru", "vkontakte.ru")):
        return "vk"
    if host.endswith("rutube.ru"):
        return "rutube"
    return "other"


def _is_cc_by(reported: str | None) -> bool:
    text = (reported or "").lower()
    if any(m in text for m in CC_BY_MARKERS):
        # CC BY-ND запрещает переработку — нарезка с субтитрами и монтажом это переработка
        return not any(x in text for x in ("-nd", " nd", "noderiv", "no deriv"))
    return False


def evaluate(
    source: dict | None,
    *,
    reported_license: str | None = None,
    channel_id: str | None = None,
    user_confirmed_license: bool = False,
    origin_url: str = "",
) -> GateDecision:
    """Главное правило шлюза. Возвращает решение и человеческое объяснение."""
    if not source:
        return GateDecision(False, "Источника нет в белом списке (sources.yaml). Добавьте его с основанием.")
    if not source.get("enabled", True):
        return GateDecision(False, f"Источник «{source.get('name')}» выключен в белом списке.")
    lic = str(source.get("license", "")).strip().lower()
    if lic not in ALLOWED_LICENSES:
        return GateDecision(False, f"Неизвестное основание «{lic}». Допустимо: own, permission, cc-by, public-domain.")

    # Сверяем канал, если в источнике он указан, а площадка его сообщила
    expected = str(source.get("youtube_channel_id") or source.get("vk_owner_id") or "").strip()
    if expected and channel_id and str(channel_id).strip() != expected and lic in {"own", "permission"}:
        return GateDecision(
            False,
            f"Видео с канала {channel_id}, а разрешение выдано каналу {expected}. Разрешение не переносится на чужие каналы.",
            lic,
        )

    if lic == "own":
        return GateDecision(True, f"own: {source.get('proof') or 'вы автор'}", lic)
    if lic == "public-domain":
        return GateDecision(True, f"public-domain: {source.get('proof') or 'общественное достояние'}", lic)
    if lic == "permission":
        proof = str(source.get("proof") or "").strip()
        if not proof:
            return GateDecision(False, "Основание «permission», но нет доказательства (поле proof). Сохраните скриншот или ссылку на разрешение.", lic)
        date = source.get("proof_date") or "дата не указана"
        return GateDecision(True, f"permission: {proof} ({date})", lic)
    # cc-by
    if _is_cc_by(reported_license):
        return GateDecision(True, f"cc-by: лицензия подтверждена площадкой ({reported_license})", lic)
    if user_confirmed_license and origin_url:
        return GateDecision(True, f"cc-by: лицензию подтвердили вы вручную, оригинал {origin_url}", lic)
    if reported_license:
        return GateDecision(False, f"Площадка сообщает лицензию «{reported_license}», а не CC BY. Брать нельзя.", lic)
    return GateDecision(False, "Лицензия CC BY не подтверждена. Укажите ссылку на оригинал и подтвердите лицензию.", lic)


def record(db: DB, video_id: int | None, source: dict | None, origin_url: str, decision: GateDecision) -> None:
    db.insert(
        "rights_ledger",
        video_id=video_id,
        source_id=(source or {}).get("id", ""),
        origin_url=origin_url,
        license=decision.license or (source or {}).get("license", ""),
        proof=str((source or {}).get("proof", "")),
        decision="allowed" if decision.allowed else "blocked",
        reason=decision.reason,
        checked_at=now(),
    )


def attribution_for(source: dict | None, *, channel: str = "", url: str = "", title: str = "") -> str:
    """Строка атрибуции для описания и титра. Для своих видео — пустая."""
    if not source or source.get("license") == "own":
        return ""
    template = str(source.get("attribution") or "").strip() or "Источник: {channel} — {url}"
    text = template.format(
        channel=channel or source.get("name", ""),
        url=url or source.get("channel_url", ""),
        title=title,
        name=source.get("name", ""),
    )
    if source.get("license") == "cc-by" and "CC" not in text:
        text += " (CC BY)"
    return text.strip().strip("—").strip()
