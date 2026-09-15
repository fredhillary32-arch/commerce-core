from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session import get_db
from app.main import create_app


@pytest.fixture(scope="session")
def test_engine() -> Generator[Engine, None, None]:
    settings = get_settings()
    engine = create_engine(settings.test_database_url, pool_pre_ping=True, future=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(test_engine: Engine) -> Generator[Session, None, None]:
    testing_session_local = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, future=True)
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client_with_working_db(app, test_engine: Engine):
    from fastapi.testclient import TestClient

    testing_session_local = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, future=True)

    def _override_get_db() -> Generator[Session, None, None]:
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def client_with_unreachable_db(app):
    from fastapi.testclient import TestClient

    # Nothing listens here; a short connect_timeout keeps the test fast.
    broken_engine = create_engine(
        "postgresql+psycopg://postgres:postgres@localhost:1/commerce_platform",
        connect_args={"connect_timeout": 1},
    )
    broken_session_local = sessionmaker(bind=broken_engine, autoflush=False, autocommit=False, future=True)

    def _override_get_db() -> Generator[Session, None, None]:
        db = broken_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    broken_engine.dispose()
