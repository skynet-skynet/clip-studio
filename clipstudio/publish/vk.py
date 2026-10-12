"""Публикация в VK Видео через VK API: video.save → загрузка файла на upload_url."""
from __future__ import annotations

import logging

import requests

from ..config import Config
from .base import PublishRequest, PublishResult, full_description

log = logging.getLogger(__name__)
API = "https://api.vk.com/method/"

VK_ERRORS = {
    5: "Токен VK недействителен или истёк. Получите новый (гайд, раздел «VK»).",
    6: "Слишком много запросов к VK. Подождите минуту.",
    7: "У токена нет права на это действие. Нужен доступ video (и wall, если включён wallpost).",
    15: "Доступ запрещён. Проверьте, что вы админ сообщества и у токена есть право video.",
    17: "VK требует подтвердить действие в браузере (validation required).",
    100: "VK отклонил параметры запроса.",
    204: "Нет доступа к видеоальбому или сообществу.",
}


def _vk_error(data: dict) -> str | None:
    err = data.get("error") if isinstance(data, dict) else None
    if not err:
        return None
    code = err.get("error_code")
    return f"{VK_ERRORS.get(code, 'Ошибка VK')} (код {code}: {err.get('error_msg', '')})"


class VKPublisher:
    name = "vk"
    title = "VK Видео"

    def is_configured(self, cfg: Config) -> tuple[bool, str]:
        if not cfg.secret("VK_ACCESS_TOKEN"):
            return False, "Нет VK_ACCESS_TOKEN в .env — авторизуйтесь в «Настройках»"
        gid = cfg.platform("vk").get("group_id")
        return True, f"Готово: публикация в сообщество {gid}" if gid else "Готово: публикация на вашу страницу"

    def publish(self, req: PublishRequest, cfg: Config) -> PublishResult:
        pc = cfg.platform("vk")
        token = cfg.secret("VK_ACCESS_TOKEN")
        public = req.privacy == "public"
        params = {
            "access_token": token,
            "v": pc.get("api_version", "5.199"),
            "name": (req.title or "Клип")[:128],
            "description": full_description(req)[:5000],
            "wallpost": 1 if (public and pc.get("wallpost")) else 0,
            "repeat": 1 if req.vertical and req.duration <= 60 else 0,
        }
        if pc.get("group_id"):
            params["group_id"] = str(pc["group_id"]).lstrip("-")
        if pc.get("album_id"):
            params["album_id"] = pc["album_id"]
        if not public:
            # Своя страница: видимо только вам. Сообщество: privacy_view к нему не применяется,
            # поэтому видео грузится «приватным» (is_private=1 — не попадает в список видео сообщества).
            if pc.get("group_id"):
                params["is_private"] = 1
            else:
                params["privacy_view"] = "only_me"
        try:
            r = requests.post(API + "video.save", data=params, timeout=60)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            return PublishResult(self.name, False, "error", message=f"VK недоступен: {e}")
        if err := _vk_error(data):
            return PublishResult(self.name, False, "error", message=err)
        resp = data.get("response") or {}
        upload_url = resp.get("upload_url")
        if not upload_url:
            return PublishResult(self.name, False, "error", message="VK не вернул адрес загрузки")
        try:
            with open(req.clip_path, "rb") as fh:
                up = requests.post(upload_url, files={"video_file": (req.clip_path.name, fh, "video/mp4")}, timeout=1800)
            up_data = up.json() if up.content else {}
        except (requests.RequestException, ValueError, OSError) as e:
            return PublishResult(self.name, False, "error", message=f"Загрузка файла в VK не удалась: {e}")
        if isinstance(up_data, dict) and up_data.get("error"):
            return PublishResult(self.name, False, "error", message=f"VK отклонил файл: {up_data.get('error')}")
        owner_id = up_data.get("owner_id") or resp.get("owner_id")
        video_id = up_data.get("video_id") or resp.get("video_id")
        url = f"https://vkvideo.ru/video{owner_id}_{video_id}" if owner_id and video_id else None
        return PublishResult(
            self.name, True, "published" if public else "draft", url=url,
            external_id=f"{owner_id}_{video_id}" if video_id else None,
            message="Опубликовано" if public else "Загружено приватно — откройте в VK и опубликуйте вручную",
        )
