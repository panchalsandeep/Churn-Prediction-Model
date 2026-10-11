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

What was **not** measured: multi-process or multi-node throughput (waitress is one process; the Docker image runs gunicorn with 2 workers × 8 threads, which should scale reads and explanations roughly with worker count, but that is an expectation, not a result), Postgres, uploads beyond 100,000 rows, and long-duration soak behaviour.
