from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping


@dataclass(frozen=True)
class LossCandidate:
    amount: Decimal | None = None
    currency: str | None = None
    reported_term: str | None = None
    category: str | None = None
    subcategory: str | None = None
    evidence_text: str | None = None
    evidence_locator: str | None = None
    review_status: str = "needs_review"
    observation_date: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    operator: str = "="
    amount_type: str | None = None
    cumulative: bool = False
    classification_confidence: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LossCandidate":
        raw_amount = value.get("amount")
        amount = None if raw_amount in (None, "") else Decimal(str(raw_amount))
        currency = _optional_text(value.get("currency"))
        if currency:
            currency = currency.upper()
        return cls(
            amount=amount,
            currency=currency,
            reported_term=_optional_text(value.get("reported_term")),
            category=_optional_text(value.get("category")),
            subcategory=_optional_text(value.get("subcategory")),
            evidence_text=_optional_text(value.get("evidence_text")),
            evidence_locator=_optional_text(value.get("evidence_locator")),
            review_status=_optional_text(value.get("review_status")) or "needs_review",
            observation_date=_optional_text(value.get("observation_date")),
            period_start=_optional_text(value.get("period_start")),
            period_end=_optional_text(value.get("period_end")),
            operator=_optional_text(value.get("operator")) or "=",
            amount_type=_optional_text(value.get("amount_type")),
            cumulative=_as_bool(value.get("cumulative")),
            classification_confidence=_optional_text(
                value.get("classification_confidence")
            ),
        )


@dataclass(frozen=True)
class SourceRecord:
    source_name: str
    source_record_id: str
    source_url: str
    company_name: str
    incident_date: str | None = None
    published_date: str | None = None
    incident_type: str | None = None
    event: str | None = None
    incident_key: str | None = None
    individuals_affected: int | None = None
    losses: tuple[LossCandidate, ...] = field(default_factory=tuple)
    company_features: Mapping[str, Any] = field(default_factory=dict, compare=False)
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceRecord":
        required = ("source_name", "source_record_id", "source_url", "company_name")
        missing = [key for key in required if not _optional_text(value.get(key))]
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}")

        loss_values = value.get("losses") or []
        if not isinstance(loss_values, list):
            raise ValueError("losses must be a list")

        affected = value.get("individuals_affected")
        individuals_affected = None if affected in (None, "") else int(affected)

        return cls(
            source_name=str(value["source_name"]).strip(),
            source_record_id=str(value["source_record_id"]).strip(),
            source_url=str(value["source_url"]).strip(),
            company_name=str(value["company_name"]).strip(),
            incident_date=_optional_text(value.get("incident_date")),
            published_date=_optional_text(value.get("published_date")),
            incident_type=_optional_text(value.get("incident_type")),
            event=_optional_text(value.get("event")),
            incident_key=_optional_text(value.get("incident_key")),
            individuals_affected=individuals_affected,
            losses=tuple(LossCandidate.from_mapping(item) for item in loss_values),
            company_features=dict(value.get("company_features") or {}),
            raw=dict(value),
        )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().casefold() in {"1", "true", "yes", "y"}
