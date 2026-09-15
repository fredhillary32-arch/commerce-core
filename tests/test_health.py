from fastapi.testclient import TestClient

from app.core.config import get_settings


def test_health_returns_ok(app):
    with TestClient(app) as client:
        response = client.get(f"{get_settings().api_v1_prefix}/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
