"""Configuration read from environment variables.

The MCP SDK v2 no longer reads ``MCP_*`` variables, so the application reads
its own ``GIT_INSIGHTS_*`` settings here and passes them on explicitly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeVar

DEFAULT_GIT_TIMEOUT = 10.0
DEFAULT_MAX_OUTPUT_BYTES = 5_000_000
# Stand-in issuer: the server checks a static token and never issues one itself.
DEFAULT_ISSUER_URL = "http://localhost"

_Number = TypeVar("_Number", int, float)


@dataclass(frozen=True)
class Settings:
    """Validated application settings."""

    repos: dict[str, str]
    """Repository alias -> path as configured (resolved later by ``security.resolve_repos``)."""
    git_timeout: float = DEFAULT_GIT_TIMEOUT
    """Seconds a single git call may run before it is killed."""
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    """Largest stdout a single git call may produce."""
    token: str | None = None
    """Bearer token the streamable-http transport requires."""
    issuer_url: str = DEFAULT_ISSUER_URL
    """Authorization server advertised to HTTP clients (a stand-in; see the README)."""
    allowed_hosts: list[str] = field(default_factory=list)
    """Host header values the HTTP transport accepts, e.g. ``git-insights:8000`` or ``host:*``."""


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build ``Settings`` from ``env`` (defaults to ``os.environ``).

    Raises ``ValueError`` with the offending variable name when a value is invalid.
    """
    env = os.environ if env is None else env
    return Settings(
        repos=_parse_repos(env.get("GIT_INSIGHTS_REPOS")),
        git_timeout=_positive(env, "GIT_INSIGHTS_GIT_TIMEOUT", float, DEFAULT_GIT_TIMEOUT),
        max_output_bytes=_positive(
            env, "GIT_INSIGHTS_MAX_OUTPUT_BYTES", int, DEFAULT_MAX_OUTPUT_BYTES
        ),
        token=env.get("GIT_INSIGHTS_TOKEN") or None,
        issuer_url=env.get("GIT_INSIGHTS_ISSUER_URL") or DEFAULT_ISSUER_URL,
        allowed_hosts=[
            host.strip()
            for host in env.get("GIT_INSIGHTS_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        ],
    )


def _parse_repos(value: str | None) -> dict[str, str]:
    """Parse ``alias=path,alias=path`` into a dict."""
    if value is None or not value.strip():
        raise ValueError(
            "GIT_INSIGHTS_REPOS is required, e.g. GIT_INSIGHTS_REPOS='app=/path/to/repo'"
        )
    repos: dict[str, str] = {}
    for entry in value.split(","):
        alias, sep, path = entry.strip().partition("=")
        alias, path = alias.strip(), path.strip()
        if not sep or not alias or not path:
            raise ValueError(f"GIT_INSIGHTS_REPOS entry {entry.strip()!r} is not 'alias=path'")
        if alias in repos:
            raise ValueError(f"GIT_INSIGHTS_REPOS lists alias {alias!r} twice")
        repos[alias] = path
    return repos


def _positive(env: Mapping[str, str], name: str, kind: type[_Number], default: _Number) -> _Number:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero, got {raw!r}")
    return value
