from __future__ import annotations

import os
import tomllib
from collections import Counter
from pathlib import Path

from .collectors import (
    STAGE_ENRICHMENT,
    STAGE_INCIDENT_LIST,
    JsonlCollector,
    RunContext,
    SkipSource,
    get_collector_class,
)
from .export import export_all
from .storage import Store
from .taxonomy import Taxonomy


def run_jsonl(
    input_path: str | Path,
    database_path: str | Path,
    taxonomy_path: str | Path,
) -> dict[str, int]:
    taxonomy = Taxonomy.from_csv(taxonomy_path)
    counts: Counter[str] = Counter()
    with Store(database_path) as store:
        for record in JsonlCollector(input_path).collect():
            counts[store.upsert(record, taxonomy)] += 1
    return {status: counts.get(status, 0) for status in ("new", "updated", "unchanged")}


def run_live(
    sources_path: str | Path,
    database_path: str | Path,
    taxonomy_path: str | Path,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    """Run enabled sources: Stage A (incident lists) first, then Stage C (enrichment).

    A source that fails is recorded under ``failed`` and the remaining sources
    still run. A source that is enabled but not configured is recorded under
    ``skipped``. An enabled source naming an unknown collector is a
    configuration error and stops the run before anything is collected.
    """
    sources_config = tomllib.loads(Path(sources_path).read_text(encoding="utf-8"))
    taxonomy = Taxonomy.from_csv(taxonomy_path)
    context = RunContext(environment=dict(os.environ if environment is None else environment))

    enabled = [source for source in sources_config.get("sources", []) if source.get("enabled", False)]
    planned = []
    for source in enabled:
        collector_name = source.get("collector")
        if not collector_name:
            raise ValueError(f"Enabled source {source.get('key')!r} has no collector")
        planned.append((source, get_collector_class(collector_name)))

    source_counts: dict[str, dict[str, int]] = {}
    skipped: dict[str, str] = {}
    failed: dict[str, str] = {}

    with Store(database_path) as store:
        for stage in (STAGE_INCIDENT_LIST, STAGE_ENRICHMENT):
            if stage == STAGE_ENRICHMENT:
                context.incident_companies = store.list_incident_companies()
            for source, collector_class in planned:
                if collector_class.stage != stage:
                    continue
                key = source["key"]
                try:
                    collector = collector_class.from_config(source, context)
                except SkipSource as exc:
                    skipped[key] = str(exc)
                    continue

                counts: Counter[str] = Counter()
                try:
                    for record in collector.collect():
                        counts[store.upsert(record, taxonomy)] += 1
                except Exception as exc:  # keep other sources running
                    failed[key] = f"{type(exc).__name__}: {exc}"
                source_result = {
                    status: counts.get(status, 0)
                    for status in ("new", "updated", "unchanged")
                }
                if hasattr(collector, "skipped_rows"):
                    source_result["skipped_rows"] = collector.skipped_rows
                source_counts[key] = source_result

    return {"sources": source_counts, "skipped": skipped, "failed": failed}


def run_weekly(
    sources_path: str | Path,
    database_path: str | Path,
    taxonomy_path: str | Path,
    output_dir: str | Path,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    """Collect, then always export, even if some sources failed."""
    collection = run_live(sources_path, database_path, taxonomy_path, environment)
    exported = export_all(database_path, output_dir, taxonomy_path)
    return {
        **collection,
        "exported": [str(path) for path in exported],
    }
