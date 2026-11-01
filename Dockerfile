# syntax=docker/dockerfile:1

# ---- stage 1: fetch Leaflet (map library) so the UI has no runtime CDN dependency
FROM node:20-alpine AS leaflet
WORKDIR /tmp/lf
RUN npm pack leaflet@1.9.4 --silent \
 && tar xzf leaflet-1.9.4.tgz \
 && mkdir -p /out \
 && cp -r package/dist/leaflet.js package/dist/leaflet.css package/dist/images /out/

# ---- stage 2: the application (pure Python, so the same Dockerfile builds for amd64 and arm64)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY --from=leaflet /out ./app/static/vendor/leaflet

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
