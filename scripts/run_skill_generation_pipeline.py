#!/usr/bin/env python3
"""Skill auto-generation pipeline orchestrator.

Mines session logs for skill ideas (weekly) and will later
design, review, create skills, and open PRs (daily).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

logger = logging.getLogger("skill_generation")

# Script paths (relative to project root)
MINE_SCRIPT = "skills/skill-idea-miner/scripts/mine_session_logs.py"
SCORE_SCRIPT = "skills/skill-idea-miner/scripts/score_ideas.py"

# File paths (relative to project root)
LOCK_FILE = "logs/.skill_generation.lock"
STATE_FILE = "logs/.skill_generation_state.json"
BACKLOG_FILE = "logs/.skill_generation_backlog.yaml"
SUMMARY_DIR = "reports/skill-generation-log"
LOG_DIR = "logs"

# Limits
CLAUDE_TIMEOUT = 600
CLAUDE_BUDGET_MINE = 1.00
CLAUDE_BUDGET_SCORE = 0.50
HISTORY_LIMIT = 60
LOG_RETENTION_DAYS = 30


# -- Lock --


def acquire_lock(project_root: Path) -> bool:
    """Acquire a PID-based lock file. Returns True if acquired."""
    lock_path = project_root / LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text().strip())
            os.kill(old_pid, 0)
            logger.info("Another instance (PID %d) is running. Exiting.", old_pid)
            return False
        except (ValueError, OSError):
            logger.info("Stale lock found, removing.")
            lock_path.unlink(missing_ok=True)

    lock_path.write_text(str(os.getpid()))
    return True


def release_lock(project_root: Path) -> None:
    lock_path = project_root / LOCK_FILE
    lock_path.unlink(missing_ok=True)


# -- State management --


def load_state(project_root: Path) -> dict:
    state_path = project_root / STATE_FILE
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupt state file, starting fresh.")
    return {"last_run": None, "history": []}


def save_state(project_root: Path, state: dict) -> None:
    state_path = project_root / STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state["history"] = state["history"][-HISTORY_LIMIT:]
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


# -- Backlog --


def load_backlog(project_root: Path) -> dict:
    """Load the skill ideas backlog YAML. Returns empty structure if missing or corrupt."""
    backlog_path = project_root / BACKLOG_FILE
    if not backlog_path.exists():
        return {"ideas": []}
    try:
        data = yaml.safe_load(backlog_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"ideas": []}
        return data
    except (yaml.YAMLError, OSError):
        logger.warning("Corrupt backlog file, returning empty.")
        return {"ideas": []}


# -- Mining --


def run_mine(project_root: Path, dry_run: bool = False) -> Path | None:
    """Execute mine_session_logs.py as subprocess.

    Returns path to raw_candidates.yaml on success, None on failure.
    """
    script = project_root / MINE_SCRIPT
    if not script.exists():
        logger.error("Mine script not found: %s", script)
        return None

    output_dir = project_root / SUMMARY_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(script),
        "--output-dir",
        str(output_dir),
    ]
    if dry_run:
        cmd.append("--dry-run")

    try:
        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=CLAUDE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.error("Mine script timed out after %d seconds.", CLAUDE_TIMEOUT)
        return None

    if result.returncode != 0:
        logger.error(
            "Mine script failed (rc=%d): %s", result.returncode, result.stderr.strip()[:500]
        )
        return None

    # Find the output file
    candidates_files = sorted(output_dir.glob("raw_candidates*.yaml"), reverse=True)
    if not candidates_files:
        # Also check for .yml extension
        candidates_files = sorted(output_dir.glob("raw_candidates*.yml"), reverse=True)
    if not candidates_files:
        logger.error("No raw_candidates file found after mining.")
        return None

    return candidates_files[0]


# -- Scoring --


def run_score(
    project_root: Path,
    candidates_path: Path,
    dry_run: bool = False,
) -> bool:
    """Execute score_ideas.py as subprocess. Returns True on success."""
    script = project_root / SCORE_SCRIPT
    if not script.exists():
        logger.error("Score script not found: %s", script)
        return False

    backlog_path = project_root / BACKLOG_FILE

    cmd = [
        sys.executable,
        str(script),
        "--candidates",
        str(candidates_path),
        "--project-root",
        str(project_root),
        "--backlog",
        str(backlog_path),
    ]
    if dry_run:
        cmd.append("--dry-run")

    try:
        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=CLAUDE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.error("Score script timed out after %d seconds.", CLAUDE_TIMEOUT)
        return False

    if result.returncode != 0:
        logger.error(
            "Score script failed (rc=%d): %s", result.returncode, result.stderr.strip()[:500]
        )
        return False

    return True


# -- Summary --


def write_weekly_summary(
    project_root: Path,
    candidates_path: Path | None,
    backlog: dict,
) -> None:
    """Write markdown summary to SUMMARY_DIR/YYYY-MM-DD_summary.md."""
    summary_dir = project_root / SUMMARY_DIR
    summary_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    summary_path = summary_dir / f"{today}_summary.md"

    ideas = backlog.get("ideas", [])
    total_ideas = len(ideas)

    # Top scored ideas (up to 5)
    sorted_ideas = sorted(ideas, key=lambda x: x.get("score", 0), reverse=True)
    top_ideas = sorted_ideas[:5]

    entry = (
        f"\n## Weekly Mining Summary\n"
        f"- Date: {today}\n"
        f"- Candidates file: {candidates_path.name if candidates_path else 'N/A'}\n"
        f"- Total backlog ideas: {total_ideas}\n"
    )
    if top_ideas:
        entry += "- Top scored ideas:\n"
        for idea in top_ideas:
            name = idea.get("name", "unnamed")
            score = idea.get("score", 0)
            entry += f"  - {name} (score: {score})\n"

    if summary_path.exists():
        existing = summary_path.read_text(encoding="utf-8")
        summary_path.write_text(existing + entry, encoding="utf-8")
    else:
        header = f"# Skill Generation Summary - {today}\n"
        summary_path.write_text(header + entry, encoding="utf-8")


# -- Log rotation --


def rotate_logs(project_root: Path) -> None:
    """Remove log files older than LOG_RETENTION_DAYS."""
    log_dir = project_root / LOG_DIR
    if not log_dir.is_dir():
        return
    cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
    for f in log_dir.iterdir():
        if f.is_file() and f.suffix == ".log":
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    f.unlink()
                    logger.info("Rotated old log: %s", f.name)
            except OSError:
                pass


# -- Weekly flow --


def run_weekly(project_root: Path, dry_run: bool = False) -> int:
    """Main weekly flow: mine ideas, score them, update backlog.

    Returns 0 on success, 1 on failure.
    """
    log_dir = project_root / LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "skill_generation.log"),
        ],
    )

    if not acquire_lock(project_root):
        return 0

    try:
        # Mine session logs
        logger.info("Starting weekly mining run (dry_run=%s).", dry_run)
        candidates_path = run_mine(project_root, dry_run=dry_run)
        if candidates_path is None:
            logger.error("Mining failed; aborting weekly run.")
            return 1

        logger.info("Mining produced: %s", candidates_path.name)

        # Score and update backlog
        score_ok = run_score(project_root, candidates_path, dry_run=dry_run)
        if not score_ok:
            logger.error("Scoring failed; writing partial summary.")

        # Load backlog for summary
        backlog = load_backlog(project_root)

        # Write summary
        write_weekly_summary(project_root, candidates_path, backlog)

        # Update state
        state = load_state(project_root)
        state["last_run"] = datetime.now().isoformat()
        state["history"].append(
            {
                "mode": "weekly",
                "candidates_file": candidates_path.name if candidates_path else None,
                "score_ok": score_ok,
                "backlog_size": len(backlog.get("ideas", [])),
                "timestamp": datetime.now().isoformat(),
            }
        )
        save_state(project_root, state)

        # Rotate old logs
        rotate_logs(project_root)

        logger.info("Weekly run complete.")
        return 0

    finally:
        release_lock(project_root)


# -- CLI --


def parse_args():
    parser = argparse.ArgumentParser(description="Skill auto-generation pipeline orchestrator")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["weekly"],
        help="Pipeline mode (weekly: mine + score)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run to subscripts")
    parser.add_argument("--project-root", default=".", help="Project root directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()

    if args.mode == "weekly":
        return run_weekly(project_root, dry_run=args.dry_run)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
