"""Repository allowlist, validation of everything a client can pass to git, and the
bearer-token verifier for the HTTP transport."""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError

_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Revision syntax we accept: names, tags, hashes, ~/^ ancestry and @{...} reflog suffixes.
_REF_RE = re.compile(r"^[A-Za-z0-9_./~^@{}+-]+$")
_MAX_REF_LENGTH = 256


def resolve_repos(raw: Mapping[str, str]) -> dict[str, Path]:
    """Validate aliases and resolve configured paths once, at startup.

    Raises ``ValueError`` for a bad alias, a missing path or a directory that is
    not the top of a git work tree.
    """
    repos: dict[str, Path] = {}
    for alias, configured in raw.items():
        if not _ALIAS_RE.match(alias):
            raise ValueError(
                f"Repository alias {alias!r} is invalid: use letters, digits, '.', '_' or '-'"
            )
        try:
            path = Path(configured).expanduser().resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError):
            raise ValueError(f"Repository {alias!r}: path {configured!r} does not exist") from None
        if not path.is_dir() or not (path / ".git").exists():
            raise ValueError(f"Repository {alias!r}: {configured!r} is not a git work tree")
        repos[alias] = path
    return repos


class RepoRegistry:
    """The allowlist of repositories, addressed by alias only."""

    def __init__(self, repos: Mapping[str, Path]) -> None:
        self._repos = dict(repos)

    def aliases(self) -> list[str]:
        return sorted(self._repos)

    def get(self, alias: str) -> Path:
        try:
            return self._repos[alias]
        except KeyError:
            configured = ", ".join(self.aliases())
            raise ToolError(
                f"Unknown repository {alias!r}. Configured repositories: {configured}"
            ) from None


def validate_ref(ref: str) -> str:
    """Accept a single revision; reject anything git could parse as an option or a range."""
    if (
        not ref
        or len(ref) > _MAX_REF_LENGTH
        or ref.startswith("-")
        or ".." in ref
        or not _REF_RE.match(ref)
    ):
        raise ToolError(
            f"Invalid ref {ref[:50]!r}: "
            "pass a branch, tag, commit hash or an expression like HEAD~3"
        )
    return ref


def validate_file_path(root: Path, path: str) -> str:
    """Return ``path`` as a normalized POSIX path relative to ``root``.

    The file does not have to exist (history of deleted files is valid), but the
    path must stay inside the repository, also after following symlinks.
    """
    if not path or "\x00" in path or path.startswith("-"):
        raise ToolError(f"Invalid file path {path[:50]!r}")
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ToolError(f"File path {path[:50]!r} must be relative to the repository root")
    normalized = candidate.as_posix()
    resolved_root = root.resolve()
    resolved = (resolved_root / normalized).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ToolError(f"File path {path[:50]!r} points outside the repository")
    return normalized


class StaticTokenVerifier:
    """``TokenVerifier`` that accepts exactly one pre-shared bearer token.

    The comparison is constant-time so response timing does not leak the token.
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("A non-empty token is required")
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token):
            return AccessToken(token=token, client_id="git-insights-client", scopes=[])
        return None
