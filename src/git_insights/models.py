"""Pydantic result models.

Every tool returns one of the ``*Result`` / ``RepoSummary`` objects so clients get an
object output schema (lists and scalars would be wrapped as ``{"result": ...}``).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class AuthorCount(BaseModel):
    name: str
    email: str
    commits: int


class ExtensionCount(BaseModel):
    extension: str = Field(description="Lower-case file suffix such as '.py', or '(none)'")
    files: int


class RepoSummary(BaseModel):
    repo: str = Field(description="Repository alias")
    branch: str | None = Field(description="Checked-out branch, or null for a detached HEAD")
    head: str = Field(description="Full hash of HEAD")
    commit_count: int
    first_commit_at: datetime
    last_commit_at: datetime
    authors: list[AuthorCount] = Field(description="Authors by commit count, descending")
    file_count: int = Field(description="Files in the HEAD tree")
    file_extensions: list[ExtensionCount] = Field(description="Files in HEAD per extension")


class Hotspot(BaseModel):
    path: str
    commits: int = Field(description="Commits in the period that changed this file")
    added: int
    deleted: int
    churn: int = Field(description="added + deleted")


class HotspotsResult(BaseModel):
    repo: str
    since: date | None
    until: date | None
    total_commits: int = Field(description="Non-merge commits in the period")
    hotspots: list[Hotspot] = Field(description="Ranked by commits, then churn")


class AuthorActivity(BaseModel):
    name: str
    email: str
    commits: int
    added: int
    deleted: int


class AuthorActivityResult(BaseModel):
    repo: str
    since: date | None
    until: date | None
    authors: list[AuthorActivity] = Field(description="Ranked by commits, then changed lines")


class FileCommit(BaseModel):
    sha: str
    author: str
    date: datetime
    subject: str
    added: int = Field(description="Lines added to this file by the commit")
    deleted: int = Field(description="Lines deleted from this file by the commit")


class FileHistoryResult(BaseModel):
    repo: str
    path: str
    commits: list[FileCommit] = Field(description="Newest first")


class CoupledFile(BaseModel):
    path: str
    co_changes: int = Field(description="Commits that changed both files")
    ratio: float = Field(description="co_changes / commits that changed the given file")


class CoupledFilesResult(BaseModel):
    repo: str
    path: str
    file_commits: int = Field(description="Non-merge commits that changed the given file")
    coupled: list[CoupledFile] = Field(description="Ranked by co_changes, then path")


class ChangelogEntry(BaseModel):
    sha: str
    author: str
    subject: str = Field(description="Original commit subject")
    description: str = Field(description="Subject without the conventional-commit prefix")
    scope: str | None
    breaking: bool = Field(description="Marked with '!' after the type or scope")


class ChangelogSection(BaseModel):
    type: str = Field(description="Conventional-commit type, or 'other'")
    title: str
    entries: list[ChangelogEntry] = Field(description="Newest first")


class ChangelogResult(BaseModel):
    repo: str
    from_ref: str
    to_ref: str
    total_commits: int
    sections: list[ChangelogSection] = Field(description="Non-empty sections in a fixed order")
