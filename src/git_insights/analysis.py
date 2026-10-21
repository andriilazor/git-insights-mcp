"""Repository analysis.

Each tool-level function takes a ``GitRunner`` (an async callable bound to one
repository) so it can be tested against a real repository without the MCP layer.
Parsing and aggregation are pure functions over git's output.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Awaitable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Protocol

from git_insights.models import (
    AuthorActivity,
    AuthorActivityResult,
    AuthorCount,
    ChangelogEntry,
    ChangelogResult,
    ChangelogSection,
    CoupledFile,
    CoupledFilesResult,
    ExtensionCount,
    FileCommit,
    FileHistoryResult,
    Hotspot,
    HotspotsResult,
    RepoSummary,
)


class GitRunner(Protocol):
    """``run_git`` with the repository and limits already bound."""

    def __call__(self, args: Sequence[str], /) -> Awaitable[str]: ...


class ProgressCallback(Protocol):
    """Receives absolute progress: ``done`` out of ``total``."""

    def __call__(self, done: float, total: float, message: str, /) -> Awaitable[None]: ...


async def _no_progress(done: float, total: float, message: str) -> None:
    pass


# Record separator before each commit, unit separator between header fields.
_RS, _US = "\x1e", "\x1f"
_LOG_FORMAT = f"--format={_RS}%H{_US}%an{_US}%ae{_US}%aI{_US}%s"
_EMPTY_TREE = {
    40: "4b825dc642cb6eb9a060e54bf8d69288fbee4904",  # SHA-1 repositories
    64: "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",  # SHA-256
}
_SHORTLOG_RE = re.compile(r"^\s*(\d+)\t(.*) <(.*)>$")


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int
    deleted: int


@dataclass(frozen=True)
class CommitRecord:
    sha: str
    author: str
    email: str
    date: datetime
    subject: str
    files: tuple[FileChange, ...]


# --- git log -------------------------------------------------------------------


def period_args(since: date | None, until: date | None) -> list[str]:
    """Whole-day, UTC, inclusive bounds. Times are explicit because git's approxidate
    fills a bare date with the current time of day."""
    args = []
    if since is not None:
        args.append(f"--since={since.isoformat()}T00:00:00+00:00")
    if until is not None:
        args.append(f"--until={until.isoformat()}T23:59:59+00:00")
    return args


def parse_numstat_log(text: str) -> list[CommitRecord]:
    """Parse ``git log --numstat`` output produced with ``_LOG_FORMAT``."""
    commits = []
    for record in text.split(_RS)[1:]:
        header, _, body = record.partition("\n")
        sha, author, email, when, subject = header.split(_US, 4)
        files = []
        for line in body.splitlines():
            if not line.strip():
                continue
            added, deleted, path = line.split("\t", 2)
            # Binary files report "-" for both counts.
            files.append(
                FileChange(
                    path=path,
                    added=int(added) if added != "-" else 0,
                    deleted=int(deleted) if deleted != "-" else 0,
                )
            )
        commits.append(
            CommitRecord(
                sha=sha,
                author=author,
                email=email,
                date=datetime.fromisoformat(when),
                subject=subject,
                files=tuple(files),
            )
        )
    return commits


async def read_log(
    git: GitRunner,
    *,
    since: date | None = None,
    until: date | None = None,
    extra: Sequence[str] = (),
    paths: Sequence[str] = (),
) -> list[CommitRecord]:
    """Non-merge commits with per-file line counts, newest first."""
    args = ["log", "--no-merges", "--no-renames", "--numstat", _LOG_FORMAT]
    args += period_args(since, until)
    args += extra
    if paths:
        args += ["--", *paths]
    return parse_numstat_log(await git(args))


# --- repo_summary --------------------------------------------------------------


def parse_branch(decorations: str) -> str | None:
    """Branch name from ``%D`` decorations such as ``HEAD -> main, tag: v1.1``."""
    for item in decorations.split(","):
        item = item.strip()
        if item.startswith("HEAD -> "):
            return item.removeprefix("HEAD -> ")
    return None


def parse_shortlog(text: str) -> list[AuthorCount]:
    authors = []
    for line in text.splitlines():
        match = _SHORTLOG_RE.match(line)
        if match:
            count, name, email = match.groups()
            authors.append(AuthorCount(name=name, email=email, commits=int(count)))
    return sorted(authors, key=lambda a: (-a.commits, a.name))


def extension_counts(paths: Iterable[str]) -> list[ExtensionCount]:
    counts = Counter(PurePosixPath(p).suffix.lower() or "(none)" for p in paths)
    return [
        ExtensionCount(extension=ext, files=n)
        for ext, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


async def repo_summary(git: GitRunner, repo: str) -> RepoSummary:
    head_line = await git(["log", "-1", f"--format=%H{_US}%D{_US}%aI", "HEAD"])
    head, decorations, last = head_line.strip().split(_US)
    commit_count = int((await git(["rev-list", "--count", "HEAD"])).strip())
    roots = await git(["log", "--max-parents=0", "--format=%aI", "HEAD"])
    first = min(datetime.fromisoformat(line) for line in roots.split())
    authors = parse_shortlog(await git(["shortlog", "-sne", "HEAD"]))
    # Diffing HEAD against the empty tree lists every file in HEAD.
    tree = await git(["diff", "--numstat", _EMPTY_TREE[len(head)], "HEAD"])
    paths = [line.split("\t", 2)[2] for line in tree.splitlines() if line.strip()]
    return RepoSummary(
        repo=repo,
        branch=parse_branch(decorations),
        head=head,
        commit_count=commit_count,
        first_commit_at=first,
        last_commit_at=datetime.fromisoformat(last),
        authors=authors,
        file_count=len(paths),
        file_extensions=extension_counts(paths),
    )


# --- hotspots ------------------------------------------------------------------


def compute_hotspots(commits: Iterable[CommitRecord], limit: int) -> list[Hotspot]:
    """Rank files by number of commits, then churn, then path."""
    stats: dict[str, list[int]] = {}
    for commit in commits:
        for change in commit.files:
            entry = stats.setdefault(change.path, [0, 0, 0])
            entry[0] += 1
            entry[1] += change.added
            entry[2] += change.deleted
    ranked = sorted(
        stats.items(), key=lambda item: (-item[1][0], -(item[1][1] + item[1][2]), item[0])
    )
    return [
        Hotspot(path=path, commits=n, added=added, deleted=deleted, churn=added + deleted)
        for path, (n, added, deleted) in ranked[:limit]
    ]


async def hotspots(
    git: GitRunner,
    repo: str,
    *,
    since: date | None,
    until: date | None,
    limit: int,
    progress: ProgressCallback | None = None,
) -> HotspotsResult:
    report = progress or _no_progress
    await report(0, 2, "Reading git history")
    commits = await read_log(git, since=since, until=until)
    await report(1, 2, f"Ranking files from {len(commits)} commits")
    result = HotspotsResult(
        repo=repo,
        since=since,
        until=until,
        total_commits=len(commits),
        hotspots=compute_hotspots(commits, limit),
    )
    await report(2, 2, "Done")
    return result


# --- author_activity -----------------------------------------------------------


def compute_author_activity(commits: Iterable[CommitRecord]) -> list[AuthorActivity]:
    """Group by email; the name is the one used in the newest commit."""
    by_email: dict[str, AuthorActivity] = {}
    for commit in commits:  # newest first
        entry = by_email.setdefault(
            commit.email,
            AuthorActivity(name=commit.author, email=commit.email, commits=0, added=0, deleted=0),
        )
        entry.commits += 1
        entry.added += sum(f.added for f in commit.files)
        entry.deleted += sum(f.deleted for f in commit.files)
    return sorted(by_email.values(), key=lambda a: (-a.commits, -(a.added + a.deleted), a.name))


async def author_activity(
    git: GitRunner, repo: str, *, since: date | None, until: date | None
) -> AuthorActivityResult:
    commits = await read_log(git, since=since, until=until)
    return AuthorActivityResult(
        repo=repo, since=since, until=until, authors=compute_author_activity(commits)
    )


# --- file_history --------------------------------------------------------------


async def file_history(git: GitRunner, repo: str, path: str, *, limit: int) -> FileHistoryResult:
    """``path`` must already be validated with ``security.validate_file_path``."""
    commits = await read_log(git, extra=["-n", str(limit)], paths=[path])
    history = []
    for commit in commits:
        change = next((f for f in commit.files if f.path == path), FileChange(path, 0, 0))
        history.append(
            FileCommit(
                sha=commit.sha,
                author=commit.author,
                date=commit.date,
                subject=commit.subject,
                added=change.added,
                deleted=change.deleted,
            )
        )
    return FileHistoryResult(repo=repo, path=path, commits=history)


# --- coupled_files ---------------------------------------------------------------


def compute_coupling(
    commits: Iterable[CommitRecord], path: str, limit: int
) -> tuple[int, list[CoupledFile]]:
    """Count how often each other file appears in the commits that changed ``path``."""
    file_commits = 0
    counts: Counter[str] = Counter()
    for commit in commits:
        paths = {f.path for f in commit.files}
        if path not in paths:
            continue
        file_commits += 1
        counts.update(paths - {path})
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return file_commits, [
        CoupledFile(path=other, co_changes=n, ratio=round(n / file_commits, 2))
        for other, n in ranked
    ]


async def coupled_files(git: GitRunner, repo: str, path: str, *, limit: int) -> CoupledFilesResult:
    """``path`` must already be validated with ``security.validate_file_path``."""
    # --full-diff keeps every file of the matching commits, not only ``path``.
    commits = await read_log(git, extra=["--full-diff"], paths=[path])
    file_commits, coupled = compute_coupling(commits, path, limit)
    return CoupledFilesResult(repo=repo, path=path, file_commits=file_commits, coupled=coupled)


# --- draft_changelog -------------------------------------------------------------

_CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?:\s+(?P<description>.+)$"
)
# Section order of the changelog; anything else lands in "other".
CHANGELOG_SECTIONS = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "test": "Tests",
    "build": "Build",
    "ci": "CI",
    "style": "Style",
    "chore": "Chores",
    "revert": "Reverts",
    "other": "Other",
}


def group_changelog(commits: Iterable[tuple[str, str, str]]) -> list[ChangelogSection]:
    """Group ``(sha, author, subject)`` tuples by conventional-commit type."""
    grouped: dict[str, list[ChangelogEntry]] = {}
    for sha, author, subject in commits:
        match = _CONVENTIONAL_RE.match(subject)
        kind = match["type"].lower() if match else "other"
        if match and kind in CHANGELOG_SECTIONS:
            entry = ChangelogEntry(
                sha=sha,
                author=author,
                subject=subject,
                description=match["description"],
                scope=match["scope"] or None,
                breaking=bool(match["breaking"]),
            )
        else:
            kind = "other"
            entry = ChangelogEntry(
                sha=sha,
                author=author,
                subject=subject,
                description=subject,
                scope=None,
                breaking=False,
            )
        grouped.setdefault(kind, []).append(entry)
    return [
        ChangelogSection(type=kind, title=title, entries=grouped[kind])
        for kind, title in CHANGELOG_SECTIONS.items()
        if kind in grouped
    ]


async def draft_changelog(git: GitRunner, repo: str, from_ref: str, to_ref: str) -> ChangelogResult:
    """Refs must already be validated with ``security.validate_ref``."""
    out = await git(
        ["log", "--no-merges", f"--format=%H{_US}%an{_US}%s", f"{from_ref}..{to_ref}", "--"]
    )
    commits = [
        (sha, author, subject)
        for sha, author, subject in (line.split(_US, 2) for line in out.splitlines() if line)
    ]
    return ChangelogResult(
        repo=repo,
        from_ref=from_ref,
        to_ref=to_ref,
        total_commits=len(commits),
        sections=group_changelog(commits),
    )
