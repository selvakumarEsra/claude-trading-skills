"""Tests for the skill generation pipeline orchestrator."""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
import yaml


@pytest.fixture(scope="module")
def pipeline_module():
    """Load run_skill_generation_pipeline.py as a module."""
    script_path = Path(__file__).resolve().parents[1] / "run_skill_generation_pipeline.py"
    spec = importlib.util.spec_from_file_location("run_skill_generation_pipeline", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load run_skill_generation_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- Lock tests --


def test_acquire_lock_creates_file(pipeline_module, tmp_path: Path):
    assert pipeline_module.acquire_lock(tmp_path) is True
    lock_path = tmp_path / pipeline_module.LOCK_FILE
    assert lock_path.exists()
    assert lock_path.read_text().strip() == str(os.getpid())
    pipeline_module.release_lock(tmp_path)
    assert not lock_path.exists()


def test_acquire_lock_rejects_running_pid(pipeline_module, tmp_path: Path):
    lock_path = tmp_path / pipeline_module.LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()))  # Current PID is alive

    assert pipeline_module.acquire_lock(tmp_path) is False
    lock_path.unlink()


def test_acquire_lock_removes_stale(pipeline_module, tmp_path: Path):
    lock_path = tmp_path / pipeline_module.LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999999999")  # Unlikely to be a real PID

    assert pipeline_module.acquire_lock(tmp_path) is True
    pipeline_module.release_lock(tmp_path)


# -- State tests --


def test_load_state_empty(pipeline_module, tmp_path: Path):
    state = pipeline_module.load_state(tmp_path)
    assert state == {"last_run": None, "history": []}


def test_save_and_load_state(pipeline_module, tmp_path: Path):
    state = {"last_run": "2026-03-01T00:00:00", "history": [{"mode": "weekly", "score_ok": True}]}
    pipeline_module.save_state(tmp_path, state)

    loaded = pipeline_module.load_state(tmp_path)
    assert loaded["last_run"] == "2026-03-01T00:00:00"
    assert loaded["history"][0]["mode"] == "weekly"


def test_save_state_trims_history(pipeline_module, tmp_path: Path):
    state = {
        "last_run": None,
        "history": [{"i": i} for i in range(100)],
    }
    pipeline_module.save_state(tmp_path, state)
    loaded = pipeline_module.load_state(tmp_path)
    assert len(loaded["history"]) == pipeline_module.HISTORY_LIMIT


# -- Backlog tests --


def test_load_backlog_empty(pipeline_module, tmp_path: Path):
    backlog = pipeline_module.load_backlog(tmp_path)
    assert backlog == {"ideas": []}


def test_load_backlog_existing(pipeline_module, tmp_path: Path):
    backlog_path = tmp_path / pipeline_module.BACKLOG_FILE
    backlog_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"ideas": [{"name": "test-idea", "score": 75}]}
    backlog_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    loaded = pipeline_module.load_backlog(tmp_path)
    assert len(loaded["ideas"]) == 1
    assert loaded["ideas"][0]["name"] == "test-idea"


def test_load_backlog_corrupt(pipeline_module, tmp_path: Path):
    backlog_path = tmp_path / pipeline_module.BACKLOG_FILE
    backlog_path.parent.mkdir(parents=True, exist_ok=True)
    backlog_path.write_text(":::\ninvalid: [yaml: {broken", encoding="utf-8")

    backlog = pipeline_module.load_backlog(tmp_path)
    assert backlog == {"ideas": []}


# -- Weekly flow tests --


def _setup_mine_script(tmp_path: Path, pipeline_module) -> None:
    """Create a dummy mine script that the pipeline can find."""
    mine_path = tmp_path / pipeline_module.MINE_SCRIPT
    mine_path.parent.mkdir(parents=True, exist_ok=True)
    mine_path.write_text("# placeholder", encoding="utf-8")


def _setup_score_script(tmp_path: Path, pipeline_module) -> None:
    """Create a dummy score script that the pipeline can find."""
    score_path = tmp_path / pipeline_module.SCORE_SCRIPT
    score_path.parent.mkdir(parents=True, exist_ok=True)
    score_path.write_text("# placeholder", encoding="utf-8")


def test_weekly_flow_success(pipeline_module, tmp_path: Path):
    """Mine + score succeed, summary written, state updated."""
    _setup_mine_script(tmp_path, pipeline_module)
    _setup_score_script(tmp_path, pipeline_module)

    # Create a candidates file that run_mine will find
    output_dir = tmp_path / pipeline_module.SUMMARY_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates_file = output_dir / "raw_candidates_2026-03-01.yaml"
    candidates_file.write_text(
        yaml.safe_dump({"candidates": [{"name": "test-skill"}]}),
        encoding="utf-8",
    )

    # Create a backlog file for the summary
    backlog_path = tmp_path / pipeline_module.BACKLOG_FILE
    backlog_path.parent.mkdir(parents=True, exist_ok=True)
    backlog_path.write_text(
        yaml.safe_dump({"ideas": [{"name": "idea-1", "score": 80}]}),
        encoding="utf-8",
    )

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "", "")

    with patch.object(pipeline_module.subprocess, "run", fake_run):
        rc = pipeline_module.run_weekly(tmp_path, dry_run=False)

    assert rc == 0

    # Summary file created
    summary_files = list((tmp_path / pipeline_module.SUMMARY_DIR).glob("*_summary.md"))
    assert len(summary_files) >= 1
    content = summary_files[0].read_text(encoding="utf-8")
    assert "Weekly Mining Summary" in content

    # State updated
    state = pipeline_module.load_state(tmp_path)
    assert len(state["history"]) >= 1
    assert state["history"][-1]["mode"] == "weekly"
    assert state["last_run"] is not None


def test_weekly_flow_mine_failure(pipeline_module, tmp_path: Path):
    """When mine fails, score is not called and run returns 1."""
    _setup_mine_script(tmp_path, pipeline_module)

    call_log = []

    def fake_run(cmd, **kwargs):
        call_log.append(list(cmd))
        # Mine script fails
        return CompletedProcess(cmd, 1, "", "mining error")

    with patch.object(pipeline_module.subprocess, "run", fake_run):
        rc = pipeline_module.run_weekly(tmp_path, dry_run=False)

    assert rc == 1

    # Score script should NOT have been called
    score_calls = [c for c in call_log if pipeline_module.SCORE_SCRIPT in " ".join(c)]
    assert len(score_calls) == 0


def test_weekly_flow_dry_run(pipeline_module, tmp_path: Path):
    """Dry run passes --dry-run to subscripts."""
    _setup_mine_script(tmp_path, pipeline_module)
    _setup_score_script(tmp_path, pipeline_module)

    # Create a candidates file
    output_dir = tmp_path / pipeline_module.SUMMARY_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates_file = output_dir / "raw_candidates_2026-03-01.yaml"
    candidates_file.write_text(yaml.safe_dump({"candidates": []}), encoding="utf-8")

    call_log = []

    def fake_run(cmd, **kwargs):
        call_log.append(list(cmd))
        return CompletedProcess(cmd, 0, "", "")

    with patch.object(pipeline_module.subprocess, "run", fake_run):
        rc = pipeline_module.run_weekly(tmp_path, dry_run=True)

    assert rc == 0

    # Verify --dry-run was passed to both subscripts
    mine_calls = [c for c in call_log if pipeline_module.MINE_SCRIPT in " ".join(c)]
    assert len(mine_calls) == 1
    assert "--dry-run" in mine_calls[0]

    score_calls = [c for c in call_log if pipeline_module.SCORE_SCRIPT in " ".join(c)]
    assert len(score_calls) == 1
    assert "--dry-run" in score_calls[0]


def test_weekly_flow_lock_conflict(pipeline_module, tmp_path: Path):
    """When lock is held by another process, exits with 0."""
    lock_path = tmp_path / pipeline_module.LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()))  # Current PID holds lock

    rc = pipeline_module.run_weekly(tmp_path, dry_run=False)
    assert rc == 0

    lock_path.unlink()


# -- Summary tests --


def test_write_weekly_summary_creates(pipeline_module, tmp_path: Path):
    """Summary file is created with mining results."""
    candidates_path = tmp_path / "raw_candidates.yaml"
    candidates_path.write_text("dummy", encoding="utf-8")

    backlog = {"ideas": [{"name": "idea-a", "score": 90}, {"name": "idea-b", "score": 60}]}

    pipeline_module.write_weekly_summary(tmp_path, candidates_path, backlog)

    summary_dir = tmp_path / pipeline_module.SUMMARY_DIR
    files = list(summary_dir.glob("*_summary.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "Weekly Mining Summary" in content
    assert "Total backlog ideas: 2" in content
    assert "idea-a" in content


def test_write_weekly_summary_appends(pipeline_module, tmp_path: Path):
    """Second call appends to existing summary."""
    candidates_path = tmp_path / "raw_candidates.yaml"
    candidates_path.write_text("dummy", encoding="utf-8")

    backlog1 = {"ideas": [{"name": "idea-1", "score": 80}]}
    backlog2 = {"ideas": [{"name": "idea-1", "score": 80}, {"name": "idea-2", "score": 70}]}

    pipeline_module.write_weekly_summary(tmp_path, candidates_path, backlog1)
    pipeline_module.write_weekly_summary(tmp_path, candidates_path, backlog2)

    summary_dir = tmp_path / pipeline_module.SUMMARY_DIR
    files = list(summary_dir.glob("*_summary.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert content.count("Weekly Mining Summary") == 2


# -- Log rotation test --


def test_rotate_logs(pipeline_module, tmp_path: Path):
    """Old logs deleted, recent kept."""
    log_dir = tmp_path / pipeline_module.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create an "old" log file with mtime in the past
    old_log = log_dir / "old.log"
    old_log.write_text("old log content")
    old_time = time.time() - (60 * 86400)
    os.utime(old_log, (old_time, old_time))

    # Create a "new" log file
    new_log = log_dir / "new.log"
    new_log.write_text("new log content")

    pipeline_module.rotate_logs(tmp_path)

    assert not old_log.exists(), "Old log should have been removed"
    assert new_log.exists(), "New log should still exist"
