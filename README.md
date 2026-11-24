# Ruleta Express — Can any model beat a lottery's randomness?

A reproducible statistical study of **Ruleta Express**, a Dominican Republic lottery game that draws a number from 0–18 every few minutes. The project collects every published draw, then asks one question with a pre-defined decision rule:

> Is there an **exploitable** edge, or only noise?

**Verdict: no exploitable edge.** The data does show small, statistically significant deviations from pure randomness, but none of them is large enough to beat the game's break-even point. The system therefore stays in paper-trading mode by design.

This is a data-science and statistics project, not a betting tool.

---

## Key results

Out-of-sample results on a locked test set of 6,637 draws, from the reproducible report in [`informe_quant.md`](informe_quant.md):

| Metric | Best model (Markov order 1) | Uniform baseline (1/19) |
|---|---|---|
| Log-loss | **2.9246** | 2.9444 |
| Brier score | **0.9459** | 0.9474 |
| Top-1 hit rate | 5.70% | 5.26% |
| Calibration error (ECE) | 0.011 | — |

- **The model is genuinely better than chance at predicting probabilities.** The log-loss improvement is significant (Diebold-Mariano z = 7.27, p < 0.001) and stays positive in all 5 walk-forward blocks.
- **It is still not worth betting on.** The game pays 17 to 1, so break-even requires a 5.56% hit rate. The Bayesian posterior gives only a 67% probability that the true hit rate exceeds break-even, and the lower credible bound (5.15%) is below it. The decision gate requires ≥95%.
- **Monte Carlo check.** Betting 1% of bankroll per round under the skeptical posterior gives a median ROI of −41% and only a 31% chance of ending in profit.

Statistical significance is not the same as an exploitable edge, and making that distinction rigorously is the main result of this project.

---

## Methodology

1. **Data collection.** A scraper captures every draw from the official results page and fills gaps from the historical endpoint, with de-duplication and range validation. The dataset has 33,181 draws in the analysis snapshot and keeps growing.
2. **Leakage-free splits.** Chronological train / validation / test split (60/20/20). The test set is locked and used once. Model selection uses validation only.
3. **Candidate models (25 in total).** 24 configurations of Markov chains of order 1–3 and Dirichlet-multinomial frequency models, with different smoothing priors (α) and window sizes, plus the uniform baseline.
4. **Evaluation.** Log-loss, Brier score, top-k accuracy, Expected Calibration Error, Diebold-Mariano test against the uniform baseline, and walk-forward stability.
5. **Randomness tests.** Chi-square goodness of fit and independence (lags 1–3), runs test, Ljung-Box and KS on randomized PIT, with Holm and Benjamini-Hochberg multiple-testing corrections.
6. **Bayesian layer.** Beta-binomial posteriors on the hit rate under uniform, Jeffreys and skeptical priors, plus Bayes factors for Markov versus i.i.d.
7. **Decision gate.** Seven explicit criteria, defined before looking at the test results. All must pass before any "signal" is allowed. Two fail, so the verdict is NO EDGE.
8. **PRNG analysis.** A bounded SMT search (z3) over known LCG parameters. The result is inconclusive, and the report explains why recovering the generator is not feasible from 4.25 bits per draw.

---

## Architecture

```
Official results page ──► scraper (+ Cloudflare Worker proxy) ──► CSV dataset
                                                                    │
            ┌───────────────────────────────────────────────────────┤
            ▼                                                       ▼
   quant/ (offline study)                                  live monitor (scheduled)
   reproducible report + decision gate                     predictions logged, never acted on
            │                                                       │
            └──────────────► password-protected dashboard ◄─────────┘
                             (Flask / Vercel serverless, Supabase Storage)
```

| Folder / file | Purpose |
|---|---|
| `quant/` | Main study: data loading, models, validation, goodness-of-fit, Bayes, Monte Carlo, PRNG search, decision gate and report |
| `m4_engine/` | Offline validation of an alert strategy: walk-forward, sensitivity analysis, multiplicity corrections |
| `recencia/`, `papel/` | Experimental recency models and paper-trading log |
| `live_models.py`, `live_monitor.py` | Incremental single-pass models used by the live monitor |
| `sync_ruleta_express.py`, `scraper_web.py` | Data collection with ETag caching, back-fill and retries |
| `api/`, `web/`, `render_app.py` | Authenticated dashboard (Vercel serverless functions or Render) |
| `tests/` | 144 pytest tests: parsers, models, statistics, decision rules, integration |

---

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# Reproduce the full statistical report (writes informe_quant.md / .json)
python -m quant --seed 20261108

# Run the test suite
pytest -q
```

To run the dashboard, copy `.env.example` to `.env` and fill in your own values. No credentials are committed to this repository.

---

## Limitations

- Only the published number is observed. The internal state of the generator and how it maps to 0–18 are unknown.
- The deviations found are real but small, and they could come from post-processing in the game itself (for example, how repeats are handled) rather than from a weak generator.
- The study covers May–November 2026. Results may not hold if the operator changes its system.

## Tech stack

Python · pandas · NumPy · SciPy · scikit-learn · z3-solver · pytest · Flask · Vercel · Render · Supabase · Cloudflare Workers

## Author

**Raymond Paradell** · Junior Software Developer · Software Development (ITLA) and Pure Mathematics (UASD)
[GitHub](https://github.com/raxcode1) · [LinkedIn](https://www.linkedin.com/in/raxdotpy)
