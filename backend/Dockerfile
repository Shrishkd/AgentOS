FROM python:3.12-slim AS dependencies
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.24
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=dependencies /app/.venv /app/.venv
COPY agentos agentos
COPY migrations migrations
COPY alembic.ini .
USER 65532:65532
CMD ["python", "-m", "agentos.main"]
