"""Analysis functions run against the known-history fixture repository (see conftest.py).

Every expected number below is derived by hand from the table in conftest.py.
"""

from __future__ import annotations

import functools
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from git_insights import analysis
from git_insights.analysis import GitRunner
from git_insights.git import run_git
from git_insights.models import ChangelogResult


@pytest.fixture
def git(git_repo: Path) -> GitRunner:
    return functools.partial(run_git, git_repo, timeout=10.0, max_output_bytes=1_000_000)


def _ts(day: int, month: int) -> datetime:
    return datetime(2026, month, day, 10, 0, tzinfo=UTC)


# --- repo_summary --------------------------------------------------------------


@pytest.mark.anyio
async def test_repo_summary(git: GitRunner) -> None:
    summary = await analysis.repo_summary(git, "demo")

    assert summary.repo == "demo"
    assert summary.branch == "main"
    assert len(summary.head) == 40
    assert summary.commit_count == 6
    assert summary.first_commit_at == _ts(5, 1)
    assert summary.last_commit_at == _ts(20, 2)
    assert [(a.name, a.email, a.commits) for a in summary.authors] == [
        ("Alice", "alice@example.com", 3),
        ("Bob", "bob@example.com", 3),
    ]
    assert summary.file_count == 4
    assert [(e.extension, e.files) for e in summary.file_extensions] == [
        (".py", 2),
        (".md", 1),
        (".yaml", 1),
    ]


def test_parse_branch_handles_detached_head() -> None:
    assert analysis.parse_branch("HEAD -> main, tag: v1.1") == "main"
    assert analysis.parse_branch("HEAD, tag: v1.0") is None
    assert analysis.parse_branch("") is None


# --- hotspots ------------------------------------------------------------------


@pytest.mark.anyio
async def test_hotspots_whole_history(git: GitRunner) -> None:
    result = await analysis.hotspots(git, "demo", since=None, until=None, limit=10)

    assert result.total_commits == 6
    assert [(h.path, h.commits, h.added, h.deleted, h.churn) for h in result.hotspots] == [
        ("src/auth.py", 4, 9, 2, 11),
        ("src/app.py", 4, 6, 0, 6),
        ("README.md", 2, 3, 0, 3),
        ("config.yaml", 1, 2, 0, 2),
    ]


@pytest.mark.anyio
async def test_hotspots_period_and_limit(git: GitRunner) -> None:
    result = await analysis.hotspots(
        git, "demo", since=date(2026, 2, 1), until=date(2026, 2, 28), limit=3
    )

    assert result.total_commits == 3
    assert [(h.path, h.commits, h.churn) for h in result.hotspots] == [
        ("src/auth.py", 2, 3),
        ("README.md", 1, 2),
        ("config.yaml", 1, 2),
    ]


@pytest.mark.anyio
async def test_period_bounds_are_inclusive_whole_days(git: GitRunner) -> None:
    result = await analysis.hotspots(
        git, "demo", since=date(2026, 1, 10), until=date(2026, 1, 10), limit=10
    )

    assert result.total_commits == 1
    assert [(h.path, h.added) for h in result.hotspots] == [("src/auth.py", 5), ("src/app.py", 1)]


# --- author_activity -----------------------------------------------------------


@pytest.mark.anyio
async def test_author_activity_whole_history(git: GitRunner) -> None:
    result = await analysis.author_activity(git, "demo", since=None, until=None)

    assert [(a.name, a.email, a.commits, a.added, a.deleted) for a in result.authors] == [
        ("Alice", "alice@example.com", 3, 11, 1),
        ("Bob", "bob@example.com", 3, 9, 1),
    ]


@pytest.mark.anyio
async def test_author_activity_period(git: GitRunner) -> None:
    result = await analysis.author_activity(git, "demo", since=date(2026, 2, 1), until=None)

    assert [(a.name, a.commits, a.added, a.deleted) for a in result.authors] == [
        ("Bob", 2, 3, 1),
        ("Alice", 1, 4, 0),
    ]


# --- file_history --------------------------------------------------------------


@pytest.mark.anyio
async def test_file_history_newest_first(git: GitRunner) -> None:
    result = await analysis.file_history(git, "demo", "src/app.py", limit=10)

    assert result.path == "src/app.py"
    assert [(c.subject, c.author, c.date, c.added, c.deleted) for c in result.commits] == [
        ("feat: add config loader", "Alice", _ts(10, 2), 1, 0),
        ("fix(auth): handle empty password", "Alice", _ts(15, 1), 1, 0),
        ("feat(auth): add login", "Bob", _ts(10, 1), 1, 0),
        ("feat: initial app", "Alice", _ts(5, 1), 3, 0),
    ]
    assert all(len(c.sha) == 40 for c in result.commits)


@pytest.mark.anyio
async def test_file_history_limit(git: GitRunner) -> None:
    result = await analysis.file_history(git, "demo", "src/auth.py", limit=2)

    assert [c.subject for c in result.commits] == [
        "refactor auth module",
        "feat: add config loader",
    ]


@pytest.mark.anyio
async def test_file_history_of_untouched_path_is_empty(git: GitRunner) -> None:
    result = await analysis.file_history(git, "demo", "nope.txt", limit=10)

    assert result.commits == []


# --- hotspots progress -----------------------------------------------------------


@pytest.mark.anyio
async def test_hotspots_reports_progress_up_to_total(git: GitRunner) -> None:
    calls: list[tuple[float, float]] = []

    async def progress(done: float, total: float, message: str) -> None:
        calls.append((done, total))

    await analysis.hotspots(git, "demo", since=None, until=None, limit=10, progress=progress)

    assert [done for done, _ in calls] == sorted(done for done, _ in calls)
    assert calls[0][0] == 0
    assert calls[-1][0] == calls[-1][1]
    assert len({total for _, total in calls}) == 1


# --- coupled_files ---------------------------------------------------------------


@pytest.mark.anyio
async def test_coupled_files(git: GitRunner) -> None:
    result = await analysis.coupled_files(git, "demo", "src/app.py", limit=10)

    assert result.path == "src/app.py"
    assert result.file_commits == 4
    assert [(c.path, c.co_changes, c.ratio) for c in result.coupled] == [
        ("src/auth.py", 3, 0.75),
        ("README.md", 1, 0.25),
        ("config.yaml", 1, 0.25),
    ]


@pytest.mark.anyio
async def test_coupled_files_limit(git: GitRunner) -> None:
    result = await analysis.coupled_files(git, "demo", "src/app.py", limit=1)

    assert [c.path for c in result.coupled] == ["src/auth.py"]


@pytest.mark.anyio
async def test_coupled_files_of_untouched_path(git: GitRunner) -> None:
    result = await analysis.coupled_files(git, "demo", "nope.txt", limit=10)

    assert result.file_commits == 0
    assert result.coupled == []


# --- draft_changelog -------------------------------------------------------------


def _changelog_view(result: ChangelogResult) -> list[tuple[str, list[tuple[str, str | None]]]]:
    return [
        (section.type, [(e.description, e.scope) for e in section.entries])
        for section in result.sections
    ]


@pytest.mark.anyio
async def test_draft_changelog_between_tags(git: GitRunner) -> None:
    result = await analysis.draft_changelog(git, "demo", "v1.0", "v1.1")

    assert result.total_commits == 3
    assert _changelog_view(result) == [
        ("feat", [("add config loader", None)]),
        ("docs", [("update readme", None)]),
        ("other", [("refactor auth module", None)]),
    ]


@pytest.mark.anyio
async def test_draft_changelog_scopes(git: GitRunner) -> None:
    result = await analysis.draft_changelog(git, "demo", "HEAD~5", "v1.0")

    assert _changelog_view(result) == [
        ("feat", [("add login", "auth")]),
        ("fix", [("handle empty password", "auth")]),
    ]


@pytest.mark.anyio
async def test_draft_changelog_unknown_ref(git: GitRunner) -> None:
    with pytest.raises(ToolError, match="no-such-tag"):
        await analysis.draft_changelog(git, "demo", "no-such-tag", "HEAD")


def test_group_changelog_breaking_unknown_type_and_case() -> None:
    commits = [
        ("a" * 40, "Ann", "feat(api)!: drop v1 endpoints"),
        ("b" * 40, "Ann", "wip: half done"),
        ("c" * 40, "Ann", "Fix: typo"),
        ("d" * 40, "Ann", "Merge-free subject without type"),
    ]

    sections = analysis.group_changelog(commits)

    assert [(s.type, [(e.description, e.breaking) for e in s.entries]) for s in sections] == [
        ("feat", [("drop v1 endpoints", True)]),
        ("fix", [("typo", False)]),
        ("other", [("wip: half done", False), ("Merge-free subject without type", False)]),
    ]
