"""Tests for the skill idea scorer and deduplication script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def score_module():
    """Load score_ideas.py as a module via importlib."""
    script_path = Path(__file__).resolve().parents[1] / "score_ideas.py"
    spec = importlib.util.spec_from_file_location("score_ideas", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load score_ideas.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── Jaccard similarity tests ──


def test_jaccard_identical(score_module):
    """Identical text returns 1.0."""
    assert score_module.jaccard_similarity("hello world", "hello world") == 1.0


def test_jaccard_partial(score_module):
    """Partially overlapping words return expected value."""
    # "hello world" -> {"hello", "world"}
    # "hello there" -> {"hello", "there"}
    # intersection = {"hello"}, union = {"hello", "world", "there"}
    # Jaccard = 1/3
    result = score_module.jaccard_similarity("hello world", "hello there")
    assert abs(result - 1.0 / 3.0) < 1e-9


def test_jaccard_disjoint(score_module):
    """No common words returns 0.0."""
    assert score_module.jaccard_similarity("alpha beta", "gamma delta") == 0.0


def test_jaccard_empty(score_module):
    """Empty string returns 0.0."""
    assert score_module.jaccard_similarity("", "hello") == 0.0
    assert score_module.jaccard_similarity("hello", "") == 0.0
    assert score_module.jaccard_similarity("", "") == 0.0


# ── list_existing_skills tests ──


def _make_skill(project_root: Path, name: str, description: str = "test") -> None:
    """Create a minimal skill directory with SKILL.md."""
    skill_dir = project_root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )


def test_list_existing_skills(score_module, tmp_path: Path):
    """Create mock skills/*/SKILL.md and verify parsing."""
    _make_skill(tmp_path, "alpha-skill", "Alpha skill for testing")
    _make_skill(tmp_path, "beta-skill", "Beta skill for testing")

    results = score_module.list_existing_skills(tmp_path)

    assert len(results) == 2
    names = {r["name"] for r in results}
    assert "alpha-skill" in names
    assert "beta-skill" in names

    # Verify descriptions are parsed
    alpha = next(r for r in results if r["name"] == "alpha-skill")
    assert alpha["description"] == "Alpha skill for testing"


def test_list_existing_skills_skips_nested(score_module, tmp_path: Path):
    """Verify skills/**/SKILL.md nested entries are NOT returned."""
    _make_skill(tmp_path, "top-skill", "Top level skill")

    # Create a nested SKILL.md that should NOT be picked up
    nested_dir = tmp_path / "skills" / "top-skill" / "sub-component"
    nested_dir.mkdir(parents=True, exist_ok=True)
    (nested_dir / "SKILL.md").write_text(
        "---\nname: nested-skill\ndescription: Should not appear\n---\n",
        encoding="utf-8",
    )

    results = score_module.list_existing_skills(tmp_path)

    names = {r["name"] for r in results}
    assert "top-skill" in names
    assert "nested-skill" not in names
    assert len(results) == 1


# ── merge_into_backlog tests ──


def test_merge_new_ideas(score_module):
    """New ideas are added to backlog."""
    backlog = {"updated_at_utc": "", "ideas": []}
    candidates = [
        {"id": "idea_001", "title": "New Idea", "description": "desc", "scores": {"composite": 70}},
        {"id": "idea_002", "title": "Another", "description": "desc2", "scores": {"composite": 80}},
    ]

    result = score_module.merge_into_backlog(backlog, candidates)

    assert len(result["ideas"]) == 2
    assert result["ideas"][0]["id"] == "idea_001"
    assert result["ideas"][1]["id"] == "idea_002"
    assert result["updated_at_utc"] != ""


def test_merge_skip_duplicates(score_module):
    """Ideas with same id are not duplicated in backlog."""
    backlog = {
        "updated_at_utc": "2026-02-28T06:15:00Z",
        "ideas": [
            {"id": "idea_001", "title": "Existing Idea", "status": "pending"},
        ],
    }
    candidates = [
        {"id": "idea_001", "title": "Existing Idea Updated", "scores": {"composite": 90}},
        {"id": "idea_002", "title": "New Idea", "scores": {"composite": 80}},
    ]

    result = score_module.merge_into_backlog(backlog, candidates)

    assert len(result["ideas"]) == 2
    # Original idea unchanged
    assert result["ideas"][0]["title"] == "Existing Idea"
    # New idea added
    assert result["ideas"][1]["id"] == "idea_002"


def test_merge_preserves_status(score_module):
    """Existing idea status is not overwritten by merge."""
    backlog = {
        "updated_at_utc": "2026-02-28T06:15:00Z",
        "ideas": [
            {
                "id": "idea_001",
                "title": "Old Idea",
                "status": "attempted",
                "scores": {"composite": 60},
            },
        ],
    }
    # Candidate with same id tries to change status
    candidates = [
        {
            "id": "idea_001",
            "title": "Old Idea Revisited",
            "status": "pending",
            "scores": {"composite": 90},
        },
    ]

    result = score_module.merge_into_backlog(backlog, candidates)

    assert len(result["ideas"]) == 1
    assert result["ideas"][0]["status"] == "attempted"
    assert result["ideas"][0]["scores"]["composite"] == 60


# ── find_duplicates tests ──


def test_find_duplicates_marks_similar(score_module):
    """Candidate with high Jaccard similarity is marked as duplicate."""
    candidates = [
        {
            "id": "cand_001",
            "title": "Market Breadth Weekly Reporter",
            "description": "Weekly market breadth summary reports for trading",
        },
    ]
    existing_skills = [
        {
            "name": "market-breadth-reporter",
            "description": "Weekly market breadth summary reports for trading",
        },
    ]
    backlog_ideas = []

    result = score_module.find_duplicates(candidates, existing_skills, backlog_ideas)

    assert result[0].get("status") == "duplicate"
    assert "skill:market-breadth-reporter" in result[0].get("duplicate_of", "")
    assert result[0].get("jaccard_score", 0) > score_module.JACCARD_THRESHOLD
