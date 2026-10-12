"""Загрузка настроек: config.yaml + .env + sources.yaml.

Секреты читаются только из окружения/.env и никогда не пишутся в config.yaml.
"""
from __future__ import annotations

import copy
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "app": {"host": "127.0.0.1", "port": 8765, "data_dir": "data", "language": "ru"},
    "llm": {
        "mode": "claude_cli",
        "model_moments": "sonnet",
        "model_review": "opus",
        "api_model_moments": "claude-sonnet-5-5",
        "api_model_review": "claude-opus-5-5",
        "chunk_minutes": 8,
        "max_candidates_per_chunk": 6,
        "claude_cli_path": "claude",
        "timeout_s": 600,
    },
    "transcribe": {"engine": "faster_whisper", "model": "small", "device": "auto", "compute_type": "auto"},
    "clips": {
        "min_seconds": 12,
        "max_seconds": 60,
        "pad_before": 0.35,
        "pad_after": 0.6,
        "format": "vertical",
        "reframe": "blur",
        "subtitle_style": "karaoke",
        "hook_title": True,
        "attribution_overlay": True,
        "loudness_lufs": -14,
        "crf": 20,
        "preset": "veryfast",
        "font": "Montserrat ExtraBold",
    },
    "publish": {
        "default_privacy": "private",
        "platforms": {
            "vk": {"enabled": True, "group_id": "", "album_id": "", "wallpost": False, "api_version": "5.199",
                   "id_base": "https://id.vk.ru"},
            "youtube": {
                "enabled": False,
                "client_secrets": "secrets/youtube_client_secret.json",
                "token_file": "secrets/youtube_token.json",
                "category_id": "23",
                "made_for_kids": False,
            },
            "telegram": {"enabled": False, "chat_id": "", "review_chat_id": ""},
            "pack": {"enabled": True, "targets": ["rutube", "dzen", "vk_clips", "ok"]},
        },
    },
    "discover": {"min_height": 1080, "max_results": 25},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env без внешних зависимостей. Не перетирает уже заданные переменные."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def ensure_user_files(root: Path = ROOT) -> None:
    """При первом запуске создаёт config.yaml, sources.yaml и .env из примеров."""
    for example, target in (
        ("config.example.yaml", "config.yaml"),
        ("sources.example.yaml", "sources.yaml"),
        (".env.example", ".env"),
    ):
        if not (root / target).exists() and (root / example).exists():
            shutil.copyfile(root / example, root / target)


class Config(dict):
    """Словарь настроек с удобным доступом к путям и секретам."""

    root: Path

    @property
    def data_dir(self) -> Path:
        p = Path(self["app"]["data_dir"])
        if not p.is_absolute():
            p = self.root / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def path(self, *parts: str) -> Path:
        p = self.data_dir.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @staticmethod
    def secret(name: str) -> str:
        return os.environ.get(name, "").strip()

    def platform(self, name: str) -> dict:
        return self["publish"]["platforms"].get(name, {})


def load_config(root: Path | None = None) -> Config:
    root = Path(root or os.environ.get("CLIPSTUDIO_ROOT") or ROOT)
    ensure_user_files(root)
    load_dotenv(root / ".env")
    user: dict = {}
    cfg_path = root / "config.yaml"
    if cfg_path.exists():
        user = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = Config(_deep_merge(DEFAULTS, user))
    cfg.root = root
    return cfg


def load_sources(cfg: Config) -> list[dict]:
    path = cfg.root / "sources.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("sources") or [])


def save_sources(cfg: Config, sources: list[dict]) -> None:
    path = cfg.root / "sources.yaml"
    header = (
        "# Белый список источников. Редактируется из GUI или вручную.\n"
        "# license: own | permission | cc-by | public-domain\n"
    )
    body = yaml.safe_dump({"sources": sources}, allow_unicode=True, sort_keys=False)
    path.write_text(header + body, encoding="utf-8")
