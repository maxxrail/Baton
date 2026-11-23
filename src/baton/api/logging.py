"""Structured JSON logging with structlog. Every event is PII-redacted before it's written."""

import logging
import sys
from collections.abc import MutableMapping

import structlog

from baton.runtime.redact import redact_value


def _redact_event(
    _logger: object, _method: str, event_dict: MutableMapping[str, object]
) -> MutableMapping[str, object]:
    return {k: redact_value(v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper(), force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # request_id lands here
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            _redact_event,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
