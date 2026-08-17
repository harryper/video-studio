from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Content Studio")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "app": "content-studio"}

    return app


app = create_app()
