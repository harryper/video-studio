FROM python:3.11-slim

# Force Asia/Shanghai so datetime.now() matches the host's wall clock
# (shared with voice-studio and all other skills on this VM).
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# curl for HEALTHCHECK. We don't need ffmpeg inside this container —
# video merging happens in the host-side narrate daemon, not here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/logs

EXPOSE 9998

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS -m 3 http://127.0.0.1:9998/api/health || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
