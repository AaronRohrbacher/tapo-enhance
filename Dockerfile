# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS build

WORKDIR /src/app
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg nmap iproute2 curl tini gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 tapo

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TAPO_CACHE_ROOT=/data \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=tapo:tapo . /app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN mkdir -p /data /keys \
    && chown tapo:tapo /data /keys \
    && chmod 700 /keys \
    && chmod 755 /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
VOLUME ["/data", "/keys"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl --fail --silent http://127.0.0.1:8000/api/app >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "app.py"]
