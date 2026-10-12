"""Подключение Clip Studio к Claude Code и Claude Desktop как MCP-сервера, плюс внешние MCP."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import FROZEN, ROOT, Config

SERVER_NAME = "clip-studio"


def mcp_command() -> list[str]:
    """Команда запуска MCP-сервера на этом компьютере (с абсолютными путями)."""
    if FROZEN:
        return [sys.executable, "mcp"]
    return [sys.executable, "-m", "clipstudio", "mcp"]


def mcp_env() -> dict[str, str]:
    """Окружение MCP-сервера: где лежат данные, и (для запуска из исходников) где лежит пакет."""
    env = {"CLIPSTUDIO_ROOT": str(ROOT)}
    if not FROZEN:
        env["PYTHONPATH"] = str(ROOT)  # чтобы `python -m clipstudio` работал из любой папки
    return env


def _claude(cfg: Config) -> str | None:
    return shutil.which(cfg["llm"].get("claude_cli_path") or "claude")


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def claude_code_status(cfg: Config) -> dict:
    cli = _claude(cfg)
    if not cli:
        return {"available": False, "registered": False, "message": "Claude Code не установлен"}
    try:
        res = _run([cli, "mcp", "get", SERVER_NAME], timeout=30)
    except Exception as e:  # noqa: BLE001
        return {"available": True, "registered": False, "message": f"Не удалось проверить: {e}"}
    ok = res.returncode == 0
    return {"available": True, "registered": ok, "message": "Подключён" if ok else "Не подключён"}


def connect_claude_code(cfg: Config) -> dict:
    """Регистрирует MCP-сервер в Claude Code для всех проектов (scope user)."""
    cli = _claude(cfg)
    if not cli:
        return {"ok": False, "message": "Claude Code не найден. Установите его (методичка, раздел «Claude Code»)."}
    _run([cli, "mcp", "remove", SERVER_NAME, "--scope", "user"], timeout=30)  # перерегистрация — не ошибка
    # --env ставим перед другими флагами: иначе Claude Code примет имя сервера за ещё одну пару KEY=value
    env_flags = [x for k, v in mcp_env().items() for x in ("--env", f"{k}={v}")]
    cmd = [cli, "mcp", "add", *env_flags, "--scope", "user", "--transport", "stdio", SERVER_NAME, "--", *mcp_command()]
    res = _run(cmd)
    if res.returncode != 0:
        return {"ok": False, "message": (res.stderr or res.stdout or "ошибка").strip()[-500:]}
    return {"ok": True, "message": "Готово. В Claude Code появятся инструменты clip-studio (проверка: /mcp)."}


def desktop_config_path() -> Path:
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def connect_claude_desktop(cfg: Config) -> dict:
    """Добавляет сервер в claude_desktop_config.json (с резервной копией). Нужен перезапуск Claude Desktop."""
    path = desktop_config_path()
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "message": f"Файл {path} повреждён — исправьте его вручную"}
        backup = path.with_suffix(f".backup-{datetime.now():%Y%m%d-%H%M%S}.json")
        shutil.copyfile(path, backup)
    cmd = mcp_command()
    data.setdefault("mcpServers", {})[SERVER_NAME] = {"command": cmd[0], "args": cmd[1:], "env": mcp_env()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "message": f"Записано в {path}. Перезапустите Claude Desktop."}


def connect_ffmpeg_mcp(cfg: Config) -> dict:
    """Дополнительно: универсальный ffmpeg-mcp-lite (github.com/kevinwatt/ffmpeg-mcp-lite) через uvx."""
    cli, uvx = _claude(cfg), shutil.which("uvx")
    if not cli:
        return {"ok": False, "message": "Claude Code не найден"}
    if not uvx:
        return {"ok": False, "message": "Нужен uv (https://docs.astral.sh/uv/): после установки появится команда uvx"}
    res = _run([cli, "mcp", "add", "--scope", "user", "--transport", "stdio", "ffmpeg", "--", uvx, "ffmpeg-mcp-lite"])
    if res.returncode != 0:
        return {"ok": False, "message": (res.stderr or res.stdout).strip()[-400:]}
    return {"ok": True, "message": "ffmpeg-mcp-lite подключён к Claude Code"}


def mcp_json_snippet() -> str:
    cmd = mcp_command()
    return json.dumps({"mcpServers": {SERVER_NAME: {"command": cmd[0], "args": cmd[1:], "env": mcp_env()}}},
                      ensure_ascii=False, indent=2)
