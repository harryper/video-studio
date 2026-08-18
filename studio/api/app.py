"""FastAPI app factory for Content Studio.

Mounts:

* ``/api/health`` — public liveness probe.
* ``/api/projects/...`` — content workflow routes (require session + CSRF
  on mutations).
* ``/api/projects/{id}/events`` — SSE stream of job progress.
* ``/api/session`` — login / logout.
* ``/api/csrf`` — current per-session CSRF token.

All routes other than ``/api/health`` and ``POST /api/session`` require a
valid session cookie; mutating routes additionally require an
``X-CSRF-Token`` header that matches the per-session token.
"""

from __future__ import annotations

from fastapi import FastAPI

from studio.api.errors import register_error_handlers
from studio.api.routes import comments, events, projects, stages


def create_app() -> FastAPI:
    app = FastAPI(title="Content Studio")

    register_error_handlers(app)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "app": "content-studio"}

    app.include_router(projects.router)
    app.include_router(stages.router)
    app.include_router(comments.router)
    app.include_router(events.router)

    return app


app = create_app()