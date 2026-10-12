"""Пакет для ручной загрузки туда, где нет удобного API: Rutube, Дзен, VK Клипы, Одноклассники."""
from __future__ import annotations

import shutil

from ..config import Config
from .base import PublishRequest, PublishResult, full_description

TARGETS = {
    "rutube": {
        "title": "Rutube", "title_limit": 100, "desc_limit": 5000,
        "steps": "studio.rutube.ru → «Загрузить видео» → выберите video.mp4 → вставьте название и описание из rutube.txt → "
                 "категория «Юмор» → обложка cover.jpg → «Опубликовать» (или «Сохранить как черновик»).",
    },
    "dzen": {
        "title": "Дзен", "title_limit": 100, "desc_limit": 5000,
        "steps": "dzen.ru/profile/editor → «Создать» → «Ролик» (вертикальное видео до 3 минут) → загрузите video.mp4 → "
                 "вставьте текст из dzen.txt → «Опубликовать».",
    },
    "vk_clips": {
        "title": "VK Клипы", "title_limit": 100, "desc_limit": 2000,
        "steps": "Приложение ВКонтакте → «Клипы» → «+» → «Загрузить из галереи» (сначала перешлите video.mp4 на телефон) → "
                 "описание из vk_clips.txt → опубликуйте от имени сообщества.",
    },
    "ok": {
        "title": "Одноклассники", "title_limit": 100, "desc_limit": 3000,
        "steps": "ok.ru → ваша группа → «Видео» → «Загрузить» → video.mp4 → текст из ok.txt → «Опубликовать».",
    },
}


class PackPublisher:
    name = "pack"
    title = "Пакет для ручной загрузки"

    def is_configured(self, cfg: Config) -> tuple[bool, str]:
        targets = cfg.platform("pack").get("targets") or []
        names = ", ".join(TARGETS[t]["title"] for t in targets if t in TARGETS)
        return True, f"Готовит папку для: {names or 'нет целей'}"

    def publish(self, req: PublishRequest, cfg: Config) -> PublishResult:
        folder = cfg.path("packs", f"clip_{req.clip_id}")
        shutil.copyfile(req.clip_path, folder / "video.mp4")
        if req.cover_path and req.cover_path.exists():
            shutil.copyfile(req.cover_path, folder / "cover.jpg")
        desc = full_description(req)
        hashtags = " ".join(f"#{t.strip().lstrip('#').replace(' ', '_')}" for t in req.tags if t.strip())
        readme = [
            f"Клип {req.clip_id}: {req.title}",
            "",
            "Атрибуция обязательна: не удаляйте строку «Источник…» из описания." if req.attribution else
            "Собственное видео: атрибуция не нужна.",
            "Монетизация разрешена источником." if req.monetization_allowed else
            "ВНИМАНИЕ: источник НЕ разрешил монетизацию — не включайте рекламу и монетизацию для этого клипа.",
            "",
        ]
        for key in cfg.platform("pack").get("targets") or []:
            t = TARGETS.get(key)
            if not t:
                continue
            text = (
                f"НАЗВАНИЕ:\n{req.title[: t['title_limit']]}\n\n"
                f"ОПИСАНИЕ:\n{desc[: t['desc_limit']]}\n\n"
                f"ТЕГИ:\n{hashtags}\n"
            )
            (folder / f"{key}.txt").write_text(text, encoding="utf-8")
            readme += [f"== {t['title']} ==", t["steps"], ""]
        (folder / "README.txt").write_text("\n".join(readme), encoding="utf-8")
        return PublishResult(self.name, True, "pack_ready", url=folder.resolve().as_uri(),
                             message=f"Папка готова: {folder}")
