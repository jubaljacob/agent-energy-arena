FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=0

# Runtime deps for both `world` and `agent` services.  httpx powers the
# agent's live-HTTP transport; the rest are world deps.
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel && \
    pip install fastapi 'uvicorn[standard]' numpy pydantic httpx

COPY world ./world
COPY agents ./agents
COPY baselines ./baselines
COPY evaluate.py ./

EXPOSE 8000
CMD ["uvicorn", "world.api:app", "--host", "0.0.0.0", "--port", "8000"]
