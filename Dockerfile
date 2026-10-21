# syntax=docker/dockerfile:1

# --- build: resolve the locked dependencies into a virtual environment with uv ---
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Dependencies first, so source changes do not invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# --- runtime: git + the virtual environment, running as a non-root user ---
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    # Mounted repositories belong to another uid; without this git refuses to read them.
    && git config --system --add safe.directory '*' \
    && useradd --create-home --uid 10001 app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
# Mount repositories read-only, e.g. -v /path/to/repo:/repos/app:ro, and set
# GIT_INSIGHTS_REPOS=app=/repos/app, GIT_INSIGHTS_TOKEN and GIT_INSIGHTS_ALLOWED_HOSTS.
ENTRYPOINT ["git-insights-mcp"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
