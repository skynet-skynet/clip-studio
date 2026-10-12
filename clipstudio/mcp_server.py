"""MCP-сервер Clip Studio: Claude Code и Claude Desktop смотрят, режут и публикуют через инструменты.

Запуск: `python -m clipstudio mcp` (stdio). Регистрация: кнопка «Подключить к Claude» в настройках
или `python -m clipstudio connect-claude`.

Юридический шлюз работает и здесь: импорт идёт только из источников белого списка.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from . import ingest, llm
from .config import load_config, load_sources
from .db import DB
from .ingest import GateBlocked
from .media import probe
from .moments import find_phrase, fmt_time
from .pipeline import Pipeline
from .publish import publisher_statuses
from .transcribe import ensure_transcript, load_transcript
from .visual import contact_sheet

log = logging.getLogger(__name__)

INSTRUCTIONS = """Clip Studio — нарезка смешных моментов из видео, на которые у пользователя есть права.
Порядок: list_videos → transcript / search_transcript → look (кадры отрезка) → cut (рендер клипа) →
clip_preview (проверь результат глазами) → publish (по умолчанию private).
find_moments запускает автоматический поиск «редакцией менторов» (отдельные вызовы Claude, Sonnet).
Публикуй с privacy="public" только если пользователь прямо попросил опубликовать публично.
Новые видео добавляются только через import_file/import_url из источников белого списка (list_sources)."""


def _server_classes():
    try:  # mcp 2.x
        from mcp.server.mcpserver import Image, MCPServer
        return MCPServer, Image
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP, Image
        return FastMCP, Image


def build_server():
    Server, Image = _server_classes()
    cfg = load_config()
    db = DB(cfg.data_dir / "clipstudio.db")
    pipe = Pipeline(cfg, db)
    mcp = Server(name="clip-studio", instructions=INSTRUCTIONS)

    def _video(video_id: int) -> dict:
        v = db.get("videos", int(video_id))
        if not v:
            raise ValueError(f"Видео {video_id} не найдено. Список: list_videos")
        return v

    def _transcript(v: dict) -> dict:
        path = ensure_transcript(cfg, v)
        db.update("videos", v["id"], transcript_path=str(path))
        return load_transcript(path)

    @mcp.tool()
    def status() -> str:
        """Что настроено: Claude, ffmpeg, распознавание речи, площадки публикации."""
        from .media import ffmpeg_status
        from .transcribe import whisper_status

        return json.dumps({"llm": llm.status(cfg), "ffmpeg": ffmpeg_status(), "transcribe": whisper_status(cfg),
                           "publishers": publisher_statuses(cfg)}, ensure_ascii=False, indent=1)

    @mcp.tool()
    def list_sources() -> str:
        """Белый список источников с основаниями (own, permission, cc-by, public-domain)."""
        rows = [{k: s.get(k) for k in ("id", "name", "platform", "license", "enabled", "monetization_allowed", "rules")}
                for s in load_sources(cfg)]
        return json.dumps(rows, ensure_ascii=False, indent=1)

    @mcp.tool()
    def list_videos() -> str:
        """Библиотека видео: id, название, длительность, права, есть ли транскрипт."""
        rows = db.query("SELECT id, title, source_id, duration, width, height, rights_status, status, transcript_path FROM videos ORDER BY id DESC")
        for r in rows:
            r["duration"] = fmt_time(r["duration"] or 0)
            r["has_transcript"] = bool(r.pop("transcript_path"))
        return json.dumps(rows, ensure_ascii=False, indent=1)

    @mcp.tool()
    def import_file(path: str, source_id: str, subs_path: str = "", title: str = "", origin_url: str = "",
                    license_confirmed: bool = False) -> str:
        """Добавить видеофайл с диска. source_id — из list_sources. Для cc-by нужны origin_url и license_confirmed=true."""
        try:
            v = ingest.import_local(cfg, db, Path(path), source_id, title=title, subs_path=Path(subs_path) if subs_path else None,
                             origin_url=origin_url, license_confirmed=license_confirmed)
        except GateBlocked as e:
            return f"Заблокировано юридическим шлюзом: {e}"
        return f"Видео {v['id']} добавлено: {v['title']} ({fmt_time(v['duration'] or 0)}). Основание: {v['rights_reason']}"

    @mcp.tool()
    def import_url(url: str, source_id: str) -> str:
        """Скачать видео по ссылке после проверки прав (канал и лицензия сверяются с площадкой)."""
        try:
            v = ingest.import_url(cfg, db, url, source_id)
        except GateBlocked as e:
            return f"Заблокировано юридическим шлюзом: {e}"
        return f"Видео {v['id']}: {v['title']} — {v['status']} {v.get('status_message') or ''}"

    @mcp.tool()
    def transcript(video_id: int, start: float = 0, end: float = 0, max_lines: int = 400) -> str:
        """Транскрипт видео строками «[начало–конец] текст» (секунды). end=0 — до конца. Создаётся при первом вызове."""
        v = _video(video_id)
        t = _transcript(v)
        end = end or float(v.get("duration") or 1e9)
        lines = [f"[{s['start']:.1f}–{s['end']:.1f}] {s['text']}" for s in t["segments"] if s["end"] >= start and s["start"] <= end]
        more = len(lines) > max_lines
        text = "\n".join(lines[:max_lines])
        return text + (f"\n… ещё {len(lines) - max_lines} строк: запросите со start={t['segments'][0]['start']}" if more else "")

    @mcp.tool()
    def search_transcript(video_id: int, phrase: str) -> str:
        """Нечёткий поиск фразы по словам транскрипта (ё/е, регистр и пунктуация не важны)."""
        v = _video(video_id)
        found = find_phrase(_transcript(v), phrase, v["id"])
        if not found:
            return "Не найдено"
        return "\n".join(f"{c.start:.2f}–{c.end:.2f}: {c.quote}" for c in found)

    @mcp.tool()
    def look(video_id: int, start: float, end: float, frames: int = 6):
        """Посмотреть отрезок глазами: лист кадров, равномерно между start и end (до 16 кадров)."""
        v = _video(video_id)
        sheet, times = contact_sheet(cfg, Path(v["local_path"]), float(start), float(end), frames)
        caption = "Кадры слева направо, сверху вниз, на секундах: " + ", ".join(f"{t:.1f}" for t in times)
        return [caption, Image(path=str(sheet))]

    @mcp.tool()
    def find_moments(video_ids: list[int], request: str, count: int = 5, format: str = "vertical") -> str:
        """Автопоиск моментов редакцией менторов (Sonnet). Возвращает кандидатов; рендер — render_job."""
        jid = pipe.create_job(request, {"video_ids": video_ids, "count": count, "format": format})
        pipe.run_sync(jid, "analyze")
        return candidates(jid)

    @mcp.tool()
    def candidates(job_id: int) -> str:
        """Кандидаты задачи: id, оценка, время, название, хук, риски."""
        rows = db.query("SELECT id, video_id, start, \"end\", total, status, title, hook, quote, risk FROM candidates WHERE job_id = ? ORDER BY total DESC", (job_id,))
        job = db.get("jobs", job_id) or {}
        return json.dumps({"job_id": job_id, "status": job.get("status"), "stage": job.get("stage"), "candidates": rows},
                          ensure_ascii=False, indent=1)

    @mcp.tool()
    def render_job(job_id: int, candidate_ids: list[int]) -> str:
        """Одобрить кандидатов и отрендерить клипы."""
        for cid in candidate_ids:
            db.update("candidates", int(cid), status="approved")
        pipe.run_sync(job_id, "render")
        rows = db.query("SELECT id, title, path, duration FROM clips WHERE job_id = ? ORDER BY id", (job_id,))
        return json.dumps(rows, ensure_ascii=False, indent=1)

    @mcp.tool()
    def cut(video_id: int, start: float, end: float, title: str = "", hook: str = "", format: str = "vertical",
            reframe: str = "blur", subtitles: str = "karaoke", jumpcut: bool = False):
        """Вырезать клип [start, end] с оформлением: format vertical|horizontal|square, reframe blur|center|face,
        subtitles karaoke|plain|none, jumpcut — вырезать паузы. Возвращает клип и его превью."""
        jid = pipe.create_manual_clip(int(video_id), float(start), float(end), title=title, hook=hook,
                                      params={"format": format, "reframe": reframe, "subtitle_style": subtitles, "jumpcut": jumpcut})
        pipe.run_sync(jid, "render_manual")
        clip = (db.query("SELECT * FROM clips WHERE job_id = ? ORDER BY id DESC LIMIT 1", (jid,)) or [None])[0]
        if not clip:
            job = db.get("jobs", jid)
            return f"Рендер не удался: {job.get('error') or job.get('stage')}"
        text = f"Клип {clip['id']} готов: {clip['path']} ({clip['duration']:.1f} с, {clip['width']}×{clip['height']})"
        return [text, Image(path=clip["thumb"])] if clip.get("thumb") else text

    @mcp.tool()
    def clip_preview(clip_id: int, frames: int = 6):
        """Проверить готовый клип глазами: лист кадров по всей длине (хук, субтитры, кадрирование)."""
        clip = db.get("clips", int(clip_id))
        if not clip:
            return "Клип не найден"
        info = probe(Path(clip["path"]))
        sheet, times = contact_sheet(cfg, Path(clip["path"]), 0.0, info["duration"], frames, cols=min(frames, 6), width=240)
        return [f"Клип {clip_id}, кадры на секундах: " + ", ".join(f"{t:.1f}" for t in times), Image(path=str(sheet))]

    @mcp.tool()
    def review(clip_id: int) -> str:
        """Разбор клипа менторами (Opus): вердикт, заметки, варианты названий и хуков, подрезка."""
        try:
            return json.dumps(pipe.review_clip(int(clip_id)), ensure_ascii=False, indent=1)
        except llm.ManualNeeded as m:
            return "Ручной режим LLM. Промпт для разбора:\n" + m.prompt

    @mcp.tool()
    def publish(clip_id: int, platforms: list[str], privacy: str = "private") -> str:
        """Опубликовать клип: platforms из vk, youtube, telegram, pack. privacy private|unlisted|public.
        public — ТОЛЬКО если пользователь прямо попросил опубликовать публично."""
        if privacy not in ("private", "unlisted", "public"):
            return "privacy: private, unlisted или public"
        return json.dumps(pipe.publish_clip(int(clip_id), platforms, privacy), ensure_ascii=False, indent=1, default=str)

    @mcp.tool()
    def export(job_id: int, format: str = "edl") -> str:
        """Экспорт отрезков задачи: edl (DaVinci Resolve, Premiere) или csv (LosslessCut). Возвращает путь к файлу."""
        name, content = pipe.export_job(int(job_id), "csv" if format == "csv" else "edl")
        out = cfg.path("exports") / name
        out.write_text(content, encoding="utf-8")
        return f"Сохранено: {out}"

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    build_server().run("stdio")

