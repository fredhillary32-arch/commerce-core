from app.core.config import get_settings


def test_ready_returns_ok_when_database_available(client_with_working_db):
    response = client_with_working_db.get(f"{get_settings().api_v1_prefix}/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


def test_ready_returns_error_when_database_unavailable(client_with_unreachable_db):
    response = client_with_unreachable_db.get(f"{get_settings().api_v1_prefix}/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "INFRASTRUCTURE_ERROR"
