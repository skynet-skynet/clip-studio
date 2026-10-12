"""Обёртки над ffmpeg/ffprobe."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


class MediaError(RuntimeError):
    pass


def _bundled(name: str) -> str | None:
    """ffmpeg, положенный рядом с программой (сборка для Windows), важнее системного."""
    from .config import RES, ROOT

    exe = f"{name}.exe" if sys.platform.startswith("win") else name
    for base in (ROOT / "ffmpeg", RES / "ffmpeg", ROOT / "ffmpeg" / "bin", RES / "ffmpeg" / "bin"):
        cand = base / exe
        if cand.exists():
            return str(cand)
    return None


def ffmpeg_bin() -> str:
    return _bundled("ffmpeg") or shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_bin() -> str:
    return _bundled("ffprobe") or shutil.which("ffprobe") or "ffprobe"


def ffmpeg_status() -> dict:
    path = _bundled("ffmpeg") or shutil.which("ffmpeg")
    if not path:
        return {"ready": False, "message": "ffmpeg не найден. Установите его (см. гайд, раздел «Установка»)."}
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=10).stdout
        first = out.splitlines()[0] if out else "ffmpeg"
        return {"ready": True, "message": " ".join(first.split()[:3])}
    except Exception as e:  # noqa: BLE001
        return {"ready": False, "message": f"ffmpeg не запускается: {e}"}


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(map(str, cmd)))
    proc = subprocess.run(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise MediaError(f"Команда завершилась с ошибкой ({proc.returncode}):\n{tail}")
    return proc


def probe(path: Path) -> dict:
    proc = run([ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    data = json.loads(proc.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    width, height = int(v.get("width") or 0), int(v.get("height") or 0)
    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    if abs(rotation) in (90, 270):
        width, height = height, width
    fps = 30.0
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or ""
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            fps = float(num) / float(den) if float(den) else fps
        except ValueError:
            pass
    return {"duration": duration, "width": width, "height": height, "has_audio": a is not None, "fps": round(fps, 3)}


def filter_path(p: Path | str) -> str:
    """Путь для аргументов фильтров ffmpeg (subtitles/ass): экранирование под Windows и Linux."""
    s = str(p).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'").replace(",", "\\,").replace("[", "\\[").replace("]", "\\]")


def extract_frame(video: Path, t: float, out: Path, width: int = 0) -> Path:
    vf = ["-vf", f"scale={width}:-2"] if width else []
    run([ffmpeg_bin(), "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(video), "-frames:v", "1", *vf, "-q:v", "3", str(out)])
    return out
