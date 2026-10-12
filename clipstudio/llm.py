"""Связь с Claude. Три режима:

claude_cli — через установленный Claude Code: `claude -p ... --output-format json --json-schema ...`.
             Тратит лимиты вашей подписки Pro/Max, отдельный API-ключ не нужен.
api        — через Anthropic API (ANTHROPIC_API_KEY), оплата по токенам.
manual     — промпт показывается в GUI; вы вставляете его в claude.ai или Claude Code web,
             а JSON-ответ возвращаете обратно. Ничего не стоит сверх подписки.

Распределение моделей: purpose="moments" → Sonnet (массовая работа), purpose="review" → Opus (разбор менторами).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from .config import ROOT, Config

log = logging.getLogger(__name__)

PROMPTS = ROOT / "prompts"

MOMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "quote": {"type": "string"},
                    "setup": {"type": "string"},
                    "why": {"type": "string"},
                    "scores": {
                        "type": "object",
                        "properties": {k: {"type": "number"} for k in ("editor", "comedian", "retention", "compliance")},
                        "required": ["editor", "comedian", "retention", "compliance"],
                    },
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "risk": {"type": "string"},
                },
                "required": ["start", "end", "quote", "why", "scores", "title", "hook"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["moments"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["publish", "fix", "drop"]},
        "notes": {"type": "array", "items": {"type": "string"}},
        "trim": {
            "type": "object",
            "properties": {"start_delta": {"type": "number"}, "end_delta": {"type": "number"}},
        },
        "title_options": {"type": "array", "items": {"type": "string"}},
        "hook_options": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "platform_tips": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "notes", "title_options", "description"],
}


class LLMError(RuntimeError):
    pass


class ManualNeeded(Exception):
    """В режиме manual: нужно, чтобы человек отнёс промпт в Claude и вернул ответ."""

    def __init__(self, prompt: str):
        super().__init__("Нужен ответ Claude, вставленный вручную")
        self.prompt = prompt


@dataclass
class LLMResult:
    data: dict
    cost_usd: float = 0.0
    model: str = ""


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def _cli_path(cfg: Config) -> str | None:
    return shutil.which(cfg["llm"].get("claude_cli_path") or "claude")


def status(cfg: Config) -> dict:
    mode = cfg["llm"]["mode"]
    base = {"mode": mode, "model_moments": cfg["llm"]["model_moments"], "model_review": cfg["llm"]["model_review"]}
    if mode == "claude_cli":
        path = _cli_path(cfg)
        if not path:
            return {**base, "ready": False, "message": "Claude Code не найден. Установите его (гайд, раздел «Claude Code») или переключитесь в режим manual."}
        try:
            ver = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception as e:  # noqa: BLE001
            return {**base, "ready": False, "message": f"Claude Code не отвечает: {e}"}
        return {**base, "ready": True, "message": f"Claude Code {ver.split()[0] if ver else ''} найден"}
    if mode == "api":
        if not cfg.secret("ANTHROPIC_API_KEY"):
            return {**base, "ready": False, "message": "Нет ANTHROPIC_API_KEY в .env"}
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return {**base, "ready": False, "message": "pip install anthropic"}
        return {**base, "model_moments": cfg["llm"]["api_model_moments"], "model_review": cfg["llm"]["api_model_review"],
                "ready": True, "message": "Anthropic API"}
    return {**base, "ready": True, "message": "Ручной режим: промпт копируется в claude.ai или Claude Code web"}


def extract_json(text: str) -> dict:
    """Достаёт JSON-объект из ответа модели (в т.ч. из ```json ... ``` блока)."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        first, last = text.find("{"), text.rfind("}")
        if first != -1 and last > first:
            text = text[first:last + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"Ответ Claude — не JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMError("Ответ Claude должен быть JSON-объектом")
    return data


def manual_prompt(system: str, user: str, schema: dict) -> str:
    return (
        f"{system}\n\n---\n\n{user}\n\n---\n\n"
        "Ответь ОДНИМ блоком JSON без пояснений вокруг, строго по этой JSON Schema:\n"
        f"```json\n{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}\n```"
    )


def complete_json(cfg: Config, system: str, user: str, schema: dict, purpose: str = "moments") -> LLMResult:
    mode = cfg["llm"]["mode"]
    if mode == "manual":
        raise ManualNeeded(manual_prompt(system, user, schema))
    if mode == "api":
        return _via_api(cfg, system, user, schema, purpose)
    return _via_cli(cfg, system, user, schema, purpose)


def _via_cli(cfg: Config, system: str, user: str, schema: dict, purpose: str) -> LLMResult:
    path = _cli_path(cfg)
    if not path:
        raise LLMError("Claude Code не найден (команда claude). Установите его или смените llm.mode.")
    model = cfg["llm"]["model_review" if purpose == "review" else "model_moments"]
    # Короткий системный промпт в аргументах, всё объёмное — через stdin (нет лимита длины командной строки)
    cmd = [
        path, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--model", model,
        "--system-prompt", "Ты — редакция нарезок. Следуй инструкциям в сообщении и отвечай строго по JSON-схеме.",
        "--tools", "",
        "--no-session-persistence",
    ]
    with tempfile.TemporaryDirectory(prefix="clipstudio_") as tmp:  # пустая папка: без чужого CLAUDE.md и хуков
        try:
            proc = subprocess.run(
                cmd, input=f"{system}\n\n---\n\n{user}", capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=tmp, timeout=int(cfg["llm"].get("timeout_s", 600)),
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError("Claude Code не ответил вовремя (llm.timeout_s). Уменьшите llm.chunk_minutes.") from e
    raw = (proc.stdout or "").strip()
    if not raw:
        raise LLMError(f"Claude Code вернул пустой ответ. {(proc.stderr or '').strip()[-400:]}")
    envelope: dict = {"result": raw}
    for candidate in (raw, raw.splitlines()[-1]):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            envelope = parsed
            break
    if envelope.get("is_error") or proc.returncode != 0:
        msg = envelope.get("result") or proc.stderr or "неизвестная ошибка"
        raise LLMError(f"Claude Code: {str(msg)[:500]}")
    data = envelope.get("structured_output")
    if not isinstance(data, dict):
        data = extract_json(str(envelope.get("result", "")))
    return LLMResult(data=data, cost_usd=float(envelope.get("total_cost_usd") or 0), model=model)


def _via_api(cfg: Config, system: str, user: str, schema: dict, purpose: str) -> LLMResult:
    try:
        import anthropic
    except ImportError as e:
        raise LLMError("Для режима api: pip install anthropic") from e
    key = cfg.secret("ANTHROPIC_API_KEY")
    if not key:
        raise LLMError("Нет ANTHROPIC_API_KEY в .env")
    model = cfg["llm"]["api_model_review" if purpose == "review" else "api_model_moments"]
    client = anthropic.Anthropic(api_key=key)
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[{"name": "submit", "description": "Вернуть результат по схеме", "input_schema": schema}],
            tool_choice={"type": "tool", "name": "submit"},
        )
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"Anthropic API: {e}") from e
    block = next((b for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
    if block is None:
        text = "".join(getattr(b, "text", "") for b in msg.content)
        return LLMResult(data=extract_json(text), model=model)
    return LLMResult(data=dict(block.input), model=model)
