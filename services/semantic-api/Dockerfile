FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

USER 65532
EXPOSE 8000
CMD ["uvicorn", "semantic_api.api:app", "--host", "0.0.0.0", "--port", "8000"]
