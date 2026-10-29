"""Logging setup.

Privacy rule for the whole code base: log counts, status codes, exception
class names and timings, but NEVER addresses, Location IDs or request URLs.
(requests exception messages embed the full URL including the query string,
so exception text must not be logged either.)
"""
from __future__ import annotations

import logging

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging once. Safe to call repeatedly (Streamlit reruns)."""
    root = logging.getLogger()
    if any(getattr(h, "_exposure_geocoder", False) for h in root.handlers):
        root.setLevel(level)
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler._exposure_geocoder = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)

    # urllib3 debug logging prints request paths with query strings.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
