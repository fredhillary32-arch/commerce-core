import json
import logging

from app.core.config import get_settings
from app.core.logging import JSONFormatter, configure_logging


def test_configure_logging_sets_level_from_settings(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    try:
        configure_logging()
        assert logging.getLogger().level == logging.WARNING
    finally:
        get_settings.cache_clear()
        configure_logging()


def test_json_formatter_produces_structured_output():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert "timestamp" in payload


def test_json_formatter_redacts_sensitive_extra_fields():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="user login",
        args=(),
        exc_info=None,
    )
    record.password = "hunter2"
    record.request_id = "abc-123"

    payload = json.loads(formatter.format(record))

    assert payload["password"] == "***REDACTED***"
    assert payload["request_id"] == "abc-123"
