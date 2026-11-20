import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from cyber_loss_pipeline.export import LOSS_OBSERVATION_COLUMNS, export_all
from cyber_loss_pipeline.pipeline import run_jsonl


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = self.root / "test.sqlite"
        self.input = self.root / "records.jsonl"
        self.taxonomy = Path(__file__).parents[1] / "config" / "taxonomy_mapping.csv"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_record(self, amount="125000", term="Forensic investigation costs"):
        record = {
            "source_name": "test_fixture",
            "source_record_id": "record-001",
            "source_url": "https://example.invalid/record-001",
            "company_name": "Example Company",
            "incident_date": "2025-01-15",
            "published_date": "2025-02-01",
            "incident_type": "Test incident",
            "individuals_affected": 1000,
            "losses": [
                {
                    "amount": amount,
                    "currency": "USD",
                    "reported_term": term,
                    "evidence_text": "Synthetic unit-test evidence only.",
                    "review_status": "needs_review",
                }
            ],
        }
        self.input.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def test_incremental_import_preserves_revisions(self):
        self._write_record()
        self.assertEqual(run_jsonl(self.input, self.db, self.taxonomy)["new"], 1)
        self.assertEqual(run_jsonl(self.input, self.db, self.taxonomy)["unchanged"], 1)

        self._write_record(amount="150000")
        self.assertEqual(run_jsonl(self.input, self.db, self.taxonomy)["updated"], 1)

        connection = sqlite3.connect(self.db)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_versions").fetchone()[0], 2)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM loss_observations").fetchone()[0], 2)
        connection.close()

    def test_ambiguous_term_enters_review_queue(self):
        self._write_record(term="Malicious Attack")
        run_jsonl(self.input, self.db, self.taxonomy)
        connection = sqlite3.connect(self.db)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0], 1)
        connection.close()

    def test_export_creates_wide_model_file(self):
        self._write_record()
        run_jsonl(self.input, self.db, self.taxonomy)
        output = self.root / "exports"
        export_all(self.db, output, self.taxonomy)
        with (output / "model_input_wide.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Incident Response"], "125000")

    def test_loss_observations_xlsx_has_fixed_audit_columns(self):
        self._write_record(term="Malicious Attack")
        run_jsonl(self.input, self.db, self.taxonomy)
        output = self.root / "exports"
        export_all(self.db, output, self.taxonomy)

        workbook = load_workbook(output / "loss_observations.xlsx", data_only=True)
        worksheet = workbook["Loss Observations"]
        self.assertEqual(tuple(cell.value for cell in worksheet[1]), LOSS_OBSERVATION_COLUMNS)
        self.assertEqual(worksheet.max_column, 21)
        self.assertEqual(worksheet["H2"].value, "Unallocated Cyber Loss")
        self.assertEqual(worksheet["P2"].value, "No")
        self.assertEqual(workbook.sheetnames, ["Loss Observations"])

    def test_latest_cumulative_disclosure_is_current_even_when_imported_first(self):
        records = []
        for record_id, amount, filed in (
            ("filing-newer", "200000", "2025-03-01"),
            ("filing-older", "100000", "2025-02-01"),
        ):
            records.append(
                {
                    "source_name": "test_fixture",
                    "source_record_id": record_id,
                    "incident_key": "same-incident",
                    "source_url": f"https://example.invalid/{record_id}",
                    "company_name": "Example Company",
                    "published_date": filed,
                    "losses": [
                        {
                            "amount": amount,
                            "currency": "USD",
                            "reported_term": "Business Interruption",
                            "evidence_text": f"Costs through {filed}: ${amount}.",
                            "cumulative": True,
                            "observation_date": filed,
                        }
                    ],
                }
            )
        self.input.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        run_jsonl(self.input, self.db, self.taxonomy)
        connection = sqlite3.connect(self.db)
        rows = connection.execute(
            "SELECT amount, current_snapshot FROM loss_observations ORDER BY amount"
        ).fetchall()
        connection.close()
        self.assertEqual(rows, [("100000", 0), ("200000", 1)])
        output = self.root / "exports"
        export_all(self.db, output, self.taxonomy)
        with (output / "model_input_wide.csv").open(newline="", encoding="utf-8") as handle:
            model_rows = list(csv.DictReader(handle))
        self.assertEqual(len(model_rows), 1)
        self.assertEqual(model_rows[0]["Business Interruption"], "200000")

    def test_company_features_export_separately_and_missing_amount_stays_blank(self):
        record = {
            "source_name": "test_fixture",
            "source_record_id": "record-with-features",
            "source_url": "https://example.invalid/record-with-features",
            "company_name": "Example Company",
            "company_features": {
                "industry": "Software",
                "industry_code": "7372",
                "revenue": "1500000000",
                "revenue_currency": "USD",
                "revenue_period": "2024-12-31",
                "employees": 1200,
                "mfa_required_for": "All users",
            },
            "losses": [
                {
                    "reported_term": "Forensic investigation costs",
                    "evidence_text": "The costs were not quantified.",
                    "review_status": "needs_review",
                }
            ],
        }
        self.input.write_text(json.dumps(record) + "\n", encoding="utf-8")
        run_jsonl(self.input, self.db, self.taxonomy)
        output = self.root / "exports"
        export_all(self.db, output, self.taxonomy)

        workbook = load_workbook(output / "loss_observations.xlsx", data_only=True)
        observations = workbook["Loss Observations"]
        self.assertEqual(workbook.sheetnames, ["Loss Observations"])
        self.assertIsNone(observations["J2"].value)
        with (output / "company_features.csv").open(newline="", encoding="utf-8") as handle:
            features = list(csv.DictReader(handle))
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]["Industry"], "Software")
        self.assertEqual(features[0]["Revenue (Latest FY)"], "1500000000")
        self.assertEqual(features[0]["MFA Required For"], "All users")
        self.assertEqual(features[0]["Breached in Past 6 Months"], "")
        with (output / "model_input_wide.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["industry"], "Software")
        self.assertEqual(rows[0]["mfa_required_for"], "All users")
        self.assertEqual(rows[0]["Incident Response"], "")


if __name__ == "__main__":
    unittest.main()
