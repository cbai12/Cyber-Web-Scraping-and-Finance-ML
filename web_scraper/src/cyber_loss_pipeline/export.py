from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from .taxonomy import Taxonomy
from .feature_schema import COMPANY_FEATURE_COLUMNS, COMPANY_FEATURE_KEYS

LOSS_OBSERVATION_COLUMNS = (
    "Observation ID",
    "Incident ID",
    "Company",
    "Event",
    "Observation Date",
    "Period Start",
    "Period End",
    "Target Field",
    "Observed Term",
    "Amount",
    "Currency",
    "Operator",
    "Amount Type",
    "Cumulative?",
    "Current Snapshot?",
    "Model Ready?",
    "Classification Confidence",
    "Evidence Summary",
    "Source URL",
    "Source Location",
    "Retrieved At",
)


def export_all(
    database_path: str | Path,
    output_dir: str | Path,
    taxonomy_path: str | Path,
) -> list[Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    exported = [
        _export_query(connection, output / "incidents.csv", "SELECT * FROM incidents ORDER BY incident_id"),
        _export_query(
            connection,
            output / "incident_sources.csv",
            """
            SELECT source_id, incident_id, source_name, source_record_id, source_url,
                   published_date, individuals_affected, content_hash,
                   first_observed_at, last_observed_at
            FROM incident_sources
            ORDER BY source_name, source_record_id
            """,
        ),
        _export_query(
            connection,
            output / "loss_observations.csv",
            "SELECT * FROM loss_observations ORDER BY incident_id, observation_id",
        ),
        _export_query(
            connection,
            output / "review_queue.csv",
            """
            SELECT q.review_id, q.observation_id, q.reason, q.status, q.created_at,
                   l.incident_id, l.reported_term, l.amount, l.currency,
                   l.category, l.subcategory, l.evidence_text, l.evidence_locator
            FROM review_queue q
            JOIN loss_observations l USING (observation_id)
            ORDER BY q.review_id
            """,
        ),
    ]
    exported.append(
        _export_loss_observations_xlsx(connection, output / "loss_observations.xlsx")
    )
    exported.append(_export_company_features_csv(connection, output / "company_features.csv"))
    taxonomy = Taxonomy.from_csv(taxonomy_path)
    exported.append(_export_model_wide(connection, output / "model_input_wide.csv", taxonomy))
    connection.close()
    return exported


def _export_query(connection: sqlite3.Connection, path: Path, query: str) -> Path:
    rows = connection.execute(query).fetchall()
    headers = [item[0] for item in connection.execute(query).description]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows([list(row) for row in rows])
    return path


def _export_loss_observations_xlsx(connection: sqlite3.Connection, path: Path) -> Path:
    rows = connection.execute(
        """
        SELECT l.observation_id, l.incident_id, i.company_name, i.event,
               l.observation_date, l.period_start, l.period_end,
               COALESCE(l.category, 'Unallocated Cyber Loss') AS target_field,
               l.reported_term, l.amount, l.currency, l.operator, l.amount_type,
               l.cumulative, l.current_snapshot, l.model_ready,
               l.classification_confidence, l.evidence_text, s.source_url,
               l.evidence_locator, l.observed_at
        FROM loss_observations l
        JOIN incidents i USING (incident_id)
        JOIN incident_sources s USING (source_id)
        ORDER BY i.company_name, l.observation_date, l.observation_id
        """
    ).fetchall()
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Loss Observations"
    worksheet.append(LOSS_OBSERVATION_COLUMNS)
    for row in rows:
        amount = Decimal(row["amount"]) if row["amount"] is not None else None
        worksheet.append(
            [
                row["observation_id"],
                row["incident_id"],
                row["company_name"],
                row["event"],
                row["observation_date"],
                row["period_start"],
                row["period_end"],
                row["target_field"],
                row["reported_term"],
                amount,
                row["currency"],
                row["operator"],
                row["amount_type"],
                _yes_no(row["cumulative"]),
                _yes_no(row["current_snapshot"]),
                _yes_no(row["model_ready"]),
                row["classification_confidence"],
                row["evidence_text"],
                row["source_url"],
                row["evidence_locator"],
                row["observed_at"],
            ]
        )
    _style_worksheet(worksheet)
    workbook.save(path)
    return path


def _export_company_features_csv(connection: sqlite3.Connection, path: Path) -> Path:
    """One row per company: the latest feature snapshot (X variables)."""
    headers = (
        "Company",
        *(label for label, _ in COMPANY_FEATURE_COLUMNS),
        "Source URL",
        "As Of Date",
        "Retrieved At",
    )
    feature_rows = connection.execute(
        """
        SELECT company_name, company_cik, ticker, industry, industry_code, revenue,
               revenue_currency, revenue_period, employees, source_url, as_of_date,
               retrieved_at, features_json
        FROM (
            SELECT company_features.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY company_key
                       ORDER BY as_of_date DESC, retrieved_at DESC
                   ) AS row_number
            FROM company_features
        )
        WHERE row_number = 1
        ORDER BY company_name
        """
    ).fetchall()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in feature_rows:
            values = json.loads(row["features_json"])
            for key in (
                "company_cik",
                "ticker",
                "industry",
                "industry_code",
                "revenue",
                "revenue_currency",
                "revenue_period",
                "employees",
            ):
                if values.get(key) in (None, ""):
                    values[key] = row[key]
            writer.writerow(
                [
                    row["company_name"],
                    *[_csv_feature_value(values.get(key)) for key in COMPANY_FEATURE_KEYS],
                    row["source_url"],
                    row["as_of_date"],
                    row["retrieved_at"],
                ]
            )
    return path


def _style_worksheet(worksheet) -> None:
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")


def _yes_no(value: int | bool | None) -> str:
    return "Yes" if value else "No"


def _csv_feature_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return _yes_no(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _export_model_wide(
    connection: sqlite3.Connection,
    path: Path,
    taxonomy: Taxonomy,
) -> Path:
    categories = taxonomy.categories
    incident_rows = connection.execute(
        """
        SELECT i.incident_id, i.company_name, i.incident_date, i.event,
               s.source_name, s.source_record_id, i.company_key
        FROM incidents i
        LEFT JOIN incident_sources s
          ON s.source_id = (
              SELECT source_id
              FROM incident_sources
              WHERE incident_id = i.incident_id
              ORDER BY published_date DESC, source_id DESC
              LIMIT 1
          )
        ORDER BY i.incident_id
        """
    ).fetchall()
    features_by_company = {
        row["company_key"]: row
        for row in connection.execute(
            """
            SELECT company_key, industry, industry_code, revenue, revenue_currency,
                   revenue_period, employees, features_json
            FROM (
                SELECT company_features.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY company_key
                           ORDER BY as_of_date DESC, retrieved_at DESC
                       ) AS row_number
                FROM company_features
            )
            WHERE row_number = 1
            """
        )
    }
    totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for row in connection.execute(
        """
        SELECT incident_id, category, amount
        FROM loss_observations
        WHERE model_ready = 1 AND current_snapshot = 1
          AND amount IS NOT NULL
          AND category IS NOT NULL
        """
    ):
        totals[row["incident_id"]][row["category"]] += Decimal(row["amount"])

    headers = [
        "incident_id",
        "company_name",
        "company_key",
        "industry",
        "industry_code",
        "revenue",
        "revenue_currency",
        "revenue_period",
        "employees",
        "event",
        "incident_date",
        "source_name",
        "source_record_id",
        *[
            key
            for key in COMPANY_FEATURE_KEYS
            if key
            not in {
                "industry",
                "industry_code",
                "revenue",
                "revenue_currency",
                "revenue_period",
                "employees",
            }
        ],
        *categories,
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for incident in incident_rows:
            output_row = dict(incident)
            features = features_by_company.get(incident["company_key"])
            if features:
                feature_values = json.loads(features["features_json"])
                for field in COMPANY_FEATURE_KEYS:
                    if field in output_row:
                        continue
                    output_row[field] = _csv_feature_value(feature_values.get(field))
            else:
                for field in COMPANY_FEATURE_KEYS:
                    if field not in output_row:
                        output_row[field] = ""
            for category in categories:
                amount = totals[incident["incident_id"]].get(category)
                output_row[category] = "" if amount is None else format(amount, "f")
            writer.writerow(output_row)
    return path
