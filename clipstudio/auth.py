"""Авторизация площадок и хранение секретов в .env."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
from urllib.parse import urlencode

import requests

from .config import Config

ALLOWED_SECRETS = {
    "ANTHROPIC_API_KEY", "VK_ACCESS_TOKEN", "VK_REFRESH_TOKEN", "VK_CLIENT_ID", "YOUTUBE_API_KEY", "TELEGRAM_BOT_TOKEN",
}
_vk_pending: dict[str, dict] = {}
_yt_state: dict = {"running": False, "error": "", "done": False}


def save_secret(cfg: Config, name: str, value: str) -> None:
    """Пишет секрет в .env (обновляет строку или добавляет новую) и в окружение процесса."""
    if name not in ALLOWED_SECRETS:
        raise ValueError(f"Нельзя сохранить «{name}»: разрешены {', '.join(sorted(ALLOWED_SECRETS))}")
    value = (value or "").strip().replace("\n", "")
    path = cfg.root / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == name:
            lines[i] = f"{name}={value}"
            found = True
    if not found:
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[name] = value


def secrets_status() -> dict[str, bool]:
    return {name: bool(os.environ.get(name, "").strip()) for name in sorted(ALLOWED_SECRETS)}


# ---------- YouTube ----------

def youtube_auth_start(cfg: Config) -> dict:
    """Запускает вход в Google в фоне (откроется браузер). Токен сохранится сам."""
    from .publish.youtube import run_oauth_flow

    secrets_file = cfg.resolve(cfg.platform("youtube")["client_secrets"])
    if not secrets_file.exists():
        return {"started": False, "error": f"Сначала положите файл OAuth-клиента в {secrets_file} (гайд, раздел «YouTube»)."}
    if _yt_state["running"]:
        return {"started": True, "message": "Вход уже идёт — проверьте открытую вкладку браузера"}

    def worker():
        _yt_state.update(running=True, error="", done=False)
        try:
            run_oauth_flow(cfg)
            _yt_state["done"] = True
        except Exception as e:  # noqa: BLE001
            _yt_state["error"] = str(e)
        finally:
            _yt_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True, "message": "Откроется браузер: войдите в аккаунт Google с вашим каналом"}


def youtube_auth_state() -> dict:
    return dict(_yt_state)


# ---------- VK ID (OAuth 2.1 + PKCE) ----------

def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def vk_auth_start(cfg: Config, redirect_uri: str) -> dict:
    client_id = cfg.secret("VK_CLIENT_ID")
    if not client_id:
        return {"error": "Укажите VK_CLIENT_ID (ID приложения VK ID) — гайд, раздел «VK»."}
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    _vk_pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri}
    base = cfg.platform("vk").get("id_base", "https://id.vk.ru").rstrip("/")
    query = urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "video wall groups",
    })
    return {"url": f"{base}/authorize?{query}"}


def vk_auth_callback(cfg: Config, params: dict) -> dict:
    state = params.get("state", "")
    pending = _vk_pending.pop(state, None)
    if not pending:
        return {"ok": False, "error": "Сессия входа устарела. Нажмите «Войти через VK ID» ещё раз."}
    base = cfg.platform("vk").get("id_base", "https://id.vk.ru").rstrip("/")
    try:
        r = requests.post(f"{base}/oauth2/auth", data={
            "grant_type": "authorization_code", "code": params.get("code", ""), "code_verifier": pending["verifier"],
            "client_id": cfg.secret("VK_CLIENT_ID"), "device_id": params.get("device_id", ""),
            "redirect_uri": pending["redirect_uri"], "state": state,
        }, timeout=20)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "error": f"VK ID недоступен: {type(e).__name__}"}
    token = data.get("access_token")
    if not token:
        return {"ok": False, "error": f"VK ID не выдал токен: {data.get('error_description') or data.get('error') or 'неизвестно'}"}
    save_secret(cfg, "VK_ACCESS_TOKEN", token)
    if data.get("refresh_token"):
        save_secret(cfg, "VK_REFRESH_TOKEN", data["refresh_token"])
    return {"ok": True, "scope": data.get("scope", "")}


def vk_legacy_token_url(client_id: str) -> str:
    """Старый способ (Implicit Flow): токен появляется в адресной строке после входа.

    VK переводит приложения на VK ID, поэтому для новых приложений этот способ может не сработать.
    """
    return "https://oauth.vk.com/authorize?" + urlencode({
        "client_id": client_id, "display": "page", "redirect_uri": "https://oauth.vk.com/blank.html",
        "scope": "video,wall,groups,offline", "response_type": "token", "v": "5.199",
    })
