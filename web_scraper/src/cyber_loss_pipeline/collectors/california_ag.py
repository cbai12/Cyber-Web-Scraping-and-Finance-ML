from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator
from urllib.request import Request

from ..models import SourceRecord
from ..normalization import stable_identifier
from .base import STAGE_INCIDENT_LIST, Collector, register
from .http import fetch_bytes


@register("california_ag_csv")
class CaliforniaAGCollector(Collector):
    """Collect incident candidates from the California AG's published CSV."""

    stage = STAGE_INCIDENT_LIST

    @classmethod
    def from_config(cls, source, context) -> "CaliforniaAGCollector":
        return cls(source["data_url"], source["landing_url"])

    def __init__(self, data_url: str, landing_url: str, timeout: int = 90) -> None:
        self.data_url = data_url
        self.landing_url = landing_url
        self.timeout = timeout
        self.skipped_rows = 0

    def collect(self) -> Iterator[SourceRecord]:
        self.skipped_rows = 0
        request = Request(
            self.data_url,
            headers={"User-Agent": "cyber-loss-pipeline/0.1 (research data collection)"},
        )
        payload = fetch_bytes(request, timeout=self.timeout).decode("utf-8-sig")

        reader = csv.DictReader(io.StringIO(payload))
        required = {"Organization Name", "Date(s) of Breach  (if known)", "Reported Date"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(
                "California AG CSV schema changed; expected columns: "
                + ", ".join(sorted(required))
            )

        for row in reader:
            company = _clean(row.get("Organization Name"))
            reported_date = _clean(row.get("Reported Date"))
            breach_dates = _clean(row.get("Date(s) of Breach  (if known)"))
            if not company:
                if any(_clean(value) for value in row.values()):
                    self.skipped_rows += 1
                continue

            incident_date = _first_date(breach_dates)
            source_record_id = stable_identifier(
                company, reported_date or "", breach_dates or "", length=32
            )
            yield SourceRecord.from_mapping(
                {
                    "source_name": "california_ag",
                    "source_record_id": source_record_id,
                    "incident_key": stable_identifier(
                        "california_ag", company, incident_date or reported_date or ""
                    ),
                    "source_url": self.data_url,
                    "company_name": company,
                    "event": "Data breach notice",
                    "incident_type": "Data breach notice",
                    "incident_date": incident_date,
                    "published_date": reported_date,
                    "losses": [],
                    "raw": {
                        "organization_name": company,
                        "breach_dates": breach_dates,
                        "reported_date": reported_date,
                        "landing_url": self.landing_url,
                    },
                }
            )


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text or None


def _first_date(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", value)
    if not match:
        return None
    from datetime import datetime

    return datetime.strptime(match.group(), "%m/%d/%Y").date().isoformat()
