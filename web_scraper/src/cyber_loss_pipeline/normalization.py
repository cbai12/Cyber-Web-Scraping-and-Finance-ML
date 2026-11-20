from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def normalize_company_name(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    return text.rstrip(".,")


def normalize_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    for parser in (
        lambda item: date.fromisoformat(item),
        lambda item: datetime.strptime(item, "%m/%d/%Y").date(),
        lambda item: datetime.strptime(item, "%Y/%m/%d").date(),
    ):
        try:
            return parser(text).isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {value}")


def decimal_to_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def stable_identifier(*parts: str | None, length: int = 24) -> str:
    payload = "|".join((part or "").strip().casefold() for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
