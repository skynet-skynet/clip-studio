"""Субтитры в формате ASS: крупные слова с подсветкой текущего (karaoke), хук-заголовок, атрибуция."""
from __future__ import annotations

import re

WHITE = "&H00FFFFFF"
YELLOW = "&H0000E5FF"   # ASS: &HAABBGGRR → тёплый жёлтый
BLACK = "&H00000000"
SHADOW = "&H96000000"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def _t(sec: float) -> str:
    sec = max(sec, 0)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def group_words(words: list[dict], max_words: int, max_chars: int, gap: float = 0.6) -> list[list[dict]]:
    """Делит слова на короткие строки: по числу слов, длине и паузам/пунктуации."""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            chars = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
            pause = w["start"] - cur[-1]["end"]
            ends_phrase = bool(re.search(r"[.!?…,:;]$", cur[-1]["word"]))
            if len(cur) >= max_words or chars > max_chars or pause > gap or (ends_phrase and len(cur) >= 2):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_ass(
    words: list[dict], *, width: int, height: int, duration: float, style: str = "karaoke",
    hook: str = "", hook_seconds: float = 2.8, attribution: str = "", font: str = "Montserrat ExtraBold",
    upper: bool = True,
) -> str:
    """words — слова со временем ОТНОСИТЕЛЬНО начала клипа."""
    vertical = height > width
    fs = round(height * (0.042 if vertical else 0.062))
    hook_fs = round(height * (0.046 if vertical else 0.07))
    attr_fs = max(18, round(height * (0.017 if vertical else 0.026)))
    margin_v = round(height * (0.27 if vertical else 0.08))
    hook_margin = round(height * (0.13 if vertical else 0.06))
    side = round(width * 0.07)
    outline = max(3, round(fs * 0.09))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},{fs},{WHITE},{YELLOW},{BLACK},{SHADOW},-1,0,0,0,100,100,0,0,1,{outline},2,2,{side},{side},{margin_v},1
Style: Hook,{font},{hook_fs},{BLACK},{BLACK},{YELLOW},{YELLOW},-1,0,0,0,100,100,0,0,3,{round(hook_fs * 0.28)},0,8,{side},{side},{hook_margin},1
Style: Attr,{font},{attr_fs},&H40FFFFFF,&H40FFFFFF,{BLACK},{SHADOW},0,0,0,0,100,100,0,0,1,2,0,9,{round(width * 0.03)},{round(width * 0.03)},{round(height * 0.025)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    if hook:
        text = _esc(hook.upper())
        events.append(f"Dialogue: 2,{_t(0)},{_t(min(hook_seconds, duration))},Hook,,0,0,0,,{{\\fad(120,200)}}{text}")
    if attribution:
        events.append(f"Dialogue: 1,{_t(0)},{_t(duration)},Attr,,0,0,0,,{_esc(attribution)}")

    if style != "none" and words:
        max_words, max_chars = (3, 18) if vertical else (6, 34)
        for group in group_words(words, max_words, max_chars):
            g_start = group[0]["start"]
            g_end = max(group[-1]["end"], g_start + 0.25)
            tokens = [_esc(w["word"].upper() if upper else w["word"]) for w in group]
            if style == "plain":
                events.append(f"Dialogue: 0,{_t(g_start)},{_t(g_end)},Sub,,0,0,0,,{' '.join(tokens)}")
                continue
            for i, w in enumerate(group):
                s = w["start"] if i else g_start
                e = group[i + 1]["start"] if i + 1 < len(group) else g_end
                if e <= s:
                    continue
                parts = [
                    f"{{\\c{YELLOW}\\fscx108\\fscy108}}{tok}{{\\c{WHITE}\\fscx100\\fscy100}}" if j == i else tok
                    for j, tok in enumerate(tokens)
                ]
                events.append(f"Dialogue: 0,{_t(s)},{_t(e)},Sub,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(events) + "\n"


def words_in_range(all_words: list[dict], start: float, end: float) -> list[dict]:
    """Слова внутри [start, end], со временем относительно start."""
    out = []
    for w in all_words:
        if w["end"] <= start or w["start"] >= end:
            continue
        out.append({"word": w["word"], "start": max(0.0, w["start"] - start), "end": min(end, w["end"]) - start})
    return out
