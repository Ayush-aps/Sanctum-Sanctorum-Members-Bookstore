FROM python:3.12-slim AS builder

WORKDIR /app

RUN pip install --no-cache-dir "uv==0.12.18"

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev


FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

COPY --from=builder /app/.venv /app/.venv
COPY app /app/app
COPY frontend /app/frontend

RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 10000

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]