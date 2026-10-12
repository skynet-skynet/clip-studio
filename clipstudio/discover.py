"""Поиск видео-кандидатов только внутри белого списка: каналы с разрешением и YouTube CC BY."""
from __future__ import annotations

import logging
import re

import requests

from . import rights
from .config import Config

log = logging.getLogger(__name__)
YT = "https://www.googleapis.com/youtube/v3/"


def _iso_duration(s: str) -> int:
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _gate(source: dict, api_license: str) -> dict:
    d = rights.evaluate(source, reported_license=api_license)
    return {"allowed": d.allowed, "reason": d.reason}


def _youtube(cfg: Config, query: str, source: dict, limit: int, warnings: list[str]) -> list[dict]:
    key = cfg.secret("YOUTUBE_API_KEY")
    if not key:
        warnings.append("YOUTUBE_API_KEY не задан — поиск по YouTube пропущен")
        return []
    params = {"part": "snippet", "type": "video", "maxResults": min(limit, 50), "key": key, "q": query or "",
              "relevanceLanguage": cfg["app"].get("language", "ru"), "videoEmbeddable": "true"}
    if source.get("license") == "cc-by":
        params["videoLicense"] = "creativeCommon"
    elif source.get("youtube_channel_id"):
        params["channelId"] = source["youtube_channel_id"]
        params["order"] = "date" if not query else "relevance"
    else:
        warnings.append(f"Источник «{source.get('name')}»: не указан youtube_channel_id")
        return []
    try:
        s = requests.get(YT + "search", params=params, timeout=20).json()
        ids = [it["id"]["videoId"] for it in s.get("items", []) if it.get("id", {}).get("videoId")]
        if not ids:
            return []
        v = requests.get(YT + "videos", params={"part": "snippet,contentDetails,status,statistics", "id": ",".join(ids), "key": key}, timeout=20).json()
    except (requests.RequestException, ValueError, KeyError) as e:
        warnings.append(f"YouTube недоступен: {type(e).__name__}")
        return []
    if "error" in v:
        warnings.append(f"YouTube API: {v['error'].get('message', 'ошибка')}")
        return []
    out = []
    for it in v.get("items", []):
        sn, cd, st = it.get("snippet", {}), it.get("contentDetails", {}), it.get("status", {})
        lic = st.get("license", "")
        height = 1080 if cd.get("definition") == "hd" else 480
        out.append({
            "platform": "youtube", "video_id": it["id"], "url": f"https://www.youtube.com/watch?v={it['id']}",
            "title": sn.get("title", ""), "channel_title": sn.get("channelTitle", ""),
            "duration_s": _iso_duration(cd.get("duration", "")), "max_height": height,
            "views": int(it.get("statistics", {}).get("viewCount", 0) or 0), "published_at": sn.get("publishedAt", "")[:10],
            "license": lic, "thumbnail": (sn.get("thumbnails", {}).get("high") or {}).get("url", ""),
            "source_id": source["id"], "gate": _gate(source, lic),
        })
    return out


def _vk(cfg: Config, query: str, source: dict, limit: int, warnings: list[str]) -> list[dict]:
    token = cfg.secret("VK_ACCESS_TOKEN")
    if not token:
        warnings.append("VK_ACCESS_TOKEN не задан — поиск по VK пропущен")
        return []
    owner = str(source.get("vk_owner_id") or "").strip()
    if not owner:
        warnings.append(f"Источник «{source.get('name')}»: не указан vk_owner_id")
        return []
    try:
        data = requests.post("https://api.vk.com/method/video.get", data={
            "owner_id": owner, "count": 200, "access_token": token, "v": cfg.platform("vk").get("api_version", "5.199"),
        }, timeout=20).json()
    except (requests.RequestException, ValueError) as e:
        warnings.append(f"VK недоступен: {type(e).__name__}")
        return []
    if "error" in data:
        warnings.append(f"VK: {data['error'].get('error_msg', 'ошибка')}")
        return []
    words = [w for w in re.findall(r"\w+", (query or "").lower()) if len(w) > 2]
    out = []
    for it in (data.get("response") or {}).get("items", []):
        text = f"{it.get('title', '')} {it.get('description', '')}".lower()
        if words and not any(w in text for w in words):
            continue
        heights = [int(k.split("_")[1]) for k in (it.get("files") or {}) if k.startswith("mp4_") and k.split("_")[1].isdigit()]
        img = max(it.get("image") or [{}], key=lambda x: x.get("width", 0))
        out.append({
            "platform": "vk", "video_id": f"{it.get('owner_id')}_{it.get('id')}",
            "url": f"https://vkvideo.ru/video{it.get('owner_id')}_{it.get('id')}",
            "title": it.get("title", ""), "channel_title": source.get("name", ""), "duration_s": int(it.get("duration") or 0),
            "max_height": max(heights) if heights else int(it.get("height") or 0), "views": int(it.get("views") or 0),
            "published_at": "", "license": source.get("license", ""), "thumbnail": img.get("url", ""),
            "source_id": source["id"], "gate": _gate(source, ""),
        })
    return out[:limit]


def discover(query: str, sources: list[dict], cfg: Config, limit: int = 15, source_ids: list[str] | None = None) -> dict:
    warnings: list[str] = []
    results: list[dict] = []
    picked = [s for s in sources if (not source_ids or s.get("id") in source_ids)]
    for src in picked:
        if not src.get("enabled", True):
            continue
        if src.get("platform") == "youtube":
            results += _youtube(cfg, query, src, limit, warnings)
        elif src.get("platform") == "vk":
            results += _vk(cfg, query, src, limit, warnings)
    min_h = int(cfg["discover"].get("min_height", 1080))
    for r in results:
        if r["max_height"] and r["max_height"] < min_h:
            r["note"] = "низкое качество"
    results.sort(key=lambda r: (not r["gate"]["allowed"], -r["views"]))
    if not picked:
        warnings.append("Нет подходящих источников в белом списке")
    return {"results": results[: max(limit, 1) * 2], "warnings": warnings}
