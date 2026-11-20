from __future__ import annotations

import argparse
import json
from pathlib import Path

from .export import export_all
from .pipeline import run_jsonl, run_weekly
from .review import export_review_sheet, import_review_decisions
from .storage import Store

# web_scraper/ (this file is web_scraper/src/cyber_loss_pipeline/cli.py).
# Defaults are anchored here so commands work from any working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cyber loss research data pipeline")
    parser.add_argument(
        "--mode",
        required=True,
        choices=("init", "backfill", "update", "export", "weekly", "review-export", "review-import"),
        help=(
            "weekly: collect enabled sources then export; export: re-export only; "
            "review-export / review-import: human review round trip"
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="JSONL file for backfill/update, or the filled-in review CSV for review-import",
    )
    parser.add_argument("--db", type=Path, default=DATA_DIR / "processed" / "cyber_loss.sqlite")
    parser.add_argument(
        "--sources",
        type=Path,
        default=PROJECT_ROOT / "config" / "sources.toml",
        help="Live source registry used by weekly mode",
    )
    parser.add_argument(
        "--taxonomy",
        type=Path,
        default=PROJECT_ROOT / "config" / "taxonomy_mapping.csv",
    )
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR / "processed")
    parser.add_argument(
        "--review-file",
        type=Path,
        default=DATA_DIR / "review" / "review_queue_to_decide.csv",
        help="Where review-export writes the sheet to fill in",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "init":
        with Store(args.db):
            pass
        _print({"database": str(args.db), "status": "initialized"})
        return 0

    if args.mode in {"backfill", "update"}:
        if args.input is None:
            raise SystemExit("--input is required for backfill and update modes")
        _print(run_jsonl(args.input, args.db, args.taxonomy))
        return 0

    if args.mode == "weekly":
        result = run_weekly(args.sources, args.db, args.taxonomy, args.output_dir)
        _print(result)
        # Exports were still written; a non-zero code tells schedulers something failed.
        return 1 if result["failed"] else 0

    if args.mode == "review-export":
        path, count = export_review_sheet(args.db, args.review_file)
        _print({"review_file": str(path), "open_items": count})
        return 0

    if args.mode == "review-import":
        if args.input is None:
            raise SystemExit("--input is required for review-import (the filled-in review CSV)")
        _print(import_review_decisions(args.db, args.input, args.taxonomy))
        return 0

    paths = export_all(args.db, args.output_dir, args.taxonomy)
    _print({"exported": [str(path) for path in paths]})
    return 0


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))
