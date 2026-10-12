"""Картинки для глаз — человека и Claude: раскадровка, лист кадров, волна звука.

Идея из browser-use/video-use: модель не смотрит видео целиком, а получает компактный «вид таймлайна»:
ряд кадров + волну громкости. Так Claude видит визуальные моменты, которых нет в транскрипте.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import numpy as np

from .config import Config
from .media import ffmpeg_bin, run


def _frames(video: Path, times: list[float], out_dir: Path, width: int) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, t in enumerate(times):
        p = out_dir / f"f{i:03d}.jpg"
        try:
            run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(video),
                 "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(p)])
        except Exception:  # noqa: BLE001 — кадр за концом файла и т.п.
            continue
        if p.exists():
            paths.append(p)
    return paths


def _tile(frames: list[Path], out: Path, cols: int) -> Path:
    """Склеивает кадры в сетку средствами ffmpeg (без Pillow)."""
    work = frames[0].parent
    # приводим к последовательности для image2
    for i, f in enumerate(frames):
        target = work / f"seq{i:03d}.jpg"
        if f != target:
            f.replace(target)
    rows = (len(frames) + cols - 1) // cols
    vf = f"tile={cols}x{rows}:padding=4:color=black"
    run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-framerate", "1", "-i", str(work / "seq%03d.jpg"),
         "-frames:v", "1", "-vf", vf, "-q:v", "4", str(out)])
    return out


def filmstrip(cfg: Config, video: dict, n: int = 40, width: int = 160) -> Path:
    """Раскадровка всего видео в одну полосу (для таймлайна в редакторе). Кэшируется."""
    out = cfg.path("thumbs") / f"{video['id']}_strip_{n}.jpg"
    if out.exists():
        return out
    dur = float(video.get("duration") or 0)
    times = [dur * (i + 0.5) / n for i in range(n)]
    work = cfg.path("work", f"strip_{uuid.uuid4().hex[:8]}")
    try:
        frames = _frames(Path(video["local_path"]), times, work, width)
        if not frames:
            raise RuntimeError("Не удалось извлечь кадры")
        _tile(frames, out, cols=len(frames))
    finally:
        for p in work.glob("*"):
            p.unlink(missing_ok=True)
        work.rmdir()
    return out


def contact_sheet(cfg: Config, video_path: Path, start: float, end: float, n: int = 6, cols: int = 3,
                  width: int = 360) -> tuple[Path, list[float]]:
    """Лист кадров из отрезка: n кадров равномерно между start и end. Возвращает путь и времена кадров."""
    n = max(1, min(int(n), 16))
    times = [round(start + (end - start) * (i + 0.5) / n, 2) for i in range(n)]
    work = cfg.path("work", f"sheet_{uuid.uuid4().hex[:8]}")
    out = cfg.path("thumbs") / f"sheet_{uuid.uuid4().hex[:10]}.jpg"
    try:
        frames = _frames(video_path, times, work, width)
        if not frames:
            raise RuntimeError("Не удалось извлечь кадры")
        _tile(frames, out, cols=min(cols, len(frames)))
    finally:
        for p in work.glob("*"):
            p.unlink(missing_ok=True)
        work.rmdir()
    return out, times[: len(frames)]


def waveform(cfg: Config, video: dict, points: int = 1200) -> dict:
    """Пики громкости для отрисовки волны: points значений 0..1 на всю длину видео. Кэшируется."""
    points = max(100, min(int(points), 6000))
    out = cfg.path("signals") / f"{video['id']}_wave_{points}.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    sr = 4000
    cmd = [ffmpeg_bin(), "-v", "error", "-i", str(video["local_path"]), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=3600)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if pcm.size == 0:
        data = {"points": points, "duration": float(video.get("duration") or 0), "peaks": [0.0] * points}
    else:
        hop = max(pcm.size // points, 1)
        usable = pcm[: hop * points] if pcm.size >= hop * points else np.pad(pcm, (0, hop * points - pcm.size))
        frames = np.abs(usable.reshape(points, hop))
        peaks = frames.max(axis=1)
        peaks = peaks / (peaks.max() + 1e-9)
        data = {"points": points, "duration": pcm.size / sr, "peaks": [round(float(x), 3) for x in peaks]}
    out.write_text(json.dumps(data), encoding="utf-8")
    return data
