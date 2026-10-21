# git-insights-mcp: PRD and implementation plan (verified against MCP Python SDK v2)

## Context

The author wants a portfolio project that shows a recruiter two things: hands-on MCP experience, and the ability to stand up and configure a working MCP server. It should be non-trivial but finishable in a few days.

Chosen idea: **an MCP server that analyzes a local Git repository**. It works on any repo, needs no API keys, and a recruiter can point it at their own code and see results immediately.

Conventions requested by the author: **uv** for everything; **English** for the project description, code comments, docstrings and `README.md`.

## SDK facts verified before planning

Checked against the SDK README, the v1→v2 migration guide and the docs at py.sdk.modelcontextprotocol.io (PyPI latest: `mcp` 2.3.0).

| Topic | Correct for v2 |
|---|---|
| Install | `uv add "mcp[cli]>=2,<3"` (the `cli` extra gives `mcp dev` / `mcp run`) |
| Server class | `from mcp.server import MCPServer` (**not** `FastMCP`; `mcp.server.fastmcp` raises `ModuleNotFoundError`) |
| Context | `from mcp.server.mcpserver import Context`, declared as a `ctx: Context` parameter; `get_context()` is removed |
| Errors | `from mcp.server.mcpserver.exceptions import ToolError` for failures the model can fix; any other exception reaches the client only as a generic "Error executing tool ..." |
| Prompts | `@mcp.prompt()`; message classes in `mcp.server.mcpserver.prompts.base` |
| Constructor | Only `name` is positional; pass `instructions=`, `title=` etc. by keyword |
| Transport options | Go on `mcp.run(transport="streamable-http", host=..., port=..., ...)`, **not** on the constructor. `MCP_*` env vars are no longer read, so the app reads its own env |
| Progress | `await ctx.report_progress(progress, total=..., message=...)`, absolute values; a no-op if the client sent no progress callback |
| Logging | `Context.log()` helpers are deprecated: use stdlib `logging`, writing to **stderr** (stdout belongs to the stdio protocol) |
| Structured output | Return a Pydantic model for an object schema. `list[...]` and scalars are wrapped as `{"result": ...}`, so tools return wrapper models. Output is validated before it leaves the server |
| Handlers | Sync `def` handlers run on a worker thread; use `async def` so git calls can use `asyncio.create_subprocess_exec` and report progress |
| Resource templates | Parameter values containing `..`, null bytes or absolute paths are rejected by default, so templates take a repo **alias**, never a path |
| HTTP auth | `token_verifier=` and `auth=AuthSettings(...)` must be passed together (otherwise `ValueError`). `AuthSettings` expects an `issuer_url`; the SDK never issues tokens |
| DNS rebinding | Auto-protection covers `127.0.0.1`/`localhost`; for any other hostname (Docker, remote) set `TransportSecuritySettings` or clients get 421 |
| Testing | `from mcp import Client`; `async with Client(mcp, raise_exceptions=True) as c`; `@pytest.mark.anyio` with an `anyio_backend` fixture returning `"asyncio"`. The in-memory client **skips HTTP auth**, so auth needs its own test |
| Dev tools | `uv run mcp dev <file>` opens the Inspector (needs `npx`); `uv run mcp run <file>` finds a module-level `mcp` object |
| Python | Target 3.11+ (README says 3.11+; PyPI metadata says >=3.10) |

Still to confirm during implementation (docs pages did not show them): client method names for reading resources and getting prompts, whether `issuer_url` can be omitted, and the exact technique for testing the HTTP app with an ASGI test client.

## Product description

`git-insights-mcp` lets an AI client (Claude Desktop, Claude Code, MCP Inspector) ask questions about a Git repository's history:

- "Which files change most often, and where is the risk?"
- "Who worked on what in the last three months?"
- "Which files usually change together with `auth.py`?"
- "Draft a changelog between v1.2 and v1.3."

The server is **read-only** and returns structured data. Interpretation is left to the AI client.

## Goals

1. Show all three MCP primitives: tools, resources, prompts.
2. Show two transports: stdio and streamable HTTP with bearer authentication.
3. Secure by default: repository allowlist, read-only git subcommands, timeouts, output limits.
4. Show engineering hygiene: typing, tests, CI, Docker, README with ready client configs.
5. Go from clone to a connected client in 5 minutes or less.

## Non-goals

- Any write operation (commit, push, checkout, reset).
- GitHub or other hosting APIs.
- LLM calls inside the server.
- A database or a frontend.
- A real OAuth authorization server.

## Functional requirements

### Tools (all `async def`, all return Pydantic models)

| Tool | Purpose |
|---|---|
| `repo_summary` | Branch, commit count, date range, authors, file-extension distribution |
| `hotspots` | Files ranked by change frequency and churn over a period; reports progress via `ctx.report_progress` |
| `author_activity` | Commits and changed lines per author over a period |
| `file_history` | Recent commits touching a file |
| `coupled_files` | Files that frequently change together with a given file |
| `draft_changelog` | Commits between two refs grouped by conventional-commit type, with an "other" bucket |

Expected failures (unknown ref, repo not in allowlist, git timeout) raise `ToolError` with a message the model can act on.

### Resources

- `git-insights://repos`: the configured repository aliases.
- `git-insights://{repo}/summary`: summary for one alias (`mime_type="application/json"`).

### Prompts

- `onboarding`, `release_notes`, `risk_review`, each returning a ready-made user message that tells the client which tools to call.

### Security

- Repositories are configured as `alias=path` pairs via `GIT_INSIGHTS_REPOS`. Tools take the alias; paths are resolved once at startup and symlink escapes are rejected.
- Only allowlisted git subcommands (`log`, `rev-list`, `show`, `shortlog`, `diff --stat`), arguments passed as a list, never `shell=True`; refs and file arguments validated so they cannot be parsed as options.
- Per-call timeout and output size cap.
- HTTP transport requires `GIT_INSIGHTS_TOKEN`. Auth uses the SDK's `TokenVerifier` + `AuthSettings`, with a constant-time token comparison. `issuer_url` is configurable and documented in the README as a stand-in for a real authorization server.

## Architecture

```
git-insights-mcp/
├── pyproject.toml          # uv project, console script `git-insights-mcp`
├── uv.lock                 # committed
├── Dockerfile              # uv-based, non-root, repos mounted read-only
├── README.md               # English; client configs, demo transcript
├── docs/PRD.md             # this PRD, in English
├── .env.example
├── .github/workflows/ci.yml
├── src/git_insights/
│   ├── server.py           # MCPServer, tools/resources/prompts, CLI entry (--transport)
│   ├── git.py              # async git runner: allowlist, timeout, output cap
│   ├── analysis.py         # pure functions: hotspots, coupling, changelog grouping
│   ├── models.py           # Pydantic result models
│   ├── security.py         # alias resolution, token verifier
│   └── config.py           # reads env, no MCP_* magic
└── tests/
    ├── conftest.py         # builds a temp git repo with a known history
    ├── test_analysis.py
    ├── test_security.py
    ├── test_server.py      # in-memory Client
    └── test_http_auth.py   # 401 without token, 200 with token
```

Design decisions: call `git` via `subprocess` rather than GitPython; stateless server with a small cache keyed by HEAD; MIT license; `inline-snapshot` optional in tests.

## Implementation steps (uv-first)

1. `uv init --package git-insights-mcp`; `uv add "mcp[cli]>=2,<3"`; `uv add --dev pytest inline-snapshot ruff mypy`. Set Python 3.11+, ruff and `mypy --strict` config. Write `docs/PRD.md`.
2. `config.py`, `security.py`, `git.py` with tests (path escapes, option-injection refs, timeouts).
3. `models.py` and `analysis.py`: `repo_summary`, `hotspots`, `file_history`; test against the fixture repo.
4. `coupled_files`, `draft_changelog`, progress reporting.
5. `server.py`: wire tools, resources, prompts; `main()` with `--transport stdio|streamable-http`, `--host`, `--port`.
6. HTTP auth + `TransportSecuritySettings`; `test_http_auth.py`.
7. Dockerfile and a compose example; CI (`astral-sh/setup-uv`, `uv sync --frozen`, ruff, mypy, pytest).
8. English README: install via `uvx`/`uv run`, Claude Desktop and Claude Code configs, Inspector screenshots, demo transcript, security notes.

## Success criteria

1. Clone to connected client in 5 minutes or less following the README.
2. `docker run` with a read-only repo mount works over HTTP.
3. `uv run ruff check`, `uv run mypy src`, `uv run pytest` all pass in CI.
4. Escaping the allowlist is rejected, with a test proving it.
5. README includes a demo transcript and Inspector screenshots.

## Verification

- `uv run pytest`, `uv run ruff check`, `uv run mypy src`.
- `uv run mcp dev src/git_insights/server.py`: call every tool, read both resources, run all prompts.
- `claude mcp add` over stdio, then ask real questions about this repo.
- HTTP mode: request without a token returns 401, with token succeeds; a request with a wrong `Host` behaves as configured.
- Path outside the allowlist and a ref like `--output=x` both return a `ToolError`.
