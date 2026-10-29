FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY converter_playlist_fixed.py .
COPY privacy_monitor_routes.py .
COPY templates ./templates

RUN mkdir -p /downloads && chown -R 1000:1000 /downloads
USER 1000:1000

EXPOSE 8088
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8088", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
