"""Транскрибация: faster-whisper со словными таймкодами, либо готовые субтитры .srt/.vtt.

Формат транскрипта (JSON):
{"language": "ru", "source": "whisper|subs", "model": "small",
 "segments": [{"start": 1.2, "end": 3.4, "text": "...", "words": [{"start":1.2,"end":1.5,"word":"Привет"}]}]}
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
from pathlib import Path
from typing import Callable

from .config import Config

log = logging.getLogger(__name__)

_MODEL = None
_MODEL_KEY: tuple | None = None
_MODEL_LOCK = threading.Lock()

TS = r"(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}"


def whisper_status(cfg: Config) -> dict:
    t = cfg["transcribe"]
    if t.get("engine") == "subtitles_only":
        return {"ready": True, "message": "Только готовые субтитры (.srt/.vtt)"}
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return {"ready": False, "message": "faster-whisper не установлен: pip install faster-whisper (или загружайте видео с субтитрами)"}
    return {"ready": True, "message": f"faster-whisper, модель {t.get('model', 'small')}"}


# ---------- разбор субтитров ----------

def _ts(s: str) -> float:
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    sec = float(parts[-1])
    mins = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return hours * 3600 + mins * 60 + sec


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _spread_words(start: float, end: float, text: str) -> list[dict]:
    """Равномерно распределяет время сегмента по словам (пропорционально длине)."""
    words = text.split()
    if not words:
        return []
    total = sum(len(w) + 1 for w in words)
    out, t = [], start
    span = max(end - start, 0.01)
    for w in words:
        d = span * (len(w) + 1) / total
        out.append({"start": round(t, 3), "end": round(t + d, 3), "word": w})
        t += d
    return out


def parse_subtitles(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    blocks = re.split(r"\n\s*\n", raw)
    segments: list[dict] = []
    last_text = ""
    for block in blocks:
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        m = re.match(rf"\s*({TS})\s*-->\s*({TS})", lines[idx])
        if not m:
            continue
        start, end = _ts(m.group(1)), _ts(m.group(3))
        body_lines = lines[idx + 1:]
        body = "\n".join(body_lines)
        words: list[dict] = []
        inline = re.findall(rf"<({TS})>", body)
        if inline:
            # YouTube auto-subs: «слово<00:00:01.234><c> слово</c>» — берём только строку с таймкодами
            line = next((ln for ln in body_lines if re.search(rf"<{TS}>", ln)), body)
            pieces = re.split(rf"<({TS})>", line)
            cur_t = start
            first = _clean(pieces[0])
            pending = [(cur_t, first)] if first else []
            i = 1
            while i < len(pieces):
                t = _ts(pieces[i])
                txt = _clean(pieces[i + 2] if i + 2 < len(pieces) else "")
                if txt:
                    pending.append((t, txt))
                i += 3
            for j, (t, txt) in enumerate(pending):
                t_end = pending[j + 1][0] if j + 1 < len(pending) else end
                for w in _spread_words(t, t_end, txt):
                    words.append(w)
            text = " ".join(w["word"] for w in words)
        else:
            text = _clean(" ".join(body_lines))
            # YouTube-«накатывающие» дубли: пропускаем повтор предыдущей строки
            if text == last_text or not text:
                continue
            if last_text and text.startswith(last_text):
                text = text[len(last_text):].strip()
            words = _spread_words(start, end, text)
        if not text:
            continue
        last_text = _clean(" ".join(body_lines))
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": text, "words": words})
    return {"language": "ru", "source": "subs", "model": path.suffix.lstrip("."), "segments": _merge_tiny(segments)}


def _merge_tiny(segments: list[dict]) -> list[dict]:
    """Склеивает сегменты по 1–2 слова (типично для автосубтитров) в фразы до ~12 слов."""
    out: list[dict] = []
    for seg in segments:
        if out and len(out[-1]["words"]) < 12 and seg["start"] - out[-1]["end"] < 0.6 and not re.search(r"[.!?…]$", out[-1]["text"]):
            prev = out[-1]
            prev["end"] = seg["end"]
            prev["text"] = f"{prev['text']} {seg['text']}".strip()
            prev["words"].extend(seg["words"])
        else:
            out.append(dict(seg, words=list(seg["words"])))
    return out


# ---------- whisper ----------

def _get_model(cfg: Config):
    global _MODEL, _MODEL_KEY
    from faster_whisper import WhisperModel

    t = cfg["transcribe"]
    device = t.get("device", "auto")
    compute = t.get("compute_type", "auto")
    if compute == "auto":
        compute = "default"
    key = (t.get("model", "small"), device, compute)
    with _MODEL_LOCK:
        if _MODEL is None or _MODEL_KEY != key:
            log.info("Загружаю модель Whisper %s (%s)", key[0], device)
            _MODEL = WhisperModel(key[0], device=device, compute_type=compute)
            _MODEL_KEY = key
    return _MODEL


def transcribe_whisper(cfg: Config, media: Path, on_progress: Callable[[float], None] | None = None,
                       duration: float = 0) -> dict:
    model = _get_model(cfg)
    lang = cfg["app"].get("language", "ru")
    seg_iter, info = model.transcribe(
        str(media), language=lang, word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400}, condition_on_previous_text=False,
    )
    total = duration or getattr(info, "duration", 0) or 0
    segments = []
    for s in seg_iter:
        words = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip()} for w in (s.words or []) if w.word.strip()]
        segments.append({"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(), "words": words})
        if on_progress and total:
            on_progress(min(s.end / total, 1.0))
    return {"language": lang, "source": "whisper", "model": cfg["transcribe"].get("model"), "segments": segments}


def ensure_transcript(cfg: Config, video: dict, on_progress: Callable[[float], None] | None = None) -> Path:
    """Возвращает путь к JSON-транскрипту видео, создавая его при необходимости."""
    out = cfg.path("transcripts") / f"{video['id']}.json"
    if out.exists() and out.stat().st_size > 0:
        return out
    subs = Path(video["subs_path"]) if video.get("subs_path") else None
    if subs and subs.exists():
        data = parse_subtitles(subs)
    elif cfg["transcribe"].get("engine") == "subtitles_only":
        raise RuntimeError("Нет субтитров, а транскрибация выключена (transcribe.engine: subtitles_only).")
    else:
        data = transcribe_whisper(cfg, Path(video["local_path"]), on_progress, float(video.get("duration") or 0))
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return out


def load_transcript(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def all_words(transcript: dict) -> list[dict]:
    return [w for s in transcript.get("segments", []) for w in s.get("words", [])]
