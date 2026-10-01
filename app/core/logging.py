import json
import logging
import sys
from datetime import datetime, timezone

from app.core.config import get_settings

# Attributes every LogRecord carries by default. Anything else found on a
# record came from a caller's `extra=` (e.g. a future request/correlation ID
# middleware) and should be included in the JSON payload.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}

# Never log these, whether they appear as an `extra=` field or elsewhere.
_REDACTED_KEYS = {
    "password",
    "password_hash",
    "access_token",
    "refresh_token",
    "authorization",
    "card_number",
    "cvv",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key in payload:
            if key.lower() in _REDACTED_KEYS:
                payload[key] = "***REDACTED***"

        return json.dumps(payload, default=str)


def configure_logging() -> None:
    settings = get_settings()

    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root_logger.handlers.clear()
    root_logger.addHandler(handler)
