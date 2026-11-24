import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "route", "status_code", "duration_ms", "dependency", "error_type"):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        if record.exc_info and record.exc_info[0]:
            entry["error_type"] = record.exc_info[0].__name__
        return json.dumps(entry)


def logging_config(level: str) -> dict:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": JsonFormatter}},
        "handlers": {"stderr": {"class": "logging.StreamHandler", "formatter": "json"}},
        "root": {"level": level, "handlers": ["stderr"]},
        "loggers": {"uvicorn": {"handlers": [], "propagate": True, "level": level},
                    "httpx": {"level": "WARNING"}, "httpcore": {"level": "WARNING"}},
    }
