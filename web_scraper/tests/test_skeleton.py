"""Tests for the shared pipeline skeleton: registry, stages, fault tolerance,
review round trip, and working-directory-independent CLI defaults."""

import contextlib
import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cyber_loss_pipeline.cli import PROJECT_ROOT, build_parser, main
from cyber_loss_pipeline.collectors import (
    STAGE_ENRICHMENT,
    STAGE_INCIDENT_LIST,
    Collector,
    RunContext,
    SECCollector,
    SkipSource,
    get_collector_class,
    register,
    registered_collectors,
)
from cyber_loss_pipeline.models import SourceRecord
from cyber_loss_pipeline.pipeline import run_jsonl, run_live, run_weekly
from cyber_loss_pipeline.review import export_review_sheet, import_review_decisions

TAXONOMY = Path(__file__).parents[1] / "config" / "taxonomy_mapping.csv"


def _record(company: str, record_id: str, losses=None) -> SourceRecord:
    return SourceRecord.from_mapping(
        {
            "source_name": "fake_source",
            "source_record_id": record_id,
            "source_url": f"https://example.invalid/{record_id}",
            "company_name": company,
            "incident_date": "2025-01-15",
            "losses": losses or [],
        }
    )


@register("test_fake_incidents")
class FakeIncidentCollector(Collector):
    stage = STAGE_INCIDENT_LIST

    @classmethod
    def from_config(cls, source, context):
        return cls(source.get("companies", []))

    def __init__(self, companies):
        self.companies = companies

    def collect(self):
        for index, company in enumerate(self.companies):
            yield _record(company, f"incident-{index}")


@register("test_fake_failing")
class FakeFailingCollector(Collector):
    stage = STAGE_INCIDENT_LIST

    @classmethod
    def from_config(cls, source, context):
        return cls()

    def collect(self):
        yield _record("Partial Co", "partial-1")
        raise RuntimeError("site unreachable")


@register("test_fake_enrichment")
class FakeEnrichmentCollector(Collector):
    stage = STAGE_ENRICHMENT
    seen_companies: list[str] = []

    @classmethod
    def from_config(cls, source, context):
        cls.seen_companies = [row["company_name"] for row in context.incident_companies]
        return cls()

    def collect(self):
        return iter(())


class SkeletonTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = self.root / "test.sqlite"
        self.sources = self.root / "sources.toml"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_sources(self, blocks):
        lines = []
        for block in blocks:
            lines.append("[[sources]]")
            for key, value in block.items():
                lines.append(f"{key} = {json.dumps(value)}")
            lines.append("")
        self.sources.write_text("\n".join(lines), encoding="utf-8")

    # Registry ---------------------------------------------------------------

    def test_existing_collectors_are_registered(self):
        names = registered_collectors()
        self.assertIn("california_ag_csv", names)
        self.assertIn("sec_edgar", names)
        self.assertIs(get_collector_class("sec_edgar"), SECCollector)

    def test_unknown_collector_stops_before_collecting(self):
        self._write_sources([{"key": "bad", "enabled": True, "collector": "does_not_exist"}])
        with self.assertRaises(ValueError):
            run_live(self.sources, self.db, TAXONOMY, environment={})
        self.assertFalse(self.db.exists())

    def test_registering_a_bad_stage_is_rejected(self):
        with self.assertRaises(ValueError):

            @register("test_bad_stage")
            class BadStage(Collector):
                stage = "not_a_stage"

                def collect(self):
                    return iter(())

    # Fault tolerance and stages -------------------------------------------

    def test_failing_source_is_reported_and_others_still_run_and_export(self):
        self._write_sources(
            [
                {"key": "broken", "enabled": True, "collector": "test_fake_failing"},
                {"key": "good", "enabled": True, "collector": "test_fake_incidents", "companies": ["Good Co"]},
            ]
        )
        result = run_weekly(self.sources, self.db, TAXONOMY, self.root / "out", environment={})
        self.assertIn("site unreachable", result["failed"]["broken"])
        self.assertEqual(result["sources"]["broken"]["new"], 1)  # partial rows kept
        self.assertEqual(result["sources"]["good"]["new"], 1)
        self.assertTrue((self.root / "out" / "loss_observations.xlsx").exists())

    def test_enrichment_stage_receives_incident_company_list(self):
        # Enrichment listed first in the file but must run after the incident list.
        self._write_sources(
            [
                {"key": "enrich", "enabled": True, "collector": "test_fake_enrichment"},
                {"key": "list", "enabled": True, "collector": "test_fake_incidents", "companies": ["Beta", "Alpha"]},
            ]
        )
        run_live(self.sources, self.db, TAXONOMY, environment={})
        self.assertEqual(FakeEnrichmentCollector.seen_companies, ["Alpha", "Beta"])

    def test_sec_without_ciks_or_user_agent_is_skipped_not_failed(self):
        with self.assertRaises(SkipSource):
            SECCollector.from_config({"ciks": []}, RunContext(environment={}))
        with self.assertRaises(SkipSource):
            SECCollector.from_config({"ciks": ["1"]}, RunContext(environment={}))
        collector = SECCollector.from_config(
            {"ciks": ["1"]}, RunContext(environment={"SEC_USER_AGENT": "Name a@b.edu"})
        )
        self.assertEqual(collector.ciks, ["0000000001"])

    # Review round trip ----------------------------------------------------

    def _seed_review_item(self, amount="125000", term="Malicious Attack", operator="="):
        record = {
            "source_name": "test_fixture",
            "source_record_id": "record-001",
            "source_url": "https://example.invalid/record-001",
            "company_name": "Example Company",
            "losses": [
                {
                    "amount": amount,
                    "currency": "USD",
                    "reported_term": term,
                    "operator": operator,
                    "evidence_text": "Synthetic unit-test evidence only.",
                    "evidence_locator": "page 1",
                }
            ],
        }
        jsonl = self.root / "records.jsonl"
        jsonl.write_text(json.dumps(record) + "\n", encoding="utf-8")
        run_jsonl(jsonl, self.db, TAXONOMY)
        return jsonl

    def _fill_sheet(self, sheet, **values):
        with sheet.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
            fields = list(rows[0].keys())
        for row in rows:
            row.update(values)
        with sheet.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_review_approve_makes_observation_model_ready_and_survives_rerun(self):
        jsonl = self._seed_review_item()
        sheet, count = export_review_sheet(self.db, self.root / "review.csv")
        self.assertEqual(count, 1)
        self._fill_sheet(sheet, decision="approve", category="Malicious System Failure",
                         subcategory="Intentional Crash", reviewer="zoey")
        self.assertEqual(import_review_decisions(self.db, sheet, TAXONOMY)["approved"], 1)

        run_jsonl(jsonl, self.db, TAXONOMY)  # re-collecting must not undo the decision
        connection = sqlite3.connect(self.db)
        row = connection.execute(
            "SELECT category, review_status, model_ready FROM loss_observations"
        ).fetchone()
        open_items = connection.execute(
            "SELECT COUNT(*) FROM review_queue WHERE status = 'open'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(row, ("Malicious System Failure", "confirmed", 1))
        self.assertEqual(open_items, 0)
        self.assertEqual(export_review_sheet(self.db, self.root / "again.csv")[1], 0)

    def test_review_approved_bound_is_not_model_ready(self):
        self._seed_review_item(operator="<")
        sheet, _ = export_review_sheet(self.db, self.root / "review.csv")
        self._fill_sheet(sheet, decision="approve", category="Incident Response")
        import_review_decisions(self.db, sheet, TAXONOMY)
        connection = sqlite3.connect(self.db)
        self.assertEqual(connection.execute("SELECT model_ready FROM loss_observations").fetchone()[0], 0)
        connection.close()

    def test_review_reject_and_invalid_category(self):
        self._seed_review_item()
        sheet, _ = export_review_sheet(self.db, self.root / "review.csv")

        self._fill_sheet(sheet, decision="approve", category="Not A Category")
        with self.assertRaises(ValueError):
            import_review_decisions(self.db, sheet, TAXONOMY)
        connection = sqlite3.connect(self.db)
        self.assertEqual(
            connection.execute("SELECT review_status FROM loss_observations").fetchone()[0],
            "needs_review",
        )  # nothing applied when the file has an error
        connection.close()

        self._fill_sheet(sheet, decision="reject", category="")
        self.assertEqual(import_review_decisions(self.db, sheet, TAXONOMY)["rejected"], 1)
        connection = sqlite3.connect(self.db)
        self.assertEqual(
            connection.execute("SELECT review_status, model_ready FROM loss_observations").fetchone(),
            ("rejected", 0),
        )
        connection.close()

    def test_blank_decisions_are_left_open(self):
        self._seed_review_item()
        sheet, _ = export_review_sheet(self.db, self.root / "review.csv")
        self.assertEqual(
            import_review_decisions(self.db, sheet, TAXONOMY),
            {"approved": 0, "rejected": 0, "ignored": 1},
        )

    # CLI ------------------------------------------------------------------

    def test_cli_defaults_do_not_depend_on_working_directory(self):
        previous = os.getcwd()
        os.chdir(self.root)
        try:
            args = build_parser().parse_args(["--mode", "export"])
        finally:
            os.chdir(previous)
        self.assertEqual(args.sources, PROJECT_ROOT / "config" / "sources.toml")
        self.assertTrue(args.sources.exists())
        self.assertTrue(args.taxonomy.exists())
        self.assertTrue(str(args.db).startswith(str(PROJECT_ROOT)))

    def test_cli_weekly_returns_nonzero_when_a_source_fails(self):
        self._write_sources([{"key": "broken", "enabled": True, "collector": "test_fake_failing"}])
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "--mode", "weekly",
                    "--sources", str(self.sources),
                    "--db", str(self.db),
                    "--output-dir", str(self.root / "out"),
                ]
            )
        self.assertEqual(code, 1)
        self.assertTrue((self.root / "out" / "company_features.csv").exists())


if __name__ == "__main__":
    unittest.main()
