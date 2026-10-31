"""Logging (M6.3): what reaches a log line, and in which shape.

Our own messages hold ids, codes, counts and exception types by design. This
module stops the rest at the source: an exception is written as its types
and code locations, never its message (which may quote a caller); libraries
that log requests at DEBUG (the Anthropic SDK with ANTHROPIC_LOG=debug writes
whole conversations) are held at WARNING; uvicorn's lines lose the client's
address. COLLECTWISE_LOG_FORMAT=json writes one JSON object per line, with
the trace id when tracing is on (M6.2).
"""

import json
import logging
import sys
import traceback
from datetime import datetime, timezone

from opentelemetry import trace

LOG_FORMATS = ("text", "json")

# Libraries that log request bodies, frames or SQL values below WARNING.
QUIET_LIBRARIES = ("anthropic", "httpx", "httpx2", "httpcore", "websockets", "deepgram", "sqlalchemy.engine")

_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")

# The handler this module installs, so configuring again replaces it.
_HANDLER_NAME = "collectwise"


def exception_summary(exc_info) -> list[str]:
    """An exception and its causes as types and code locations: no messages,
    no source lines, no local values."""
    lines: list[str] = []
    exc, tb = exc_info[1], exc_info[2]
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if lines:
            lines.append("caused by:")
        lines.append(type(exc).__name__)
        lines += [f"  at {frame.filename}:{frame.lineno} in {frame.name}" for frame in traceback.extract_tb(tb)]
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        tb = exc.__traceback__ if exc is not None else None
    return lines


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Another handler may already have cached the full traceback, message
        # and all, on the record: never reuse it. Ours stays cached for any
        # handler after this one.
        record.exc_text = None
        return super().format(record)

    def formatException(self, ei) -> str:
        return "Exception (types and locations only):\n" + "\n".join(exception_summary(ei))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = exception_summary(record.exc_info)
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            entry["trace_id"] = format(context.trace_id, "032x")
            entry["span_id"] = format(context.span_id, "016x")
        return json.dumps(entry)


class DropClientAddress(logging.Filter):
    """uvicorn starts access and WebSocket lines with the client's address
    ('%s - "GET /path HTTP/1.1" 200'): keep the rest."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and record.msg.startswith("%s - ") and isinstance(record.args, tuple):
            record.msg, record.args = record.msg[len("%s - "):], record.args[1:]
        return True


def configure_logging(log_format: str | None) -> logging.Handler:
    """Send INFO and above to stderr in `log_format` ("text" unless "json"),
    replacing whatever handlers the root and uvicorn loggers had."""
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_HANDLER_NAME)
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(DropClientAddress())

    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if h.get_name() != _HANDLER_NAME and _is_test_capture(h)]
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in _UVICORN:
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def _is_test_capture(handler: logging.Handler) -> bool:
    # pytest's capture handlers stay, so tests can still read log output.
    return type(handler).__module__.startswith("_pytest")
