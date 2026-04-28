from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def render_opencode_log(json_log: Path) -> Path:
    txt_log = json_log.with_suffix(".txt")
    lines: list[str] = []

    with open(json_log, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                lines.append(f"[PARSE ERROR] {raw[:200]}")
                continue

            ts = _format_ts(evt.get("timestamp"))
            etype = evt.get("type", "unknown")
            body = _format_body(evt)
            if body:
                lines.append(f"[{ts}] [{etype}] {body}")
            else:
                lines.append(f"[{ts}] [{etype}]")

    txt_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return txt_log


def _format_ts(ts: int | float | None) -> str:
    if ts is None:
        return "??:??:??"
    try:
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        return dt.strftime("%H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return "??:??:??"


def _format_body(evt: dict) -> str:
    etype = evt.get("type", "")
    etype_lower = etype.lower()

    if etype_lower == "message":
        role = evt.get("role", "?")
        content = evt.get("content", "")
        return f"[{role}] {content}"

    if etype_lower == "tool_call":
        tool = evt.get("tool", evt.get("name", "?"))
        inp = json.dumps(evt.get("input", evt.get("args", {})), ensure_ascii=False)
        return f"{tool}({inp})"

    if etype_lower == "tool_result":
        tool = evt.get("tool", evt.get("name", "?"))
        out = evt.get("output", evt.get("result", evt.get("content", "")))
        if isinstance(out, dict | list):
            out = json.dumps(out, ensure_ascii=False)
        return f"{tool} → {_truncate(str(out), 500)}"

    if etype_lower in ("thinking", "thought"):
        content = evt.get("content", evt.get("text", ""))
        return _truncate(str(content), 1000)

    if etype_lower == "error":
        err = evt.get("error", {})
        if isinstance(err, dict):
            err_name = err.get("name", "")
            err_data = err.get("data", err.get("message", ""))
            if isinstance(err_data, dict):
                err_data = err_data.get("message", json.dumps(err_data))
            return f"{err_name}: {err_data}"
        return str(err)[:500]

    if etype_lower == "session":
        action = evt.get("action", evt.get("status", ""))
        sid = evt.get("sessionID", "")
        return f"{action} session={sid}"

    if etype_lower == "result":
        text = evt.get("text", evt.get("content", evt.get("output", "")))
        return _truncate(str(text), 500)

    leftover = {
        k: v for k, v in evt.items() if k not in ("type", "timestamp", "sessionID")
    }
    if leftover:
        return _truncate(json.dumps(leftover, ensure_ascii=False, default=str), 300)
    return ""


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."
