from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .models import LossCandidate, SourceRecord
from .normalization import (
    canonical_json,
    content_hash,
    decimal_to_text,
    normalize_company_name,
    normalize_iso_date,
    stable_identifier,
)
from .feature_schema import COMPANY_FEATURE_KEYS
from .taxonomy import Taxonomy, TaxonomyMatch


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    company_name TEXT NOT NULL,
    company_key TEXT NOT NULL DEFAULT '',
    event TEXT,
    incident_date TEXT,
    incident_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_sources (
    source_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    source_name TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    published_date TEXT,
    individuals_affected INTEGER,
    content_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    UNIQUE(source_name, source_record_id)
);

CREATE TABLE IF NOT EXISTS source_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES incident_sources(source_id),
    content_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(source_id, content_hash)
);

CREATE TABLE IF NOT EXISTS loss_observations (
    observation_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    source_id TEXT NOT NULL REFERENCES incident_sources(source_id),
    amount TEXT,
    currency TEXT,
    reported_term TEXT,
    category TEXT,
    subcategory TEXT,
    observation_date TEXT,
    period_start TEXT,
    period_end TEXT,
    operator TEXT NOT NULL DEFAULT '=',
    amount_type TEXT,
    cumulative INTEGER NOT NULL DEFAULT 0,
    current_snapshot INTEGER NOT NULL DEFAULT 1,
    classification_confidence TEXT,
    model_ready INTEGER NOT NULL DEFAULT 0,
    evidence_text TEXT,
    evidence_locator TEXT,
    review_status TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_features (
    feature_id TEXT PRIMARY KEY,
    company_key TEXT NOT NULL,
    company_name TEXT NOT NULL,
    company_cik TEXT,
    ticker TEXT,
    industry TEXT,
    industry_code TEXT,
    revenue TEXT,
    revenue_currency TEXT,
    revenue_period TEXT,
    employees INTEGER,
    features_json TEXT NOT NULL DEFAULT '{}',
    source_url TEXT NOT NULL,
    as_of_date TEXT,
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS update_history (
    update_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    sync_status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id TEXT NOT NULL REFERENCES loss_observations(observation_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    UNIQUE(observation_id, reason)
);

CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id TEXT NOT NULL REFERENCES loss_observations(observation_id),
    decision TEXT NOT NULL,
    category TEXT,
    subcategory TEXT,
    reviewer TEXT,
    note TEXT,
    decided_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self._migrate_schema()
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _migrate_schema(self) -> None:
        migrations = {
            "incidents": {
                "company_key": "TEXT NOT NULL DEFAULT ''",
                "event": "TEXT",
            },
            "loss_observations": {
                "observation_date": "TEXT",
                "period_start": "TEXT",
                "period_end": "TEXT",
                "operator": "TEXT NOT NULL DEFAULT '='",
                "amount_type": "TEXT",
                "cumulative": "INTEGER NOT NULL DEFAULT 0",
                "current_snapshot": "INTEGER NOT NULL DEFAULT 1",
                "classification_confidence": "TEXT",
                "model_ready": "INTEGER NOT NULL DEFAULT 0",
            },
            "company_features": {
                "features_json": "TEXT NOT NULL DEFAULT '{}'",
            },
        }
        for table, columns in migrations.items():
            existing = {
                row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            for name, declaration in columns.items():
                if name not in existing:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )
        for row in self.connection.execute(
            "SELECT incident_id, company_name FROM incidents WHERE company_key = ''"
        ).fetchall():
            self.connection.execute(
                "UPDATE incidents SET company_key = ? WHERE incident_id = ?",
                (
                    stable_identifier("company", normalize_company_name(row["company_name"])),
                    row["incident_id"],
                ),
            )

    def list_incident_companies(self) -> list[dict[str, object]]:
        """Companies on the incident list (Stage A output), one row per company.

        Stage C (enrichment) collectors use this so they only look up companies
        that actually had an incident.
        """
        rows = self.connection.execute(
            """
            SELECT i.company_key,
                   MIN(i.company_name) AS company_name,
                   COUNT(*) AS incident_count,
                   MIN(i.incident_date) AS first_incident_date,
                   MAX(i.incident_date) AS last_incident_date,
                   (
                       SELECT f.company_cik
                       FROM company_features f
                       WHERE f.company_key = i.company_key AND f.company_cik IS NOT NULL
                       LIMIT 1
                   ) AS company_cik
            FROM incidents i
            GROUP BY i.company_key
            ORDER BY company_name
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert(self, record: SourceRecord, taxonomy: Taxonomy) -> str:
        observed_at = utc_now()
        incident_id = stable_identifier(
            record.source_name, record.incident_key or record.source_record_id
        )
        source_id = stable_identifier("source", record.source_name, record.source_record_id)
        company_cik = record.company_features.get("company_cik")
        company_key = stable_identifier(
            "cik" if company_cik else "company",
            str(company_cik or normalize_company_name(record.company_name)),
        )
        raw_json = json.dumps(record.raw, sort_keys=True, ensure_ascii=False)
        record_hash = content_hash(record.raw)

        existing = self.connection.execute(
            "SELECT content_hash FROM incident_sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        sync_status = "new" if existing is None else (
            "unchanged" if existing["content_hash"] == record_hash else "updated"
        )

        company_name = normalize_company_name(record.company_name)
        incident_date = normalize_iso_date(record.incident_date)
        published_date = normalize_iso_date(record.published_date)

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO incidents (
                    incident_id, company_name, company_key, event, incident_date,
                    incident_type, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    company_name = excluded.company_name,
                    company_key = excluded.company_key,
                    event = excluded.event,
                    incident_date = excluded.incident_date,
                    incident_type = excluded.incident_type,
                    updated_at = excluded.updated_at
                """,
                (
                    incident_id,
                    company_name,
                    company_key,
                    record.event,
                    incident_date,
                    record.incident_type,
                    observed_at,
                    observed_at,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO incident_sources (
                    source_id, incident_id, source_name, source_record_id, source_url,
                    published_date, individuals_affected, content_hash, raw_json,
                    first_observed_at, last_observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_url = excluded.source_url,
                    published_date = excluded.published_date,
                    individuals_affected = excluded.individuals_affected,
                    content_hash = excluded.content_hash,
                    raw_json = excluded.raw_json,
                    last_observed_at = excluded.last_observed_at
                """,
                (
                    source_id,
                    incident_id,
                    record.source_name,
                    record.source_record_id,
                    record.source_url,
                    published_date,
                    record.individuals_affected,
                    record_hash,
                    raw_json,
                    observed_at,
                    observed_at,
                ),
            )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO source_versions (
                    source_id, content_hash, raw_json, observed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (source_id, record_hash, raw_json, observed_at),
            )
            for candidate in record.losses:
                self._insert_loss(
                    incident_id,
                    source_id,
                    candidate,
                    taxonomy,
                    observed_at,
                    published_date,
                )
            self._upsert_company_features(
                company_key,
                company_name,
                record.company_features,
                record.source_url,
                published_date,
                observed_at,
            )
            self.connection.execute(
                """
                INSERT INTO update_history (
                    source_name, source_record_id, sync_status, content_hash, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.source_name,
                    record.source_record_id,
                    sync_status,
                    record_hash,
                    observed_at,
                ),
            )
        return sync_status

    def _upsert_company_features(
        self,
        company_key: str,
        company_name: str,
        features: Mapping[str, object],
        source_url: str,
        as_of_date: str | None,
        retrieved_at: str,
    ) -> None:
        if not isinstance(features, dict):
            return
        relevant = (
            "company_cik",
            "ticker",
            "industry",
            "industry_code",
            "revenue",
            "revenue_currency",
            "revenue_period",
            "employees",
        )
        model_features = {
            key: features[key]
            for key in COMPANY_FEATURE_KEYS
            if key in features
        }
        values = {key: features.get(key) for key in relevant}
        if not any(value not in (None, "") for value in model_features.values()):
            return
        source_url = str(features.get("source_url") or source_url)
        features_json = canonical_json(model_features)
        feature_id = stable_identifier(
            company_key,
            source_url,
            str(values["revenue_period"] or ""),
            features_json,
            length=32,
        )
        employees = values["employees"]
        self.connection.execute(
            """
            INSERT OR IGNORE INTO company_features (
                feature_id, company_key, company_name, company_cik, ticker, industry,
                industry_code, revenue, revenue_currency, revenue_period, employees,
                features_json, source_url, as_of_date, retrieved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feature_id,
                company_key,
                company_name,
                values["company_cik"],
                values["ticker"],
                values["industry"],
                values["industry_code"],
                str(values["revenue"]) if values["revenue"] is not None else None,
                values["revenue_currency"],
                values["revenue_period"],
                int(employees) if employees not in (None, "") else None,
                features_json,
                source_url,
                features.get("as_of_date") or values["revenue_period"] or as_of_date,
                retrieved_at,
            ),
        )

    def _insert_loss(
        self,
        incident_id: str,
        source_id: str,
        candidate: LossCandidate,
        taxonomy: Taxonomy,
        observed_at: str,
        published_date: str | None,
    ) -> None:
        match = self._resolve_taxonomy(candidate, taxonomy)
        review_status = candidate.review_status
        if match.status == "approved_alias" and review_status == "needs_review":
            review_status = "auto_classified"
        elif match.status == "canonical" and review_status == "needs_review":
            review_status = "auto_classified"
        elif match.status == "needs_review":
            review_status = "needs_review"

        amount = decimal_to_text(candidate.amount)
        observation_date = (
            normalize_iso_date(candidate.observation_date)
            if candidate.observation_date
            else published_date or observed_at[:10]
        )
        period_start = normalize_iso_date(candidate.period_start)
        period_end = normalize_iso_date(candidate.period_end)
        model_ready = (
            review_status in {"confirmed", "auto_classified"}
            and match.category not in (None, "Unallocated Cyber Loss")
            and candidate.amount is not None
            and candidate.operator == "="
        )
        observation_id = stable_identifier(
            source_id,
            amount,
            candidate.currency,
            candidate.reported_term,
            candidate.evidence_text,
            candidate.evidence_locator,
            period_start,
            period_end,
            observation_date,
            candidate.operator,
            candidate.amount_type,
            str(candidate.cumulative),
            length=32,
        )
        current_snapshot = 1
        if candidate.cumulative:
            latest = self.connection.execute(
                """
                SELECT MAX(observation_date) AS latest_date
                FROM loss_observations
                WHERE incident_id = ?
                  AND category IS ?
                  AND reported_term IS ?
                  AND amount_type IS ?
                  AND cumulative = 1
                """,
                (
                    incident_id,
                    match.category,
                    candidate.reported_term,
                    candidate.amount_type,
                ),
            ).fetchone()["latest_date"]
            if latest and observation_date < latest:
                current_snapshot = 0
            if current_snapshot:
                self.connection.execute(
                    """
                    UPDATE loss_observations
                    SET current_snapshot = 0
                    WHERE incident_id = ?
                      AND category IS ?
                      AND reported_term IS ?
                      AND amount_type IS ?
                      AND cumulative = 1
                      AND observation_date <= ?
                    """,
                    (
                        incident_id,
                        match.category,
                        candidate.reported_term,
                        candidate.amount_type,
                        observation_date,
                    ),
                )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO loss_observations (
                observation_id, incident_id, source_id, amount, currency,
                reported_term, category, subcategory, observation_date, period_start,
                period_end, operator, amount_type, cumulative, current_snapshot,
                classification_confidence, model_ready, evidence_text, evidence_locator,
                review_status, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                incident_id,
                source_id,
                amount,
                candidate.currency,
                candidate.reported_term,
                match.category,
                match.subcategory,
                observation_date,
                period_start,
                period_end,
                candidate.operator,
                candidate.amount_type,
                int(candidate.cumulative),
                current_snapshot,
                candidate.classification_confidence
                or (
                    "High"
                    if match.status in {"canonical", "approved_alias"}
                    else "Low"
                ),
                int(model_ready),
                candidate.evidence_text,
                candidate.evidence_locator,
                review_status,
                observed_at,
            ),
        )
        if review_status == "needs_review":
            reason = match.notes or "Human review required"
            self.connection.execute(
                """
                INSERT OR IGNORE INTO review_queue (
                    observation_id, reason, status, created_at
                ) VALUES (?, ?, 'open', ?)
                """,
                (observation_id, reason, observed_at),
            )

    @staticmethod
    def _resolve_taxonomy(candidate: LossCandidate, taxonomy: Taxonomy) -> TaxonomyMatch:
        if candidate.classification_confidence == "Low":
            return TaxonomyMatch(
                "Unallocated Cyber Loss",
                None,
                "needs_review",
                "Low-confidence extraction requires human review",
            )
        if candidate.category and taxonomy.validate(candidate.category, candidate.subcategory):
            return TaxonomyMatch(candidate.category, candidate.subcategory, "canonical")
        match = taxonomy.match(candidate.reported_term)
        if match.status == "needs_review":
            return TaxonomyMatch(
                "Unallocated Cyber Loss",
                None,
                "needs_review",
                match.notes,
            )
        return match
