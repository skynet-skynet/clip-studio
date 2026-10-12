import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """Изолированный проект: свой config/sources/.env и data во временной папке."""
    for name in ("config.example.yaml", "sources.example.yaml"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / ".env.example").write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    for key in ("VK_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "YOUTUBE_API_KEY", "ANTHROPIC_API_KEY", "VK_CLIENT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLIPSTUDIO_ROOT", str(tmp_path))
    from clipstudio.config import load_config

    c = load_config(tmp_path)
    c["transcribe"]["engine"] = "subtitles_only"
    return c


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    """Короткое тестовое видео 640x360, 130 секунд, со звуком."""
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=0x3366aa:size=640x360:rate=15:duration=130",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=130",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out),
    ], check=True)
    return out


@pytest.fixture()
def sample_srt() -> Path:
    return FIXTURES / "stream.srt"
