from fastapi.testclient import TestClient

from studio.api.app import create_app


def test_health_reports_content_studio():
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "app": "content-studio"}
