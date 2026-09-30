FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_TORCH_BACKEND=cpu

ARG UV_SYNC_EXTRAS=""

RUN apt-get update \
    && apt-get install --no-install-recommends -y git curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml ./
COPY apps/api/pyproject.toml apps/api/README.md ./apps/api/
RUN uv sync --package llm-wiki-agent-api --no-dev --no-install-project ${UV_SYNC_EXTRAS}

COPY apps/api ./apps/api
COPY knowledge ./knowledge
RUN uv sync --package llm-wiki-agent-api --no-dev ${UV_SYNC_EXTRAS}

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/apps/api/src"

EXPOSE 8000
CMD ["uvicorn", "wiki_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
