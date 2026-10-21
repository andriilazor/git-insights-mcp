"""Shared fixtures: a temporary Git repository with a fully known history.

The history is small enough to check every expected number by hand:

| # | Date (UTC) | Author | Subject                          | Lines added/deleted        |
|---|------------|--------|----------------------------------|----------------------------|
| 1 | 2026-01-05 | Alice  | feat: initial app                | src/app.py +3, README.md +1|
| 2 | 2026-01-10 | Bob    | feat(auth): add login            | src/auth.py +5, app.py +1  |
| 3 | 2026-01-15 | Alice  | fix(auth): handle empty password | src/auth.py +2/-1, app.py +1|
| 4 | 2026-02-01 | Bob    | docs: update readme              | README.md +2               |
| 5 | 2026-02-10 | Alice  | feat: add config loader          | config.yaml +2, app.py +1, |
|   |            |        |                                  | auth.py +1                 |
| 6 | 2026-02-20 | Bob    | refactor auth module             | src/auth.py +1/-1          |

Tags: ``v1.0`` on commit 3, ``v1.1`` on commit 6.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ALICE = ("Alice", "alice@example.com")
BOB = ("Bob", "bob@example.com")

# Each commit: (author, ISO date, subject, {path: full file content}, tag or None)
HISTORY: list[tuple[tuple[str, str], str, str, dict[str, str], str | None]] = [
    (
        ALICE,
        "2026-01-05T10:00:00+00:00",
        "feat: initial app",
        {"src/app.py": "a1\na2\na3\n", "README.md": "r1\n"},
        None,
    ),
    (
        BOB,
        "2026-01-10T10:00:00+00:00",
        "feat(auth): add login",
        {"src/auth.py": "u1\nu2\nu3\nu4\nu5\n", "src/app.py": "a1\na2\na3\na4\n"},
        None,
    ),
    (
        ALICE,
        "2026-01-15T10:00:00+00:00",
        "fix(auth): handle empty password",
        {
            "src/auth.py": "u1\nu2\nu3\nu4\nu5-fixed\nu6\n",
            "src/app.py": "a1\na2\na3\na4\na5\n",
        },
        "v1.0",
    ),
    (
        BOB,
        "2026-02-01T10:00:00+00:00",
        "docs: update readme",
        {"README.md": "r1\nr2\nr3\n"},
        None,
    ),
    (
        ALICE,
        "2026-02-10T10:00:00+00:00",
        "feat: add config loader",
        {
            "config.yaml": "c1\nc2\n",
            "src/app.py": "a1\na2\na3\na4\na5\na6\n",
            "src/auth.py": "u1\nu2\nu3\nu4\nu5-fixed\nu6\nu7\n",
        },
        None,
    ),
    (
        BOB,
        "2026-02-20T10:00:00+00:00",
        "refactor auth module",
        {"src/auth.py": "u1-r\nu2\nu3\nu4\nu5-fixed\nu6\nu7\n"},
        "v1.1",
    ),
]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def build_repo(root: Path) -> Path:
    """Create the repository described in the module docstring under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    for (name, email), date, subject, files, tag in HISTORY:
        for rel, content in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        _git(root, "add", "-A")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": date,
        }
        _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", subject, env=env)
        if tag:
            _git(root, "-c", "tag.gpgsign=false", "tag", tag, env=env)
    return root


@pytest.fixture(scope="session")
def git_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The known-history repository. Shared and read-only: tests must not modify it."""
    return build_repo(tmp_path_factory.mktemp("fixture") / "repo")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
