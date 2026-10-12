"""Командная строка. Её же вызывает Claude Code из слэш-команд (/clip, /review, /publish).

Примеры:
  python -m clipstudio                         # запустить GUI (http://127.0.0.1:8765)
  python -m clipstudio status
  python -m clipstudio import stream.mp4 --source me --subs stream.srt
  python -m clipstudio fetch https://vkvideo.ru/video-1_2 --source example_streamer
  python -m clipstudio job "моменты, где он злится на чат" --videos 1 --count 5
  python -m clipstudio approve 12 14 15 && python -m clipstudio render 3
  python -m clipstudio review 7
  python -m clipstudio publish 7 --to vk,telegram,pack --privacy private
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

from . import llm
from .config import load_config, load_sources
from .db import DB


def _out(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1, default=str))
    elif isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=1, default=str))


def _ctx():
    cfg = load_config()
    db = DB(cfg.data_dir / "clipstudio.db")
    return cfg, db


def cmd_serve(args) -> None:
    import uvicorn

    from .server import create_app

    cfg = load_config()
    host, port = cfg["app"]["host"], int(args.port or cfg["app"]["port"])
    url = f"http://{host}:{port}"
    print(f"Clip Studio: {url}  (остановить — Ctrl+C)")
    if not args.no_browser:
        webbrowser.open(url)
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")


def cmd_status(args) -> None:
    from .media import ffmpeg_status
    from .publish import publisher_statuses
    from .transcribe import whisper_status

    cfg, _ = _ctx()
    _out({"llm": llm.status(cfg), "ffmpeg": ffmpeg_status(), "transcribe": whisper_status(cfg),
          "publishers": publisher_statuses(cfg)}, True)


def cmd_sources(args) -> None:
    cfg, _ = _ctx()
    _out([{k: s.get(k) for k in ("id", "name", "platform", "license", "enabled", "monetization_allowed")} for s in load_sources(cfg)], True)


def cmd_videos(args) -> None:
    _, db = _ctx()
    rows = db.query("SELECT id, title, source_id, duration, width, height, rights_status, status FROM videos ORDER BY id DESC")
    _out(rows, True)


def cmd_import(args) -> None:
    from .ingest import GateBlocked, import_local

    cfg, db = _ctx()
    try:
        v = import_local(cfg, db, Path(args.file), args.source, title=args.title or "",
                         subs_path=Path(args.subs) if args.subs else None, origin_url=args.origin or "",
                         license_confirmed=args.confirm_license)
    except GateBlocked as e:
        sys.exit(f"Заблокировано юридическим шлюзом: {e}")
    _out({"id": v["id"], "title": v["title"], "rights": v["rights_reason"]}, True)


def cmd_fetch(args) -> None:
    from .ingest import GateBlocked, import_url

    cfg, db = _ctx()
    try:
        v = import_url(cfg, db, args.url, args.source, on_status=lambda s: print(s, file=sys.stderr))
    except GateBlocked as e:
        sys.exit(f"Заблокировано юридическим шлюзом: {e}")
    _out({k: v.get(k) for k in ("id", "title", "status", "status_message", "rights_reason")}, True)


def cmd_discover(args) -> None:
    from .discover import discover

    cfg, _ = _ctx()
    _out(discover(args.query, load_sources(cfg), cfg, args.limit, args.source or None), True)


def _pipe():
    from .pipeline import Pipeline

    cfg, db = _ctx()
    return cfg, db, Pipeline(cfg, db)


def _print_candidates(db: DB, job_id: int) -> None:
    rows = db.query("SELECT id, video_id, start, \"end\", total, status, title, hook, quote, risk FROM candidates WHERE job_id = ? ORDER BY total DESC", (job_id,))
    for r in rows:
        risk = f"  ⚠ {r['risk']}" if r.get("risk") else ""
        print(f"#{r['id']:<4} [{r['status']:<8}] {r['total']:>4.1f}  видео {r['video_id']}  {r['start']:.1f}–{r['end']:.1f}с  {r['title']}{risk}")
        if r.get("quote"):
            print(f"       «{r['quote'][:140]}»")


def cmd_job(args) -> None:
    cfg, db, pipe = _pipe()
    params = {"video_ids": [int(x) for x in args.videos.split(",") if x.strip()], "count": args.count,
              "format": args.format, "reframe": args.reframe, "compilation": args.compilation, "auto": args.auto,
              "min_seconds": args.min, "max_seconds": args.max}
    jid = pipe.create_job(args.request, params)
    print(f"Задача {jid}: анализ…", file=sys.stderr)
    pipe.run_sync(jid, "analyze")
    job = db.get("jobs", jid)
    if job["status"] == "waiting_manual":
        print(f"Ручной режим. Промпт: python -m clipstudio manual {jid}", file=sys.stderr)
        return
    print(f"Статус: {job['status']} — {job['stage']} (стоимость ≈ ${job.get('cost_usd') or 0})")
    _print_candidates(db, jid)
    if job["status"] == "done":
        for c in db.query("SELECT id, path FROM clips WHERE job_id = ?", (jid,)):
            print(f"Клип {c['id']}: {c['path']}")


def cmd_candidates(args) -> None:
    _, db = _ctx()
    _print_candidates(db, args.job)


def cmd_approve(args) -> None:
    _, db = _ctx()
    for cid in args.ids:
        db.update("candidates", cid, status="rejected" if args.reject else "approved")
    print("ok")


def cmd_render(args) -> None:
    _, db, pipe = _pipe()
    pipe.run_sync(args.job, "render")
    for c in db.query("SELECT id, title, path, duration FROM clips WHERE job_id = ? ORDER BY id", (args.job,)):
        print(f"Клип {c['id']} ({c['duration']:.1f}с): {c['title']} → {c['path']}")


def cmd_review(args) -> None:
    _, _, pipe = _pipe()
    try:
        _out(pipe.review_clip(args.clip), True)
    except llm.ManualNeeded as m:
        print(m.prompt)


def cmd_publish(args) -> None:
    _, _, pipe = _pipe()
    res = pipe.publish_clip(args.clip, [p.strip() for p in args.to.split(",") if p.strip()], args.privacy)
    _out(res, True)


def cmd_manual(args) -> None:
    _, _, pipe = _pipe()
    if args.answer:
        text = Path(args.answer).read_text(encoding="utf-8") if Path(args.answer).exists() else args.answer
        res = pipe.manual_answer(args.job, text, enqueue=False)
        _out(res, True)
        if not res["next"]:
            pipe.run_sync(args.job, "finish_manual")
            _print_candidates(pipe.db, args.job)
        return
    state = pipe.manual_state(args.job)
    print(state["prompt"] or "Задача не ждёт ответа")


def cmd_secret(args) -> None:
    import getpass

    from .auth import save_secret

    cfg, _ = _ctx()
    value = args.value if args.value is not None else getpass.getpass(f"{args.name} (ввод скрыт): ")
    save_secret(cfg, args.name, value)
    print(f"{args.name} сохранён в .env")


def cmd_wait(args) -> None:
    _, db = _ctx()
    while True:
        j = db.get("jobs", args.job)
        print(f"{j['status']:<14} {int((j['progress'] or 0) * 100):>3}%  {j['stage']}", file=sys.stderr)
        if j["status"] in ("review", "done", "error", "cancelled", "waiting_manual"):
            break
        time.sleep(3)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="clipstudio", description="Clip Studio — нарезки смешных моментов")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="запустить GUI")
    s.add_argument("--port", type=int)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("status", help="проверить настройку").set_defaults(fn=cmd_status)
    sub.add_parser("sources", help="белый список источников").set_defaults(fn=cmd_sources)
    sub.add_parser("videos", help="библиотека видео").set_defaults(fn=cmd_videos)

    s = sub.add_parser("import", help="добавить видео с диска")
    s.add_argument("file")
    s.add_argument("--source", required=True)
    s.add_argument("--subs")
    s.add_argument("--title")
    s.add_argument("--origin", help="ссылка на оригинал (для cc-by)")
    s.add_argument("--confirm-license", action="store_true", help="я проверил лицензию CC BY")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("fetch", help="скачать видео по ссылке (после проверки прав)")
    s.add_argument("url")
    s.add_argument("--source", required=True)
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("discover", help="найти видео в разрешённых источниках")
    s.add_argument("query")
    s.add_argument("--source", action="append")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("job", help="найти моменты по запросу")
    s.add_argument("request")
    s.add_argument("--videos", required=True, help="id через запятую")
    s.add_argument("--count", type=int, default=5)
    s.add_argument("--format", choices=["vertical", "horizontal", "square"])
    s.add_argument("--reframe", choices=["blur", "center", "face"])
    s.add_argument("--min", type=float)
    s.add_argument("--max", type=float)
    s.add_argument("--compilation", action="store_true", help="склеить всё в один ролик")
    s.add_argument("--auto", action="store_true", help="сам одобрить сильные моменты и отрендерить")
    s.set_defaults(fn=cmd_job)

    s = sub.add_parser("candidates", help="кандидаты задачи")
    s.add_argument("job", type=int)
    s.set_defaults(fn=cmd_candidates)

    s = sub.add_parser("approve", help="одобрить кандидатов")
    s.add_argument("ids", type=int, nargs="+")
    s.add_argument("--reject", action="store_true")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("render", help="отрендерить одобренные")
    s.add_argument("job", type=int)
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("review", help="разбор клипа менторами (Opus)")
    s.add_argument("clip", type=int)
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("publish", help="опубликовать клип")
    s.add_argument("clip", type=int)
    s.add_argument("--to", required=True, help="vk,youtube,telegram,pack")
    s.add_argument("--privacy", choices=["private", "unlisted", "public"])
    s.set_defaults(fn=cmd_publish)

    s = sub.add_parser("manual", help="ручной режим: показать промпт или передать ответ")
    s.add_argument("job", type=int)
    s.add_argument("--answer", help="файл или текст с JSON-ответом Claude")
    s.set_defaults(fn=cmd_manual)

    s = sub.add_parser("secret", help="сохранить ключ в .env (ввод скрыт)")
    s.add_argument("name", choices=["ANTHROPIC_API_KEY", "VK_ACCESS_TOKEN", "VK_REFRESH_TOKEN", "VK_CLIENT_ID", "YOUTUBE_API_KEY", "TELEGRAM_BOT_TOKEN"])
    s.add_argument("--value")
    s.set_defaults(fn=cmd_secret)

    s = sub.add_parser("wait", help="ждать завершения задачи")
    s.add_argument("job", type=int)
    s.set_defaults(fn=cmd_wait)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        args = ap.parse_args(["serve", *(argv or [])])
    args.fn(args)
