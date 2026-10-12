"""Дешёвые сигналы «здесь что-то происходит»: всплески громкости и маркеры смеха в тексте.

Они не решают, что смешно, — это делает Claude. Сигналы подсвечивают места в транскрипте,
чтобы модель не пропустила реакцию (смех, крик) и чтобы экономить токены на длинных видео.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

from .config import Config
from .media import ffmpeg_bin

LAUGH_RE = re.compile(r"(\b(ха){2,}|\bхах|\bахах|\bхи-?хи|\bлол\b|\bржу|\[смех\]|\(смех|\(смеётся|\(смеется|\[laughter\]|😂|🤣)", re.I)
WINDOW = 0.5  # сек


def audio_energy(media: Path, sr: int = 8000) -> np.ndarray:
    """RMS-громкость в дБ по окнам WINDOW секунд."""
    cmd = [ffmpeg_bin(), "-v", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=3600)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    hop = int(sr * WINDOW)
    if pcm.size < hop:
        return np.zeros(0, dtype=np.float32)
    n = pcm.size // hop
    frames = pcm[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-10)
    return (20 * np.log10(rms)).astype(np.float32)


def loud_peaks(db: np.ndarray, z: float = 2.0, min_gap: float = 4.0) -> list[float]:
    """Моменты, где громкость заметно выше локальной нормы (окно ~30 сек)."""
    if db.size == 0:
        return []
    k = max(int(30 / WINDOW), 3)
    pad = np.pad(db, (k // 2, k // 2), mode="edge")
    med = np.array([np.median(pad[i:i + k]) for i in range(db.size)]) if db.size < 20000 else _fast_median(db, k)
    spread = np.std(db) + 1e-6
    score = (db - med) / spread
    times, last = [], -1e9
    for i in np.argsort(-score):
        if score[i] < z:
            break
        t = i * WINDOW
        if all(abs(t - x) >= min_gap for x in times):
            times.append(round(float(t), 1))
        if len(times) > 400:
            break
    return sorted(times)


def _fast_median(db: np.ndarray, k: int) -> np.ndarray:
    # Для очень длинных видео: медиана по блокам вместо скользящей
    out = np.empty_like(db)
    for i in range(0, db.size, k):
        lo, hi = max(0, i - k), min(db.size, i + 2 * k)
        out[i:i + k] = np.median(db[lo:hi])
    return out


def laugh_cues(transcript: dict) -> list[float]:
    cues = []
    for seg in transcript.get("segments", []):
        if LAUGH_RE.search(seg.get("text", "")):
            cues.append(round(float(seg["start"]), 1))
    return cues


def compute_signals(cfg: Config, video: dict, transcript: dict) -> dict:
    """Считает и кэширует сигналы для видео."""
    out = cfg.path("signals") / f"{video['id']}.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    try:
        peaks = loud_peaks(audio_energy(Path(video["local_path"])))
    except Exception:  # noqa: BLE001 — сигналы необязательны
        peaks = []
    data = {"loud": peaks, "laugh": laugh_cues(transcript)}
    out.write_text(json.dumps(data), encoding="utf-8")
    return data
