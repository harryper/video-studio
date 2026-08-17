from fastapi import FastAPI

from studio.api.routes import comments, stages


def create_app() -> FastAPI:
    app = FastAPI(title="Content Studio")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "app": "content-studio"}

    app.include_router(stages.router)
    app.include_router(comments.router)

    return app


app = create_app()
