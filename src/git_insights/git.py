"""Async git runner restricted to read-only subcommands, with a timeout and an output cap."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

ALLOWED_SUBCOMMANDS = frozenset({"log", "rev-list", "show", "shortlog", "diff"})
# `diff` is only allowed in its summary forms, never as a full patch.
_DIFF_STAT_FLAGS = frozenset({"--stat", "--numstat", "--shortstat"})
_CHUNK = 64 * 1024
_MAX_STDERR_BYTES = 64 * 1024


class _OutputLimitExceeded(Exception):
    pass


async def run_git(repo: Path, args: Sequence[str], *, timeout: float, max_output_bytes: int) -> str:
    """Run ``git <args>`` in ``repo`` and return stdout.

    ``args`` is passed as an argument list (never through a shell) and must start
    with an allowlisted subcommand. Raises ``ToolError`` when the command is not
    allowed, git fails, the call times out, or stdout exceeds ``max_output_bytes``.
    """
    _check_allowed(args)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotepath=false",
        "-C",
        str(repo),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
    )
    assert proc.stdout is not None and proc.stderr is not None
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await asyncio.gather(
                _read_capped(proc.stdout, max_output_bytes, fail=True),
                _read_capped(proc.stderr, _MAX_STDERR_BYTES, fail=False),
            )
            returncode = await proc.wait()
    except TimeoutError:
        await _kill(proc)
        raise ToolError(
            f"git {args[0]} timed out after {timeout:g}s; narrow the query (shorter period or path)"
        ) from None
    except _OutputLimitExceeded:
        await _kill(proc)
        raise ToolError(
            f"git {args[0]} exceeded the output limit of {max_output_bytes} bytes; "
            "narrow the query (shorter period, fewer commits)"
        ) from None

    if returncode != 0:
        raise ToolError(f"git {args[0]} failed: {_error_detail(stderr, returncode)}")
    return stdout.decode(errors="replace")


def _check_allowed(args: Sequence[str]) -> None:
    if not args or args[0] not in ALLOWED_SUBCOMMANDS:
        shown = args[0] if args else "(nothing)"
        raise ToolError(f"git subcommand {shown!r} is not allowed")
    if args[0] == "diff" and not _DIFF_STAT_FLAGS.intersection(args[1:]):
        raise ToolError("git diff is not allowed without --stat, --numstat or --shortstat")


def _error_detail(stderr: bytes, returncode: int) -> str:
    """The most useful stderr line: git's ``fatal:``/``error:`` line, not trailing usage hints."""
    lines = [line.strip() for line in stderr.decode(errors="replace").splitlines() if line.strip()]
    for line in lines:
        if line.startswith(("fatal:", "error:")):
            return line[:300]
    return lines[-1][:300] if lines else f"exit code {returncode}"


async def _read_capped(stream: asyncio.StreamReader, limit: int, *, fail: bool) -> bytes:
    """Read ``stream`` to EOF. Past ``limit`` bytes: raise if ``fail``, else drop the rest."""
    data = bytearray()
    while chunk := await stream.read(_CHUNK):
        if len(data) + len(chunk) > limit:
            if fail:
                raise _OutputLimitExceeded
            data.extend(chunk[: max(0, limit - len(data))])
            continue
        data.extend(chunk)
    return bytes(data)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.kill()
        await proc.wait()
