from pathlib import Path

import pytest

from clipstudio import rights
from clipstudio.moments import Candidate, dedupe, find_phrase, parse_moments, quoted_phrases, snap
from clipstudio.subtitles import build_ass, group_words, words_in_range
from clipstudio.transcribe import all_words, parse_subtitles


# ---------- юридический шлюз ----------

def test_gate_blocks_unknown_and_disabled():
    assert not rights.evaluate(None).allowed
    assert not rights.evaluate({"id": "x", "license": "own", "enabled": False}).allowed


def test_gate_permission_requires_proof():
    src = {"id": "s", "name": "Стример", "license": "permission", "enabled": True}
    assert not rights.evaluate(src).allowed
    src["proof"] = "proofs/ok.png"
    assert rights.evaluate(src).allowed


def test_gate_cc_by_needs_confirmation_and_rejects_nd():
    src = {"id": "cc", "name": "CC", "license": "cc-by", "enabled": True}
    assert not rights.evaluate(src).allowed
    assert rights.evaluate(src, reported_license="Creative Commons Attribution license (reuse allowed)").allowed
    assert not rights.evaluate(src, reported_license="CC BY-ND 4.0").allowed
    assert not rights.evaluate(src, reported_license="youtube").allowed
    assert rights.evaluate(src, user_confirmed_license=True, origin_url="https://example.org/v").allowed


def test_gate_checks_channel():
    src = {"id": "s", "license": "permission", "proof": "p", "youtube_channel_id": "UC_A", "enabled": True}
    assert rights.evaluate(src, channel_id="UC_A").allowed
    assert not rights.evaluate(src, channel_id="UC_B").allowed


def test_attribution():
    assert rights.attribution_for({"license": "own"}) == ""
    text = rights.attribution_for({"license": "cc-by", "name": "Автор", "attribution": ""}, url="https://x")
    assert "Автор" in text and "CC BY" in text


# ---------- субтитры и транскрипт ----------

def test_parse_srt(sample_srt):
    t = parse_subtitles(sample_srt)
    assert len(t["segments"]) >= 10
    words = all_words(t)
    assert words[0]["start"] >= 2.0
    assert all(w["end"] >= w["start"] for w in words)


def test_parse_youtube_vtt(tmp_path):
    vtt = tmp_path / "a.ru.vtt"
    vtt.write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.000\n"
        "привет<00:00:01.500><c> всем</c><00:00:02.000><c> друзья</c>\n\n"
        "00:00:03.000 --> 00:00:03.010\n"
        "привет всем друзья\n\n",
        encoding="utf-8",
    )
    t = parse_subtitles(vtt)
    words = all_words(t)
    assert [w["word"] for w in words] == ["привет", "всем", "друзья"]
    assert words[1]["start"] == pytest.approx(1.5, abs=0.01)


# ---------- моменты ----------

def test_quoted_phrases():
    assert quoted_phrases("найди «ну это база» и \"чат знал\"") == ["ну это база", "чат знал"]


def test_find_phrase(sample_srt):
    t = parse_subtitles(sample_srt)
    found = find_phrase(t, "Барсик ушел", video_id=1)  # без «ё» — тоже находит
    assert found and found[0].start > 110


def test_parse_moments_drops_bad_rows():
    data = {"moments": [
        {"start": 10, "end": 20, "quote": "q", "why": "w", "title": "t", "hook": "h",
         "scores": {"editor": 8, "comedian": 9, "retention": 7, "compliance": 10}},
        {"start": 30, "end": 25, "quote": "", "why": "", "title": "", "hook": "", "scores": {}},
        {"start": 40, "end": 50, "quote": "", "why": "", "title": "", "hook": "",
         "scores": {"editor": 9, "comedian": 9, "retention": 9, "compliance": 2}},
        {"start": 9000, "end": 9010, "quote": "", "why": "", "title": "", "hook": "",
         "scores": {"editor": 9, "comedian": 9, "retention": 9, "compliance": 9}},
    ]}
    out = parse_moments(data, 1, {"start": 0, "end": 100})
    assert len(out) == 1 and out[0].total > 8


def test_snap_and_dedupe(cfg, sample_srt):
    t = parse_subtitles(sample_srt)
    c = Candidate(video_id=1, start=66.0, end=68.0, scores={"editor": 8, "comedian": 8, "retention": 8, "compliance": 10})
    snap(c, t, cfg, duration=130, min_s=12, max_s=60)
    assert c.end - c.start >= 12
    assert c.start <= 65.5
    a = Candidate(video_id=1, start=10, end=30, scores={"comedian": 9, "editor": 9, "retention": 9, "compliance": 10})
    b = Candidate(video_id=1, start=12, end=31, scores={"comedian": 5, "editor": 5, "retention": 5, "compliance": 10})
    d = Candidate(video_id=2, start=12, end=31, scores={"comedian": 5, "editor": 5, "retention": 5, "compliance": 10})
    kept = dedupe([b, a, d])
    assert a in kept and b not in kept and d in kept


# ---------- ASS ----------

def test_ass_karaoke():
    words = [{"word": w, "start": i * 0.4, "end": i * 0.4 + 0.35} for i, w in enumerate("раз два три четыре пять".split())]
    groups = group_words(words, 3, 18)
    assert len(groups) == 2
    ass = build_ass(words, width=1080, height=1920, duration=3, hook="ХУК", attribution="Источник: тест")
    assert "PlayResY: 1920" in ass and "Style: Hook" in ass
    assert ass.count("Dialogue: 0,") == 5  # по событию на каждое слово
    rel = words_in_range([{"word": "a", "start": 10, "end": 11}], 9.5, 12)
    assert rel[0]["start"] == pytest.approx(0.5)


def test_ass_escapes_braces():
    ass = build_ass([{"word": "{\\b1}x", "start": 0, "end": 1}], width=1920, height=1080, duration=1, style="plain")
    assert "{\\b1}x" not in ass
