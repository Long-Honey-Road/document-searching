FROM ghcr.io/astral-sh/uv:0.12.0 AS uv
FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_NO_PYTHON_DOWNLOADS=1
COPY pyproject.toml uv.lock README.md ./
COPY packages/common/pyproject.toml packages/common/pyproject.toml
COPY services/backend/pyproject.toml services/backend/pyproject.toml
COPY services/knowledge/pyproject.toml services/knowledge/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --package regagent-backend --no-install-workspace
COPY packages/common ./packages/common
COPY services/backend ./services/backend
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --package regagent-backend

FROM python:3.12-slim AS runtime
WORKDIR /app/services/backend
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/services/backend /app/services/backend
COPY config/access_levels.yaml /app/config/access_levels.yaml
CMD ["uvicorn", "aegra_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
