import sys
import logging
import warnings

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import lognorm
import scipy.stats as stats
from scipy.special import logsumexp

import pymc as pm
import arviz as az

warnings.filterwarnings("ignore")
logging.getLogger("pymc").setLevel(logging.ERROR)

# Synthetic data settings
TRUE_MU = 13.0
TRUE_SIGMA = 1.8
N_MAIN = 15
ANNUAL_REVENUE = 1_000_000
BASE_REVENUE = 1_000_000

# Priors on the log scale
PRIOR_MU_MEAN = 12.0
PRIOR_MU_SD = 3.0
PRIOR_SIGMA_SD = 2.5

DRAWS = 2000
TUNE = 1000
CHAINS = 4
N_SIM = 100_000

QUICK = "--quick" in sys.argv
STUDY_SIZES = [5, 20, 200] if QUICK else [5, 10, 20, 50, 200]
STUDY_REPS = 8 if QUICK else 40
STUDY_TEST_SIZE = 2000

SEED = 42
FIGURE_PATH = "bayesian_vs_mle_severity.png"


################################### Estimation ##################################

def estimate_mu_sigma_mle(all_loss_amounts, annual_revenue, base_revenue=1_000_000):
    # Same as helper_func.py
    shape, loc, scale = lognorm.fit(all_loss_amounts, floc=0)
    sigma_mle = shape

    shift_mu = np.log(annual_revenue / base_revenue)
    mu_mle = np.log(scale) + shift_mu
    return mu_mle, sigma_mle


def bayesian_estimate_mu_sigma(all_loss_amounts, annual_revenue, base_revenue=1_000_000,
                               draws=DRAWS, tune=TUNE, seed=None):
    all_loss_amounts = all_loss_amounts[all_loss_amounts > 0]
    log_loss_amounts = np.log(all_loss_amounts)
    shift_mu = np.log(annual_revenue / base_revenue)

    with pm.Model():
        mu = pm.Normal("mu", mu=PRIOR_MU_MEAN, sigma=PRIOR_MU_SD)
        sigma = pm.HalfNormal("sigma", sigma=PRIOR_SIGMA_SD)
        pm.Normal("obs", mu=mu, sigma=sigma, observed=log_loss_amounts)

        trace = pm.sample(draws, tune=tune, chains=CHAINS, target_accept=0.9,
                          random_seed=seed, progressbar=False,
                          compute_convergence_checks=False)

    mu_post = trace.posterior["mu"].values.flatten() + shift_mu
    sigma_post = trace.posterior["sigma"].values.flatten()
    return mu_post, sigma_post, trace


def calculate_bayesian_params(num_samples, all_loss_amounts, annual_revenue, seed=SEED):
    # Drop-in for calculate_lognormal_params: posterior draws instead of repeated MLE values
    mu_post, sigma_post, _ = bayesian_estimate_mu_sigma(all_loss_amounts, annual_revenue, seed=seed)
    idx = np.random.default_rng(seed).integers(0, len(mu_post), size=num_samples)
    return mu_post[idx], sigma_post[idx]


def check_convergence(trace):
    summary = az.summary(trace, var_names=["mu", "sigma"], kind="diagnostics")
    divergences = int(trace.sample_stats["diverging"].values.sum())
    ok = (summary["r_hat"] < 1.01).all() and (summary["ess_bulk"] > 400).all() and divergences == 0
    return summary, divergences, ok


def hdi(samples, prob=0.95):
    x = np.sort(samples)
    k = int(np.floor(prob * len(x)))
    i = np.argmin(x[k:] - x[:len(x) - k])
    return x[i], x[i + k]


################################### Simulation ##################################

def simulate_mle(mu, sigma, n, rng):
    return rng.lognormal(mu, sigma, size=n)


def simulate_bayes(mu_post, sigma_post, n, rng):
    idx = rng.integers(0, len(mu_post), size=n)
    return rng.lognormal(mu_post[idx], sigma_post[idx])


def loss_quantiles(sim):
    q = np.quantile(sim, [0.025, 0.5, 0.95, 0.975, 0.99])
    return dict(zip(["2.5%", "Median", "VaR 95%", "97.5%", "VaR 99%"], q))


def true_quantiles():
    dist = lognorm(s=TRUE_SIGMA, scale=np.exp(TRUE_MU))
    return dict(zip(["2.5%", "Median", "VaR 95%", "97.5%", "VaR 99%"],
                    dist.ppf([0.025, 0.5, 0.95, 0.975, 0.99])))


################################### Held-out evaluation ##################################

def lpd_mle(test, mu, sigma):
    return stats.norm.logpdf(np.log(test), mu, sigma).mean()


def lpd_bayes(test, mu_post, sigma_post, max_draws=1000):
    step = max(1, len(mu_post) // max_draws)
    m, s = mu_post[::step], sigma_post[::step]
    lp = stats.norm.logpdf(np.log(test)[:, None], m[None, :], s[None, :])
    return (logsumexp(lp, axis=1) - np.log(len(m))).mean()


def coverage(test, lo, hi):
    return np.mean((test >= lo) & (test <= hi))


def small_data_study(rng):
    results = {}
    for n in STUDY_SIZES:
        rows = []
        for _ in range(STUDY_REPS):
            train = rng.lognormal(TRUE_MU, TRUE_SIGMA, size=n)
            test = rng.lognormal(TRUE_MU, TRUE_SIGMA, size=STUDY_TEST_SIZE)

            mu_hat, sigma_hat = estimate_mu_sigma_mle(train, ANNUAL_REVENUE)
            mu_post, sigma_post, _ = bayesian_estimate_mu_sigma(train, ANNUAL_REVENUE, draws=1000,
                                                                seed=int(rng.integers(1e9)))

            q_mle = loss_quantiles(simulate_mle(mu_hat, sigma_hat, 20_000, rng))
            q_bayes = loss_quantiles(simulate_bayes(mu_post, sigma_post, 20_000, rng))

            rows.append([
                lpd_mle(test, mu_hat, sigma_hat),
                lpd_bayes(test, mu_post, sigma_post),
                coverage(test, q_mle["2.5%"], q_mle["97.5%"]),
                coverage(test, q_bayes["2.5%"], q_bayes["97.5%"]),
            ])

        rows = np.array(rows)
        results[n] = {"lpd_mle": rows[:, 0], "lpd_bayes": rows[:, 1],
                      "cov_mle": rows[:, 2], "cov_bayes": rows[:, 3]}
        print(f"  n={n:>4}: LPD MLE {rows[:, 0].mean():.3f} | Bayes {rows[:, 1].mean():.3f}   "
              f"95% coverage MLE {rows[:, 2].mean():.3f} | Bayes {rows[:, 3].mean():.3f}")
    return results


################################### Plot ##################################

def make_figure(mu_hat, sigma_hat, mu_post, sigma_post, sim_mle, sim_bayes, study):
    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    c_mle, c_bayes, c_true = "#d1495b", "#00798c", "#333333"

    ax = axes[0, 0]
    ax.scatter(mu_post[::4], sigma_post[::4], s=3, alpha=0.25, color=c_bayes, label="Posterior draws")
    ax.scatter(mu_hat, sigma_hat, color=c_mle, s=80, marker="X", label="MLE", zorder=3)
    ax.scatter(TRUE_MU, TRUE_SIGMA, color=c_true, s=80, marker="*", label="True value", zorder=3)
    ax.set_xlabel("mu")
    ax.set_ylabel("sigma")
    ax.set_title(f"Parameter uncertainty (n = {N_MAIN})")
    ax.legend(frameon=False)

    ax = axes[0, 1]
    bins = np.linspace(4, 24, 120)
    ax.hist(np.log(sim_mle), bins=bins, density=True, histtype="step", lw=2, color=c_mle, label="MLE")
    ax.hist(np.log(sim_bayes), bins=bins, density=True, histtype="step", lw=2, color=c_bayes, label="Bayesian")
    x = np.linspace(4, 24, 400)
    ax.plot(x, stats.norm.pdf(x, TRUE_MU, TRUE_SIGMA), "--", color=c_true, label="True")
    for sim, col in [(sim_mle, c_mle), (sim_bayes, c_bayes)]:
        for v in np.log(np.quantile(sim, [0.025, 0.975])):
            ax.axvline(v, color=col, ls=":", lw=1)
    ax.set_xlabel("log(loss)")
    ax.set_ylabel("Density")
    ax.set_title("Predictive distributions (dotted = 95% interval)")
    ax.legend(frameon=False)

    sizes = list(study.keys())

    ax = axes[1, 0]
    ax.plot(sizes, [study[n]["cov_mle"].mean() for n in sizes], "o-", color=c_mle, label="MLE")
    ax.plot(sizes, [study[n]["cov_bayes"].mean() for n in sizes], "o-", color=c_bayes, label="Bayesian")
    ax.axhline(0.95, color=c_true, ls="--", lw=1, label="Target 95%")
    ax.set_xscale("log")
    ax.set_xticks(sizes)
    ax.set_xticklabels(sizes)
    ax.set_xlabel("Number of training incidents")
    ax.set_ylabel("Held-out coverage")
    ax.set_title("95% interval coverage")
    ax.legend(frameon=False)

    ax = axes[1, 1]
    ax.boxplot([study[n]["lpd_bayes"] - study[n]["lpd_mle"] for n in sizes], showfliers=False)
    ax.set_xticks(range(1, len(sizes) + 1))
    ax.set_xticklabels(sizes)
    ax.axhline(0, color=c_true, lw=1)
    ax.set_xlabel("Number of training incidents")
    ax.set_ylabel("LPD(Bayes) - LPD(MLE)")
    ax.set_title("Held-out predictive accuracy (> 0 favours Bayes)")

    fig.suptitle("Cyber loss severity: Bayesian vs MLE (synthetic data)", fontsize=14)
    fig.tight_layout()
    fig.savefig(FIGURE_PATH, dpi=150)
    plt.close(fig)


################################### Main ##################################

if __name__ == "__main__":
    rng = np.random.default_rng(SEED)

    losses = rng.lognormal(TRUE_MU, TRUE_SIGMA, size=N_MAIN)
    print(f"Generated {N_MAIN} losses (true mu = {TRUE_MU}, sigma = {TRUE_SIGMA})")

    mu_hat, sigma_hat = estimate_mu_sigma_mle(losses, ANNUAL_REVENUE)
    print(f"\nMLE: mu = {mu_hat:.3f}, sigma = {sigma_hat:.3f}")

    mu_post, sigma_post, trace = bayesian_estimate_mu_sigma(losses, ANNUAL_REVENUE, seed=SEED)
    summary, divergences, ok = check_convergence(trace)
    print("\nConvergence:")
    print(summary[["r_hat", "ess_bulk", "ess_tail"]].round(3).to_string())
    print(f"Divergences: {divergences} -> {'OK' if ok else 'CHECK'}")

    print("\nPosterior mean and 95% HDI:")
    for name, s in [("mu", mu_post), ("sigma", sigma_post)]:
        lo, hi = hdi(s)
        print(f"  {name:<6}{s.mean():.3f}  [{lo:.3f}, {hi:.3f}]")

    sim_mle = simulate_mle(mu_hat, sigma_hat, N_SIM, rng)
    sim_bayes = simulate_bayes(mu_post, sigma_post, N_SIM, rng)
    q_mle, q_bayes, q_true = loss_quantiles(sim_mle), loss_quantiles(sim_bayes), true_quantiles()

    print(f"\n{'':<10}{'MLE':>16}{'Bayesian':>16}{'True':>16}")
    for k in q_mle:
        print(f"{k:<10}{q_mle[k]:>16,.0f}{q_bayes[k]:>16,.0f}{q_true[k]:>16,.0f}")

    print(f"\nSmall-data study ({STUDY_REPS} reps per size):")
    study = small_data_study(rng)

    make_figure(mu_hat, sigma_hat, mu_post, sigma_post, sim_mle, sim_bayes, study)
    print(f"\nSaved {FIGURE_PATH}")
