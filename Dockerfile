FROM python:3.13-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8016 \
    DB_PATH=/data/clipboard.db

RUN addgroup -S app \
    && adduser -S -G app app \
    && mkdir -p /app/static /data \
    && chown -R app:app /app /data

WORKDIR /app
COPY --chown=app:app server.py /app/server.py
COPY --chown=app:app static/index.html /app/static/index.html

USER app
EXPOSE 8016

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8016/health', timeout=2)"]

CMD ["python", "/app/server.py"]
