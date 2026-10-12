"""Оркестрация: задача «нарезка по запросу» от транскрипта до готовых клипов.

Одна фоновая очередь, задачи выполняются по одной — приложение не грузит компьютер параллельной работой.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import traceback
from pathlib import Path

from . import rights
from .config import Config, load_sources
from .db import DB, now
from .llm import MOMENTS_SCHEMA, REVIEW_SCHEMA, LLMError, complete_json, extract_json, load_prompt, manual_prompt
from .moments import (
    Candidate, ask_claude, build_chunks, build_user_prompt, dedupe, find_phrase, fmt_time, parse_moments,
    quoted_phrases, snap,
)
from .publish import get_publisher
from .publish.base import PublishRequest
from .media import extract_frame, probe
from .render import Segment, concat_files, render_clip
from .signals import compute_signals
from .transcribe import all_words, ensure_transcript, load_transcript

log = logging.getLogger(__name__)

DEFAULT_PARAMS = {
    "count": 5, "min_seconds": None, "max_seconds": None, "format": None, "reframe": None,
    "subtitle_style": None, "compilation": False, "auto": False, "video_ids": [], "jumpcut": None,
}


class Cancelled(Exception):
    pass


def job_params(cfg: Config, params: dict) -> dict:
    p = {**DEFAULT_PARAMS, **{k: v for k, v in (params or {}).items() if v is not None}}
    c = cfg["clips"]
    p["min_seconds"] = float(p["min_seconds"] or c["min_seconds"])
    p["max_seconds"] = float(p["max_seconds"] or c["max_seconds"])
    p["format"] = p["format"] or c["format"]
    p["reframe"] = p["reframe"] or c["reframe"]
    p["subtitle_style"] = p["subtitle_style"] or c["subtitle_style"]
    p["count"] = max(1, int(p["count"]))
    p["jumpcut"] = bool(c.get("jumpcut", False) if p.get("jumpcut") is None else p["jumpcut"])
    p["video_ids"] = [int(v) for v in p.get("video_ids") or []]
    return p


class Pipeline:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self._q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._loop, name="clipstudio-worker", daemon=True)
        self._worker.start()

    # ---------- очередь ----------
    def submit(self, job_id: int, action: str) -> None:
        self._q.put((job_id, action))

    def _loop(self) -> None:
        while True:
            job_id, action = self._q.get()
            try:
                self._actions()[action](job_id)
            except Cancelled:
                self.db.job_log(job_id, "Задача отменена", status="cancelled")
            except Exception as e:  # noqa: BLE001
                log.error("job %s failed: %s", job_id, traceback.format_exc())
                self.db.job_log(job_id, f"Ошибка: {e}", status="error", error=str(e))

    def _actions(self) -> dict:
        return {"analyze": self.analyze, "render": self.render, "finish_manual": self.finish_manual,
                "render_manual": self.render_manual}

    def run_sync(self, job_id: int, action: str) -> None:
        """Для CLI и MCP: выполнить шаг в текущем потоке."""
        self._actions()[action](job_id)

    # ---------- создание ----------
    def create_job(self, request: str, params: dict) -> int:
        p = job_params(self.cfg, params)
        if not p["video_ids"]:
            raise ValueError("Выберите хотя бы одно видео")
        for vid in p["video_ids"]:
            v = self.db.get("videos", vid)
            if not v:
                raise ValueError(f"Видео {vid} не найдено")
            if v["rights_status"] != "cleared":
                raise ValueError(f"Видео {vid} не прошло юридический шлюз: {v['rights_reason']}")
        return self.db.insert("jobs", request=request, params=p, status="queued", stage="В очереди",
                              created_at=now(), updated_at=now())

    def create_manual_clip(self, video_id: int, start: float, end: float, *, title: str = "", hook: str = "",
                           params: dict | None = None, request: str = "") -> int:
        """Ручная нарезка без модели: отрезок → задача с одним одобренным кандидатом. Возвращает id задачи."""
        if end - start < 0.5:
            raise ValueError("Отрезок слишком короткий")
        p = {**(params or {}), "video_ids": [video_id], "count": 1}
        jid = self.create_job(request or f"Ручная нарезка: {title or fmt_time(start)}", p)
        video = self.db.get("videos", video_id)
        end = min(end, float(video.get("duration") or end))
        self.db.insert("candidates", job_id=jid, video_id=video_id, start=round(start, 2), end=round(end, 2),
                       quote="", why="Ручная нарезка", scores={}, total=0, title=(title or "Клип")[:100], hook=hook[:60],
                       tags=[], risk="", origin="manual", status="approved", created_at=now())
        self.db.update("jobs", jid, status="rendering", stage="В очереди на рендер")
        return jid

    def render_manual(self, job_id: int) -> None:
        """Для ручных клипов: при необходимости сначала транскрипт (для субтитров), потом рендер."""
        job = self._check(job_id)
        p = job["params"]
        if p.get("subtitle_style") != "none":
            for vid in p["video_ids"]:
                video = self.db.get("videos", vid)
                try:
                    path = ensure_transcript(self.cfg, video, lambda f: self._progress(job_id, 0.4 * f, f"Транскрибация {int(f * 100)}%", "transcribing"))
                    self.db.update("videos", vid, transcript_path=str(path))
                except Exception as e:  # noqa: BLE001 — без транскрипта клип всё равно режется, просто без субтитров
                    self.db.job_log(job_id, f"Субтитров не будет: {e}")
        self.render(job_id)

    def _check(self, job_id: int) -> dict:
        job = self.db.get("jobs", job_id)
        if not job or job["status"] == "cancelled":
            raise Cancelled()
        return job

    def _progress(self, job_id: int, value: float, stage: str, status: str | None = None) -> None:
        fields = {"progress": round(max(0.0, min(1.0, value)), 3), "stage": stage, "updated_at": now()}
        if status:
            fields["status"] = status
        self.db.update("jobs", job_id, **fields)

    # ---------- анализ ----------
    def _prepare(self, job_id: int, params: dict) -> list[tuple[dict, dict, dict, dict | None]]:
        sources = load_sources(self.cfg)
        prepared = []
        n = len(params["video_ids"])
        for i, vid in enumerate(params["video_ids"]):
            self._check(job_id)
            video = self.db.get("videos", vid)
            base = 0.4 * i / n
            self._progress(job_id, base, f"Транскрибация: {video['title']}", "transcribing")
            path = ensure_transcript(
                self.cfg, video, lambda f, b=base: self._progress(job_id, b + 0.4 * f / n, f"Транскрибация {int(f * 100)}%"),
            )
            self.db.update("videos", vid, transcript_path=str(path))
            transcript = load_transcript(path)
            sig = compute_signals(self.cfg, video, transcript)
            prepared.append((video, transcript, sig, rights.find_source(sources, video["source_id"])))
            self.db.job_log(job_id, f"Транскрипт готов: {video['title']} ({len(transcript['segments'])} фраз, сигналов {len(sig['loud'])}+{len(sig['laugh'])})")
        return prepared

    def analyze(self, job_id: int) -> None:
        job = self._check(job_id)
        p = job["params"]
        self.db.job_log(job_id, f"Старт: «{job['request']}»", status="transcribing", error="")
        prepared = self._prepare(job_id, p)
        cands: list[Candidate] = []

        phrases = quoted_phrases(job["request"])
        for video, transcript, _sig, _src in prepared:
            for ph in phrases:
                found = find_phrase(transcript, ph, video["id"])
                cands.extend(found)
                if found:
                    self.db.job_log(job_id, f"Фраза «{ph}»: найдено {len(found)} в «{video['title']}»")

        manual = self.cfg["llm"]["mode"] == "manual"
        chunk_minutes = max(float(self.cfg["llm"]["chunk_minutes"]), 45.0) if manual else float(self.cfg["llm"]["chunk_minutes"])
        work = []
        for video, transcript, sig, src in prepared:
            chunks = build_chunks(transcript, sig, chunk_minutes)
            for idx, ch in enumerate(chunks, 1):
                work.append((video, src, ch, idx, len(chunks)))
        per_chunk = int(self.cfg["llm"]["max_candidates_per_chunk"])

        if manual:
            items = []
            for video, src, ch, idx, total in work:
                user = build_user_prompt(request=job["request"], params=p, video=video, source=src, chunk=ch,
                                         index=idx, total=total, per_chunk=per_chunk)
                prompt = manual_prompt(load_prompt("mentors"), user, MOMENTS_SCHEMA)
                items.append({"video_id": video["id"], "chunk": {"start": ch["start"], "end": ch["end"]}, "prompt": prompt})
            self.db.update("jobs", job_id, manual={"items": items, "answers": [], "index": 0,
                                                   "pre": [c.as_row() for c in cands]})
            self._progress(job_id, 0.45, f"Ждёт ответа Claude: кусок 1 из {len(items)}", "waiting_manual")
            self.db.job_log(job_id, "Ручной режим: скопируйте промпт в Claude и вставьте ответ")
            return

        cost = 0.0
        for k, (video, src, ch, idx, total) in enumerate(work):
            self._check(job_id)
            self._progress(job_id, 0.4 + 0.5 * k / max(len(work), 1),
                           f"Менторы смотрят «{video['title']}»: кусок {idx} из {total} ({fmt_time(ch['start'])}–{fmt_time(ch['end'])})",
                           "analyzing")
            user = build_user_prompt(request=job["request"], params=p, video=video, source=src, chunk=ch,
                                     index=idx, total=total, per_chunk=per_chunk)
            try:
                res = ask_claude(self.cfg, user)
            except LLMError as e:
                self.db.job_log(job_id, f"Кусок {idx}: {e}")
                continue
            cost += res.cost_usd
            found = parse_moments(res.data, video["id"], ch)
            cands.extend(found)
            note = res.data.get("notes") or ""
            self.db.job_log(job_id, f"Кусок {idx}/{total}: {len(found)} моментов{(' — ' + note[:120]) if note and not found else ''}")
        self.db.update("jobs", job_id, cost_usd=round(cost, 4))
        self._finalize(job_id, cands, prepared)

    def manual_state(self, job_id: int) -> dict:
        job = self.db.get("jobs", job_id)
        m = job.get("manual") or {}
        items = m.get("items") or []
        i = int(m.get("index", 0))
        if job["status"] != "waiting_manual" or i >= len(items):
            return {"prompt": "", "chunk": i, "chunks_total": len(items)}
        return {"prompt": items[i]["prompt"], "chunk": i + 1, "chunks_total": len(items)}

    def manual_answer(self, job_id: int, answer: str, enqueue: bool = True) -> dict:
        job = self.db.get("jobs", job_id)
        m = job.get("manual") or {}
        items = m.get("items") or []
        i = int(m.get("index", 0))
        if job["status"] != "waiting_manual" or i >= len(items):
            raise ValueError("Задача не ждёт ответа")
        data = extract_json(answer)  # бросит LLMError, если ответ не JSON
        m.setdefault("answers", []).append(data)
        m["index"] = i + 1
        self.db.update("jobs", job_id, manual=m)
        if m["index"] >= len(items):
            if enqueue:
                self.submit(job_id, "finish_manual")
            return {"ok": True, "next": False}
        self._progress(job_id, 0.45 + 0.4 * m["index"] / len(items), f"Ждёт ответа Claude: кусок {m['index'] + 1} из {len(items)}")
        return {"ok": True, "next": True}

    def finish_manual(self, job_id: int) -> None:
        job = self._check(job_id)
        m = job.get("manual") or {}
        cands = [Candidate(**{k: v for k, v in row.items() if k != "total"}) for row in m.get("pre", [])]
        for item, data in zip(m.get("items", []), m.get("answers", [])):
            cands.extend(parse_moments(data, item["video_id"], item["chunk"]))
        prepared = []
        sources = load_sources(self.cfg)
        for vid in job["params"]["video_ids"]:
            video = self.db.get("videos", vid)
            prepared.append((video, load_transcript(video["transcript_path"]), {}, rights.find_source(sources, video["source_id"])))
        self._finalize(job_id, cands, prepared)

    def _finalize(self, job_id: int, cands: list[Candidate], prepared: list) -> None:
        job = self._check(job_id)
        p = job["params"]
        by_video = {v["id"]: (v, t) for v, t, _s, _src in prepared}
        snapped = []
        for c in cands:
            video, transcript = by_video.get(c.video_id, (None, None))
            if not video:
                continue
            snapped.append(snap(c, transcript, self.cfg, float(video.get("duration") or 0), p["min_seconds"], p["max_seconds"]))
        final = dedupe(snapped)
        keep = max(p["count"] + 3, math.ceil(p["count"] * 1.5))
        final = final[:keep]
        self.db.execute("DELETE FROM candidates WHERE job_id = ? AND status = 'proposed'", (job_id,))
        ids = []
        for c in final:
            ids.append(self.db.insert("candidates", job_id=job_id, status="proposed", created_at=now(), **c.as_row()))
        self.db.job_log(job_id, f"Кандидатов: {len(final)} (из {len(cands)} найденных)")
        if not final:
            self._progress(job_id, 1.0, "Подходящих моментов не нашлось — уточните запрос", "review")
            return
        if p.get("auto"):
            approved = [i for i, c in zip(ids, final) if c.total >= 7][: p["count"]]
            for cid in approved:
                self.db.update("candidates", cid, status="approved")
            self.db.job_log(job_id, f"Авторежим: одобрено {len(approved)} с оценкой от 7")
            if approved:
                self._progress(job_id, 0.9, "Рендер", "rendering")
                self.render(job_id)
                return
        self._progress(job_id, 1.0, "Выберите моменты для рендера", "review")

    # ---------- рендер ----------
    def render(self, job_id: int) -> None:
        job = self._check(job_id)
        p = job["params"]
        cands = self.db.query("SELECT * FROM candidates WHERE job_id = ? AND status = 'approved' ORDER BY total DESC", (job_id,))
        if not cands:
            self._progress(job_id, 1.0, "Нет одобренных моментов", "review")
            return
        self._progress(job_id, 0.0, "Рендер", "rendering")
        sources = load_sources(self.cfg)
        groups = [cands] if p.get("compilation") else [[c] for c in cands]
        for gi, group in enumerate(groups):
            self._check(job_id)
            self._progress(job_id, gi / len(groups), f"Рендер клипа {gi + 1} из {len(groups)}")
            clip_id = self._render_group(job, group, sources)
            self.db.job_log(job_id, f"Клип {clip_id} готов")
        self._progress(job_id, 1.0, "Готово", "done")

    def _render_group(self, job: dict, group: list[dict], sources: list[dict]) -> int:
        p = job["params"]
        group = sorted(group, key=lambda c: (c["video_id"], c["start"])) if p.get("compilation") else group
        lead = max(group, key=lambda c: c["total"])
        attributions, monet = [], True
        out_dir = self.cfg.path("clips")
        clip_id = self.db.insert("clips", job_id=job["id"], candidate_ids=[c["id"] for c in group], created_at=now())
        out = out_dir / f"{clip_id}.mp4"
        parts: list[Path] = []
        for i, c in enumerate(group):
            video = self.db.get("videos", c["video_id"])
            src = rights.find_source(sources, video["source_id"])
            attr = rights.attribution_for(src, channel=video.get("channel") or "", url=video.get("origin_url") or "", title=video.get("title") or "")
            if attr and attr not in attributions:
                attributions.append(attr)
            if src and src.get("license") != "own" and not src.get("monetization_allowed"):
                monet = False
            tpath = video.get("transcript_path") or ""
            words = all_words(load_transcript(tpath)) if tpath and Path(tpath).exists() else []
            target = out if len(group) == 1 else out_dir / f"{clip_id}_part{i}.mp4"
            render_clip(
                self.cfg, video, [Segment(c["start"], c["end"])], words, target, fmt=p["format"], reframe=p["reframe"],
                sub_style=p["subtitle_style"], hook=(lead["hook"] if i == 0 and self.cfg["clips"].get("hook_title", True) else ""),
                attribution=(attr if self.cfg["clips"].get("attribution_overlay", True) else ""),
                jumpcut=bool(p.get("jumpcut")),
            )
            parts.append(target)
        if len(parts) > 1:
            concat_files(parts, out)
            for part in parts:
                part.unlink(missing_ok=True)
                part.with_suffix(".jpg").unlink(missing_ok=True)
        info = probe(out)
        thumb = out.with_suffix(".jpg")
        if not thumb.exists():
            extract_frame(out, info["duration"] * 0.5, thumb, width=540)
        tags = list(dict.fromkeys([t for c in group for t in (c.get("tags") or [])]))[:8]
        attribution = "\n".join(attributions)
        title = lead["title"] if len(group) == 1 else f"{lead['title']} и ещё {len(group) - 1}"
        desc_lines = [f"«{lead['quote']}»" if lead.get("quote") else "", attribution,
                      " ".join(f"#{t.replace(' ', '_')}" for t in tags)]
        self.db.update(
            "clips", clip_id, path=str(out), thumb=str(thumb), duration=info["duration"], width=info["width"],
            height=info["height"], title=title[:100], description="\n\n".join(x for x in desc_lines if x),
            tags=tags, attribution=attribution, monetization_allowed=monet,
        )
        for c in group:
            self.db.update("candidates", c["id"], status="rendered")
        return clip_id

    # ---------- экспорт в монтажки ----------
    def export_job(self, job_id: int, fmt: str) -> tuple[str, str]:
        """Возвращает (имя файла, содержимое) для EDL или CSV LosslessCut."""
        from .export import edl, losslesscut_csv

        job = self.db.get("jobs", job_id)
        if not job:
            raise ValueError("Задача не найдена")
        cands = self.db.query(
            "SELECT * FROM candidates WHERE job_id = ? AND status IN ('approved', 'rendered') ORDER BY video_id, start", (job_id,))
        if not cands:
            cands = self.db.query("SELECT * FROM candidates WHERE job_id = ? AND status != 'rejected' ORDER BY video_id, start", (job_id,))
        if not cands:
            raise ValueError("В задаче нет моментов для экспорта")
        items, fps = [], 30.0
        for c in cands:
            video = self.db.get("videos", c["video_id"])
            try:
                fps = probe(Path(video["local_path"]))["fps"] or fps
            except Exception:  # noqa: BLE001
                pass
            items.append({"file": video["local_path"], "start": c["start"], "end": c["end"], "name": c.get("title") or ""})
        if fmt == "csv":
            return f"job_{job_id}_losslesscut.csv", losslesscut_csv(items)
        return f"job_{job_id}.edl", edl(f"Clip Studio job {job_id}", items, fps)

    # ---------- разбор менторами (Opus) ----------
    def review_clip(self, clip_id: int) -> dict:
        clip = self.db.get("clips", clip_id)
        if not clip:
            raise ValueError("Клип не найден")
        job = self.db.get("jobs", clip["job_id"]) or {"request": "", "params": {}}
        cands = [self.db.get("candidates", cid) for cid in clip.get("candidate_ids") or []]
        cands = [c for c in cands if c]
        lines, offset = [], 0.0
        sources = load_sources(self.cfg)
        rules = []
        for c in cands:
            video = self.db.get("videos", c["video_id"])
            src = rights.find_source(sources, video["source_id"])
            if src and src.get("rules"):
                rules.append(src["rules"])
            tpath = video.get("transcript_path") or ""
            for w in (all_words(load_transcript(tpath)) if tpath and Path(tpath).exists() else []):
                if c["start"] <= w["start"] < c["end"]:
                    lines.append((round(w["start"] - c["start"] + offset, 1), w["word"]))
            offset += c["end"] - c["start"]
        text, cur, cur_t = [], [], None
        for t, w in lines:
            if cur_t is None:
                cur_t = t
            cur.append(w)
            if len(cur) >= 10 or w.endswith((".", "!", "?", "…")):
                text.append(f"[{cur_t:.1f}] {' '.join(cur)}")
                cur, cur_t = [], None
        if cur:
            text.append(f"[{cur_t:.1f}] {' '.join(cur)}")
        user = load_prompt("review").format(
            title=clip.get("title", ""), hook=cands[0]["hook"] if cands else "", duration=round(clip.get("duration") or 0, 1),
            format=(job.get("params") or {}).get("format", ""), description=clip.get("description", ""),
            attribution=clip.get("attribution") or "нет (собственное видео)",
            monetization="да" if clip.get("monetization_allowed") else "нет", source_rules="; ".join(rules) or "нет",
            request=job.get("request", ""), transcript="\n".join(text) or "(речи нет)",
        )
        res = complete_json(self.cfg, load_prompt("mentors"), user, REVIEW_SCHEMA, purpose="review")
        review = res.data
        self.db.update("clips", clip_id, review=review)
        return review

    def apply_review(self, clip_id: int, *, title: str | None = None, hook: str | None = None,
                     description: str | None = None, tags: list[str] | None = None, apply_trim: bool = False) -> dict:
        """Применяет правки менторов: название/описание/теги сразу, хук и подрезка — с перерендером."""
        clip = self.db.get("clips", clip_id)
        if not clip:
            raise ValueError("Клип не найден")
        fields = {k: v for k, v in (("title", title), ("description", description), ("tags", tags)) if v is not None}
        cand_ids = clip.get("candidate_ids") or []
        rerender = False
        if hook is not None and cand_ids:
            self.db.update("candidates", cand_ids[0], hook=hook)
            rerender = True
        trim = (clip.get("review") or {}).get("trim") or {}
        if apply_trim and cand_ids and (trim.get("start_delta") or trim.get("end_delta")):
            first, last = self.db.get("candidates", cand_ids[0]), self.db.get("candidates", cand_ids[-1])
            self.db.update("candidates", first["id"], start=round(max(0.0, first["start"] + float(trim.get("start_delta") or 0)), 2))
            last = self.db.get("candidates", cand_ids[-1])
            new_end = round(last["end"] + float(trim.get("end_delta") or 0), 2)
            if new_end > last["start"] + 1:
                self.db.update("candidates", last["id"], end=new_end)
            rerender = True
        if rerender:
            job = self.db.get("jobs", clip["job_id"])
            group = [self.db.get("candidates", cid) for cid in cand_ids]
            new_id = self._render_group(job, [c for c in group if c], load_sources(self.cfg))
            new_clip = self.db.get("clips", new_id)
            # переносим результат в старый id, чтобы ссылки и публикации не терялись
            for key in ("path", "thumb"):
                if clip.get(key):
                    Path(clip[key]).unlink(missing_ok=True)
            self.db.update("clips", clip_id, path=new_clip["path"], thumb=new_clip["thumb"], duration=new_clip["duration"],
                           width=new_clip["width"], height=new_clip["height"])
            self.db.delete("clips", new_id)
        fields.setdefault("review", {**(clip.get("review") or {}), "applied": True})
        self.db.update("clips", clip_id, **fields)
        return self.db.get("clips", clip_id)

    # ---------- публикация ----------
    def publish_clip(self, clip_id: int, platforms: list[str], privacy: str | None = None) -> list[dict]:
        clip = self.db.get("clips", clip_id)
        if not clip:
            raise ValueError("Клип не найден")
        privacy = privacy or self.cfg["publish"].get("default_privacy", "private")
        results = []
        for name in platforms:
            pub = get_publisher(name)
            if pub is None:
                results.append({"platform": name, "status": "error", "message": "Неизвестная площадка"})
                continue
            req = PublishRequest(
                clip_id=clip_id, clip_path=Path(clip["path"]), title=clip.get("title") or "", description=clip.get("description") or "",
                tags=clip.get("tags") or [], cover_path=Path(clip["thumb"]) if clip.get("thumb") else None,
                vertical=(clip.get("height") or 0) > (clip.get("width") or 0), duration=float(clip.get("duration") or 0),
                privacy=privacy, attribution=clip.get("attribution") or "",
                monetization_allowed=bool(clip.get("monetization_allowed")),
            )
            ok, msg = pub.is_configured(self.cfg)
            if not ok:
                res_row = {"platform": name, "status": "error", "url": "", "external_id": "", "message": msg}
            else:
                r = pub.publish(req, self.cfg)
                res_row = {"platform": name, "status": r.status, "url": r.url or "", "external_id": r.external_id or "", "message": r.message}
            pid = self.db.insert("publications", clip_id=clip_id, created_at=now(), **res_row)
            results.append(self.db.get("publications", pid))
        return results
