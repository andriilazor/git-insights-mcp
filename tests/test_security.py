"""Tests for the security layer: configuration, repository allowlist, argument
validation and the restricted git runner."""

from __future__ import annotations

import stat
import time
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from git_insights.config import load_settings
from git_insights.git import run_git
from git_insights.security import (
    RepoRegistry,
    StaticTokenVerifier,
    resolve_repos,
    validate_file_path,
    validate_ref,
)

# --- configuration -----------------------------------------------------------


def test_load_settings_parses_alias_path_pairs() -> None:
    settings = load_settings({"GIT_INSIGHTS_REPOS": "app=/srv/app, lib=/srv/lib"})
    assert settings.repos == {"app": "/srv/app", "lib": "/srv/lib"}


@pytest.mark.parametrize(
    "value",
    ["", "   ", "app", "app=", "=/srv/app", "app=/srv/a,app=/srv/b"],
    ids=["empty", "blank", "no-equals", "no-path", "no-alias", "duplicate-alias"],
)
def test_load_settings_rejects_bad_repo_lists(value: str) -> None:
    with pytest.raises(ValueError):
        load_settings({"GIT_INSIGHTS_REPOS": value})


def test_load_settings_requires_repos() -> None:
    with pytest.raises(ValueError, match="GIT_INSIGHTS_REPOS"):
        load_settings({})


def test_load_settings_reads_limits() -> None:
    settings = load_settings(
        {
            "GIT_INSIGHTS_REPOS": "app=/srv/app",
            "GIT_INSIGHTS_GIT_TIMEOUT": "2.5",
            "GIT_INSIGHTS_MAX_OUTPUT_BYTES": "1000",
        }
    )
    assert settings.git_timeout == 2.5
    assert settings.max_output_bytes == 1000


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GIT_INSIGHTS_GIT_TIMEOUT", "soon"),
        ("GIT_INSIGHTS_GIT_TIMEOUT", "0"),
        ("GIT_INSIGHTS_MAX_OUTPUT_BYTES", "-1"),
    ],
)
def test_load_settings_rejects_bad_limits(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        load_settings({"GIT_INSIGHTS_REPOS": "app=/srv/app", name: value})


# --- repository allowlist ----------------------------------------------------


def test_resolve_repos_returns_resolved_paths(git_repo: Path) -> None:
    repos = resolve_repos({"demo": str(git_repo / "src" / "..")})
    assert repos == {"demo": git_repo.resolve()}


def test_resolve_repos_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="demo"):
        resolve_repos({"demo": str(tmp_path / "missing")})


def test_resolve_repos_rejects_non_git_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a git"):
        resolve_repos({"demo": str(tmp_path)})


@pytest.mark.parametrize("alias", ["../up", "a/b", "has space", "", "-dash"])
def test_resolve_repos_rejects_bad_alias(git_repo: Path, alias: str) -> None:
    with pytest.raises(ValueError, match="alias"):
        resolve_repos({alias: str(git_repo)})


def test_registry_returns_path_for_known_alias(git_repo: Path) -> None:
    registry = RepoRegistry({"demo": git_repo})
    assert registry.get("demo") == git_repo
    assert registry.aliases() == ["demo"]


def test_registry_rejects_unknown_alias_and_lists_configured(git_repo: Path) -> None:
    registry = RepoRegistry({"demo": git_repo, "other": git_repo})
    with pytest.raises(ToolError, match=r"'secret'.*demo, other"):
        registry.get("secret")


# --- ref and path validation -------------------------------------------------


@pytest.mark.parametrize("ref", ["main", "v1.0", "HEAD~2", "HEAD^", "feature/x", "a1b2c3d"])
def test_validate_ref_accepts_revisions(ref: str) -> None:
    assert validate_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    ["--output=x", "-n", "", "a b", "a..b", "a\x00b", "a;b", "x" * 300],
    ids=["long-option", "short-option", "empty", "space", "range", "null", "semicolon", "long"],
)
def test_validate_ref_rejects_options_and_garbage(ref: str) -> None:
    with pytest.raises(ToolError):
        validate_ref(ref)


@pytest.mark.parametrize(
    ("given", "expected"),
    [("src/app.py", "src/app.py"), ("./src/app.py", "src/app.py"), ("gone.txt", "gone.txt")],
)
def test_validate_file_path_normalizes_relative_paths(
    git_repo: Path, given: str, expected: str
) -> None:
    assert validate_file_path(git_repo, given) == expected


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside", "src/../../x", "-p", "a\x00b", ""],
    ids=["absolute", "parent", "nested-parent", "option", "null", "empty"],
)
def test_validate_file_path_rejects_escapes(git_repo: Path, path: str) -> None:
    with pytest.raises(ToolError):
        validate_file_path(git_repo, path)


def test_validate_file_path_rejects_symlink_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "link").symlink_to(outside)
    with pytest.raises(ToolError, match="outside"):
        validate_file_path(repo, "link/secret.txt")


# --- restricted git runner ---------------------------------------------------

LIMITS = {"timeout": 10.0, "max_output_bytes": 1_000_000}


@pytest.mark.anyio
async def test_run_git_returns_stdout(git_repo: Path) -> None:
    out = await run_git(git_repo, ["log", "--format=%s", "-n", "1"], **LIMITS)
    assert out == "refactor auth module\n"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args",
    [
        ["config", "--list"],
        ["push"],
        ["-c", "core.pager=sh", "log"],
        ["diff", "HEAD~1", "HEAD"],
        [],
    ],
    ids=["config", "push", "global-option", "diff-without-stat", "empty"],
)
async def test_run_git_rejects_disallowed_commands(git_repo: Path, args: list[str]) -> None:
    with pytest.raises(ToolError, match="not allowed"):
        await run_git(git_repo, args, **LIMITS)


@pytest.mark.anyio
async def test_run_git_allows_diff_with_numstat(git_repo: Path) -> None:
    out = await run_git(git_repo, ["diff", "--numstat", "HEAD~1", "HEAD"], **LIMITS)
    assert out == "1\t1\tsrc/auth.py\n"


@pytest.mark.anyio
async def test_run_git_reports_git_errors(git_repo: Path) -> None:
    with pytest.raises(ToolError, match="no-such-ref"):
        await run_git(git_repo, ["log", "no-such-ref"], **LIMITS)


@pytest.mark.anyio
async def test_run_git_enforces_output_cap(git_repo: Path) -> None:
    with pytest.raises(ToolError, match="output limit"):
        await run_git(git_repo, ["log"], timeout=10.0, max_output_bytes=10)


@pytest.mark.anyio
async def test_run_git_kills_slow_git(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(fake.parent))

    started = time.monotonic()
    with pytest.raises(ToolError, match="timed out"):
        await run_git(git_repo, ["log"], timeout=0.3, max_output_bytes=1000)
    assert time.monotonic() - started < 5


# --- HTTP settings and token verifier ------------------------------------------


def test_load_settings_reads_http_options() -> None:
    settings = load_settings(
        {
            "GIT_INSIGHTS_REPOS": "app=/srv/app",
            "GIT_INSIGHTS_TOKEN": "s3cret",
            "GIT_INSIGHTS_ISSUER_URL": "https://auth.example.com",
            "GIT_INSIGHTS_ALLOWED_HOSTS": "git-insights:8000, localhost:*",
        }
    )
    assert settings.token == "s3cret"
    assert settings.issuer_url == "https://auth.example.com"
    assert settings.allowed_hosts == ["git-insights:8000", "localhost:*"]


def test_load_settings_http_options_are_optional() -> None:
    settings = load_settings({"GIT_INSIGHTS_REPOS": "app=/srv/app", "GIT_INSIGHTS_TOKEN": ""})
    assert settings.token is None
    assert settings.allowed_hosts == []


@pytest.mark.anyio
async def test_static_token_verifier() -> None:
    verifier = StaticTokenVerifier("s3cret")

    accepted = await verifier.verify_token("s3cret")
    assert accepted is not None and accepted.token == "s3cret"
    assert await verifier.verify_token("s3cret ") is None
    assert await verifier.verify_token("wrong") is None
    assert await verifier.verify_token("") is None


def test_static_token_verifier_requires_a_token() -> None:
    with pytest.raises(ValueError):
        StaticTokenVerifier("")
