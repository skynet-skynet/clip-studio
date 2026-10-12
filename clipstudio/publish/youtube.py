"""Публикация на YouTube через Data API v3 (videos.insert) с OAuth-токеном пользователя."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import Config
from .base import PublishRequest, PublishResult, full_description

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _libs_ok() -> bool:
    try:
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401
    except ImportError:
        return False
    return True


def load_credentials(cfg: Config):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = cfg.resolve(cfg.platform("youtube")["token_file"])
    if not token_file.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _tags(tags: list[str], limit: int = 480) -> list[str]:
    out, total = [], 0
    for t in tags:
        t = t.strip().lstrip("#")
        if not t:
            continue
        add = len(t) + (2 if " " in t else 0) + 1
        if total + add > limit:
            break
        out.append(t)
        total += add
    return out


class YouTubePublisher:
    name = "youtube"
    title = "YouTube"

    def is_configured(self, cfg: Config) -> tuple[bool, str]:
        if not _libs_ok():
            return False, "pip install google-api-python-client google-auth-oauthlib"
        pc = cfg.platform("youtube")
        if not cfg.resolve(pc["client_secrets"]).exists():
            return False, f"Нет файла OAuth-клиента: {pc['client_secrets']} (гайд, раздел «YouTube»)"
        if not cfg.resolve(pc["token_file"]).exists():
            return False, "Нажмите «Войти в YouTube» в настройках"
        return True, "Готово"

    def publish(self, req: PublishRequest, cfg: Config) -> PublishResult:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        pc = cfg.platform("youtube")
        try:
            creds = load_credentials(cfg)
        except Exception as e:  # noqa: BLE001
            return PublishResult(self.name, False, "error", message=f"Токен YouTube не обновился, войдите заново: {e}")
        if not creds:
            return PublishResult(self.name, False, "error", message="Нет токена YouTube — войдите в настройках")
        shorts = req.vertical and 0 < req.duration <= 180
        title = (req.title or "Клип").strip()
        desc = full_description(req)
        if shorts and "#shorts" not in (title + desc).lower():
            desc = f"{desc}\n\n#Shorts".strip()
        body = {
            "snippet": {"title": title[:100], "description": desc[:5000], "tags": _tags(req.tags),
                        "categoryId": str(pc.get("category_id", "23"))},
            "status": {"privacyStatus": req.privacy if req.privacy in ("private", "unlisted", "public") else "private",
                       "selfDeclaredMadeForKids": bool(pc.get("made_for_kids", False))},
        }
        try:
            yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
            media = MediaFileUpload(str(req.clip_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
            request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                _status, response = request.next_chunk()
        except HttpError as e:
            reason = ""
            try:
                reason = json.loads(e.content.decode()).get("error", {}).get("errors", [{}])[0].get("reason", "")
            except Exception:  # noqa: BLE001
                pass
            if reason in ("quotaExceeded", "uploadLimitExceeded"):
                return PublishResult(self.name, False, "error", message="Лимит загрузок YouTube на сегодня исчерпан. Попробуйте завтра.")
            return PublishResult(self.name, False, "error", message=f"YouTube: {reason or e}")
        except Exception as e:  # noqa: BLE001
            return PublishResult(self.name, False, "error", message=f"YouTube: {e}")
        vid = response.get("id")
        url = f"https://www.youtube.com/shorts/{vid}" if shorts else f"https://www.youtube.com/watch?v={vid}"
        public = req.privacy == "public"
        return PublishResult(self.name, True, "published" if public else "draft", url=url, external_id=vid,
                             message="Опубликовано" if public else f"Загружено ({req.privacy}) — опубликуйте в YouTube Studio")


def run_oauth_flow(cfg: Config) -> Path:
    """Открывает браузер для входа в Google и сохраняет токен. Блокирующий вызов."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    pc = cfg.platform("youtube")
    secrets = cfg.resolve(pc["client_secrets"])
    if not secrets.exists():
        raise FileNotFoundError(f"Положите JSON OAuth-клиента (тип «Desktop app») в {secrets}")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    token_file = cfg.resolve(pc["token_file"])
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return token_file
