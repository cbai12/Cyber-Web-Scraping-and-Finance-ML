import unittest

from cyber_loss_pipeline.normalization import (
    normalize_company_name,
    normalize_iso_date,
    stable_identifier,
)


class NormalizationTests(unittest.TestCase):
    def test_company_name_whitespace_and_terminal_punctuation(self):
        self.assertEqual(normalize_company_name("  Example   Company,  "), "Example Company")

    def test_supported_date_formats(self):
        self.assertEqual(normalize_iso_date("01/15/2025"), "2025-01-15")
        self.assertEqual(normalize_iso_date("2025-01-15"), "2025-01-15")

    def test_identifier_is_stable_and_case_insensitive(self):
        self.assertEqual(
            stable_identifier("SEC", "123"),
            stable_identifier("sec", "123"),
        )


if __name__ == "__main__":
    unittest.main()
