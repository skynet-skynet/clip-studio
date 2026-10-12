from fastapi.testclient import TestClient

from clipstudio.server import create_app


def test_status_and_static(cfg):
    client = TestClient(create_app(cfg))
    s = client.get("/api/status").json()
    assert {"llm", "ffmpeg", "transcribe", "publishers"} <= s.keys()
    assert {p["name"] for p in s["publishers"]} == {"vk", "youtube", "telegram", "pack"}
    assert "Clip Studio" in client.get("/").text
    assert client.get("/fonts/Montserrat-ExtraBold.ttf").status_code == 200
    assert "Clip Studio" in client.get("/api/guide").json()["markdown"]


def test_sources_validation(cfg):
    client = TestClient(create_app(cfg))
    assert client.post("/api/sources", json={"id": "bad id!", "license": "own"}).status_code == 400
    assert client.post("/api/sources", json={"id": "x", "license": "steal"}).status_code == 400
    r = client.post("/api/sources", json={"id": "ivan", "name": "Иван", "license": "permission", "proof": "p.png", "enabled": True})
    assert r.status_code == 200
    assert any(s["id"] == "ivan" for s in client.get("/api/sources").json()["sources"])


def test_upload_blocked_by_gate(cfg, sample_video):
    client = TestClient(create_app(cfg))
    with open(sample_video, "rb") as fh:
        r = client.post("/api/videos/upload", files={"file": ("v.mp4", fh, "video/mp4")}, data={"source_id": "nobody"})
    assert r.status_code == 403 and "шлюз" in r.json()["detail"]


def test_secret_endpoint_whitelist(cfg):
    client = TestClient(create_app(cfg))
    assert client.post("/api/settings/secret", json={"name": "HOME", "value": "x"}).status_code == 400
    r = client.post("/api/settings/secret", json={"name": "TELEGRAM_BOT_TOKEN", "value": "1:a"})
    assert r.status_code == 200 and r.json()["secrets"]["TELEGRAM_BOT_TOKEN"] is True
