from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TaxonomyMatch:
    category: str | None
    subcategory: str | None
    status: str
    notes: str | None = None


class Taxonomy:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._by_source_term: dict[str, TaxonomyMatch] = {}
        self._canonical_pairs: set[tuple[str, str | None]] = set()

        for row in rows:
            source_term = row["source_term"].strip()
            category = row["target_category"].strip() or None
            subcategory = row["target_subcategory"].strip() or None
            status = row["mapping_status"].strip()
            match = TaxonomyMatch(category, subcategory, status, row.get("notes") or None)
            self._by_source_term[source_term.casefold()] = match
            if status == "canonical" and category:
                self._canonical_pairs.add((category, subcategory))

    @classmethod
    def from_csv(cls, path: str | Path) -> "Taxonomy":
        with Path(path).open(newline="", encoding="utf-8-sig") as handle:
            return cls(list(csv.DictReader(handle)))

    @property
    def categories(self) -> list[str]:
        return sorted({category for category, _ in self._canonical_pairs})

    def match(self, reported_term: str | None) -> TaxonomyMatch:
        if not reported_term:
            return TaxonomyMatch(
                "Unallocated Cyber Loss",
                None,
                "needs_review",
                "Missing reported loss term",
            )
        return self._by_source_term.get(
            reported_term.strip().casefold(),
            TaxonomyMatch(
                "Unallocated Cyber Loss",
                None,
                "needs_review",
                "No configured taxonomy mapping",
            ),
        )

    def validate(self, category: str, subcategory: str | None) -> bool:
        return (category, subcategory) in self._canonical_pairs
