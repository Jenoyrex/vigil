"""Vigil: the official Python SDK for sending trace/span telemetry to Vigil.

    from vigil import Vigil

    vigil = Vigil(api_key="vgl_...", base_url="http://127.0.0.1:8000")
    with vigil.start_span("my operation", span_type="function") as span:
        span.set_attribute("key", "value")
    vigil.flush()
    vigil.close()

See README.md for full usage, configuration, and V1 limitations.
"""

from __future__ import annotations

from vigil._version import __version__
from vigil.client import Vigil
from vigil.exceptions import (
    VigilConfigurationError,
    VigilDeliveryError,
    VigilError,
    VigilFlushError,
)
from vigil.span import Span
from vigil.types import RECOMMENDED_SPAN_TYPES

__all__ = [
    "RECOMMENDED_SPAN_TYPES",
    "Span",
    "Vigil",
    "VigilConfigurationError",
    "VigilDeliveryError",
    "VigilError",
    "VigilFlushError",
    "__version__",
]
