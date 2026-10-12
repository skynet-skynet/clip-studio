"""Локальный веб-сервер: API для GUI и раздача медиа. Слушает только 127.0.0.1."""
from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, auth, llm, rights
from .config import ROOT, Config, load_config, load_sources, save_sources
from .db import DB
from .discover import discover
from .ingest import GateBlocked, import_local, import_url
from .media import ffmpeg_status
from .pipeline import Pipeline
from .publish import publisher_statuses
from .transcribe import whisper_status


def _media_url(cfg: Config, path: str | None) -> str:
    if not path:
        return ""
    try:
        rel = Path(path).resolve().relative_to(cfg.data_dir.resolve())
    except ValueError:
        return ""
    return "/media/" + rel.as_posix()


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    db = DB(cfg.data_dir / "clipstudio.db")
    pipe = Pipeline(cfg, db)
    app = FastAPI(title="Clip Studio", version=__version__)
    # Защита от DNS rebinding: API отвечает только на обращения к localhost
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.state.cfg, app.state.db, app.state.pipe = cfg, db, pipe

    # ---------- сериализация ----------
    def video_out(v: dict) -> dict:
        return {
            **{k: v.get(k) for k in ("id", "source_id", "title", "origin_url", "duration", "width", "height",
                                     "rights_status", "rights_reason", "status", "status_message", "created_at", "channel")},
            "media_url": _media_url(cfg, v.get("local_path")),
            "has_transcript": bool(v.get("transcript_path")) or (cfg.data_dir / "transcripts" / f"{v['id']}.json").exists(),
            "has_subs": bool(v.get("subs_path")),
        }

    def cand_out(c: dict) -> dict:
        v = db.get("videos", c["video_id"]) or {}
        url = _media_url(cfg, v.get("local_path"))
        return {**c, "preview_url": f"{url}#t={c['start']:.1f},{c['end']:.1f}" if url else ""}

    def clip_out(c: dict) -> dict:
        out = {k: c.get(k) for k in ("id", "job_id", "candidate_ids", "duration", "width", "height", "title",
                                     "description", "tags", "attribution", "review", "created_at")}
        out["monetization_allowed"] = bool(c.get("monetization_allowed"))
        out["media_url"] = _media_url(cfg, c.get("path"))
        out["thumb_url"] = _media_url(cfg, c.get("thumb"))
        return out

    def job_brief(j: dict) -> dict:
        return {k: j.get(k) for k in ("id", "request", "params", "status", "progress", "stage", "log", "error",
                                      "cost_usd", "created_at", "updated_at")}

    # ---------- статус ----------
    @app.get("/api/status")
    def status():
        return {
            "version": __version__, "llm": llm.status(cfg), "ffmpeg": ffmpeg_status(),
            "transcribe": whisper_status(cfg), "publishers": publisher_statuses(cfg), "secrets": auth.secrets_status(),
            "youtube_auth": auth.youtube_auth_state(),
        }

    # ---------- источники ----------
    @app.get("/api/sources")
    def sources_list():
        return {"sources": load_sources(cfg)}

    @app.post("/api/sources")
    async def sources_save(request: Request):
        body = await request.json()
        sid = str(body.get("id") or "").strip()
        if not sid or not sid.replace("_", "").replace("-", "").isalnum():
            raise HTTPException(400, "id источника: латиница, цифры, _ и -")
        if body.get("license") not in rights.ALLOWED_LICENSES:
            raise HTTPException(400, "license: own, permission, cc-by или public-domain")
        fields = ("id", "name", "platform", "channel_url", "vk_owner_id", "youtube_channel_id", "license", "proof",
                  "proof_date", "monetization_allowed", "attribution", "rules", "enabled")
        src = {k: body.get(k) for k in fields if body.get(k) is not None}
        src.setdefault("enabled", True)
        items = load_sources(cfg)
        items = [s for s in items if s.get("id") != sid] + [src]
        save_sources(cfg, items)
        return {"source": src}

    @app.delete("/api/sources/{sid}")
    def sources_delete(sid: str):
        save_sources(cfg, [s for s in load_sources(cfg) if s.get("id") != sid])
        return {"ok": True}

    # ---------- видео ----------
    @app.get("/api/videos")
    def videos_list():
        return {"videos": [video_out(v) for v in db.query("SELECT * FROM videos ORDER BY id DESC")]}

    @app.post("/api/videos/upload")
    async def videos_upload(
        file: UploadFile = File(...), source_id: str = Form(...), title: str = Form(""),
        origin_url: str = Form(""), license_confirmed: bool = Form(False), subs: UploadFile | None = File(None),
    ):
        tmp = Path(tempfile.mkdtemp(prefix="upload_", dir=cfg.path("tmp")))
        try:
            fpath = tmp / Path(file.filename or "video.mp4").name
            with fpath.open("wb") as fh:
                shutil.copyfileobj(file.file, fh, length=4 * 1024 * 1024)
            spath = None
            if subs is not None and subs.filename:
                spath = tmp / Path(subs.filename).name
                with spath.open("wb") as fh:
                    shutil.copyfileobj(subs.file, fh)
            try:
                v = import_local(cfg, db, fpath, source_id, title=title, subs_path=spath, origin_url=origin_url,
                                 license_confirmed=license_confirmed, move=True)
            except GateBlocked as e:
                raise HTTPException(403, f"Юридический шлюз: {e}") from e
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return {"video": video_out(v)}

    @app.post("/api/videos/url")
    async def videos_url(request: Request):
        body = await request.json()
        url, source_id = str(body.get("url", "")).strip(), str(body.get("source_id", "")).strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "Нужна ссылка http(s)")
        result: dict = {}

        def work():
            try:
                result["video"] = import_url(cfg, db, url, source_id)
            except GateBlocked as e:
                result["blocked"] = str(e)
            except Exception as e:  # noqa: BLE001
                result["error"] = str(e)

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(timeout=25)  # проверка прав быстрая; долгая загрузка продолжится в фоне
        if "blocked" in result:
            raise HTTPException(403, f"Юридический шлюз: {result['blocked']}")
        if "error" in result:
            raise HTTPException(400, result["error"])
        if "video" in result:
            return {"video": video_out(result["video"])}
        rows = db.query("SELECT * FROM videos WHERE origin_url = ? ORDER BY id DESC LIMIT 1", (url,))
        if rows:
            return {"video": video_out(rows[0])}
        return {"video": None, "message": "Проверка ещё идёт — обновите список через минуту"}

    @app.delete("/api/videos/{vid}")
    def videos_delete(vid: int):
        v = db.get("videos", vid)
        if not v:
            raise HTTPException(404, "Видео не найдено")
        for key in ("local_path", "subs_path", "transcript_path"):
            if v.get(key):
                Path(v[key]).unlink(missing_ok=True)
        db.delete("videos", vid)
        return {"ok": True}

    @app.get("/api/videos/{vid}/transcript")
    def video_transcript(vid: int):
        v = db.get("videos", vid)
        path = Path(v["transcript_path"]) if v and v.get("transcript_path") else None
        if not path or not path.exists():
            raise HTTPException(404, "Транскрипта ещё нет")
        return FileResponse(path, media_type="application/json")

    # ---------- поиск ----------
    @app.post("/api/discover")
    async def discover_route(request: Request):
        body = await request.json()
        return discover(str(body.get("query", "")), load_sources(cfg), cfg, int(body.get("limit") or 15),
                        body.get("source_ids") or None)

    # ---------- задачи ----------
    @app.post("/api/jobs")
    async def jobs_create(request: Request):
        body = await request.json()
        params = {k: body.get(k) for k in ("video_ids", "count", "min_seconds", "max_seconds", "format", "reframe",
                                           "subtitle_style", "compilation", "auto")}
        try:
            jid = pipe.create_job(str(body.get("request", "")), params)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        pipe.submit(jid, "analyze")
        return {"job": job_brief(db.get("jobs", jid))}

    @app.get("/api/jobs")
    def jobs_list():
        return {"jobs": [job_brief(j) for j in db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT 100")]}

    @app.get("/api/jobs/{jid}")
    def jobs_get(jid: int):
        j = db.get("jobs", jid)
        if not j:
            raise HTTPException(404, "Задача не найдена")
        cands = db.query("SELECT * FROM candidates WHERE job_id = ? ORDER BY total DESC", (jid,))
        clips = db.query("SELECT * FROM clips WHERE job_id = ? ORDER BY id", (jid,))
        return {"job": job_brief(j), "candidates": [cand_out(c) for c in cands], "clips": [clip_out(c) for c in clips]}

    @app.post("/api/jobs/{jid}/render")
    def jobs_render(jid: int):
        if not db.get("jobs", jid):
            raise HTTPException(404, "Задача не найдена")
        db.update("jobs", jid, status="rendering", stage="В очереди на рендер")
        pipe.submit(jid, "render")
        return {"ok": True}

    @app.post("/api/jobs/{jid}/cancel")
    def jobs_cancel(jid: int):
        db.update("jobs", jid, status="cancelled", stage="Отменено")
        return {"ok": True}

    @app.get("/api/jobs/{jid}/manual")
    def jobs_manual_get(jid: int):
        return pipe.manual_state(jid)

    @app.post("/api/jobs/{jid}/manual")
    async def jobs_manual_post(jid: int, request: Request):
        body = await request.json()
        try:
            return pipe.manual_answer(jid, str(body.get("answer", "")))
        except (ValueError, llm.LLMError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/candidates/{cid}")
    async def candidate_update(cid: int, request: Request):
        c = db.get("candidates", cid)
        if not c:
            raise HTTPException(404, "Кандидат не найден")
        body = await request.json()
        fields = {k: body[k] for k in ("status", "start", "end", "title", "hook") if k in body}
        if "status" in fields and fields["status"] not in ("approved", "rejected", "proposed"):
            raise HTTPException(400, "status: approved, rejected или proposed")
        for k in ("start", "end"):
            if k in fields:
                fields[k] = round(float(fields[k]), 2)
        if fields.get("end", c["end"]) <= fields.get("start", c["start"]):
            raise HTTPException(400, "Конец должен быть позже начала")
        db.update("candidates", cid, **fields)
        return {"candidate": cand_out(db.get("candidates", cid))}

    # ---------- клипы ----------
    @app.get("/api/clips")
    def clips_list():
        return {"clips": [clip_out(c) for c in db.query("SELECT * FROM clips WHERE path IS NOT NULL ORDER BY id DESC")]}

    @app.get("/api/clips/{clip_id}")
    def clips_get(clip_id: int):
        c = db.get("clips", clip_id)
        if not c:
            raise HTTPException(404, "Клип не найден")
        pubs = db.query("SELECT * FROM publications WHERE clip_id = ? ORDER BY id DESC", (clip_id,))
        return {"clip": clip_out(c), "publications": pubs}

    @app.post("/api/clips/{clip_id}")
    async def clips_update(clip_id: int, request: Request):
        if not db.get("clips", clip_id):
            raise HTTPException(404, "Клип не найден")
        body = await request.json()
        fields = {k: body[k] for k in ("title", "description", "tags") if k in body}
        db.update("clips", clip_id, **fields)
        return {"clip": clip_out(db.get("clips", clip_id))}

    @app.post("/api/clips/{clip_id}/review")
    def clips_review(clip_id: int):
        try:
            return {"review": pipe.review_clip(clip_id)}
        except llm.ManualNeeded as m:
            return {"review": None, "manual_prompt": m.prompt}
        except (ValueError, llm.LLMError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/clips/{clip_id}/apply")
    async def clips_apply(clip_id: int, request: Request):
        body = await request.json()
        try:
            clip = pipe.apply_review(
                clip_id, title=body.get("title"), hook=body.get("hook"), description=body.get("description"),
                tags=body.get("tags"), apply_trim=bool(body.get("apply_trim")),
            )
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        return {"clip": clip_out(clip)}

    @app.post("/api/clips/{clip_id}/review/manual")
    async def clips_review_manual(clip_id: int, request: Request):
        body = await request.json()
        try:
            data = llm.extract_json(str(body.get("answer", "")))
        except llm.LLMError as e:
            raise HTTPException(400, str(e)) from e
        db.update("clips", clip_id, review=data)
        return {"review": data}

    @app.delete("/api/clips/{clip_id}")
    def clips_delete(clip_id: int):
        c = db.get("clips", clip_id)
        if c:
            for key in ("path", "thumb"):
                if c.get(key):
                    Path(c[key]).unlink(missing_ok=True)
            db.delete("clips", clip_id)
        return {"ok": True}

    @app.post("/api/clips/{clip_id}/publish")
    async def clips_publish(clip_id: int, request: Request):
        body = await request.json()
        platforms = [p for p in body.get("platforms") or [] if isinstance(p, str)]
        if not platforms:
            raise HTTPException(400, "Выберите площадки")
        privacy = body.get("privacy") or cfg["publish"].get("default_privacy", "private")
        if privacy not in ("private", "unlisted", "public"):
            raise HTTPException(400, "privacy: private, unlisted или public")
        try:
            return {"results": pipe.publish_clip(clip_id, platforms, privacy)}
        except ValueError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/publications")
    def publications():
        return {"publications": db.query("SELECT * FROM publications ORDER BY id DESC LIMIT 300")}

    # ---------- авторизация ----------
    @app.get("/api/auth/youtube/start")
    def yt_start():
        return auth.youtube_auth_start(cfg)

    @app.get("/api/auth/vk/start")
    def vk_start(request: Request):
        redirect = str(request.base_url).rstrip("/") + "/api/auth/vk/callback"
        res = auth.vk_auth_start(cfg, redirect)
        res["legacy_url"] = auth.vk_legacy_token_url(cfg.secret("VK_CLIENT_ID")) if cfg.secret("VK_CLIENT_ID") else ""
        return res

    @app.get("/api/auth/vk/callback", response_class=HTMLResponse)
    def vk_callback(request: Request):
        res = auth.vk_auth_callback(cfg, dict(request.query_params))
        msg = "VK подключён. Можно закрыть вкладку." if res.get("ok") else f"Не получилось: {res.get('error')}"
        return f"<!doctype html><meta charset=utf-8><body style='font:16px system-ui;padding:40px'>{msg}</body>"

    @app.post("/api/settings/secret")
    async def set_secret(request: Request):
        body = await request.json()
        try:
            auth.save_secret(cfg, str(body.get("name", "")), str(body.get("value", "")))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "secrets": auth.secrets_status()}

    # ---------- настройки ----------
    def _public_config() -> dict:
        return {k: v for k, v in cfg.items()}

    @app.get("/api/config")
    def config_get():
        return {"config": _public_config()}

    @app.post("/api/config")
    async def config_set(request: Request):
        body = await request.json()
        from .config import _deep_merge  # noqa: PLC0415

        allowed = {"llm", "transcribe", "clips", "publish", "discover"}
        patch = {k: v for k, v in body.items() if k in allowed and isinstance(v, dict)}
        merged = _deep_merge(dict(cfg), patch)
        cfg.clear()
        cfg.update(merged)
        path = cfg.root / "config.yaml"
        user = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        user = _deep_merge(user or {}, patch)
        path.write_text(yaml.safe_dump(user, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return {"config": _public_config()}

    @app.get("/api/guide")
    def guide():
        path = ROOT / "docs" / "GUIDE.md"
        return {"markdown": path.read_text(encoding="utf-8") if path.exists() else "Гайд не найден"}

    @app.get("/api/ledger")
    def ledger():
        return {"rows": db.query("SELECT * FROM rights_ledger ORDER BY id DESC LIMIT 1000")}

    # ---------- статика ----------
    app.mount("/media", StaticFiles(directory=str(cfg.data_dir)), name="media")
    app.mount("/fonts", StaticFiles(directory=str(ROOT / "fonts")), name="fonts")
    app.mount("/", StaticFiles(directory=str(ROOT / "web"), html=True), name="web")
    return app
