"""Монтаж клипов через ffmpeg: точная нарезка, рефрейм, субтитры, громкость, склейка, превью."""
from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import RES, Config
from .media import extract_frame, ffmpeg_bin, probe, run
from .subtitles import build_ass, words_in_range

log = logging.getLogger(__name__)

SIZES = {"vertical": (1080, 1920), "horizontal": (1920, 1080), "square": (1080, 1080)}
FONTS_DIR = RES / "fonts"


@dataclass
class Segment:
    start: float
    end: float


def face_center_x(video: Path, start: float, end: float) -> float | None:
    """Медианный центр лица по нескольким кадрам (0..1). Нужен opencv-python-headless; иначе None."""
    try:
        import cv2  # type: ignore
    except ImportError:
        return None
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(video))
    xs = []
    for i in range(8):
        t = start + (end - start) * (i + 0.5) / 8
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.15, 5, minSize=(60, 60))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            xs.append((x + w / 2) / frame.shape[1])
    cap.release()
    if not xs:
        return None
    xs.sort()
    return xs[len(xs) // 2]


def keep_ranges(words: list[dict], start: float, end: float, max_gap: float = 0.6, pad: float = 0.15) -> list[tuple[float, float]]:
    """Вырезание пауз: оставляет речь, выбрасывая тишину длиннее max_gap (как auto-editor, но по словам)."""
    inside = [w for w in words if w["end"] > start and w["start"] < end]
    if len(inside) < 2:
        return [(start, end)]
    ranges: list[list[float]] = [[start, end]]
    for prev, nxt in zip(inside, inside[1:]):
        gap_a, gap_b = prev["end"] + pad, nxt["start"] - pad
        if gap_b - gap_a >= max_gap - 2 * pad and gap_a > start and gap_b < end:
            ranges[-1][1] = gap_a
            ranges.append([gap_b, end])
    return [(round(a, 3), round(b, 3)) for a, b in ranges if b - a > 0.05]


def remap_words(words: list[dict], ranges: list[tuple[float, float]]) -> list[dict]:
    """Переносит слова на новую шкалу времени после вырезания пауз (время от начала клипа)."""
    out, offset = [], 0.0
    for a, b in ranges:
        for w in words:
            if w["start"] >= a - 0.01 and w["start"] < b:
                out.append({"word": w["word"], "start": offset + (w["start"] - a), "end": offset + (min(w["end"], b) - a)})
        offset += b - a
    return out


def _video_filter(fmt: str, reframe: str, src_w: int, src_h: int, face_x: float | None, src: str = "[0:v]") -> str:
    W, H = SIZES[fmt]
    src_ar = src_w / max(src_h, 1)
    out_ar = W / H
    if fmt == "horizontal" or abs(src_ar - out_ar) < 0.02:
        return (f"{src}scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[base]")
    if reframe in ("center", "face") and src_ar > out_ar:
        cx = face_x if (reframe == "face" and face_x is not None) else 0.5
        # кроп по высоте: ширина окна = H * src_w/src_h → после scale по высоте
        return (f"{src}scale=-2:{H},crop={W}:{H}:"
                f"'max(0,min(iw-{W},iw*{cx:.3f}-{W}/2))':0,setsar=1[base]")
    # blur: размытый фон + видео целиком по центру, чуть увеличенное
    fg_w = W if src_ar >= out_ar else -2
    fg_h = -2 if src_ar >= out_ar else H
    return (f"{src}split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=luma_radius=24:luma_power=2,eq=brightness=-0.10:saturation=1.1[bg];"
            f"[fgsrc]scale={fg_w}:{fg_h}[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")


def render_segment(
    cfg: Config, video: Path, seg: Segment, words: list[dict], out: Path, *, fmt: str, reframe: str,
    sub_style: str, hook: str, attribution: str, work: Path, src_w: int, src_h: int, has_audio: bool = True,
    jumpcut: bool = False,
) -> tuple[Path, float]:
    """Рендерит один отрезок. Возвращает (файл, итоговая длительность)."""
    W, H = SIZES[fmt]
    dur = seg.end - seg.start
    ranges = keep_ranges(words, seg.start, seg.end, float(cfg["clips"].get("jumpcut_gap", 0.6))) if jumpcut else [(seg.start, seg.end)]
    if len(ranges) > 1:
        clip_words = remap_words(words, ranges)
    else:
        clip_words = words_in_range(words, seg.start, seg.end)
    out_dur = sum(b - a for a, b in ranges)
    ass = build_ass(
        clip_words, width=W, height=H, duration=out_dur, style=sub_style, hook=hook,
        attribution=attribution, font=cfg["clips"].get("font", "Montserrat ExtraBold"), upper=(fmt != "horizontal"),
    )
    # Субтитры и шрифт кладём в рабочую папку и запускаем ffmpeg из неё: относительные пути
    # не нужно экранировать (на Windows абсолютные пути вида C:\... ломают фильтр ass).
    ass_name = f"{uuid.uuid4().hex[:8]}.ass"
    (work / ass_name).write_text(ass, encoding="utf-8")
    fonts = work / "fonts"
    if not fonts.exists():
        shutil.copytree(FONTS_DIR, fonts)
    face_x = face_center_x(video, seg.start, seg.end) if reframe == "face" else None
    lufs = cfg["clips"].get("loudness_lufs", -14)
    fade = 0.03  # 30 мс на краях — без щелчков на склейках
    graph: list[str] = []
    if len(ranges) > 1:
        labels = []
        for i, (a, b) in enumerate(ranges):
            ra, rb = a - seg.start, b - seg.start
            graph.append(f"[0:v]trim=start={ra:.3f}:end={rb:.3f},setpts=PTS-STARTPTS[v{i}]")
            labels.append(f"[v{i}]")
            if has_audio:
                ln = rb - ra
                graph.append(f"[0:a]atrim=start={ra:.3f}:end={rb:.3f},asetpts=PTS-STARTPTS,"
                             f"afade=t=in:d={fade},afade=t=out:st={max(ln - fade, 0):.3f}:d={fade}[a{i}]")
                labels.append(f"[a{i}]")
        graph.append(f"{''.join(labels)}concat=n={len(ranges)}:v=1:a={1 if has_audio else 0}[cv]" + ("[ca]" if has_audio else ""))
        vsrc, asrc = "[cv]", "[ca]"
    else:
        vsrc, asrc = "[0:v]", "[0:a]"
    graph.append(_video_filter(fmt, reframe, src_w, src_h, face_x, vsrc))
    graph.append(f"[base]ass={ass_name}:fontsdir=fonts[v]")
    audio: list[str] = ["-an"]
    if has_audio:
        edge = f"afade=t=in:d={fade},afade=t=out:st={max(out_dur - 0.05, 0):.3f}:d=0.05,"
        graph.append(f"{asrc}{edge}loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000[aout]")
        audio = ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd = [
        ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{seg.start:.3f}", "-i", str(Path(video).resolve()), "-t", f"{dur:.3f}",
        "-filter_complex", ";".join(graph), "-map", "[v]", *audio,
        "-c:v", "libx264", "-preset", cfg["clips"].get("preset", "veryfast"), "-crf", str(cfg["clips"].get("crf", 20)),
        "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart", str(Path(out).resolve()),
    ]
    run(cmd, cwd=work)
    return out, out_dur


def concat_files(parts: list[Path], out: Path) -> Path:
    """Склейка одинаково закодированных клипов без перекодирования."""
    work = Path(parts[0]).parent
    lst = work / f"concat_{uuid.uuid4().hex[:6]}.txt"
    lst.write_text("".join(f"file '{Path(p).resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    try:
        run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)])
    finally:
        lst.unlink(missing_ok=True)
    return out


def render_clip(
    cfg: Config, video: dict, segments: list[Segment], words: list[dict], out: Path, *,
    fmt: str = "vertical", reframe: str = "blur", sub_style: str = "karaoke", hook: str = "",
    attribution: str = "", jumpcut: bool = False,
) -> dict:
    """Рендерит один клип (из одного или нескольких сегментов — склейка). Возвращает метаданные."""
    src = Path(video["local_path"])
    info = probe(src)
    src_w, src_h, has_audio = info["width"], info["height"], info["has_audio"]
    work = Path(cfg.path("work", uuid.uuid4().hex[:10]))
    try:
        parts, first_dur = [], 0.0
        for i, seg in enumerate(segments):
            part = work / f"part{i:03d}.mp4"
            _, seg_dur = render_segment(
                cfg, src, seg, words, part, fmt=fmt, reframe=reframe, sub_style=sub_style,
                hook=hook if i == 0 else "", attribution=attribution, work=work, src_w=src_w, src_h=src_h,
                has_audio=has_audio, jumpcut=jumpcut,
            )
            first_dur = first_dur or seg_dur
            parts.append(part)
        out.parent.mkdir(parents=True, exist_ok=True)
        if len(parts) == 1:
            shutil.move(str(parts[0]), out)
        else:
            concat_files(parts, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    info = probe(out)
    thumb = out.with_suffix(".jpg")
    # превью — кадр ближе к панчлайну (60% длительности первого сегмента)
    extract_frame(out, min(first_dur * 0.6, max(info["duration"] - 0.2, 0)), thumb, width=540)
    return {"path": str(out), "thumb": str(thumb), "duration": info["duration"], "width": info["width"], "height": info["height"]}
