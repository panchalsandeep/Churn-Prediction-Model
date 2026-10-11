# Benchmarks

All numbers below were measured on one Windows laptop (12 logical cores, Python 3.13, scikit-learn 1.9, pandas 3.0),
with model training capped at 4 threads. Everything is reproducible:

```bash
python scripts/download_datasets.py     # public datasets -> datasets/raw (SHA-256 checked) and datasets/ready
python scripts/benchmark.py             # model-family comparison (≈25 min)
python scripts/train_real.py            # the shipped pipeline on each dataset
python scripts/scale_test.py 100000     # big-upload cost: stages, memory, query latency
python scripts/loadtest_local.py 100000 20 20 [--reads-only]   # concurrent HTTP load
```

The scale and load scripts use two small dev-only tools: `pip install psutil waitress`.

## Datasets

| Name | Source | Rows | Churn | Notes |
|---|---|---|---|---|
| IBM Telco | [IBM/telco-customer-churn-on-icp4d](https://github.com/IBM/telco-customer-churn-on-icp4d) | 7,043 | 26.5% | 19 features, mostly categorical services |
| Bank customers | [Kaggle "Churn Modelling" mirror](https://github.com/sagnikghoshcr7/Bank-Customer-Churn-Prediction) | 10,000 | 20.4% | age, balance, products, activity |
| Orange telecom | [BigML sample](https://github.com/caroljmcdonald/mapr-sparkml-churn) | 3,333 | 14.5% | call-minute and customer-care features |
| Demo (synthetic) | `api/churn/demo_data.py` | 8,000 | 11.3% | rolling behaviour, monthly snapshots, time-based split |

The cell2cell holdout file that turned up in the search has no labels, so it was discarded. The raw files are never committed; check each source's licence before commercial use.

## Which model family wins? (identical splits, Platt calibration, 3 seeds; demo has 1 time-split seed)

PR-AUC is the headline because churn is imbalanced. ROC-AUC and top-decile lift are shown for context.

| Model | Orange ROC / PR | Telco ROC / PR | Bank ROC / PR | Demo ROC / PR |
|---|---|---|---|---|
| **Gradient boosting, tuned** | .917 / **.865** | .851 / **.668** | .861 / .702 | .795 / **.404** |
| Gradient boosting, default | .916 / .852 | .849 / .666 | .859 / .699 | .789 / .397 |
| Gradient boosting + ratio features | .909 / .863 | .848 / .665 | .859 / **.702** | .787 / .372 |
| Stacking (LR + GB + RF) | .912 / .852 | **.852** / .666 | .858 / .699 | .796 / .395 |
| Gradient boosting, class-weighted | .913 / .838 | .849 / .666 | .859 / .697 | .784 / .399 |
| Random forest | .901 / .807 | .849 / .663 | .852 / .683 | .783 / .386 |
| Extra trees | .879 / .689 | .841 / .638 | .847 / .655 | .782 / .370 |
| Logistic regression | .829 / .484 | .848 / .657 | .768 / .460 | .797 / .372 |

What this says:

* **Tuned gradient boosting is first or within noise of first on PR-AUC on all four datasets**, so it is in the shipped candidate pool (`gradient_boosting_tuned`, a 12-iteration randomised search scored on different CV folds than it was tuned on).
* **Stacking** matches the best ROC-AUC on Telco but costs about 10× more to train and does not move PR-AUC. Not shipped.
* **Ratio/product features** helped by 0.0002 on Bank, hurt on Demo and Orange. Within noise, not shipped.
* **Class weighting** lowers PR-AUC and worsens calibration (Brier). Calibrated probabilities are used instead.
* **Logistic regression** is competitive only when relationships are near-linear (Telco, the synthetic demo) and collapses on Bank and Orange (non-linear age, products, plan interactions). It stays in the pool as the transparent baseline.
* **Random forest / extra trees** trail boosting everywhere.
* **The ceiling is the data.** ≈0.85 ROC-AUC on Telco and ≈0.86 on Bank are where well-tuned models land in published work, and no model family here moved them. More signal needs richer data (usage events over time), not a bigger model; this is also why deep learning was not added.

## The shipped pipeline on real data (`algorithm="auto"`, stratified 80/20, seed 42)

| Dataset | Winner | ROC-AUC | PR-AUC (baseline) | Brier | Top-decile lift | Train time |
|---|---|---|---|---|---|---|
| Orange telecom | Gradient boosting (tuned) | 94.1 | 90.8 (14.5) | 0.033 | 6.7× | 40 s |
| Bank customers | Gradient boosting (tuned) | 86.1 | 69.6 (20.4) | 0.104 | 4.0× | 28 s |
| IBM Telco | Gradient boosting (tuned) | 84.2 | 66.5 (26.5) | 0.136 | 2.8× | 37 s |

Probabilities are calibrated and clipped to [0.2%, 99.5%]. Confidence intervals and the lift/gain/calibration curves are in each run's results (`datasets/results/product_run.json`).

## Scale

100,000-row upload (14 MB CSV), single process, SQLite:

| Step | Result |
|---|---|
| Training, end to end | 71–83 s: the ML pipeline 52 s (including out-of-fold scoring of all 100k customers, 10 s, and the final fit, 10 s) plus 20–25 s of writing results. At this size the plan skips the tuned variant and the forest. |
| Writing results | persist customers 12.7 s, snapshot 3.2 s, alerts 1 s, segments 0.35 s |
| Peak memory | ≈ 540 MB RSS |
| Storage | database 84 MB, model + scored snapshot 10 MB |

Query latency at 100,000 customers (single request, warm):

| Query | p50 |
|---|---|
| Dashboard payload (cached, persisted per data revision) | 0.3 ms (6.6 ms on first build after a change) |
| Customers page 1 / filtered by tier, sorted by risk or loss | 1.5 ms |
| Customers page at offset 10,000 | 146 ms (deeper than 50,000 rows is refused; use filters or export) |
| ID prefix search | 1.2 ms |
| Customer detail with on-demand explanation | 125 ms |
| Single prediction with explanation | 150 ms |
| Streaming export, 20,000 rows | 0.7 s (keyset pagination) |

Concurrent load, 20 client threads against one waitress process (16 threads) holding the 100k-customer workspace:

| Mix | Throughput | p50 | p95 | Errors |
|---|---|---|---|---|
| Reads only (dashboard, pages, filters, search, alerts) | **285 req/s** | 75 ms | 127 ms | 0 |
| Realistic mix including explanations (detail, predict) | 27 req/s | 188 ms | 3.4 s | 0 |

Explanations are the CPU-bound part of serving: scoring a customer under 12 perturbations holds the Python interpreter, so under heavy concurrency they queue (a semaphore limits them to 3 at a time so they cannot starve cheap reads). Reads stay fast regardless. The what-if slider re-scores probability only while dragging and computes the explanation when released.

Bugs this testing found and fixed: an uncached dashboard aggregate that took 3.1 s, an ID search that scanned JSON attributes (1.7 s), segments computed by re-parsing every customer's JSON (4.6 s), and an off-by-one in the "large workspace" threshold that kept a 100,000-row workspace on the slow path.

### Evaluation history and the regression gate

`python scripts/eval.py` trains the shipped pipeline (`auto`) on every available dataset, appends the result with the git commit to `evals/history.jsonl`, and with `--check` fails if a metric falls below `evals/baseline.json` by more than its tolerance (PR-AUC 2.0 points, ROC-AUC 1.5, lift 0.3, Brier +0.01). Baselines move only through `--update-baseline`, so a regression is a visible, committed decision. CI runs the offline datasets (`demo`, `b2b`); the public ones (`telco`, `bank`, `orange`) need `scripts/download_datasets.py` and are checked locally.

Baseline recorded on 2026-10-12 (holdout metrics of the shipped pipeline; the logistic-regression floor is a separate random 75/25 split, so compare roughly, not point for point):

| Dataset | Rows | Churn | ROC-AUC | PR-AUC | Brier | Top-decile lift | Logistic floor ROC / PR |
|---|---|---|---|---|---|---|---|
| demo (synthetic B2B-style snapshots) | 8,000 | 11.3% | 78.6 | 38.6 | 0.083 | 3.9× | 79.3 / 37.5 |
| b2b (synthetic accounts, 60-day horizon) | 4,381 | 6.9% | 93.8 | 67.2 | 0.072 | 6.8× | 92.2 / 71.2 |
| telco | 7,043 | 26.5% | 84.0 | 65.6 | 0.137 | 2.9× | 84.4 / 63.8 |
| bank | 10,000 | 20.4% | 86.3 | 69.8 | 0.104 | 4.0× | 77.0 / 47.6 |
| orange | 3,333 | 14.5% | 94.0 | 90.9 | 0.033 | 6.7× | 81.8 / 46.5 |

What this says plainly: on `bank` and `orange` the boosted model adds a lot over a linear one (+22 and +44 PR-AUC points); on `demo`, `telco` and `b2b` it is no better than plain logistic regression within noise, and on `b2b` the floor is nominally higher. The synthetic sets were built with mostly additive effects, which is exactly where a linear model is hard to beat, so they say little about real data. The honest reading is that the extra machinery pays where there are real interactions, and that we should not claim more than that.

### Phase 1: scaling out

**Several web processes.** Same 100,000-customer workspace, reads-only mix, 40 client threads, SQLite (WAL) shared by all processes, client on the same 12-core machine (so the client competes for CPU):

| Server processes | Throughput | Overall p50 | Overall p95 | Errors |
|---|---|---|---|---|
| 1 | 307 req/s | 132 ms | 195 ms | 0 |
| 4 | **756 req/s** | 26 ms | 98 ms | 0 |

2.5× from 4 processes, not 4×: the load generator and SQLite's single writer share the same box. Page-by-risk p99 rose to ~1 s under four processes, which is SQLite lock contention; Postgres is the intended store for this. The workspace trained in one process was served correctly by the other three (cache invalidation by database revision works across processes).

**Queue.** A 100,000-row training job submitted over HTTP ran through the durable queue in 59 s (same as before the queue). A separate `python -m churn.worker` process trained a model for a job enqueued by another process (automated test).

**Narrow table.** Moving the encrypted `attrs`/`reasons` out of `customers` cut writing 100,000 scored customers from 12.7 s to 9.0 s.

**LightGBM vs scikit-learn's HistGradientBoosting** (`scripts/bench_lgbm.py`, same preprocessing, 3 seeds, uncalibrated, PR-AUC): telco 67.0 vs 65.5, bank 69.0 vs 68.9, orange 86.0 vs 86.6, demo-8k 37.1 vs 36.4, demo-100k 41.6 vs 41.7 (HGB first). Fit time is similar. **LightGBM is not more accurate, so it was not adopted.** Its one advantage is exact per-customer explanations: 1,000 customers in 0.29 s against 1.9 s for the perturbation method on the same data. Because that only holds for a LightGBM model and the served model is a calibrated HGB, the explanation method stays as it is. Per-customer reasons are still precomputed for the riskiest 300 and computed on demand for the rest (about 110 ms each).

What was **not** measured: Postgres throughput, multiple nodes, a real Redis, S3, Kubernetes autoscaling, containers (Docker was not started), uploads beyond 100,000 rows, and long-duration soak behaviour. Phase 1's exit criterion "restore drill passed" is met for SQLite only.
