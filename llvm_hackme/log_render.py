from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def render_opencode_log(json_log: Path) -> Path:
    txt_log = json_log.with_suffix(".txt")
    lines: list[str] = []
    step_n = 0

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
            part = evt.get("part", {})
            body = _format_event(etype, part)

            if etype == "step_start":
                step_n += 1
                lines.append("")
                lines.append(f"{'─' * 60}")
                lines.append(f"STEP {step_n}  [{ts}]")
                lines.append(f"{'─' * 60}")
            elif etype == "step_finish":
                tokens = part.get("tokens", {})
                if tokens:
                    inp = tokens.get("input", 0)
                    out = tokens.get("output", 0)
                    r = tokens.get("reasoning", 0)
                    cost = tokens.get("cost", 0)
                    lines.append(
                        f"[{ts}]  tokens: in={inp} out={out} reason={r}  ${cost:.6f}"
                    )
            elif body:
                prefix = "  " if etype in ("reasoning", "text") else ""
                lines.append(f"{prefix}[{ts}] {body}")

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


def _format_event(etype: str, part: dict) -> str:
    if etype == "reasoning":
        text = part.get("text", "") if isinstance(part, dict) else ""
        return f"[THINK] {_truncate(text, 2000)}"

    if etype == "text":
        text = part.get("text", "") if isinstance(part, dict) else ""
        return f"[TEXT]  {text}"

    if etype == "tool_use":
        tool = part.get("tool", "?") if isinstance(part, dict) else "?"
        state = part.get("state", {}) if isinstance(part, dict) else {}
        inp = state.get("input", {})
        out = state.get("output", "")
        status = state.get("status", "")
        lines = [f"[TOOL]  {tool}"]
        if inp:
            inp_str = json.dumps(inp, ensure_ascii=False)
            lines.append(f"        input:  {_truncate(inp_str, 300)}")
        if out:
            out_str = str(out)
            if status == "completed":
                lines.append(f"        output: {_truncate(out_str, 2000)}")
            else:
                lines.append(f"        ({status}) {_truncate(out_str, 500)}")
        return "\n".join(lines)

    if etype == "error":
        err = part.get("error", {}) if isinstance(part, dict) else {}
        if isinstance(err, dict):
            err_name = err.get("name", "")
            err_data = err.get("data", err.get("message", ""))
            if isinstance(err_data, dict):
                err_data = err_data.get("message", json.dumps(err_data))
            return f"[ERROR] {err_name}: {err_data}"
        return f"[ERROR] {str(err)[:500]}"

    if isinstance(part, dict):
        leftover = {
            k: v
            for k, v in part.items()
            if k not in ("id", "messageID", "sessionID", "snapshot", "time", "type")
        }
        if leftover:
            return _truncate(json.dumps(leftover, ensure_ascii=False, default=str), 300)

    return ""


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."
