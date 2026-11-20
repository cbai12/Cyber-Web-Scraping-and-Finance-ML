# Model integration boundary

## Current model entry point

The existing model loads four aggregate loss files:

- `economic_loss.csv`
- `response_costs.csv`
- `litigated_cases.csv`
- `fines_penalties.csv`

It uses their totals to create broad loss proportions for Incident Response Costs, Litigation Costs, Privacy Liability Coverage, and Business Interruption Loss. The current code also multiplies the stored loss amounts by 1,000 during loading.

The repository currently stores these files under `Data/`, while the inspected loader refers to `./data/`. This case and path mismatch should be resolved separately before treating the model as reproducible across macOS and Linux.

## Why the scraper does not overwrite those files

Professor Arora's target taxonomy is different from the legacy case-type and cost-type aggregation. A direct overwrite would create at least three risks:

1. Incorrect category mapping.
2. Double counting when a disclosed total overlaps component costs.
3. Mixing incident-level observations with already aggregated legacy totals.

## Proposed data flow

```text
Authorized sources
      |
      v
Source records and versions
      |
      v
Loss observations with evidence
      |
      +----> Review queue for ambiguous mappings
      |
      v
Research exports
      |
      +----> loss_observations.xlsx (one sheet, 21-field audit trail)
      |
      +----> company_features.csv (company-level X variables)
      |
      +----> model_input_wide.csv (approved X/Y training candidates)
      |
      v
Approved aggregation adapter
      |
      v
Versioned model calibration inputs
```

The aggregation adapter should be added only after the professor or research team approves:

- the mapping from target taxonomy to model outputs;
- treatment of settlements, fines, demands, payments, and insurance recoveries;
- currency and inflation normalization;
- deduplication rules for the same incident across sources;
- whether the old and new datasets should be pooled or modeled separately.

Until then, the existing `Data/` files remain unchanged.

## X/Y data contract

`loss_observations.xlsx` keeps the supplied 21-column audit contract on its only
sheet, `Loss Observations`. Company-level X variables live in a separate
`company_features.csv` so the evidence table does not gain columns or repeat
company metadata inconsistently. It reserves fields from the supplied assessment
for company/location, multi-year revenue, breach history, exposed data counts,
regulatory applicability, current coverage, and security controls. The SEC collector
supplies SIC industry, annual revenue, employee count, address, CIK, and ticker when
available. Other variables remain blank unless directly provided by an authorized
source; email addresses and phone numbers are excluded. Missing values stay blank.

The SEC text scanner is a discovery aid, not a financial classifier. Its monetary
matches are low-confidence, mapped to `Unallocated Cyber Loss`, and excluded from
model-ready totals pending human review. California AG notices populate incident
candidates but do not imply any monetary loss. Broader X variables, cross-source
incident resolution, currency/inflation normalization, and approved loss mapping
remain research decisions rather than silent assumptions.

The assessment's LEXI rating, modeled breach likelihood, projected loss, protection
shortfall, and post-remediation changes are outputs, not inputs; they are excluded
from the X feature schema to avoid target leakage.
