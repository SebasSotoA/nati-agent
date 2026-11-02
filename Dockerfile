FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.7.21 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY src ./src
COPY profiles.toml ./

RUN uv sync --frozen --no-dev

ENV NATI_PUBLIC_CHAT=0
ENV NATI_DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["uv", "run", "--frozen", "nati", "serve", "--host", "0.0.0.0", "--port", "8080"]
