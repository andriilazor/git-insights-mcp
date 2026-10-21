"""MCP server wiring: tools, resources, prompts and the command-line entry point.

No ``from __future__ import annotations`` here: the SDK reads tool and prompt
signatures at registration, and ``mcp dev`` / ``mcp run`` load this file outside
``sys.modules``, where string annotations cannot be resolved.
"""

import argparse
import functools
import json
import logging
import sys
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable, Sequence
from datetime import date
from pathlib import Path
from typing import Annotated, Any, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.server.mcpserver.prompts import base
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from git_insights import analysis
from git_insights.analysis import GitRunner
from git_insights.config import Settings, load_settings
from git_insights.git import run_git
from git_insights.models import (
    AuthorActivityResult,
    ChangelogResult,
    CoupledFilesResult,
    FileHistoryResult,
    HotspotsResult,
    RepoSummary,
)
from git_insights.security import RepoRegistry, resolve_repos, validate_file_path, validate_ref

logger = logging.getLogger("git_insights")

INSTRUCTIONS = """\
Read-only analysis of local Git repositories. Repositories are addressed by alias:
read the git-insights://repos resource (or call any tool with a wrong alias) to see them.
Tools return structured data; interpreting it is up to you. Periods are ISO dates
(YYYY-MM-DD), inclusive, in UTC. Use repo_summary first to learn the date range.
"""

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

Repo = Annotated[str, Field(description="Repository alias, see the git-insights://repos resource")]
Since = Annotated[
    date | None, Field(description="Start of the period (ISO date, inclusive, UTC); omit for all")
]
Until = Annotated[
    date | None, Field(description="End of the period (ISO date, inclusive, UTC); omit for now")
]
Limit = Annotated[int, Field(ge=1, le=1000, description="Maximum number of items to return")]
FilePath = Annotated[str, Field(description="File path relative to the repository root")]

_Model = TypeVar("_Model", bound=BaseModel)


class ResultCache:
    """A small LRU cache. Keys include the commit hashes a result depends on, so a
    new commit produces a new key instead of a stale hit."""

    def __init__(self, maxsize: int = 128) -> None:
        self._maxsize = maxsize
        self._data: OrderedDict[Hashable, BaseModel] = OrderedDict()

    async def get_or_compute(
        self, key: Hashable, compute: Callable[[], Awaitable[_Model]]
    ) -> _Model:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]  # type: ignore[return-value]
        value = await compute()
        self._data[key] = value
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)
        return value


class App:
    """Per-process state: the repository allowlist, limits and the result cache."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = RepoRegistry(resolve_repos(settings.repos))
        self.cache = ResultCache()

    def repo_path(self, alias: str) -> Path:
        return self.registry.get(alias)

    def runner(self, alias: str) -> GitRunner:
        return functools.partial(
            run_git,
            self.registry.get(alias),
            timeout=self.settings.git_timeout,
            max_output_bytes=self.settings.max_output_bytes,
        )

    async def cached(
        self,
        key: tuple[Hashable, ...],
        alias: str,
        compute: Callable[[GitRunner], Awaitable[_Model]],
        refs: Sequence[str] = ("HEAD",),
    ) -> _Model:
        """Run ``compute`` unless a result for the same key and the same commits exists."""
        git = self.runner(alias)
        commits = [(await git(["rev-list", "-n", "1", ref, "--"])).strip() for ref in refs]
        return await self.cache.get_or_compute(
            (*key, alias, *commits), functools.partial(compute, git)
        )


def create_server(settings: Settings | None = None) -> MCPServer:
    """Build the MCP server.

    With ``settings`` the configuration is validated immediately. Without, it is read
    from the environment on first use, so the module-level ``mcp`` object below can be
    imported by ``mcp dev`` / ``mcp run`` before the environment is checked.
    """
    state: list[App] = [App(settings)] if settings is not None else []

    def app() -> App:
        if not state:
            try:
                state.append(App(load_settings()))
            except ValueError as err:
                raise ToolError(f"Server is not configured: {err}") from None
        return state[0]

    mcp = MCPServer("git-insights", instructions=INSTRUCTIONS)

    # --- tools -------------------------------------------------------------------

    @mcp.tool(annotations=READ_ONLY)
    async def repo_summary(repo: Repo) -> RepoSummary:
        """Overview of a repository: checked-out branch, commit count, first and last
        commit dates, authors by commit count, and the file-extension distribution of HEAD."""
        return await app().cached(
            ("repo_summary",), repo, lambda git: analysis.repo_summary(git, repo)
        )

    @mcp.tool(annotations=READ_ONLY)
    async def hotspots(
        repo: Repo, ctx: Context, since: Since = None, until: Until = None, limit: Limit = 20
    ) -> HotspotsResult:
        """Files ranked by how often they changed in the period, then by churn
        (added + deleted lines). Frequently changed, high-churn files are where risk
        and maintenance cost concentrate."""

        async def report(done: float, total: float, message: str) -> None:
            await ctx.report_progress(done, total=total, message=message)

        return await app().cached(
            ("hotspots", since, until, limit),
            repo,
            lambda git: analysis.hotspots(
                git, repo, since=since, until=until, limit=limit, progress=report
            ),
        )

    @mcp.tool(annotations=READ_ONLY)
    async def author_activity(
        repo: Repo, since: Since = None, until: Until = None
    ) -> AuthorActivityResult:
        """Commits and added/deleted lines per author in the period (merge commits
        excluded), ranked by commits."""
        return await app().cached(
            ("author_activity", since, until),
            repo,
            lambda git: analysis.author_activity(git, repo, since=since, until=until),
        )

    @mcp.tool(annotations=READ_ONLY)
    async def file_history(repo: Repo, path: FilePath, limit: Limit = 20) -> FileHistoryResult:
        """Most recent commits that changed a file, newest first, with the lines each
        commit added to and deleted from it. Works for deleted files too."""
        rel = validate_file_path(app().repo_path(repo), path)
        return await app().cached(
            ("file_history", rel, limit),
            repo,
            lambda git: analysis.file_history(git, repo, rel, limit=limit),
        )

    @mcp.tool(annotations=READ_ONLY)
    async def coupled_files(repo: Repo, path: FilePath, limit: Limit = 20) -> CoupledFilesResult:
        """Files that change in the same commits as the given file, with how often
        (co_changes) and the share of the file's commits (ratio). High ratios reveal
        hidden coupling."""
        rel = validate_file_path(app().repo_path(repo), path)
        return await app().cached(
            ("coupled_files", rel, limit),
            repo,
            lambda git: analysis.coupled_files(git, repo, rel, limit=limit),
        )

    @mcp.tool(annotations=READ_ONLY)
    async def draft_changelog(
        repo: Repo,
        from_ref: Annotated[str, Field(description="Exclusive start: tag, branch or commit")],
        to_ref: Annotated[str, Field(description="Inclusive end: tag, branch or commit")] = "HEAD",
    ) -> ChangelogResult:
        """Commits in from_ref..to_ref grouped by conventional-commit type (feat, fix,
        docs, ...), with an 'other' section for subjects that do not follow the convention."""
        start, end = validate_ref(from_ref), validate_ref(to_ref)
        return await app().cached(
            ("draft_changelog", start, end),
            repo,
            lambda git: analysis.draft_changelog(git, repo, start, end),
            refs=(start, end),
        )

    # --- resources ---------------------------------------------------------------

    @mcp.resource(
        "git-insights://repos",
        name="repos",
        description="Aliases of the repositories this server may read",
        mime_type="application/json",
    )
    async def repos_resource() -> str:
        try:
            return json.dumps(app().registry.aliases())
        except ToolError as err:
            raise ResourceError(str(err)) from None

    @mcp.resource(
        "git-insights://{repo}/summary",
        name="repo-summary",
        description="The repo_summary result for one repository alias",
        mime_type="application/json",
    )
    async def summary_resource(repo: str) -> str:
        try:
            current = app()
            if repo not in current.registry.aliases():
                raise ResourceNotFoundError(
                    f"Unknown repository {repo!r}. "
                    f"Configured repositories: {', '.join(current.registry.aliases())}"
                )
            summary = await current.cached(
                ("repo_summary",), repo, lambda git: analysis.repo_summary(git, repo)
            )
        except ToolError as err:
            raise ResourceError(str(err)) from None
        return summary.model_dump_json()

    # --- prompts -----------------------------------------------------------------

    @mcp.prompt()
    def onboarding(repo: str) -> list[base.Message]:
        """Get oriented in an unfamiliar repository."""
        return [
            base.UserMessage(
                f"""Help me get oriented in the Git repository "{repo}".

1. Call `repo_summary` with repo="{repo}" for its size, age, authors and file types.
2. Call `hotspots` with repo="{repo}" and limit=10 to find the most frequently changed files.
3. Call `author_activity` with repo="{repo}" and since set to about three months before
   the last commit date from step 1, to see who works on the code now.
4. For the top three hotspots, call `coupled_files` to see which files change with them.

Then write a short onboarding guide: what the codebase consists of, its most active
areas, who knows which part, and which files to read first. Base every statement on the
tool results and say where the data is thin."""
            )
        ]

    @mcp.prompt()
    def release_notes(repo: str, from_ref: str, to_ref: str = "HEAD") -> list[base.Message]:
        """Draft release notes between two refs."""
        return [
            base.UserMessage(
                f"""Write release notes for the Git repository "{repo}" covering the changes
from {from_ref} to {to_ref}.

1. Call `draft_changelog` with repo="{repo}", from_ref="{from_ref}", to_ref="{to_ref}".
2. Turn the sections into release notes for users: breaking changes first (entries with
   breaking=true), then features, then fixes. Merge entries that describe the same change,
   rewrite terse subjects as plain sentences, and leave out internal entries (tests, CI,
   chores) unless they matter to users.
3. List the commits from the "other" section separately so a maintainer can classify them."""
            )
        ]

    @mcp.prompt()
    def risk_review(repo: str, since: str = "") -> list[base.Message]:
        """Find the riskiest files and explain why."""
        period = f', since="{since}"' if since else ""
        return [
            base.UserMessage(
                f"""Review where the risk is in the Git repository "{repo}".

1. Call `hotspots` with repo="{repo}"{period} and limit=10.
2. For the five files with the highest churn, call `coupled_files` and `file_history`
   (limit=10).
3. Call `author_activity` with repo="{repo}"{period} to see how many people share the work.

Then rank the risky files. For each, give the evidence: frequent changes, high churn,
many files changing together (hidden coupling), recent fixes in its history, or
knowledge concentrated in one author. Finish with concrete suggestions: tests to add,
modules to split, people to pair with."""
            )
        ]

    return mcp


# Module-level server for `mcp dev` / `mcp run`; configuration is read on first use.
mcp = create_server()


def main(argv: Sequence[str] | None = None) -> None:
    """Command-line entry point: ``git-insights-mcp [--transport ...] [--host] [--port]``."""
    parser = argparse.ArgumentParser(
        prog="git-insights-mcp",
        description="Read-only MCP server for Git repository history. "
        "Configure repositories with GIT_INSIGHTS_REPOS='alias=/path,other=/path'.",
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="streamable-http bind address")
    parser.add_argument("--port", type=int, default=8000, help="streamable-http port")
    args = parser.parse_args(argv)

    # stdout belongs to the stdio protocol, so logs go to stderr.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        server = create_server(load_settings())
    except ValueError as err:
        parser.exit(2, f"git-insights-mcp: {err}\n")

    kwargs: dict[str, Any] = {}
    if args.transport == "streamable-http":
        kwargs = {"host": args.host, "port": args.port}
    logger.info("Starting git-insights-mcp over %s", args.transport)
    server.run(args.transport, **kwargs)


if __name__ == "__main__":
    main()
