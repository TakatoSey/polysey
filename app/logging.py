import logging

import structlog


def configure_logging(level: str) -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    # One line per polling request hides the events that actually explain a
    # copied or rejected trade. HTTP failures still surface at WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
    )
