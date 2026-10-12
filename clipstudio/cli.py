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
  python -m clipstudio cut 1 812.4 841.9 --title "Шкаф" --hook "ПЛАН БЫЛ ИДЕАЛЬНЫЙ" --jumpcut
  python -m clipstudio export 3 --format edl       # в DaVinci Resolve / Premiere (или csv для LosslessCut)
  python -m clipstudio mcp                         # MCP-сервер для Claude Code / Claude Desktop
  python -m clipstudio connect-claude [--desktop]  # подключить MCP-сервер к Claude
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
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


def _browser_candidates() -> list[str]:
    names = ["msedge", "chrome", "google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave"]
    found = [p for n in names if (p := shutil.which(n))]
    if sys.platform.startswith("win"):
        for base in (os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", ""), os.environ.get("LOCALAPPDATA", "")):
            for rel in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
                p = Path(base) / rel
                if base and p.exists():
                    found.append(str(p))
    elif sys.platform == "darwin":
        for app in ("Microsoft Edge", "Google Chrome", "Chromium"):
            p = Path(f"/Applications/{app}.app/Contents/MacOS/{app}")
            if p.exists():
                found.append(str(p))
    return found


def open_app_window(url: str) -> None:
    """Открывает интерфейс отдельным окном без вкладок (режим --app у Edge/Chrome), иначе — в браузере."""
    for exe in _browser_candidates():
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1440,920"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            continue
    webbrowser.open(url)


def _port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def cmd_serve(args) -> None:
    import uvicorn

    from .server import create_app

    cfg = load_config()
    host, port = cfg["app"]["host"], int(args.port or cfg["app"]["port"])
    url = f"http://{host}:{port}"
    if _port_busy(host, port):
        print(f"Clip Studio уже запущен: {url}")
        if not args.no_browser:
            open_app_window(url)
        return
    print(f"Clip Studio: {url}\nЭто окно не закрывайте, пока работаете. Остановить — Ctrl+C.")
    if not args.no_browser:
        threading.Timer(1.2, open_app_window, args=(url,)).start()
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")


def cmd_mcp(args) -> None:
    from .mcp_server import main as mcp_main

    mcp_main()


def cmd_connect(args) -> None:
    from . import integrations

    cfg, _ = _ctx()
    results = {"claude_code": integrations.connect_claude_code(cfg)}
    if args.desktop:
        results["claude_desktop"] = integrations.connect_claude_desktop(cfg)
    if args.ffmpeg:
        results["ffmpeg_mcp"] = integrations.connect_ffmpeg_mcp(cfg)
    _out(results, True)


def cmd_cut(args) -> None:
    _, db, pipe = _pipe()
    jid = pipe.create_manual_clip(args.video, args.start, args.end, title=args.title or "", hook=args.hook or "",
                                  params={"format": args.format, "reframe": args.reframe, "jumpcut": args.jumpcut})
    pipe.run_sync(jid, "render_manual")
    for c in db.query("SELECT id, path, duration FROM clips WHERE job_id = ?", (jid,)):
        print(f"Клип {c['id']} ({c['duration']:.1f} с): {c['path']}")


def cmd_export(args) -> None:
    cfg, _, pipe = _pipe()
    name, content = pipe.export_job(args.job, args.format)
    out = cfg.path("exports") / name
    out.write_text(content, encoding="utf-8")
    print(out)


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


def _fix_console() -> None:
    """Кириллица в консоли Windows и в собранной программе: вывод всегда в UTF-8."""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> None:
    _fix_console()
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

    sub.add_parser("mcp", help="MCP-сервер (stdio) для Claude Code / Claude Desktop").set_defaults(fn=cmd_mcp)

    s = sub.add_parser("connect-claude", help="подключить MCP-сервер к Claude Code (и Claude Desktop)")
    s.add_argument("--desktop", action="store_true", help="ещё и в Claude Desktop")
    s.add_argument("--ffmpeg", action="store_true", help="ещё и ffmpeg-mcp-lite (нужен uv)")
    s.set_defaults(fn=cmd_connect)

    s = sub.add_parser("cut", help="вырезать клип вручную по времени")
    s.add_argument("video", type=int)
    s.add_argument("start", type=float)
    s.add_argument("end", type=float)
    s.add_argument("--title")
    s.add_argument("--hook")
    s.add_argument("--format", choices=["vertical", "horizontal", "square"])
    s.add_argument("--reframe", choices=["blur", "center", "face"])
    s.add_argument("--jumpcut", action="store_true", help="вырезать паузы")
    s.set_defaults(fn=cmd_cut)

    s = sub.add_parser("export", help="экспорт отрезков задачи в EDL или CSV")
    s.add_argument("job", type=int)
    s.add_argument("--format", choices=["edl", "csv"], default="edl")
    s.set_defaults(fn=cmd_export)

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
