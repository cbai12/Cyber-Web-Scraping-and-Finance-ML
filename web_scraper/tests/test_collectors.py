import unittest
from datetime import date
from unittest.mock import patch

from cyber_loss_pipeline.collectors.california_ag import CaliforniaAGCollector
from cyber_loss_pipeline.collectors.http import fetch_bytes
from cyber_loss_pipeline.collectors.sec_edgar import (
    SECCollector,
    _VisibleText,
    _company_features,
    _extract_loss_candidates,
)


class CollectorTests(unittest.TestCase):
    def test_california_ag_collector_parses_public_csv(self):
        payload = (
            '"Organization Name","Date(s) of Breach  (if known)","Reported Date"\n'
            '"Example Corp","01/02/2025, 01/03/2025","02/01/2025"\n'
            '"","", "02/01/2025"\n'
        ).encode()
        collector = CaliforniaAGCollector(
            "https://example.invalid/data.csv",
            "https://example.invalid/list",
        )
        with patch(
            "cyber_loss_pipeline.collectors.california_ag.fetch_bytes",
            return_value=payload,
        ):
            records = list(collector.collect())

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].company_name, "Example Corp")
        self.assertEqual(records[0].incident_date, "2025-01-02")
        self.assertEqual(records[0].published_date, "02/01/2025")
        self.assertEqual(records[0].losses, ())
        self.assertEqual(collector.skipped_rows, 1)

    def test_http_fetch_retries_a_transient_timeout(self):
        request = type("Request", (), {"full_url": "https://example.invalid"})()
        with (
            patch(
                "cyber_loss_pipeline.collectors.http.urlopen",
                side_effect=[TimeoutError("temporary"), type("Response", (), {
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, *args: None,
                    "read": lambda self, size: b"ok",
                })()],
            ),
            patch("cyber_loss_pipeline.collectors.http.time.sleep"),
        ):
            self.assertEqual(fetch_bytes(request, retries=2), b"ok")

    def test_sec_extraction_preserves_bounds_and_review_status(self):
        text = (
            "The cybersecurity incident led to response costs of less than $10 million "
            "and the company incurred approximately $2.5 million in recovery expenses."
        )
        candidates = _extract_loss_candidates(text, "2025-02-01")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["amount"], "10000000")
        self.assertEqual(candidates[0]["operator"], "<")
        self.assertEqual(candidates[0]["review_status"], "needs_review")
        self.assertEqual(candidates[0]["classification_confidence"], "Low")
        self.assertEqual(candidates[1]["amount"], "2500000.0")

    def test_sec_plain_text_and_company_feature_extraction(self):
        parser = _VisibleText()
        parser.feed("<p>Security&nbsp;incident</p><p>Costs: $3 million</p>")
        self.assertIn("Security incident", parser.text())
        features = _company_features(
            {"sic": "6022", "sicDescription": "State Commercial Banks"},
            {"facts": {"us-gaap": {}}},
            "0000000001",
        )
        self.assertEqual(features["industry_code"], "6022")
        self.assertIsNone(features["revenue"])
        self.assertIn("CIK0000000001.json", features["source_url"])

    def test_sec_collector_builds_company_and_loss_records(self):
        filed = date.today().isoformat()
        submissions = {
            "name": "Example Public Company",
            "sic": "6022",
            "sicDescription": "State Commercial Banks",
            "tickers": ["EXMP"],
            "filings": {
                "recent": {
                    "accessionNumber": ["0000000001-25-000001"],
                    "filingDate": [filed],
                    "reportDate": [filed],
                    "form": ["8-K"],
                    "primaryDocument": ["filing.htm"],
                }
            },
        }
        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                {
                                    "form": "10-K",
                                    "fp": "FY",
                                    "end": "2024-12-31",
                                    "filed": "2025-02-01",
                                    "val": 125000000,
                                }
                            ]
                        }
                    }
                },
                "dei": {
                    "EntityNumberOfEmployees": {
                        "units": {
                            "employees": [
                                {
                                    "end": "2024-12-31",
                                    "filed": "2025-02-01",
                                    "val": 4000,
                                }
                            ]
                        }
                    }
                },
            }
        }
        collector = SECCollector(["1"], "Research Test test@example.invalid", request_delay=0)
        with (
            patch.object(collector, "_get_json", side_effect=[submissions, facts]),
            patch.object(
                collector,
                "_get_text",
                return_value=(
                    "<p>A cybersecurity incident resulted in response costs of "
                    "$3 million.</p>"
                ),
            ),
        ):
            records = list(collector.collect())

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.company_name, "Example Public Company")
        self.assertEqual(record.company_features["industry"], "State Commercial Banks")
        self.assertEqual(record.company_features["revenue"], "125000000")
        self.assertEqual(record.company_features["employees"], 4000)
        self.assertEqual(record.losses[0].amount, 3000000)
        self.assertEqual(record.losses[0].classification_confidence, "Low")
        self.assertTrue(record.losses[0].evidence_locator.startswith("Normalized filing text"))


if __name__ == "__main__":
    unittest.main()
