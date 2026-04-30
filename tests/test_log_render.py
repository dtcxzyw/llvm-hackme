from __future__ import annotations

import json
from pathlib import Path

from llvm_hackme.log_render import render_opencode_log


def _write_jsonl(path: Path, events: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(evt) + "\n")


def test_renders_reasoning(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "reasoning",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "part": {"type": "reasoning", "text": "I should try instcombine"},
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "[THINK]" in content
    assert "I should try instcombine" in content


def test_renders_text(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "text",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "part": {
                    "type": "text",
                    "text": "Based on analysis, no bug found",
                },
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "[TEXT]" in content
    assert "no bug found" in content


def test_renders_tool_use(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "tool_use",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "part": {
                    "type": "tool",
                    "tool": "hack_baseline_opt",
                    "state": {
                        "status": "completed",
                        "input": {},
                        "output": '{"pass_name": "instcombine"}',
                    },
                },
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "[TOOL]" in content
    assert "hack_baseline_opt" in content
    assert "instcombine" in content


def test_renders_step_start_with_header(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "step_start",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "part": {"type": "step-start", "snapshot": "abc123"},
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "STEP 1" in content


def test_renders_step_finish_tokens(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "step_finish",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "part": {
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {
                        "input": 100,
                        "output": 50,
                        "reasoning": 30,
                        "cost": 0.005,
                    },
                },
            }
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "tokens:" in content
    assert "in=100" in content
    assert "out=50" in content
    assert "$0.005000" in content


def test_renders_error(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "error",
                "timestamp": 1000000,
                "sessionID": "ses_abc",
                "part": {
                    "error": {"name": "CrashError", "data": {"message": "segfault"}},
                },
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


def test_empty_log(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    json_log.write_text("", encoding="utf-8")
    txt_log = render_opencode_log(json_log)
    assert txt_log.read_text() == "\n"


def test_multiple_steps(tmp_path: Path) -> None:
    json_log = tmp_path / "test.jsonl"
    _write_jsonl(
        json_log,
        [
            {
                "type": "step_start",
                "timestamp": 1,
                "part": {"type": "step-start"},
            },
            {
                "type": "reasoning",
                "timestamp": 2,
                "part": {"type": "reasoning", "text": "think"},
            },
            {
                "type": "step_finish",
                "timestamp": 3,
                "part": {"type": "step-finish", "tokens": {"cost": 0}},
            },
            {
                "type": "step_start",
                "timestamp": 4,
                "part": {"type": "step-start"},
            },
            {
                "type": "text",
                "timestamp": 5,
                "part": {"type": "text", "text": "done"},
            },
            {
                "type": "step_finish",
                "timestamp": 6,
                "part": {"type": "step-finish", "tokens": {}},
            },
        ],
    )
    txt_log = render_opencode_log(json_log)
    content = txt_log.read_text()
    assert "STEP 1" in content
    assert "STEP 2" in content
    assert "think" in content
    assert "done" in content
