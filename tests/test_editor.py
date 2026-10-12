"""Монтажный стол, вырезание пауз, экспорт, кадры и MCP-сервер."""
import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from clipstudio.db import DB
from clipstudio.export import edl, losslesscut_csv
from clipstudio.ingest import import_local
from clipstudio.media import probe
from clipstudio.pipeline import Pipeline
from clipstudio.render import keep_ranges, remap_words
from clipstudio.server import create_app
from clipstudio.visual import contact_sheet, filmstrip, waveform

WORDS = [
    {"word": "раз", "start": 1.0, "end": 1.4}, {"word": "два", "start": 1.5, "end": 1.9},
    {"word": "три", "start": 5.0, "end": 5.4}, {"word": "четыре", "start": 5.5, "end": 6.0},
]


def test_keep_ranges_cuts_long_pause():
    r = keep_ranges(WORDS, 0.5, 6.5, max_gap=0.6, pad=0.15)
    assert len(r) == 2
    assert r[0][1] < 2.2 and r[1][0] > 4.7
    words = remap_words(WORDS, r)
    assert [w["word"] for w in words] == ["раз", "два", "три", "четыре"]
    assert words[2]["start"] < 2.5  # «три» сдвинулось к началу после вырезанной паузы


def test_keep_ranges_no_pause():
    assert keep_ranges(WORDS[:2], 0.5, 2.5) == [(0.5, 2.5)]


def test_edl_and_csv():
    items = [{"file": "/v/a.mp4", "start": 10.0, "end": 12.5, "name": "Клип"}]
    text = edl("Тест", items, fps=25)
    assert "001  AX       B     C        00:00:10:00 00:00:12:12 01:00:00:00 01:00:02:12" in text
    assert "* FROM CLIP NAME: a.mp4" in text
    assert losslesscut_csv(items).strip() == "10.000,12.500,Клип"


def test_visual(cfg, sample_video):
    v = {"id": 9, "local_path": str(sample_video), "duration": 130}
    strip = filmstrip(cfg, v, n=8, width=80)
    assert strip.exists() and probe(strip)["width"] > 600
    sheet, times = contact_sheet(cfg, sample_video, 10, 20, n=4, cols=2, width=120)
    assert sheet.exists() and len(times) == 4
    w = waveform(cfg, v, points=200)
    assert len(w["peaks"]) == 200 and max(w["peaks"]) == 1.0


def test_jumpcut_render_and_manual_clip(cfg, sample_video, sample_srt):
    db = DB(cfg.data_dir / "e.db")
    pipe = Pipeline(cfg, db)
    v = import_local(cfg, db, sample_video, "me", subs_path=sample_srt)
    # в субтитрах между 31.5–38.0 и 40.0 есть пауза ~2 с — она должна вырезаться
    jid = pipe.create_manual_clip(v["id"], 31.0, 46.5, title="Ручной", hook="ТЕСТ",
                                  params={"format": "horizontal", "jumpcut": True})
    pipe.run_sync(jid, "render_manual")
    clip = db.query("SELECT * FROM clips WHERE job_id = ?", (jid,))[0]
    dur = probe(Path(clip["path"]))["duration"]
    assert dur < 15.5 - 1.0, dur
    name, content = pipe.export_job(jid, "edl")
    assert name.endswith(".edl") and "FROM CLIP NAME" in content


def test_editor_api(cfg, sample_video, sample_srt):
    client = TestClient(create_app(cfg))
    with open(sample_video, "rb") as fh, open(sample_srt, "rb") as sf:
        r = client.post("/api/videos/upload", files={"file": ("v.mp4", fh, "video/mp4"), "subs": ("v.srt", sf, "text/plain")},
                        data={"source_id": "me"})
    vid = r.json()["video"]["id"]
    assert client.get(f"/api/videos/{vid}/waveform?points=300").json()["points"] == 300
    assert client.get(f"/api/videos/{vid}/filmstrip?n=8").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/videos/{vid}/frames?start=1&end=5&n=2").status_code == 200
    r = client.post(f"/api/videos/{vid}/clip", json={"start": 1, "end": 0.8})
    assert r.status_code == 400
    assert client.get("/api/integrations").json()["mcp_json"].startswith("{")
    assert client.get("/help/manual.html").status_code == 200


def test_mcp_server_tools(cfg, sample_video, sample_srt):
    from clipstudio.mcp_server import build_server

    db = DB(cfg.data_dir / "clipstudio.db")
    import_local(cfg, db, sample_video, "me", subs_path=sample_srt)
    srv = build_server()

    async def run():
        names = {t.name for t in await srv.list_tools()}
        assert {"look", "cut", "transcript", "publish", "clip_preview"} <= names
        res = await srv.call_tool("search_transcript", {"video_id": 1, "phrase": "Барсик ушёл"})
        text = str(res)
        assert "Барсик" in text
        res = await srv.call_tool("look", {"video_id": 1, "start": 10, "end": 20, "frames": 2})
        assert "image" in str(res)
        res = await srv.call_tool("import_file", {"path": str(sample_video), "source_id": "nobody"})
        assert "шлюз" in str(res)

    asyncio.run(run())
