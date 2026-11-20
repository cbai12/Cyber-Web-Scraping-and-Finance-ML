"""Human review round trip for ambiguous loss observations.

1. ``export_review_sheet`` writes every open review item to a CSV with blank
   decision columns.
2. A person fills in ``decision`` (approve / reject) and, when approving,
   ``category`` and optional ``subcategory`` from config/taxonomy_mapping.csv.
3. ``import_review_decisions`` validates the whole file first, then applies it
   in one transaction. Rows with a blank decision are left open.

Approved observations become model-ready only when they also have an exact
amount (operator ``=``) and a category other than ``Unallocated Cyber Loss``.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .storage import Store
from .taxonomy import Taxonomy

UNALLOCATED = "Unallocated Cyber Loss"

REVIEW_SHEET_COLUMNS = (
    "observation_id",
    "reason",
    "company_name",
    "event",
    "reported_term",
    "amount",
    "currency",
    "operator",
    "amount_type",
    "current_category",
    "evidence_text",
    "source_url",
    "evidence_locator",
    # Filled in by the reviewer:
    "decision",
    "category",
    "subcategory",
    "reviewer",
    "note",
)
DECISIONS = {"approve", "reject"}


def export_review_sheet(database_path: str | Path, output_path: str | Path) -> tuple[Path, int]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with Store(database_path) as store:
        rows = store.connection.execute(
            """
            SELECT q.observation_id,
                   GROUP_CONCAT(q.reason, '; ') AS reason,
                   i.company_name, i.event, l.reported_term, l.amount, l.currency,
                   l.operator, l.amount_type,
                   COALESCE(l.category, ?) AS current_category,
                   l.evidence_text, s.source_url, l.evidence_locator
            FROM review_queue q
            JOIN loss_observations l USING (observation_id)
            JOIN incidents i ON i.incident_id = l.incident_id
            JOIN incident_sources s ON s.source_id = l.source_id
            WHERE q.status = 'open'
            GROUP BY q.observation_id
            ORDER BY i.company_name, l.observation_date, q.observation_id
            """,
            (UNALLOCATED,),
        ).fetchall()
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_SHEET_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({**dict(row), "decision": "", "category": "", "subcategory": "", "reviewer": "", "note": ""})
    return output, len(rows)


def import_review_decisions(
    database_path: str | Path,
    input_path: str | Path,
    taxonomy_path: str | Path,
) -> dict[str, int]:
    taxonomy = Taxonomy.from_csv(taxonomy_path)
    with Path(input_path).open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    with Store(database_path) as store:
        connection = store.connection
        planned, errors, ignored = _validate(connection, rows, taxonomy)
        if errors:
            raise ValueError("Review file not applied:\n" + "\n".join(errors))

        decided_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        counts = {"approved": 0, "rejected": 0, "ignored": ignored}
        with connection:
            for item in planned:
                _apply(connection, item, decided_at)
                counts["approved" if item["decision"] == "approve" else "rejected"] += 1
    return counts


def _validate(connection: sqlite3.Connection, rows, taxonomy: Taxonomy):
    planned: list[dict[str, object]] = []
    errors: list[str] = []
    ignored = 0
    for line_number, row in enumerate(rows, start=2):
        decision = (row.get("decision") or "").strip().casefold()
        if not decision:
            ignored += 1
            continue
        observation_id = (row.get("observation_id") or "").strip()
        where = f"line {line_number} ({observation_id or 'no observation_id'})"
        if decision not in DECISIONS:
            errors.append(f"{where}: decision must be 'approve' or 'reject', got {decision!r}")
            continue
        existing = connection.execute(
            "SELECT category, subcategory, amount, operator FROM loss_observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        if existing is None:
            errors.append(f"{where}: unknown observation_id")
            continue

        category = (row.get("category") or "").strip() or existing["category"] or UNALLOCATED
        subcategory = (row.get("subcategory") or "").strip() or None
        if decision == "approve" and category != UNALLOCATED:
            if category not in taxonomy.categories:
                errors.append(
                    f"{where}: unknown category {category!r}; use one of "
                    + ", ".join(taxonomy.categories + [UNALLOCATED])
                )
                continue
            if subcategory and not taxonomy.validate(category, subcategory):
                errors.append(f"{where}: {subcategory!r} is not a subcategory of {category!r}")
                continue
        planned.append(
            {
                "observation_id": observation_id,
                "decision": decision,
                "category": category,
                "subcategory": subcategory if category != UNALLOCATED else None,
                "model_ready": (
                    decision == "approve"
                    and category != UNALLOCATED
                    and existing["amount"] is not None
                    and existing["operator"] == "="
                ),
                "reviewer": (row.get("reviewer") or "").strip() or None,
                "note": (row.get("note") or "").strip() or None,
            }
        )
    return planned, errors, ignored


def _apply(connection: sqlite3.Connection, item: dict[str, object], decided_at: str) -> None:
    if item["decision"] == "approve":
        connection.execute(
            """
            UPDATE loss_observations
            SET category = ?, subcategory = ?, review_status = 'confirmed',
                classification_confidence = 'Reviewed', model_ready = ?
            WHERE observation_id = ?
            """,
            (item["category"], item["subcategory"], int(item["model_ready"]), item["observation_id"]),
        )
        queue_status = "approved"
    else:
        connection.execute(
            """
            UPDATE loss_observations
            SET review_status = 'rejected', model_ready = 0
            WHERE observation_id = ?
            """,
            (item["observation_id"],),
        )
        queue_status = "rejected"
    connection.execute(
        "UPDATE review_queue SET status = ? WHERE observation_id = ?",
        (queue_status, item["observation_id"]),
    )
    connection.execute(
        """
        INSERT INTO review_decisions (
            observation_id, decision, category, subcategory, reviewer, note, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item["observation_id"],
            item["decision"],
            item["category"],
            item["subcategory"],
            item["reviewer"],
            item["note"],
            decided_at,
        ),
    )
