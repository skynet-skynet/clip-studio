"""Сквозной прогон: импорт → поиск моментов (Claude подменён) → рендер → публикация пакетом."""
from pathlib import Path

import pytest

from clipstudio import pipeline as pipeline_mod
from clipstudio.db import DB
from clipstudio.ingest import GateBlocked, import_local
from clipstudio.llm import LLMResult
from clipstudio.media import probe
from clipstudio.pipeline import Pipeline

FAKE = {"moments": [
    {"start": 40.0, "end": 70.0, "quote": "Ахаха, чат, вы были правы", "why": "уверенность → провал",
     "scores": {"editor": 9, "comedian": 9, "retention": 8, "compliance": 10},
     "title": "15 лет опыта", "hook": "ПЛАН БЫЛ ИДЕАЛЬНЫЙ", "tags": ["стрим", "фейл"], "risk": ""},
]}


@pytest.fixture()
def pipe(cfg, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "ask_claude", lambda c, user: LLMResult(data=FAKE, cost_usd=0.01, model="fake"))
    db = DB(cfg.data_dir / "test.db")
    return Pipeline(cfg, db)


def test_gate_blocks_import(cfg, sample_video):
    db = DB(cfg.data_dir / "g.db")
    with pytest.raises(GateBlocked):
        import_local(cfg, db, sample_video, "example_streamer")  # в примере источник выключен
    rows = db.query("SELECT decision FROM rights_ledger")
    assert rows and rows[0]["decision"] == "blocked"


def test_full_flow(cfg, pipe, sample_video, sample_srt):
    v = import_local(cfg, pipe.db, sample_video, "me", subs_path=sample_srt, title="Шкаф")
    assert v["rights_status"] == "cleared"
    jid = pipe.create_job("смешные провалы «Барсик ушёл»", {"video_ids": [v["id"]], "count": 2})
    pipe.run_sync(jid, "analyze")
    job = pipe.db.get("jobs", jid)
    assert job["status"] == "review", job["log"]
    cands = pipe.db.query("SELECT * FROM candidates WHERE job_id = ?", (jid,))
    origins = {c["origin"] for c in cands}
    assert origins == {"llm", "phrase"}
    for c in cands:
        pipe.db.update("candidates", c["id"], status="approved")
    pipe.run_sync(jid, "render")
    clips = pipe.db.query("SELECT * FROM clips WHERE job_id = ?", (jid,))
    assert len(clips) == 2
    info = probe(Path(clips[0]["path"]))
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["has_audio"]
    assert Path(clips[0]["thumb"]).exists()
    res = pipe.publish_clip(clips[0]["id"], ["pack", "vk"], "private")
    by = {r["platform"]: r for r in res}
    assert by["pack"]["status"] == "pack_ready"
    assert by["vk"]["status"] == "error"  # нет токена — понятная ошибка, а не исключение
    assert (cfg.data_dir / "packs" / f"clip_{clips[0]['id']}" / "video.mp4").exists()


def test_compilation(cfg, pipe, sample_video, sample_srt):
    v = import_local(cfg, pipe.db, sample_video, "me", subs_path=sample_srt)
    jid = pipe.create_job("«Барсик ушёл» и провалы", {"video_ids": [v["id"]], "count": 2, "compilation": True, "format": "horizontal"})
    pipe.run_sync(jid, "analyze")
    for c in pipe.db.query("SELECT id FROM candidates WHERE job_id = ?", (jid,)):
        pipe.db.update("candidates", c["id"], status="approved")
    pipe.run_sync(jid, "render")
    clips = pipe.db.query("SELECT * FROM clips WHERE job_id = ?", (jid,))
    assert len(clips) == 1
    info = probe(Path(clips[0]["path"]))
    assert (info["width"], info["height"]) == (1920, 1080)
    assert info["duration"] > 40


def test_manual_mode(cfg, pipe, sample_video, sample_srt):
    cfg["llm"]["mode"] = "manual"
    v = import_local(cfg, pipe.db, sample_video, "me", subs_path=sample_srt)
    jid = pipe.create_job("провалы", {"video_ids": [v["id"]], "count": 1})
    pipe.run_sync(jid, "analyze")
    assert pipe.db.get("jobs", jid)["status"] == "waiting_manual"
    state = pipe.manual_state(jid)
    assert "JSON Schema" in state["prompt"] and "Монтажёр" in state["prompt"]
    import json
    res = pipe.manual_answer(jid, "ответ:\n```json\n" + json.dumps(FAKE, ensure_ascii=False) + "\n```", enqueue=False)
    assert res == {"ok": True, "next": False}
    pipe.run_sync(jid, "finish_manual")
    assert pipe.db.get("jobs", jid)["status"] == "review"
    assert pipe.db.query("SELECT COUNT(*) AS n FROM candidates WHERE job_id = ?", (jid,))[0]["n"] == 1
