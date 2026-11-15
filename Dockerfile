# One image, three roles (api, worker, auth). The command picks the role in docker-compose.yml.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN useradd -r -u 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY ui ./ui
COPY runbooks ./runbooks
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 RUNBOOK_DIR=/app/runbooks UI_DIR=/app/ui
USER 10001
EXPOSE 8080
CMD ["uvicorn", "agent.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
