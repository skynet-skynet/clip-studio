"""Добавление видео в библиотеку: локальный файл или ссылка. Всё проходит через юридический шлюз."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from . import rights
from .config import Config, load_sources
from .db import DB, now
from .media import probe

log = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".ts"}
SUB_EXT = {".srt", ".vtt"}


class GateBlocked(Exception):
    pass


def _source(cfg: Config, source_id: str) -> dict | None:
    return rights.find_source(load_sources(cfg), source_id)


def import_local(
    cfg: Config, db: DB, file_path: Path, source_id: str, *, title: str = "", subs_path: Path | None = None,
    origin_url: str = "", license_confirmed: bool = False, move: bool = False,
) -> dict:
    """Импорт файла с диска. Для источников cc-by нужен origin_url и подтверждение лицензии."""
    file_path = Path(file_path)
    if file_path.suffix.lower() not in VIDEO_EXT:
        raise ValueError(f"Неподдерживаемый формат: {file_path.suffix}")
    source = _source(cfg, source_id)
    decision = rights.evaluate(source, user_confirmed_license=license_confirmed, origin_url=origin_url)
    if not decision.allowed:
        rights.record(db, None, source, origin_url or str(file_path.name), decision)
        raise GateBlocked(decision.reason)

    vid = db.insert(
        "videos", source_id=source_id, title=title or file_path.stem, origin_url=origin_url,
        license=decision.license, rights_status="cleared", rights_reason=decision.reason,
        status="ready", created_at=now(), channel=(source or {}).get("name", ""),
    )
    dest = cfg.path("videos") / f"{vid}{file_path.suffix.lower()}"
    (shutil.move if move else shutil.copyfile)(str(file_path), str(dest))
    fields: dict = {"local_path": str(dest)}
    if subs_path and Path(subs_path).suffix.lower() in SUB_EXT:
        sdest = cfg.path("videos") / f"{vid}{Path(subs_path).suffix.lower()}"
        shutil.copyfile(subs_path, sdest)
        fields["subs_path"] = str(sdest)
    try:
        info = probe(dest)
        fields.update(duration=info["duration"], width=info["width"], height=info["height"])
    except Exception as e:  # noqa: BLE001
        fields.update(status="error", status_message=f"Не удалось прочитать видео: {e}")
    db.update("videos", vid, **fields)
    rights.record(db, vid, source, origin_url or file_path.name, decision)
    return db.get("videos", vid)


# ---------- ссылки (yt-dlp) ----------

def _ytdlp():
    try:
        import yt_dlp  # type: ignore
    except ImportError as e:
        raise RuntimeError("Для загрузки по ссылке нужен yt-dlp: pip install yt-dlp") from e
    return yt_dlp


def check_url(cfg: Config, url: str, source_id: str) -> tuple[rights.GateDecision, dict, dict | None]:
    """Читает метаданные без скачивания и прогоняет через шлюз."""
    yt_dlp = _ytdlp()
    source = _source(cfg, source_id)
    if source and source.get("platform") not in (None, "", "other", "local") and rights.url_platform(url) not in (source.get("platform"), "other"):
        return rights.GateDecision(False, f"Ссылка ведёт на {rights.url_platform(url)}, а источник «{source.get('name')}» — {source.get('platform')}."), {}, source
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    platform = rights.url_platform(url)
    # Канал сверяем автоматически только на YouTube: там channel_id надёжен.
    # Для VK формат uploader_id у yt-dlp бывает разным, поэтому сверку делает человек при выборе источника.
    channel_id = info.get("channel_id") if platform == "youtube" else None
    decision = rights.evaluate(source, reported_license=info.get("license"), channel_id=channel_id, origin_url=url)
    return decision, info, source


def import_url(cfg: Config, db: DB, url: str, source_id: str, on_status=None) -> dict:
    """Проверяет права, затем скачивает лучшее качество (до 2160p) и субтитры, если есть."""
    decision, info, source = check_url(cfg, url, source_id)
    if not decision.allowed:
        rights.record(db, None, source, url, decision)
        raise GateBlocked(decision.reason)
    vid = db.insert(
        "videos", source_id=source_id, title=(info.get("title") or url)[:200], origin_url=url,
        license=decision.license, channel=info.get("channel") or info.get("uploader") or "",
        rights_status="cleared", rights_reason=decision.reason, status="downloading", created_at=now(),
    )
    rights.record(db, vid, source, url, decision)
    yt_dlp = _ytdlp()
    out_tmpl = str(cfg.path("videos") / f"{vid}.%(ext)s")
    lang = cfg["app"].get("language", "ru")

    def hook(d):
        if on_status and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                on_status(f"Загрузка {d.get('downloaded_bytes', 0) * 100 // total}%")

    opts = {
        "quiet": True, "noplaylist": True, "outtmpl": out_tmpl,
        "format": "bv*[height<=2160]+ba/b[height<=2160]/b",
        "merge_output_format": "mp4",
        "writesubtitles": True, "writeautomaticsub": True, "subtitleslangs": [lang], "subtitlesformat": "vtt",
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:  # noqa: BLE001
        db.update("videos", vid, status="error", status_message=f"Загрузка не удалась: {e}")
        return db.get("videos", vid)
    media = next((p for p in cfg.path("videos").glob(f"{vid}.*") if p.suffix.lower() in VIDEO_EXT), None)
    subs = next((p for p in cfg.path("videos").glob(f"{vid}*.vtt")), None)
    if not media:
        db.update("videos", vid, status="error", status_message="Файл после загрузки не найден")
        return db.get("videos", vid)
    info2 = probe(media)
    db.update(
        "videos", vid, local_path=str(media), subs_path=str(subs) if subs else "", status="ready",
        duration=info2["duration"], width=info2["width"], height=info2["height"], status_message="",
    )
    return db.get("videos", vid)
