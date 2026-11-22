"""JSON logs (one object per line) so they can be searched by field.

Every line written while handling a request carries its request_id, through
structlog's contextvars integration.
"""

import logging

import structlog

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "silent": logging.CRITICAL,  # nothing in the app logs at CRITICAL
}


def configure_logging(level: str, *, json: bool) -> None:
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(LEVELS[level]),
        cache_logger_on_first_use=False,
    )


log: structlog.typing.FilteringBoundLogger = structlog.get_logger("payforge")
