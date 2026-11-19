# Bayesian vs. MLE Severity Experiment

Standalone proof of concept on branch `emeline-bayesian-model`. It does **not** modify anything in `modeling_code/`.

## Motivation

In `modeling_code/utils/helper_func.py`, `calculate_lognormal_params()` returns `mu_samples` and `sigma_samples` that repeat a single MLE estimate. So the Monte Carlo simulation ignores uncertainty in the severity parameters. The same file holds commented-out PyMC functions (`bayesian_estimate_mu_sigma()`, `calculate_bayesian_params()`). This experiment tests whether a Bayesian approach is worth completing.

## How to run

```bash
pip install -r requirements_bayesian.txt
python bayesian_severity_experiment.py --quick   # ~1 min sanity check
python bayesian_severity_experiment.py           # full study, ~7 min
```

Output: console report + `bayesian_vs_mle_severity.png`.

## What it does

1. Generates synthetic lognormal losses with known parameters (mu = 13, sigma = 1.8).
2. Fits lognormal MLE (same as `estimate_mu_sigma_mle()` in helper_func.py, including the revenue shift).
3. Fits a PyMC model: Normal likelihood on log-losses, priors mu ~ Normal(12, 3), sigma ~ HalfNormal(2.5).
4. Checks convergence (R-hat, ESS, divergences).
5. Compares predictive quantiles, including VaR 95% / 99%.
6. Small-data study: 40 repetitions at n = 5, 10, 20, 50, 200, scored on held-out data (log predictive density, 95% interval coverage).

## Preliminary results (synthetic data only)

- MCMC converged (R-hat 1.00, no divergences).
- With few incidents (n = 5), MLE 95% intervals under-cover held-out losses (~81%), while Bayesian intervals stay near the nominal 95%.
- With 50+ incidents the two methods give nearly identical results.
- The Bayesian predictive has a heavier tail (higher VaR 99%) because it includes parameter uncertainty.

These results check the method; they are not evidence of improvement on real cyber-loss data.

## Open items

- [ ] Test on real historical loss data from the repo.
- [ ] Try the Student-t likelihood from the commented-out code (robust to outliers).
- [ ] Prior sensitivity analysis.
- [ ] Match `calculate_bayesian_params()` signature to the pipeline and test integration.
