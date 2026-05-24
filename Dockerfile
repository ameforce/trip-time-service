FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    TTS_HOST=0.0.0.0 \
    TTS_PORT=8500 \
    TTS_RELOAD=false

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        chromium \
        chromium-sandbox \
        curl \
        fonts-noto-cjk \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app /home/appuser/.cache /home/appuser/.config \
    && chown -R appuser:appuser /app /home/appuser

COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src

RUN uv sync --frozen --no-dev --no-editable
RUN chown -R appuser:appuser /app /home/appuser

ARG APP_VERSION=v0.0.0.0
ENV TTS_VERSION=${APP_VERSION}
ENV TTS_CHROME_BINARY_PATH=/usr/bin/chromium
ENV HOME=/home/appuser
ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8500

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl --fail --silent "http://127.0.0.1:${TTS_PORT}/healthz" >/dev/null || exit 1

USER appuser

CMD ["sh", "-c", "uvicorn trip_time_service.api.main:app --host ${TTS_HOST:-0.0.0.0} --port ${TTS_PORT:-8500}"]
