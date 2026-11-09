"""JSON structured logging (one object per line, stderr)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_STD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}
_REDACT = ("key", "secret", "password", "token", "connection_string")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in _STD or k.startswith("_"):
                continue
            out[k] = "***" if any(r in k.lower() for r in _REDACT) else v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "azure", "uamqp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
