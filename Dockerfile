FROM python:3.13-alpine

LABEL org.opencontainers.image.title="calendar-proxy" \
      org.opencontainers.image.description="OIDC-authenticated CalDAV proxy and web viewer for a shared calendar"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt .
# Every dependency publishes musllinux wheels for both amd64 and arm64, so no
# compiler or Rust toolchain is needed. --only-binary fails the build loudly if
# that ever stops being true, rather than falling back to a source build that
# would fail later with a confusing error.
RUN pip install --only-binary=:all: -r requirements.txt

COPY app ./app

RUN addgroup -S -g 10001 calproxy \
 && adduser -S -u 10001 -G calproxy -H -s /sbin/nologin calproxy \
 && mkdir -p /data \
 && chown calproxy:calproxy /data

USER calproxy
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=5).status == 200 else 1)"

# X-Forwarded-* headers are honoured only from FORWARDED_ALLOW_IPS (uvicorn
# reads the env var; default 127.0.0.1). The compose file sets it to the edge
# network's subnet. Never set it to "*": the per-IP rate limiter on CalDAV
# auth keys on the forwarded client IP, so trusting the header from anywhere
# lets clients spoof their way past brute-force protection.
CMD ["uvicorn", "app.main:build", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers"]
