from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import SourceRecord

# Stage A collectors find incidents (who was breached, when, how many records).
STAGE_INCIDENT_LIST = "incident_list"
# Stage C collectors enrich companies that are already on the incident list
# (firmographics, loss amounts, security controls).
STAGE_ENRICHMENT = "enrichment"
STAGES = (STAGE_INCIDENT_LIST, STAGE_ENRICHMENT)


class SkipSource(Exception):
    """Raised by ``from_config`` when a source is enabled but not runnable yet.

    Use this for missing configuration (for example, no SEC CIKs). The pipeline
    reports the reason under ``skipped`` instead of failing the whole run.
    """


@dataclass
class RunContext:
    """Information the pipeline gives collectors when it builds them.

    ``incident_companies`` is filled after Stage A finishes, so Stage C
    (enrichment) collectors can look up only companies that had an incident.
    """

    environment: Mapping[str, str] = field(default_factory=dict)
    incident_companies: list[dict[str, Any]] = field(default_factory=list)


class Collector(ABC):
    #: Which stage this collector belongs to. Override in subclasses.
    stage: str = STAGE_INCIDENT_LIST

    @classmethod
    def from_config(cls, source: Mapping[str, Any], context: RunContext) -> "Collector":
        """Build the collector from its ``[[sources]]`` block in sources.toml."""
        raise NotImplementedError(f"{cls.__name__} does not implement from_config")

    @abstractmethod
    def collect(self) -> Iterator[SourceRecord]:
        """Yield source records without writing to storage."""
        raise NotImplementedError


_REGISTRY: dict[str, type[Collector]] = {}


def register(name: str) -> Callable[[type[Collector]], type[Collector]]:
    """Class decorator: make a collector available as ``collector = "<name>"``."""

    def decorator(cls: type[Collector]) -> type[Collector]:
        if cls.stage not in STAGES:
            raise ValueError(f"{cls.__name__}.stage must be one of {STAGES}")
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            raise ValueError(f"Collector name {name!r} is already registered")
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_collector_class(name: str) -> type[Collector]:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"No collector is registered as {name!r}") from exc


def registered_collectors() -> dict[str, type[Collector]]:
    return dict(_REGISTRY)
