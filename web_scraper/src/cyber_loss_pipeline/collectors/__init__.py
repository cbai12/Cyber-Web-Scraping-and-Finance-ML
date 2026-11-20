"""Source collectors.

To add a source:
1. Create ``collectors/<source>.py`` with a ``Collector`` subclass decorated by
   ``@register("<collector-key>")`` and a ``from_config`` classmethod.
2. Add a ``[[sources]]`` block with ``collector = "<collector-key>"`` to
   ``config/sources.toml``.

Every module in this package is imported automatically, so neither this file
nor ``pipeline.py`` needs to change when a collector is added.
"""

import pkgutil
from importlib import import_module

from .base import (
    STAGE_ENRICHMENT,
    STAGE_INCIDENT_LIST,
    Collector,
    RunContext,
    SkipSource,
    get_collector_class,
    register,
    registered_collectors,
)

# Shared helpers, not collectors.
_NOT_COLLECTORS = {"base", "http"}

for _info in pkgutil.iter_modules(__path__):
    if _info.name not in _NOT_COLLECTORS and not _info.name.startswith("_"):
        import_module(f"{__name__}.{_info.name}")

from .california_ag import CaliforniaAGCollector  # noqa: E402
from .jsonl import JsonlCollector  # noqa: E402
from .sec_edgar import SECCollector  # noqa: E402

__all__ = [
    "CaliforniaAGCollector",
    "Collector",
    "JsonlCollector",
    "RunContext",
    "SECCollector",
    "STAGE_ENRICHMENT",
    "STAGE_INCIDENT_LIST",
    "SkipSource",
    "get_collector_class",
    "register",
    "registered_collectors",
]
