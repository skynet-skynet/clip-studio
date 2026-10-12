"""Экспорт отрезков во внешние монтажки: EDL (DaVinci Resolve, Premiere) и CSV для LosslessCut."""
from __future__ import annotations

import csv
import io
from pathlib import Path


def _tc(sec: float, fps: float) -> str:
    fps_i = max(int(round(fps)), 1)
    frames = int(round(max(sec, 0) * fps_i))
    f = frames % fps_i
    s = frames // fps_i
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}:{f:02d}"


def edl(title: str, items: list[dict], fps: float = 30.0) -> str:
    """CMX 3600 EDL. items: {"file": путь к исходнику, "start", "end", "name"}.

    В Resolve: File → Import → Timeline → выбрать .edl, исходники заранее добавить в Media Pool.
    """
    lines = [f"TITLE: {title[:60]}", "FCM: NON-DROP FRAME", ""]
    rec = 3600.0  # запись с 01:00:00:00
    for i, it in enumerate(items, 1):
        dur = it["end"] - it["start"]
        lines.append(
            f"{i:03d}  AX       B     C        {_tc(it['start'], fps)} {_tc(it['end'], fps)} {_tc(rec, fps)} {_tc(rec + dur, fps)}"
        )
        lines.append(f"* FROM CLIP NAME: {Path(it['file']).name}")
        if it.get("name"):
            lines.append(f"* COMMENT: {it['name'][:80]}")
        lines.append("")
        rec += dur
    return "\n".join(lines)


def losslesscut_csv(items: list[dict]) -> str:
    """CSV для LosslessCut (Import project → CSV): начало, конец (секунды), название."""
    buf = io.StringIO()
    w = csv.writer(buf)
    for it in items:
        w.writerow([f"{it['start']:.3f}", f"{it['end']:.3f}", it.get("name", "")])
    return buf.getvalue()
