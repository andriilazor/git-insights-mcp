"""The streamable HTTP transport: bearer-token auth and Host header checks.

The in-memory client used in test_server.py skips HTTP auth, so these tests talk
HTTP to the ASGI app the server would run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from git_insights.config import Settings
from git_insights.server import create_server, http_run_options

TOKEN = "test-token-1234"
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


def _client(settings: Settings, host: str, base_url: str) -> TestClient:
    server = create_server(settings, require_auth=True)
    options = http_run_options(settings, host=host, port=8000)
    app = server.streamable_http_app(
        host=options["host"], transport_security=options["transport_security"]
    )
    return TestClient(app, base_url=base_url)


@pytest.fixture
def http(git_repo: Path) -> Iterator[TestClient]:
    settings = Settings(repos={"demo": str(git_repo)}, token=TOKEN)
    with _client(settings, host="127.0.0.1", base_url="http://127.0.0.1:8000") as client:
        yield client


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {**HEADERS, "authorization": f"Bearer {token}"}


def _sse_json(text: str) -> dict[str, Any]:
    data = [line.removeprefix("data: ") for line in text.splitlines() if line.startswith("data: ")]
    assert len(data) == 1, text
    result: dict[str, Any] = json.loads(data[0])
    return result


def test_request_without_token_is_rejected(http: TestClient) -> None:
    response = http.post("/mcp", json=INITIALIZE, headers=HEADERS)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_request_with_wrong_token_is_rejected(http: TestClient) -> None:
    response = http.post("/mcp", json=INITIALIZE, headers=_auth("not-the-token"))

    assert response.status_code == 401


def test_request_with_token_reaches_the_tools(http: TestClient) -> None:
    init = http.post("/mcp", json=INITIALIZE, headers=_auth())
    assert init.status_code == 200
    assert _sse_json(init.text)["result"]["serverInfo"]["name"] == "git-insights"

    session = {
        "mcp-session-id": init.headers["mcp-session-id"],
        "mcp-protocol-version": "2025-06-18",
    }
    notified = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**_auth(), **session},
    )
    assert notified.status_code == 202

    call = http.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "repo_summary", "arguments": {"repo": "demo"}},
        },
        headers={**_auth(), **session},
    )
    assert call.status_code == 200
    assert _sse_json(call.text)["result"]["structuredContent"]["commit_count"] == 6


def test_localhost_bind_rejects_foreign_host_header(http: TestClient) -> None:
    response = http.post("/mcp", json=INITIALIZE, headers={**_auth(), "host": "evil.example"})

    assert response.status_code == 421


def test_configured_allowed_hosts_apply_to_any_bind(git_repo: Path) -> None:
    settings = Settings(
        repos={"demo": str(git_repo)}, token=TOKEN, allowed_hosts=["git-insights:8000"]
    )
    with _client(settings, host="0.0.0.0", base_url="http://git-insights:8000") as client:
        allowed = client.post("/mcp", json=INITIALIZE, headers=_auth())
        foreign = client.post("/mcp", json=INITIALIZE, headers={**_auth(), "host": "evil.example"})
        bad_origin = client.post(
            "/mcp", json=INITIALIZE, headers={**_auth(), "origin": "http://evil.example"}
        )

    assert allowed.status_code == 200
    assert foreign.status_code == 421
    assert bad_origin.status_code == 403


def test_http_server_requires_a_token(git_repo: Path) -> None:
    with pytest.raises(ValueError, match="GIT_INSIGHTS_TOKEN"):
        create_server(Settings(repos={"demo": str(git_repo)}), require_auth=True)


def test_cli_refuses_http_without_token(git_repo: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_INSIGHTS_")}
    env["GIT_INSIGHTS_REPOS"] = f"demo={git_repo}"
    proc = subprocess.run(
        [sys.executable, "-m", "git_insights.server", "--transport", "streamable-http"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 2
    assert "GIT_INSIGHTS_TOKEN" in proc.stderr
