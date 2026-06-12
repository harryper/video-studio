# Gunicorn config for video-studio Web.
# Single-user, single-pod: 2 workers is enough. Long render times
# (5min+ for puppeteer renders) are handled by host-side systemd
# daemons, NOT by the web container. The web container is just API + UI.

import os


bind = "0.0.0.0:9998"

# Workers: 2 sync workers (matches voice-studio). Single-user traffic only.
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
worker_class = "sync"

# Timeout: 60s is plenty — video job creation is just JSON write + trigger touch.
# Actual heavy work (script/render/narrate) runs in host-side systemd daemons.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "10"))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

# Memory-leak guard: recycle workers after N requests.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "500"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "100"))

# Logging
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "/app/logs/access.log")
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "/app/logs/error.log")
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
