from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.exceptions import NotFoundError, register_exception_handlers


class _Widget(BaseModel):
    name: str
    quantity: int


def _build_test_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom/not-found")
    def _raise_not_found():
        raise NotFoundError("Widget not found")

    @app.get("/boom/unhandled")
    def _raise_unhandled():
        raise RuntimeError("something exploded")

    @app.post("/widgets")
    def _create_widget(widget: _Widget):
        return widget

    return app


def test_app_exception_produces_structured_error_response():
    client = TestClient(_build_test_app(), raise_server_exceptions=False)

    response = client.get("/boom/not-found")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "Widget not found",
        }
    }


def test_unhandled_exception_produces_structured_internal_error():
    client = TestClient(_build_test_app(), raise_server_exceptions=False)

    response = client.get("/boom/unhandled")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "something exploded" not in body["error"]["message"]


def test_request_validation_error_produces_structured_error_response():
    client = TestClient(_build_test_app(), raise_server_exceptions=False)

    response = client.post("/widgets", json={"quantity": "not-a-number"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "name" in body["error"]["message"]
    assert "quantity" in body["error"]["message"]
    assert "Traceback" not in body["error"]["message"]
    assert "File \"" not in body["error"]["message"]
