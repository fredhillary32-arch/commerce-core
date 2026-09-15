import json
import logging
import sys
from datetime import datetime, timezone

from app.core.config import get_settings

# Fields carried on every record besides the ones we always emit. A future
# request/correlation ID middleware can attach more of these via `extra=`
# without needing any change here.
_EXTRA_RECORD_ATTRS = ("request_id",)

# Never log these, even if a caller accidentally passes them as `extra`.
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

        for attr in _EXTRA_RECORD_ATTRS:
            value = getattr(record, attr, None)
            if value is not None:
                payload[attr] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key in list(payload):
            if key.lower() in _REDACTED_KEYS:
                payload[key] = "***REDACTED***"

        return json.dumps(payload)


def configure_logging() -> None:
    settings = get_settings()

    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root_logger.handlers.clear()
    root_logger.addHandler(handler)
