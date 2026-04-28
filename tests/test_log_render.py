from __future__ import annotations

import json
from pathlib import Path

from llvm_hackme.log_render import render_opencode_log


def _write_jsonl(path: Path, events: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(evt) + "\n")


def test_renders_message_event(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "message",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "role": "assistant",
                "content": "Hello world",
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "[assistant] Hello world" in content


def test_renders_tool_call(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "tool_call",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "tool": "hack_context",
                "input": {"key": "value"},
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "hack_context" in content
    assert '"key": "value"' in content


def test_renders_tool_result(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "tool_result",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "tool": "hack_alive2",
                "output": "0 incorrect transformations",
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "hack_alive2" in content
    assert "0 incorrect" in content


def test_renders_thinking(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "thinking",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "content": "I should try instcombine",
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "I should try instcombine" in content


def test_renders_error(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "error",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "error": {"name": "CrashError", "data": {"message": "segfault"}},
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "CrashError" in content
    assert "segfault" in content


def test_handles_parse_error(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    json_log.write_text("not json\n", encoding="utf-8")
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "PARSE ERROR" in content


def test_multiple_events(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {"type": "thinking", "timestamp": 1, "content": "a"},
            {"type": "message", "timestamp": 2, "role": "user", "content": "b"},
        ],
    )
    txt_log = render_opencode_log(json_log)
    lines = txt_log.read_text().strip().split("\n")
    assert len(lines) == 2
    assert "a" in lines[0]
    assert "b" in lines[1]


def test_renders_session_event(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "session",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "action": "started",
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "session=ses_abc" in content


def test_renders_unknown_event(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "custom_event",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "foo": "bar",
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "foo" in content
    assert "bar" in content


def test_empty_log(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    json_log.write_text("", encoding="utf-8")
    txt_log = render_opencode_log(json_log)
    assert txt_log.read_text() == "\n"
