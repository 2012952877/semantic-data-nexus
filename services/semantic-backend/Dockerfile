FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY connectors/databricks ./connectors/databricks
COPY services/semantic-api ./services/semantic-api
COPY services/query-runtime ./services/query-runtime
COPY services/semantic-backend ./services/semantic-backend

RUN python -m pip install --prefix=/install \
    ./connectors/databricks \
    ./services/semantic-api \
    ./services/query-runtime \
    "./services/semantic-backend[databricks]"

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SEMANTIC_NEXUS_RESOLVER=fake \
    SEMANTIC_NEXUS_DATA_DIR=/app/data/synthetic/generated

RUN addgroup --system nexus && adduser --system --ingroup nexus nexus
COPY --from=build /install /usr/local
COPY data/synthetic/generated /app/data/synthetic/generated

USER nexus
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=2)"]
CMD ["python", "-m", "semantic_backend"]
