FROM python:3.12-slim AS test

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests

RUN python -m pip install --upgrade pip && \
    python -m pip install .

RUN python -m unittest discover -s tests -v


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN apt-get update && \
    apt-get install -y --no-install-recommends fuse3 && \
    rm -rf /var/lib/apt/lists/* && \
    python -m pip install --upgrade pip && \
    python -m pip install . && \
    adduser --disabled-password --gecos "" appuser && \
    mkdir -p /data /tmp/t2-fuse && \
    chown -R appuser:appuser /data /tmp/t2-fuse

USER appuser

EXPOSE 8770
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=3)" || exit 1

CMD ["t2-web", "--host", "0.0.0.0", "--port", "8770", "--db", "/data/workspace.db"]
