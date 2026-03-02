"""Tests for mine_session_logs.py."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def mine_module():
    """Load mine_session_logs.py as a module."""
    script_path = Path(__file__).resolve().parents[1] / "mine_session_logs.py"
    spec = importlib.util.spec_from_file_location("mine_session_logs", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load mine_session_logs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── find_project_dirs ──


def test_find_project_dirs(mine_module, tmp_path: Path):
    """Create mock dirs matching allowlist encoding, verify matches."""
    # Simulate ~/.claude/projects/ directory structure
    base = tmp_path / "projects"
    base.mkdir()

    # Directory encoding: dash-separated absolute path
    (base / "-Users-alice-PycharmProjects-claude-trading-skills").mkdir()
    (base / "-Users-bob-Code-trade-edge-finder").mkdir()
    (base / "-Users-carol-Projects-unrelated-project").mkdir()

    allowlist = ["claude-trading-skills", "trade-edge-finder"]
    result = mine_module.find_project_dirs(base, allowlist)

    assert len(result) == 2
    names = [name for name, _ in result]
    assert "claude-trading-skills" in names
    assert "trade-edge-finder" in names


def test_find_project_dirs_no_match(mine_module, tmp_path: Path):
    """No matching dirs returns empty list."""
    base = tmp_path / "projects"
    base.mkdir()
    (base / "-Users-alice-Projects-something-else").mkdir()

    result = mine_module.find_project_dirs(base, ["claude-trading-skills"])
    assert result == []


# ── list_session_logs ──


def test_list_session_logs_date_filter(mine_module, tmp_path: Path):
    """Create files with recent and old mtime, verify filter."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()

    # Recent file (now)
    recent = proj_dir / "recent_session.jsonl"
    recent.write_text('{"type":"user"}\n')

    # Old file (30 days ago)
    old = proj_dir / "old_session.jsonl"
    old.write_text('{"type":"user"}\n')
    old_time = time.time() - (30 * 86400)
    os.utime(old, (old_time, old_time))

    project_dirs = [("test-project", proj_dir)]
    result = mine_module.list_session_logs(project_dirs, lookback_days=7)

    assert len(result) == 1
    assert result[0][0] == "test-project"
    assert result[0][1].name == "recent_session.jsonl"


# ── parse_session ──


def test_parse_user_messages_str(mine_module, tmp_path: Path):
    """Parse JSONL with string content format."""
    log = tmp_path / "session.jsonl"
    lines = [
        json.dumps(
            {
                "type": "user",
                "message": {"type": "user", "content": "Analyze AAPL"},
                "userType": "external",
                "timestamp": "2026-02-28T10:00:00+00:00",
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {"type": "user", "content": "Check breadth"},
                "userType": "external",
                "timestamp": "2026-02-28T10:01:00+00:00",
            }
        ),
    ]
    log.write_text("\n".join(lines))

    result = mine_module.parse_session(log)
    assert len(result["user_messages"]) == 2
    assert result["user_messages"][0] == "Analyze AAPL"
    assert result["user_messages"][1] == "Check breadth"


def test_parse_user_messages_list(mine_module, tmp_path: Path):
    """Parse JSONL with list[{type,text}] content format."""
    log = tmp_path / "session.jsonl"
    lines = [
        json.dumps(
            {
                "type": "user",
                "message": {
                    "type": "user",
                    "content": [
                        {"type": "text", "text": "Create a new skill"},
                        {"type": "text", "text": "for dividend analysis"},
                    ],
                },
                "userType": "external",
                "timestamp": "2026-02-28T10:00:00+00:00",
            }
        ),
    ]
    log.write_text("\n".join(lines))

    result = mine_module.parse_session(log)
    assert len(result["user_messages"]) == 2
    assert result["user_messages"][0] == "Create a new skill"
    assert result["user_messages"][1] == "for dividend analysis"


def test_parse_tool_usage(mine_module, tmp_path: Path):
    """Extract tool_use blocks from assistant messages."""
    log = tmp_path / "session.jsonl"
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "type": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {
                                "command": "python3 skills/pead-screener/scripts/screen_pead.py"
                            },
                        },
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/tmp/report.md"},
                        },
                    ],
                },
                "timestamp": "2026-02-28T10:00:00+00:00",
            }
        ),
    ]
    log.write_text("\n".join(lines))

    result = mine_module.parse_session(log)
    tool_uses = [t for t in result["tool_uses"] if not t["name"].startswith("__")]
    assert len(tool_uses) == 2
    assert tool_uses[0]["name"] == "Bash"
    assert tool_uses[1]["name"] == "Read"


def test_parse_malformed_jsonl(mine_module, tmp_path: Path):
    """Bad lines are skipped, good lines parsed."""
    log = tmp_path / "session.jsonl"
    lines = [
        "this is not json",
        json.dumps(
            {
                "type": "user",
                "message": {"type": "user", "content": "Valid message"},
                "userType": "external",
                "timestamp": "2026-02-28T10:00:00+00:00",
            }
        ),
        "{broken json",
        json.dumps(
            {
                "type": "user",
                "message": {"type": "user", "content": "Another valid one"},
                "userType": "external",
                "timestamp": "2026-02-28T10:01:00+00:00",
            }
        ),
    ]
    log.write_text("\n".join(lines))

    result = mine_module.parse_session(log)
    assert len(result["user_messages"]) == 2
    assert result["user_messages"][0] == "Valid message"
    assert result["user_messages"][1] == "Another valid one"


# ── detect_signals ──


def test_detect_skill_usage(mine_module):
    """Detect skills/ references in tool args."""
    tool_uses = [
        {
            "name": "Bash",
            "input": {"command": "python3 skills/earnings-trade-analyzer/scripts/run.py"},
        },
        {
            "name": "Read",
            "input": {"file_path": "skills/pead-screener/SKILL.md"},
        },
        {
            "name": "Bash",
            "input": {"command": "ls -la"},
        },
    ]
    result = mine_module._detect_skill_usage(tool_uses)
    assert result["count"] == 2
    assert "earnings-trade-analyzer" in result["skills"]
    assert "pead-screener" in result["skills"]


def test_detect_errors(mine_module):
    """Detect error patterns in tool results."""
    tool_uses = [
        {"name": "__tool_result_error__", "output": "Error: API key missing"},
        {"name": "__tool_result_error__", "output": "Traceback (most recent call last):\n..."},
        {"name": "Bash", "input": {"command": "echo hello"}},
    ]
    result = mine_module._detect_errors(tool_uses)
    assert result["count"] == 2
    assert len(result["samples"]) == 2


def test_detect_automation_requests(mine_module):
    """Detect automation keywords in user messages."""
    messages = [
        "Can you create a skill for this?",
        "Just run the analysis",
        "I want to automate this workflow",
        "スキルを作成してほしい",
    ]
    result = mine_module._detect_automation_requests(messages)
    assert result["count"] == 3
    assert len(result["samples"]) == 3


# ── _extract_json_from_claude ──


def test_extract_json_from_claude_candidates(mine_module):
    """JSON with candidates key is extracted."""
    raw = json.dumps(
        {
            "candidates": [
                {
                    "name": "test-skill",
                    "description": "A test",
                    "rationale": "Because",
                    "priority": "high",
                },
            ],
        }
    )
    result = mine_module._extract_json_from_claude(raw)
    assert result is not None
    assert "candidates" in result
    assert len(result["candidates"]) == 1


def test_extract_json_from_claude_wrapped(mine_module):
    """JSON wrapped in claude --output-format json envelope."""
    inner = json.dumps(
        {
            "candidates": [{"name": "x", "description": "y", "rationale": "z", "priority": "low"}],
        }
    )
    wrapper = json.dumps({"result": f"Here are the ideas:\n{inner}\nDone."})
    result = mine_module._extract_json_from_claude(wrapper)
    assert result is not None
    assert result["candidates"][0]["name"] == "x"


def test_extract_json_from_claude_no_candidates(mine_module):
    """JSON without 'candidates' key returns None."""
    raw = '{"score": 85, "summary": "review"}'
    result = mine_module._extract_json_from_claude(raw)
    assert result is None
