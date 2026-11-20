from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ..models import SourceRecord
from .base import Collector


class JsonlCollector(Collector):
    """Read already-authorized source records from a local JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def collect(self) -> Iterator[SourceRecord]:
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    yield SourceRecord.from_mapping(payload)
                except Exception as exc:
                    raise ValueError(f"Invalid record at {self.path}:{line_number}: {exc}") from exc
