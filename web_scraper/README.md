# Cyber Loss Intelligence Pipeline

This package collects public cyber-incident notices and SEC filings, preserves source
versions and monetary evidence, and exports an auditable research workbook. The
database is the source of truth; exports can be regenerated at any time.

## What the pipeline does

- Fetches the California Attorney General's public breach-list CSV.
- Fetches SEC submissions and Company Facts for explicitly configured CIKs, then
  screens recent 8-K, 10-Q, and 10-K filings for cyber-related financial evidence.
- Preserves raw source records and versions in SQLite, and avoids duplicate
  observations when the same filings are collected again.
- Keeps SEC text-extraction candidates in `Unallocated Cyber Loss` for review.
  Heuristically extracted amounts are not model-ready.
- Tracks cumulative observations as history and marks only the latest dated
  observation per incident, target field, observed term, and amount type as current.
- Exports the 21-column `Loss Observations` workbook (one sheet) and a separate
  `company_features.csv` for X variables from the supplied report: location, industry, multi-year
  revenue, employees, breach history, exposed record counts, regulatory exposure,
  insurance coverages, security assessments, and controls. SEC SIC, revenue, employee
  count, CIK, ticker, and address are populated when available; other fields remain
  blank until a source record actually reports them.
- Leaves missing values blank. A missing disclosure is never treated as zero.

The pipeline does not yet resolve one incident reported by multiple sources, scrape
or redistribute sources whose access terms have not been reviewed, or infer a
company's industry/revenue when SEC facts are unavailable. California AG records
are incident candidates and do not by themselves provide monetary loss observations.
The SEC collector is scoped to the CIKs configured below; it does not crawl every
public company.

## Setup

Run these commands from the `web_scraper` directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Weekly live run

Set at least one SEC CIK in `config/sources.toml` to enable SEC collection:

```toml
[[sources]]
key = "sec_edgar"
# ...
enabled = true
ciks = ["0000731766", "0000789570"]
lookback_days = 30
```

The sample CIKs are UnitedHealth Group and MGM Resorts from the reference workbook;
replace them with the companies in your intended training population. SEC requires
a descriptive User-Agent containing a real contact email:

```bash
export SEC_USER_AGENT="Your Name your.real.email@example.edu"
python -m cyber_loss_pipeline --mode weekly
```

`weekly` runs enabled sources in two stages (incident-list collectors first, then
enrichment collectors) and always regenerates exports under `data/processed`.
A source that is enabled but not configured (for example, SEC without CIKs) is
reported under `skipped`. A source that errors is reported under `failed`, the
other sources still run, exports are still written, and the command exits with
code 1 so a scheduler can flag the failure.

Commands can be run from any directory: default paths are anchored to the
`web_scraper/` folder.

Schedule the command with the host's scheduler (for example, cron every Monday at
03:00 UTC). Keep the database and output directory on persistent storage, and
provide `SEC_USER_AGENT` in the scheduler environment:

```cron
0 3 * * 1 cd /path/to/web_scraper && SEC_USER_AGENT="Your Name your.real.email@example.edu" /path/to/web_scraper/.venv/bin/python -m cyber_loss_pipeline --mode weekly
```

The 30-day SEC lookback intentionally overlaps weekly runs. Stable source IDs and
content hashes make repeated runs idempotent while retaining revised disclosures.

## Other commands

Initialize an empty database:

```bash
python -m cyber_loss_pipeline --mode init
```

Import normalized, authorized JSONL records:

```bash
python -m cyber_loss_pipeline \
  --mode backfill \
  --input path/to/authorized_source_records.jsonl
```

Re-export without collecting:

```bash
python -m cyber_loss_pipeline --mode export
```

Human review of ambiguous observations:

```bash
# 1. Write open review items to data/review/review_queue_to_decide.csv
python -m cyber_loss_pipeline --mode review-export
# 2. Fill in decision (approve/reject), category, subcategory, reviewer, note
# 3. Apply the decisions (the whole file is validated before anything changes)
python -m cyber_loss_pipeline --mode review-import --input data/review/review_queue_to_decide.csv
```

An approved observation becomes model-ready only if it has an exact amount
(operator `=`) and a category other than `Unallocated Cyber Loss`. Decisions are
kept when sources are collected again and are logged in `review_decisions`.

Default database: `data/processed/cyber_loss.sqlite`

Default taxonomy: `config/taxonomy_mapping.csv`

## Output files

- `loss_observations.xlsx`: a single `Loss Observations` sheet, one row per
  monetary evidence observation and exactly 21 fields; includes the source URL,
  evidence locator, raw observed term, amount bound, cumulative flag, snapshot
  status, and review/model-readiness flags.
- `company_features.csv`: fixed X-variable fields aligned to the supplied report,
  one row per company. Public SEC features are populated where available;
  self-reported and control fields stay blank unless a source explicitly supplies
  them. Contact emails and phone numbers are not included as model features.
- `incidents.csv`: incident candidates, including notices without a disclosed loss.
- `incident_sources.csv`: source IDs, source URLs, content hashes, and fetch times.
- `loss_observations.csv`: machine-readable copy of the full observation table.
- `review_queue.csv`: ambiguous or low-confidence observations requiring review.
- `model_input_wide.csv`: one incident per row, X firmographics plus approved
  current Y loss totals. `needs_review`, bounds, and older cumulative snapshots are
  excluded from model-ready totals.

The fixed `Loss Observations` column order is:

`Observation ID`, `Incident ID`, `Company`, `Event`, `Observation Date`,
`Period Start`, `Period End`, `Target Field`, `Observed Term`, `Amount`, `Currency`,
`Operator`, `Amount Type`, `Cumulative?`, `Current Snapshot?`, `Model Ready?`,
`Classification Confidence`, `Evidence Summary`, `Source URL`, `Source Location`,
`Retrieved At`.

## JSONL input contract

Each line is one source record. A source record can contain zero or more loss
observations and optional company features:

```json
{
  "source_name": "source_key",
  "source_record_id": "stable-record-id",
  "incident_key": "stable-incident-id",
  "source_url": "https://example.org/record/stable-record-id",
  "company_name": "Company name as reported",
  "event": "Named incident",
  "incident_date": "2025-01-15",
  "published_date": "2025-02-01",
  "company_features": {
    "industry": "Industry as reported",
    "revenue": "125000000",
    "revenue_currency": "USD",
    "revenue_period": "2024-12-31"
  },
  "losses": [
    {
      "amount": "125000.00",
      "currency": "USD",
      "reported_term": "Forensic investigation costs",
      "operator": "=",
      "amount_type": "Actual / incurred",
      "cumulative": false,
      "evidence_text": "Short source-backed evidence excerpt",
      "evidence_locator": "page 4",
      "review_status": "needs_review"
    }
  ]
}
```

Use `null` or omit unknown values. Do not encode missing monetary amounts as zero.
An observation with no confident taxonomy match is exported as `Unallocated Cyber
Loss` and added to the review queue. Only confirmed or approved exact mappings with
an exact amount can enter model-ready totals.

The JSONL `company_features` object can supply any of the `company_features.csv` fields
using their snake_case column keys. Unsupported keys are ignored rather than
silently treated as model variables.

## Source registry status

The California AG CSV and the SEC CIK-based collector are enabled. HHS OCR, Privacy
Rights Clearinghouse, VCDB, ransomware feeds, European DPAs, CISA, and Wikipedia
remain disabled because their schemas, access terms, redistribution permissions,
or tested adapters are not yet cleared. An enabled source without a collector is an
error rather than silently falling back to an empty result.

## Relationship to the existing model

The legacy prediction code reads aggregate files such as `economic_loss.csv`,
`response_costs.csv`, `litigated_cases.csv`, and `fines_penalties.csv`. This pipeline
does not overwrite those files. See [model integration](docs/model_integration.md)
for the separation and remaining decisions.
