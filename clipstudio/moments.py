"""Поиск моментов: точные фразы без модели + смысловой поиск менторами через Claude.

Итог — список кандидатов с границами, подогнанными к словам и паузам.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from .config import Config
from .llm import MOMENTS_SCHEMA, LLMResult, complete_json, load_prompt

WEIGHTS = {"comedian": 0.35, "editor": 0.25, "retention": 0.25, "compliance": 0.15}
QUOTE_RE = re.compile(r"[«\"“„']([^«»\"“”„']{2,120})[»\"”']")


@dataclass
class Candidate:
    video_id: int
    start: float
    end: float
    quote: str = ""
    why: str = ""
    scores: dict = field(default_factory=dict)
    title: str = ""
    hook: str = ""
    tags: list = field(default_factory=list)
    risk: str = ""
    origin: str = "llm"

    @property
    def total(self) -> float:
        return total_score(self.scores)

    def as_row(self) -> dict:
        return {
            "video_id": self.video_id, "start": round(self.start, 2), "end": round(self.end, 2),
            "quote": self.quote, "why": self.why, "scores": self.scores, "total": round(self.total, 2),
            "title": self.title, "hook": self.hook, "tags": self.tags, "risk": self.risk, "origin": self.origin,
        }


def total_score(scores: dict) -> float:
    if not scores:
        return 0.0
    s = sum(float(scores.get(k, 0)) * w for k, w in WEIGHTS.items())
    comp = float(scores.get("compliance", 10))
    if comp < 5:  # рискованные моменты уходят вниз списка
        s *= 0.5
    return round(s, 2)


# ---------- подготовка транскрипта ----------

def fmt_time(t: float) -> str:
    t = max(0, int(t))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_chunks(transcript: dict, signals: dict, chunk_minutes: float, overlap: float = 30.0) -> list[dict]:
    """Режет транскрипт на куски по ~chunk_minutes с перекрытием и размечает сигналы."""
    segs = transcript.get("segments", [])
    if not segs:
        return []
    loud = signals.get("loud", []) if signals else []
    laugh = set(signals.get("laugh", []) if signals else [])
    lines = []
    for s in segs:
        marks = ""
        if any(s["start"] - 0.5 <= t <= s["end"] + 1.0 for t in loud):
            marks += " ⚡"
        if round(float(s["start"]), 1) in laugh:
            marks += " 😂"
        lines.append((s["start"], s["end"], f"[{s['start']:.1f}–{s['end']:.1f}] {s['text']}{marks}"))
    size = chunk_minutes * 60
    chunks, start = [], lines[0][0]
    end_all = lines[-1][1]
    while start < end_all:
        stop = start + size
        part = [ln for ln in lines if ln[0] >= start - 0.01 and ln[0] < stop]
        if part:
            chunks.append({"start": part[0][0], "end": part[-1][1], "text": "\n".join(p[2] for p in part)})
        start = stop - overlap
    return chunks


# ---------- точные фразы ----------

def quoted_phrases(request: str) -> list[str]:
    return [m.group(1).strip() for m in QUOTE_RE.finditer(request or "") if len(m.group(1).split()) >= 1]


def _norm(w: str) -> str:
    return re.sub(r"[^\wё]", "", w.lower().replace("ё", "е"))


def find_phrase(transcript: dict, phrase: str, video_id: int, threshold: float = 0.8) -> list[Candidate]:
    """Нечёткий поиск фразы по словам транскрипта. Возвращает кандидатов вокруг каждого вхождения."""
    words = [w for s in transcript.get("segments", []) for w in s.get("words", [])]
    target = [_norm(w) for w in phrase.split() if _norm(w)]
    if not words or not target:
        return []
    normed = [_norm(w["word"]) for w in words]
    n = len(target)
    hits: list[int] = []
    for i in range(0, len(words) - n + 1):
        window = normed[i:i + n]
        ratio = difflib.SequenceMatcher(None, " ".join(window), " ".join(target)).ratio()
        if ratio >= threshold and (not hits or i - hits[-1] > n):
            hits.append(i)
    out = []
    for i in hits:
        start, end = words[i]["start"], words[i + n - 1]["end"]
        out.append(Candidate(
            video_id=video_id, start=start, end=end, quote=" ".join(w["word"] for w in words[i:i + n]),
            why=f"Точное совпадение с фразой «{phrase}»",
            scores={"editor": 6, "comedian": 6, "retention": 6, "compliance": 9},
            title=phrase[:70], hook=phrase.upper()[:40], tags=["нарезка"], origin="phrase",
        ))
    return out


# ---------- смысловой поиск через Claude ----------

def build_user_prompt(*, request: str, params: dict, video: dict, source: dict | None, chunk: dict,
                      index: int, total: int, per_chunk: int) -> str:
    fmt_names = {"vertical": "вертикальный 9:16", "horizontal": "горизонтальный 16:9", "square": "квадрат 1:1"}
    return load_prompt("find_moments").format(
        request=request.strip() or "Самые смешные моменты для коротких клипов",
        count=params.get("count", 5),
        per_chunk=per_chunk,
        min_seconds=params.get("min_seconds", 12),
        max_seconds=params.get("max_seconds", 60),
        format=fmt_names.get(params.get("format", "vertical"), params.get("format")),
        video_title=video.get("title") or f"видео {video['id']}",
        video_id=video["id"],
        video_duration=fmt_time(float(video.get("duration") or 0)),
        source_rules=(source or {}).get("rules") or "нет особых правил",
        chunk_index=index,
        chunk_total=total,
        chunk_range=f"{fmt_time(chunk['start'])}–{fmt_time(chunk['end'])}",
        transcript=chunk["text"],
    )


def parse_moments(data: dict, video_id: int, chunk: dict | None = None) -> list[Candidate]:
    out = []
    for m in data.get("moments", []) or []:
        try:
            start, end = float(m["start"]), float(m["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        if chunk and (end < chunk["start"] - 5 or start > chunk["end"] + 5):
            continue  # модель придумала время вне куска — отбрасываем
        scores = {k: max(0.0, min(10.0, float((m.get("scores") or {}).get(k, 0)))) for k in WEIGHTS}
        if scores["compliance"] < 4:
            continue
        out.append(Candidate(
            video_id=video_id, start=start, end=end, quote=str(m.get("quote", ""))[:300],
            why=str(m.get("why", ""))[:400], scores=scores, title=str(m.get("title", ""))[:100],
            hook=str(m.get("hook", ""))[:60], tags=[str(t).lstrip("#") for t in (m.get("tags") or [])][:8],
            risk=str(m.get("risk", "") or ""),
        ))
    return out


def ask_claude(cfg: Config, user_prompt: str) -> LLMResult:
    return complete_json(cfg, load_prompt("mentors"), user_prompt, MOMENTS_SCHEMA, purpose="moments")


# ---------- подгонка границ ----------

def snap(c: Candidate, transcript: dict, cfg: Config, duration: float, min_s: float, max_s: float) -> Candidate:
    """Подгоняет границы к словам: начало чуть раньше первого слова, конец — после реакции.

    Короткие клипы расширяются до соседних фраз, длинные — обрезаются по концу слова.
    """
    words = [w for s in transcript.get("segments", []) for w in s.get("words", [])]
    pad_b = float(cfg["clips"].get("pad_before", 0.35))
    pad_a = float(cfg["clips"].get("pad_after", 0.6))
    start, end = c.start, c.end
    if words:
        # первое слово, которое заканчивается после start
        first = next((w for w in words if w["end"] > start), words[0])
        start = min(start, first["start"])
        last = next((w for w in reversed(words) if w["start"] < end), words[-1])
        end = max(end, last["end"])
    start = max(0.0, start - pad_b)
    end = min(duration or end + pad_a, end + pad_a)

    segs = transcript.get("segments", [])
    if end - start < min_s and segs:
        # Расширяем по одной фразе, чередуя: сначала назад (завязка важнее), потом вперёд (реакция)
        before = [s for s in segs if s["end"] <= start + pad_b + 0.05]
        after = [s for s in segs if s["start"] >= end - pad_a - 0.05]
        back = True
        while end - start < min_s and (before or after):
            if back and before:
                s = before.pop()
                start = max(0.0, s["start"] - pad_b)
            elif after:
                s = after.pop(0)
                end = min(s["end"] + pad_a, duration or s["end"] + pad_a)
            elif before:
                s = before.pop()
                start = max(0.0, s["start"] - pad_b)
            back = not back
        if end - start < min_s:
            end = min(start + min_s, duration or start + min_s)
    if end - start > max_s:
        limit = start + max_s
        cut = [w["end"] for w in words if start < w["end"] <= limit - pad_a]
        end = (max(cut) + pad_a) if cut else limit
    c.start, c.end = round(start, 2), round(end, 2)
    return c


def dedupe(cands: list[Candidate], overlap: float = 0.5) -> list[Candidate]:
    """Убирает пересекающиеся кандидаты, оставляя более сильный."""
    kept: list[Candidate] = []
    for c in sorted(cands, key=lambda x: x.total, reverse=True):
        clash = False
        for k in kept:
            if k.video_id != c.video_id:
                continue
            inter = max(0.0, min(k.end, c.end) - max(k.start, c.start))
            if inter / max(min(k.end - k.start, c.end - c.start), 0.01) > overlap:
                clash = True
                break
        if not clash:
            kept.append(c)
    return kept
