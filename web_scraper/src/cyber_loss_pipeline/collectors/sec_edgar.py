from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
from urllib.request import Request

from ..models import SourceRecord
from ..normalization import stable_identifier
from .base import STAGE_INCIDENT_LIST, Collector, SkipSource, register
from .http import fetch_bytes

_CYBER = re.compile(
    r"\b(?:cybersecurity|cyber\s+security|ransomware|data\s+breach|"
    r"security\s+incident|unauthorized\s+access|malware)\b",
    re.IGNORECASE,
)
_LOSS_CONTEXT = re.compile(
    r"\b(?:costs?|expenses?|loss(?:es)?|impact|impairment|settlement|fines?|"
    r"penalties|ransom|revenue|incurred|spend|response|restoration)\b",
    re.IGNORECASE,
)
_MONEY = re.compile(
    r"(?P<prefix>(?:(?:less than|under|up to|at least|more than|over|approximately|about)"
    r"\s+|[<>]=?))?"
    r"(?P<currency>USD\s*)?(?P<symbol>\$\s*)"
    r"(?P<value>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<scale>trillion|billion|million|thousand|bn|mm|m|k|b)?\b",
    re.IGNORECASE,
)
_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)
_FORMS = {"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A"}
_MAX_RESPONSE_BYTES = 30_000_000


@register("sec_edgar")
class SECCollector(Collector):
    """Collect recent cyber-related filings for explicitly configured SEC CIKs."""

    stage = STAGE_INCIDENT_LIST

    @classmethod
    def from_config(cls, source, context) -> "SECCollector":
        ciks = source.get("ciks", [])
        if not ciks:
            raise SkipSource("Set one or more issuer CIKs in config/sources.toml.")
        user_agent = context.environment.get("SEC_USER_AGENT", "")
        if not user_agent.strip() or "@" not in user_agent:
            raise SkipSource(
                "Set SEC_USER_AGENT to a descriptive name and contact email."
            )
        return cls(
            ciks,
            user_agent,
            lookback_days=int(source.get("lookback_days", 30)),
        )

    def __init__(
        self,
        ciks: list[str],
        user_agent: str,
        lookback_days: int = 30,
        timeout: int = 30,
        request_delay: float = 0.12,
    ) -> None:
        if not ciks:
            raise ValueError("At least one SEC CIK must be configured")
        if not user_agent.strip() or "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must include a descriptive name and contact email")
        self.ciks = [_normalize_cik(cik) for cik in ciks]
        self.user_agent = user_agent.strip()
        self.lookback_days = lookback_days
        self.timeout = timeout
        self.request_delay = request_delay

    def collect(self) -> Iterator[SourceRecord]:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=self.lookback_days)).isoformat()
        for cik in self.ciks:
            submissions = self._get_json(
                f"https://data.sec.gov/submissions/CIK{cik}.json"
            )
            facts = self._get_json(
                f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
            )
            facts_data = _company_features(submissions, facts, cik)
            recent = submissions.get("filings", {}).get("recent", {})
            column_names = (
                "accessionNumber",
                "filingDate",
                "reportDate",
                "form",
                "primaryDocument",
            )
            columns = {name: recent.get(name, []) for name in column_names}

            for index, form in enumerate(columns["form"]):
                filed = columns["filingDate"][index]
                if form not in _FORMS or not filed or filed < cutoff:
                    continue
                accession = columns["accessionNumber"][index]
                document = columns["primaryDocument"][index]
                if not accession or not document:
                    continue
                accession_path = accession.replace("-", "")
                filing_url = (
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                    f"{accession_path}/{document}"
                )
                html = self._get_text(filing_url)
                text = _VisibleText()
                text.feed(html)
                normalized = text.text()
                if not _CYBER.search(normalized):
                    continue

                losses = _extract_loss_candidates(normalized, filed)
                report_date = columns["reportDate"][index] or None
                event_date = (
                    report_date
                    if form.startswith("8-K") and _valid_iso_date(report_date)
                    else None
                )
                company_name = submissions.get("name")
                if not company_name:
                    raise ValueError(f"SEC submissions response for CIK {cik} has no company name")
                record_id = accession
                company_features = dict(facts_data)
                company_features.update(
                    {
                        "company_cik": cik,
                        "ticker": next(iter(submissions.get("tickers", [])), None),
                        "publicly_traded": True,
                        "as_of_date": filed,
                    }
                )
                yield SourceRecord.from_mapping(
                    {
                        "source_name": "sec_edgar",
                        "source_record_id": record_id,
                        "incident_key": (
                            stable_identifier("sec_edgar", cik, event_date)
                            if event_date
                            else None
                        ),
                        "source_url": filing_url,
                        "company_name": company_name,
                        "event": f"Cyber-related SEC filing ({form})",
                        "incident_type": "Cyber-related SEC filing",
                        "incident_date": event_date,
                        "published_date": filed,
                        "company_features": company_features,
                        "losses": losses,
                        "raw": {
                            "cik": cik,
                            "form": form,
                            "accession_number": accession,
                            "filing_date": filed,
                            "report_date": report_date,
                            "primary_document": document,
                        },
                    }
                )

    def _get_json(self, url: str) -> Mapping[str, object]:
        payload = self._get_bytes(url)
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"SEC returned invalid JSON for {url}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"SEC returned an unexpected JSON object for {url}")
        return value

    def _get_text(self, url: str) -> str:
        return self._get_bytes(url).decode("utf-8", errors="replace")

    def _get_bytes(self, url: str) -> bytes:
        time.sleep(self.request_delay)
        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "identity",
            },
        )
        return fetch_bytes(
            request,
            timeout=self.timeout,
            max_bytes=_MAX_RESPONSE_BYTES,
        )


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def _extract_loss_candidates(text: str, filed: str) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for cyber_match in _CYBER.finditer(text):
        start = max(0, cyber_match.start() - 450)
        end = min(len(text), cyber_match.end() + 450)
        window = text[start:end]
        for money_match in _MONEY.finditer(window):
            amount_start = start + money_match.start()
            snippet_start = max(0, amount_start - 180)
            snippet_end = min(len(text), start + money_match.end() + 180)
            snippet = text[snippet_start:snippet_end]
            if not _LOSS_CONTEXT.search(snippet):
                continue
            key = (amount_start, money_match.group("value"))
            if key in seen:
                continue
            seen.add(key)

            amount = Decimal(money_match.group("value").replace(",", ""))
            scale = (money_match.group("scale") or "").casefold()
            amount *= {
                "k": Decimal("1000"),
                "thousand": Decimal("1000"),
                "m": Decimal("1000000"),
                "mm": Decimal("1000000"),
                "million": Decimal("1000000"),
                "b": Decimal("1000000000"),
                "bn": Decimal("1000000000"),
                "billion": Decimal("1000000000"),
                "trillion": Decimal("1000000000000"),
            }.get(scale, Decimal("1"))
            prefix = (money_match.group("prefix") or "").casefold()
            operator = {
                "less than ": "<",
                "under ": "<",
                "up to ": "<=",
                "<": "<",
                "<=": "<=",
                "at least ": ">=",
                "more than ": ">",
                "over ": ">",
                ">": ">",
                ">=": ">=",
            }.get(prefix, "=")
            amount_type = (
                "Estimated"
                if re.search(r"\b(?:estimate|estimated|expect|approximately|about)\b", snippet, re.I)
                else "Actual / incurred"
            )
            cumulative = bool(
                re.search(r"\b(?:to date|cumulative|since the incident|total costs to date)\b", snippet, re.I)
            )
            term_match = _LOSS_CONTEXT.search(snippet)
            reported_term = (
                text[
                    max(snippet_start, snippet_start + term_match.start() - 35):
                    min(snippet_end, snippet_start + term_match.end() + 45)
                ].strip()
                if term_match
                else "Cyber-related monetary disclosure"
            )
            candidates.append(
                {
                    "amount": str(amount),
                    "currency": "USD",
                    "reported_term": reported_term,
                    "evidence_text": snippet,
                    "evidence_locator": (
                        f"Normalized filing text characters {snippet_start}-{snippet_end}"
                    ),
                    "observation_date": filed,
                    "period_end": filed,
                    "operator": operator,
                    "amount_type": amount_type,
                    "cumulative": cumulative,
                    "review_status": "needs_review",
                    "classification_confidence": "Low",
                }
            )
    return candidates


def _company_features(
    submissions: Mapping[str, object],
    facts: Mapping[str, object],
    cik: str,
) -> dict[str, object]:
    industry_code = _optional_text(submissions.get("sic"))
    industry = _optional_text(submissions.get("sicDescription"))
    facts_root = facts.get("facts", {})
    us_gaap = facts_root.get("us-gaap", {}) if isinstance(facts_root, dict) else {}
    dei = facts_root.get("dei", {}) if isinstance(facts_root, dict) else {}
    revenue_by_end: dict[str, tuple[str, Decimal]] = {}
    if isinstance(us_gaap, dict):
        for tag in _REVENUE_TAGS:
            fact = us_gaap.get(tag, {})
            units = fact.get("units", {}) if isinstance(fact, dict) else {}
            for entries in units.values():
                for entry in entries:
                    if (
                        entry.get("form") == "10-K"
                        and entry.get("fp") == "FY"
                        and entry.get("end")
                        and entry.get("val") is not None
                    ):
                        filed = entry.get("filed", "")
                        existing = revenue_by_end.get(entry["end"])
                        if existing is None or filed > existing[0]:
                            revenue_by_end[entry["end"]] = (
                                filed,
                                Decimal(str(entry["val"])),
                            )
            if revenue_by_end:
                break
        employee_fact = dei.get("EntityNumberOfEmployees", {})
        employee_units = employee_fact.get("units", {}) if isinstance(employee_fact, dict) else {}
        employee_entries = [
            entry
            for entries in employee_units.values()
            for entry in entries
            if entry.get("val") is not None and entry.get("end")
        ]
    else:
        employee_entries = []

    revenue_periods = sorted(revenue_by_end, reverse=True)
    latest_revenues = [revenue_by_end[period][1] for period in revenue_periods[:3]]
    employees = max(
        employee_entries,
        default=None,
        key=lambda item: (item["end"], item.get("filed", "")),
    )
    addresses = submissions.get("addresses", {})
    address = addresses.get("business", {}) if isinstance(addresses, dict) else {}
    city = _optional_text(address.get("city")) if isinstance(address, dict) else None
    state = (
        _optional_text(address.get("stateOrCountryDescription"))
        if isinstance(address, dict)
        else None
    )
    if not state and isinstance(address, dict):
        state = _optional_text(address.get("stateOrCountry"))
    location = ", ".join(part for part in (city, state) if part) or None
    return {
        "industry": industry,
        "industry_code": industry_code,
        "revenue": str(latest_revenues[0]) if latest_revenues else None,
        "revenue_currency": "USD" if latest_revenues else None,
        "revenue_fy_minus_1": str(latest_revenues[1]) if len(latest_revenues) > 1 else None,
        "revenue_fy_minus_2": str(latest_revenues[2]) if len(latest_revenues) > 2 else None,
        "revenue_period": revenue_periods[0] if revenue_periods else None,
        "employees": employees["val"] if employees else None,
        "location": location,
        "country": _optional_text(address.get("country"))
        if isinstance(address, dict)
        else None,
        "state_or_province": state,
        "source_url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
    }


def _normalize_cik(value: str) -> str:
    digits = re.sub(r"\D", "", str(value))
    if not digits or len(digits) > 10:
        raise ValueError(f"Invalid SEC CIK: {value!r}")
    return digits.zfill(10)


def _valid_iso_date(value: str | None) -> bool:
    if not value:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
