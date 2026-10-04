# One image for every component; Kubernetes picks the command per Deployment:
#   pdfsearch-worker                                   (default)
#   uvicorn pdfsearch.api:app --host 0.0.0.0 --port 8000

# --- Build stage: install the project and its locked dependencies into a venv.
FROM python:3.13-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv

# Compile .py files now so containers start faster; copy files instead of
# hard-linking from the cache (the cache is not part of the image); and use
# the image's Python instead of downloading one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lock file changes,
# not on every code change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Then the project itself, installed as a regular (non-editable) package so
# the runtime image needs no source tree.
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- Runtime stage: the venv and nothing else (no uv, no source, no caches).
FROM python:3.13-slim

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home app

COPY --from=build /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Never run as root inside the container.
USER 10001

CMD ["pdfsearch-worker"]
