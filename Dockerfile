# syntax=docker/dockerfile:1
#
# Two stages: a builder that resolves the locked dependencies with uv, and a runtime that
# only carries the finished virtualenv. Base images are pinned by version AND digest;
# Renovate keeps both current. No secrets are used at build time and none end up in a layer.

FROM python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# Dependencies first: this layer is rebuilt only when the lock file changes.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Then the project itself, installed non-editable so the runtime needs no source tree.
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime

LABEL org.opencontainers.image.title="minecraft-neoforge-telegram-monitor" \
      org.opencontainers.image.description="Posts new Minecraft Java Edition and NeoForge releases to two Telegram channels." \
      org.opencontainers.image.source="https://github.com/athegreat90/minecraft-neoforge-telegram-monitor" \
      org.opencontainers.image.licenses="MIT"

# Unprivileged user; /data is where the SQLite state lives (mount a volume there).
RUN groupadd --system --gid 10001 monitor \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin monitor \
    && install -d -o monitor -g monitor -m 0750 /data

COPY --from=builder /app/.venv /app/.venv

# The app runs from /app/.venv and never needs pip at runtime. Removing the base image's pip also
# removes its vendored libraries (msgpack, urllib3, ...), which image scanners report as CVEs.
RUN python -m pip uninstall --yes pip \
    && ! python -m pip --version > /dev/null 2>&1

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/releases.sqlite3

WORKDIR /app
USER 10001:10001
VOLUME ["/data"]

STOPSIGNAL SIGTERM
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "release_monitor", "healthcheck"]

ENTRYPOINT ["python", "-m", "release_monitor"]
CMD ["run"]
