from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).parents[1] / "src" / "devgod" / "assets"
SKILL = ASSETS / "devgod" / "skills" / "devgod-manager" / "SKILL.md"
AGENTS_BLOCK = ASSETS / "agents-block.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_manager_skill_requires_early_planning_and_bounded_implementation() -> None:
    text = _text(SKILL)

    assert "dispatch the architecture or planning agent first" in text
    assert "before making more than two local read or search tool calls" in text
    assert "when agent delegation is technically unavailable" in text
    assert "dispatch at least one implementation child" in text
    assert "acceptance criteria, owned paths, dependencies, and required checks" in text


def test_manager_skill_requires_same_turn_autonomy() -> None:
    text = _text(SKILL)

    assert "continue in the same turn" in text
    assert "implementation, integration, checks, verification, and repair" in text
    assert "Do not end a turn merely to announce a next step" in text
    assert "material product choice remains unresolved" in text
    assert "real permission, credential, external-system, or destructive-action boundary" in text


def test_manager_skill_defines_complete_terminal_report() -> None:
    text = _text(SKILL)
    headings = [
        "### Outcome",
        "### Changes",
        "### Verification",
        "### Agents",
        "### Limitations",
    ]

    assert [text.index(heading) for heading in headings] == sorted(
        text.index(heading) for heading in headings
    )
    assert "local branch and worktree status" in text
    assert "actual check and its result" in text
    assert "each delegated role, its bounded assignment, and its conclusion" in text
    assert "including a blocked ending" in text
    assert "summarize useful work already completed" in text
    assert "identify the exact blocker" in text


def test_agents_block_preserves_manager_contract_for_installed_repositories() -> None:
    text = _text(AGENTS_BLOCK)

    assert "before more than two local read or search calls" in text
    assert "at least one implementation child a bounded assignment" in text
    assert "continue in\nthe same turn" in text
    assert "Never end merely by announcing a next step" in text
    for section in ("Outcome", "Changes", "Verification", "Agents", "Limitations"):
        assert f"`{section}`" in text
    assert "branch and worktree status" in text
    assert "exact blocker and action required to resume" in text
