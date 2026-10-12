from pathlib import Path
from unittest import mock

import pytest

from clipstudio.auth import save_secret
from clipstudio.discover import discover
from clipstudio.publish.base import PublishRequest
from clipstudio.publish.pack import PackPublisher
from clipstudio.publish.telegram import TelegramPublisher, build_caption
from clipstudio.publish.vk import VKPublisher


@pytest.fixture()
def clip_file(tmp_path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 1024)
    return p


def _req(clip_file, **kw):
    base = dict(clip_id=1, clip_path=clip_file, title="Название", description="Описание", tags=["a", "b c"],
                attribution="Источник: Автор — https://vk.com/a", duration=30, privacy="private")
    base.update(kw)
    return PublishRequest(**base)


def test_caption_keeps_attribution(clip_file):
    req = _req(clip_file, description="х" * 3000)
    cap = build_caption(req)
    assert len(cap) <= 1024
    assert cap.endswith("Источник: Автор — https://vk.com/a")


def test_telegram_routing(cfg, clip_file, monkeypatch):
    tg = TelegramPublisher()
    assert not tg.is_configured(cfg)[0]
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    cfg["publish"]["platforms"]["telegram"]["chat_id"] = "@chan"
    assert tg.publish(_req(clip_file), cfg).status == "skipped"  # приватно, но нет чата для черновиков
    with mock.patch("clipstudio.publish.telegram.requests.post") as post:
        post.return_value.json.return_value = {"ok": True, "result": {"message_id": 7}}
        r = tg.publish(_req(clip_file, privacy="public"), cfg)
    assert r.status == "published" and r.url == "https://t.me/chan/7"
    assert post.call_args.kwargs["data"]["chat_id"] == "@chan"


def test_telegram_size_limit(cfg, tmp_path, monkeypatch):
    big = tmp_path / "big.mp4"
    with open(big, "wb") as fh:
        fh.seek(51 * 1024 * 1024)
        fh.write(b"\0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:a")
    r = TelegramPublisher().publish(_req(big, privacy="public"), cfg)
    assert r.status == "error" and "50" in r.message


def test_vk_flow_and_errors(cfg, clip_file, monkeypatch):
    monkeypatch.setenv("VK_ACCESS_TOKEN", "tok")
    cfg["publish"]["platforms"]["vk"]["group_id"] = "555"
    vk = VKPublisher()
    with mock.patch("clipstudio.publish.vk.requests.post") as post:
        save = mock.Mock(); save.json.return_value = {"response": {"upload_url": "https://up", "owner_id": -555, "video_id": 9}}
        up = mock.Mock(); up.content = b"{}"; up.json.return_value = {"owner_id": -555, "video_id": 9}
        post.side_effect = [save, up]
        r = vk.publish(_req(clip_file), cfg)
    assert r.ok and r.status == "draft" and r.url == "https://vkvideo.ru/video-555_9"
    sent = post.call_args_list[0].kwargs["data"]
    assert sent["is_private"] == 1 and sent["group_id"] == "555"
    assert "Источник" in sent["description"]
    with mock.patch("clipstudio.publish.vk.requests.post") as post:
        post.return_value.json.return_value = {"error": {"error_code": 5, "error_msg": "invalid"}}
        r = vk.publish(_req(clip_file), cfg)
    assert not r.ok and "истёк" in r.message


def test_pack(cfg, clip_file):
    r = PackPublisher().publish(_req(clip_file, monetization_allowed=False), cfg)
    folder = cfg.data_dir / "packs" / "clip_1"
    assert r.status == "pack_ready"
    assert (folder / "video.mp4").exists() and (folder / "rutube.txt").exists()
    assert "НЕ разрешил монетизацию" in (folder / "README.txt").read_text(encoding="utf-8")


def test_discover_gate(cfg, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    sources = [{"id": "cc", "name": "CC", "platform": "youtube", "license": "cc-by", "enabled": True}]
    search = mock.Mock(); search.json.return_value = {"items": [{"id": {"videoId": "a"}}, {"id": {"videoId": "b"}}]}
    videos = mock.Mock(); videos.json.return_value = {"items": [
        {"id": "a", "snippet": {"title": "A", "channelTitle": "c"}, "contentDetails": {"duration": "PT10M5S", "definition": "hd"},
         "status": {"license": "creativeCommon"}, "statistics": {"viewCount": "10"}},
        {"id": "b", "snippet": {"title": "B", "channelTitle": "c"}, "contentDetails": {"duration": "PT1H", "definition": "sd"},
         "status": {"license": "youtube"}, "statistics": {"viewCount": "999"}},
    ]}
    with mock.patch("clipstudio.discover.requests.get", side_effect=[search, videos]):
        res = discover("тест", sources, cfg)
    by = {r["video_id"]: r for r in res["results"]}
    assert by["a"]["gate"]["allowed"] and by["a"]["duration_s"] == 605
    assert not by["b"]["gate"]["allowed"] and by["b"].get("note") == "низкое качество"
    assert res["results"][0]["video_id"] == "a"


def test_save_secret(cfg):
    env = cfg.root / ".env"
    env.write_text("# комментарий\nTELEGRAM_BOT_TOKEN=old\nOTHER=1\n", encoding="utf-8")
    save_secret(cfg, "TELEGRAM_BOT_TOKEN", "new")
    save_secret(cfg, "VK_ACCESS_TOKEN", "vk")
    text = env.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=new" in text and "OTHER=1" in text and "# комментарий" in text and "VK_ACCESS_TOKEN=vk" in text
    with pytest.raises(ValueError):
        save_secret(cfg, "PATH", "x")
