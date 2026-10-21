"""End-to-end tests through an in-memory MCP client (and one over stdio)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, TextContent, TextResourceContents

from git_insights.config import Settings
from git_insights.server import create_server

from .conftest import build_repo

TOOLS = {
    "repo_summary",
    "hotspots",
    "author_activity",
    "file_history",
    "coupled_files",
    "draft_changelog",
}


@pytest.fixture
async def client(git_repo: Path) -> AsyncIterator[Client]:
    server = create_server(Settings(repos={"demo": str(git_repo)}))
    async with Client(server, raise_exceptions=True) as c:
        yield c


def _ok(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def _error_text(result: CallToolResult) -> str:
    assert result.is_error
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


# --- tools -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_lists_read_only_tools_with_output_schemas(client: Client) -> None:
    tools = (await client.list_tools()).tools

    assert {t.name for t in tools} == TOOLS
    for tool in tools:
        assert tool.output_schema is not None, tool.name
        assert tool.annotations is not None and tool.annotations.read_only_hint, tool.name


@pytest.mark.anyio
async def test_repo_summary_tool(client: Client) -> None:
    data = _ok(await client.call_tool("repo_summary", {"repo": "demo"}))

    assert data["branch"] == "main"
    assert data["commit_count"] == 6
    assert data["file_count"] == 4


@pytest.mark.anyio
async def test_hotspots_tool_with_period_and_progress(client: Client) -> None:
    progress: list[tuple[float, float | None]] = []

    async def on_progress(done: float, total: float | None, message: str | None) -> None:
        progress.append((done, total))

    data = _ok(
        await client.call_tool(
            "hotspots",
            {"repo": "demo", "since": "2026-02-01", "until": "2026-02-28", "limit": 3},
            progress_callback=on_progress,
        )
    )

    assert data["since"] == "2026-02-01"
    assert [h["path"] for h in data["hotspots"]] == ["src/auth.py", "README.md", "config.yaml"]
    assert progress, "no progress notifications received"
    assert progress[-1][0] == progress[-1][1]


@pytest.mark.anyio
async def test_author_activity_tool(client: Client) -> None:
    data = _ok(await client.call_tool("author_activity", {"repo": "demo", "since": "2026-02-01"}))

    assert [(a["name"], a["commits"]) for a in data["authors"]] == [("Bob", 2), ("Alice", 1)]


@pytest.mark.anyio
async def test_file_history_tool_normalizes_path(client: Client) -> None:
    data = _ok(
        await client.call_tool("file_history", {"repo": "demo", "path": "./README.md", "limit": 5})
    )

    assert data["path"] == "README.md"
    assert [c["subject"] for c in data["commits"]] == ["docs: update readme", "feat: initial app"]


@pytest.mark.anyio
async def test_coupled_files_tool(client: Client) -> None:
    data = _ok(await client.call_tool("coupled_files", {"repo": "demo", "path": "README.md"}))

    assert data["file_commits"] == 2
    assert [(c["path"], c["co_changes"]) for c in data["coupled"]] == [("src/app.py", 1)]


@pytest.mark.anyio
async def test_draft_changelog_tool_defaults_to_head(client: Client) -> None:
    data = _ok(await client.call_tool("draft_changelog", {"repo": "demo", "from_ref": "v1.0"}))

    assert data["to_ref"] == "HEAD"
    assert [s["type"] for s in data["sections"]] == ["feat", "docs", "other"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("repo_summary", {"repo": "secret"}, "Configured repositories: demo"),
        ("file_history", {"repo": "demo", "path": "../outside"}, "relative"),
        ("coupled_files", {"repo": "demo", "path": "/etc/passwd"}, "relative"),
        ("draft_changelog", {"repo": "demo", "from_ref": "--output=x"}, "Invalid ref"),
        ("draft_changelog", {"repo": "demo", "from_ref": "no-such-tag"}, "no-such-tag"),
    ],
    ids=["unknown-repo", "path-escape", "absolute-path", "option-ref", "unknown-ref"],
)
async def test_expected_failures_are_tool_errors_the_model_can_read(
    client: Client, tool: str, args: dict[str, Any], expected: str
) -> None:
    assert expected in _error_text(await client.call_tool(tool, args))


@pytest.mark.anyio
@pytest.mark.parametrize("limit", [0, 1001])
async def test_limit_is_bounded(client: Client, limit: int) -> None:
    result = await client.call_tool("hotspots", {"repo": "demo", "limit": limit})

    assert result.is_error


@pytest.mark.anyio
async def test_results_follow_head(tmp_path: Path) -> None:
    repo = build_repo(tmp_path / "repo")
    server = create_server(Settings(repos={"live": str(repo)}))
    async with Client(server, raise_exceptions=True) as c:
        before = _ok(await c.call_tool("repo_summary", {"repo": "live"}))
        (repo / "new.txt").write_text("n\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "chore: new"],
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "C",
                "GIT_AUTHOR_EMAIL": "c@example.com",
                "GIT_COMMITTER_NAME": "C",
                "GIT_COMMITTER_EMAIL": "c@example.com",
            },
        )
        after = _ok(await c.call_tool("repo_summary", {"repo": "live"}))

    assert (before["commit_count"], after["commit_count"]) == (6, 7)


# --- resources -------------------------------------------------------------------


def _resource_text(contents: list[Any]) -> TextResourceContents:
    assert len(contents) == 1
    assert isinstance(contents[0], TextResourceContents)
    return contents[0]


@pytest.mark.anyio
async def test_repos_resource_lists_aliases(client: Client) -> None:
    listed = {str(r.uri) for r in (await client.list_resources()).resources}
    assert "git-insights://repos" in listed

    content = _resource_text((await client.read_resource("git-insights://repos")).contents)
    assert json.loads(content.text) == ["demo"]


@pytest.mark.anyio
async def test_summary_resource_template(client: Client) -> None:
    templates = (await client.list_resource_templates()).resource_templates
    assert [t.uri_template for t in templates] == ["git-insights://{repo}/summary"]

    content = _resource_text((await client.read_resource("git-insights://demo/summary")).contents)
    assert content.mime_type == "application/json"
    assert json.loads(content.text)["commit_count"] == 6


@pytest.mark.anyio
async def test_summary_resource_unknown_alias(client: Client) -> None:
    with pytest.raises(MCPError, match="secret"):
        await client.read_resource("git-insights://secret/summary")


# --- prompts ---------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("prompt", "args"),
    [
        ("onboarding", {"repo": "demo"}),
        ("release_notes", {"repo": "demo", "from_ref": "v1.0", "to_ref": "v1.1"}),
        ("risk_review", {"repo": "demo"}),
    ],
)
async def test_prompts_point_the_client_at_real_tools(
    client: Client, prompt: str, args: dict[str, str]
) -> None:
    result = await client.get_prompt(prompt, args)

    assert len(result.messages) == 1
    message = result.messages[0]
    assert message.role == "user"
    assert isinstance(message.content, TextContent)
    text = message.content.text
    mentioned = set(re.findall(r"`([a-z_]+)`", text))
    assert mentioned, "prompt names no tools"
    assert mentioned <= TOOLS
    assert all(value in text for value in args.values())


@pytest.mark.anyio
async def test_lists_prompts(client: Client) -> None:
    names = {p.name for p in (await client.list_prompts()).prompts}
    assert names == {"onboarding", "release_notes", "risk_review"}


# --- command line ----------------------------------------------------------------


def test_cli_fails_fast_without_repos() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_INSIGHTS_")}
    proc = subprocess.run(
        [sys.executable, "-m", "git_insights.server"], env=env, capture_output=True, text=True
    )

    assert proc.returncode == 2
    assert "GIT_INSIGHTS_REPOS" in proc.stderr


@pytest.mark.anyio
async def test_cli_serves_stdio(git_repo: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "git_insights.server", "--transport", "stdio"],
        env={**os.environ, "GIT_INSIGHTS_REPOS": f"demo={git_repo}"},
    )
    async with Client(params) as c:
        data = _ok(await c.call_tool("repo_summary", {"repo": "demo"}))

    assert data["commit_count"] == 6


@pytest.mark.anyio
async def test_server_file_loads_the_way_mcp_cli_loads_it() -> None:
    """`mcp dev` / `mcp run` import server.py by path, outside sys.modules."""
    import importlib.util

    import git_insights.server

    spec = importlib.util.spec_from_file_location("server_module", git_insights.server.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async with Client(module.mcp) as c:
        assert {t.name for t in (await c.list_tools()).tools} == TOOLS
