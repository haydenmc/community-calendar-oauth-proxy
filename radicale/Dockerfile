# Radicale has no official image, so build a minimal one with a pinned range.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir "radicale>=3.3,<4" \
 && useradd --system --uid 10002 radicale \
 && mkdir -p /data/collections && chown -R radicale:radicale /data

COPY config /etc/radicale/config

USER radicale
EXPOSE 5232
CMD ["radicale", "--config", "/etc/radicale/config"]
