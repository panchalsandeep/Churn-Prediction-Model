# ChurnSight roadmap

A prioritised plan for improving the system, grounded in what we have measured and what we have not.
Sizes: **S** ≤ 1 day · **M** 2–5 days · **L** 1–3 weeks · **XL** 3+ weeks. Impact: ●●● high · ●● medium · ● low.

## 1. Where we are

**Solid (measured):** calibrated, tuned boosted models at the published ceiling on Telco/Bank/Orange (ROC-AUC 84–94); honest evaluation (time split, out-of-fold scores, CIs); tenancy, jobs, registry, drift, alerts, closed-loop log; 100k customers on one process serves reads at 285 req/s, p95 127 ms.

**Weak or unproven:**

| Gap | Evidence |
|---|---|
| Explanations are CPU-bound | 3 s p95 under 20-way concurrency; 27 req/s on the realistic mix |
| One process does everything | training threads, rate limiter, caches all in-process; jobs die on restart |
| Schema is created with `create_all` | no migrations; any column change breaks existing databases |
| Models are `joblib` pickles | unsafe to load from untrusted storage; breaks across scikit-learn versions |
| Untested paths | Docker image, Postgres, real Firebase login, pandas 2.2.2 (the pinned version), mobile layout, uploads > 100k rows, multi-worker throughput |
| Static snapshots only | no event data, no scheduled scoring, no connectors; users must upload CSVs by hand |
| No treatment data yet | the action log exists, but uplift ("who responds to what") needs enough logged outcomes |
| Writing 100k scores takes ~13 s | wide `customers` table (attrs + reasons JSON in every row) |

## 2. North-star metrics

* **Model:** PR-AUC lift over baseline on the customer's own holdout; calibration error (ECE) < 3 pts; top-decile lift.
* **Business:** revenue saved per $ of outreach, measured from the action log (the number that proves the product).
* **Reliability:** job success rate > 99%, p95 read latency < 300 ms at 100k customers, zero cross-tenant reads.
* **Adoption:** time from signup to first scored dashboard < 5 minutes; weekly active CSMs per workspace.

## 3. Workstreams

### A. Trust and verification (do first: it removes unknowns)
| # | Item | Size | Impact |
|---|---|---|---|
| A1 | CI matrix: pinned `pandas==2.2.2` / newest, Python 3.12 / 3.13, scikit-learn min / max | S | ●●● |
| A2 | Bring up the Docker image + Postgres in CI; run the API suite against it | M | ●●● |
| A3 | Playwright end-to-end: login (Firebase emulator), upload, train, filter, drawer, what-if, settings | L | ●●● |
| A4 | Property/fuzz tests for schema inference (weird headers, BOMs, mixed types, all-null columns, 1 class, duplicate IDs, 250k rows, wide files) | M | ●●● |
| A5 | Tenant-isolation test suite: every endpoint × two workspaces, including exports, jobs, alerts, interventions | M | ●●● |
| A6 | Model-quality gate on a real dataset (Telco) nightly, not just generated data; fail on regression > 1 PR-AUC point | M | ●●● |
| A7 | Reproducibility: store library versions, seed, data hash and schema in the bundle; refuse to load on incompatible versions with a clear message | S | ●●● |
| A8 | Model card per version (data, split, metrics, limitations, intended use) shown in the registry | M | ●● |

### B. Model quality and new ways to detect churn
| # | Item | Size | Impact |
|---|---|---|---|
| B1 | **Event-level ingestion + feature builder**: accept raw events (logins, tickets, payments) and compute rolling 7/30/90-day windows, trends, recency/frequency, with a point-in-time-correct join (no label leakage). This is where real accuracy gains live. | XL | ●●● |
| B2 | **Target-definition wizard**: derive churn from subscription start/end dates + grace period (KKBox-style) instead of requiring a ready label | M | ●●● |
| B3 | **Multi-horizon predictions** (30/60/90 days) and "expected time to churn" from a discrete-time hazard or Cox model; survival curves per customer, not just per cohort | L | ●●● |
| B4 | **Uplift modelling** (T/X-learner) once ≥ ~1–2k logged actions with outcomes exist; recommend the action with the best expected net value, and randomise a small holdout so effects are estimable | XL | ●●● |
| B5 | Voluntary vs involuntary (payment failure) churn as separate models/alerts: different playbooks | M | ●● |
| B6 | Native-categorical boosting (LightGBM/CatBoost) as optional candidates; monotonic constraints where business logic demands it | M | ●● |
| B7 | Exact TreeSHAP explanations (fast, theoretically sound) replacing the perturbation method; precompute for the top-N at scoring time | M | ●●● |
| B8 | Conformal prediction intervals / "confidence" flag on each score; abstain on out-of-distribution rows | M | ●● |
| B9 | Rolling-origin backtesting report (train on past months, test on next) instead of one split | M | ●●● |
| B10 | Drift-triggered retraining (PSI/AUC decay) with champion/challenger auto-promotion rules and shadow scoring | L | ●●● |
| B11 | Text signals: ticket/NPS comments → sentiment/topic features (embeddings); optional LLM for summarising a customer's situation and drafting outreach (human reviewed, opt-in, no PII leaves without consent) | L | ●● |
| B12 | Fairness/segment-performance report (metrics by region/plan/size) with alerts on disparity | M | ●● |
| B13 | Label-delay and class-prior-shift handling (churn is observed late; base rates move) | M | ●● |

### C. Product features
| # | Item | Size | Impact |
|---|---|---|---|
| C1 | **Scheduled scoring + connectors**: S3/GCS, Postgres/BigQuery/Snowflake, Stripe, HubSpot/Salesforce, Segment; incremental pulls | XL | ●●● |
| C2 | **Notifications**: Slack/Teams/email/webhooks for new high-risk accounts, risk jumps, digest emails | L | ●●● |
| C3 | **Playbooks that run**: triggered tasks in CRM, email templates, SLA timers, owners, reminders | L | ●●● |
| C4 | **Impact dashboard**: revenue saved, save rate by action, ROI, cohort of treated vs untreated (feeds B4) | M | ●●● |
| C5 | Customer 360 timeline (score history, events, tickets, actions) and score-over-time sparkline | M | ●● |
| C6 | Saved views/segments, shared filters, column chooser, bulk assign/log-action from the table | M | ●● |
| C7 | Retention experiments: A/B an intervention with a built-in holdout and significance read-out | L | ●●● |
| C8 | Public REST API + API keys + OpenAPI docs + a Python SDK (`score(df)`, `explain(id)`) | L | ●●● |
| C9 | Roles and permissions (viewer/CSM/admin), team workspaces with invites, SSO (SAML/OIDC) | L | ●●● |
| C10 | Reports: scheduled PDF/slide exports, shareable read-only links | M | ●● |
| C11 | Multi-currency, i18n, timezone-aware dates, units for revenue | M | ● |
| C12 | In-app help: metric glossary tooltips ("what is PR-AUC?"), guided tour, example datasets (Telco/Bank loaders) | M | ●● |
| C13 | Compare model versions side by side (metrics, importance, which customers changed tier) | M | ●● |

### D. Scale and performance
| # | Item | Size | Impact |
|---|---|---|---|
| D1 | **Measure multi-process**: gunicorn 4×8 and a 2-node setup under the same load test; publish req/s per worker | M | ●●● |
| D2 | **Separate worker process + durable queue** (RQ/Arq/Celery on Redis): training survives web restarts, retries, cancellation, timeouts, per-job memory limits | L | ●●● |
| D3 | **Redis** for rate limiting, cache invalidation, job queue, results cache (fixes in-process limits behind a load balancer) | M | ●●● |
| D4 | **Explain at scale**: TreeSHAP (B7) + precompute reasons for all high/medium customers during scoring; cache by input hash; optional ONNX/treelite inference | L | ●●● |
| D5 | **Split `customers` into a narrow score table + `customer_details`** (attrs, reasons): 5–10× faster aggregates, faster 100k inserts (13 s today), smaller scans | M | ●●● |
| D6 | Postgres tuning: partition by workspace, covering indexes, materialised aggregates refreshed on write, `COPY` bulk loads | M | ●● |
| D7 | Object storage for artifacts/snapshots (S3), Parquet instead of gzipped CSV; direct-to-S3 uploads (presigned, chunked) so web workers never proxy 25 MB bodies | L | ●●● |
| D8 | Streaming/chunked CSV parsing with a memory budget; reject early on width/size; downcast dtypes (float32/categoricals) to cut RSS (540 MB at 100k) | M | ●● |
| D9 | Cursor pagination everywhere (OFFSET is capped at 50k today) | S | ●● |
| D10 | Autoscaling policy and cost model: cost per 100k scored customers, per training run; per-plan quotas | M | ●●● |
| D11 | CDN for the static frontend; HTTP/2; preload critical fonts; bundle-size budget in CI | S | ●● |
| D12 | Larger-data path: > 500k rows via sampling for selection + LightGBM/histogram methods, or a Spark/Polars backend | XL | ●● |

### E. Security, privacy and compliance
| # | Item | Size | Impact |
|---|---|---|---|
| E1 | **Replace pickles** with safer artifacts (skops, or ONNX for scoring) and verify hashes before loading | M | ●●● |
| E2 | Written threat model; dependency and container scanning (pip-audit, npm audit, Trivy) in CI | M | ●●● |
| E3 | PII controls: column tagging, masking in UI/exports, hashed IDs option, "never store" columns | M | ●●● |
| E4 | Data lifecycle: delete-my-data endpoint, retention windows, encrypted storage, audit-log export (GDPR/CCPA readiness) | L | ●●● |
| E5 | Upload hardening: size/row/column caps already exist; add zip/CSV-bomb checks, encoding limits, content sniffing, quarantine | S | ●● |
| E6 | CSP and stricter headers on the frontend; subresource integrity for third-party fonts | S | ●● |
| E7 | Secrets management and key rotation; restrict the (public) Firebase web key by domain | S | ●● |
| E8 | SSRF-safe connector framework (allow-lists, egress proxy), per-connector scopes | M | ●●● (with C1) |
| E9 | SOC 2 groundwork: access reviews, change management, backup tests | XL | ●● (if selling to enterprises) |

### F. Reliability and operations
| # | Item | Size | Impact |
|---|---|---|---|
| F1 | **Schema migrations** (Alembic) replacing `create_all`; migration tests; zero-downtime rules | M | ●●● |
| F2 | Observability: Prometheus + Grafana dashboards, OpenTelemetry traces, Sentry; job duration/queue-depth/error-rate panels | M | ●●● |
| F3 | SLOs and alerting (availability, p95 latency, job failure rate); on-call runbooks | M | ●●● |
| F4 | Backups + restore drills for DB and artifacts; point-in-time recovery | M | ●●● |
| F5 | Job reliability: idempotency keys, retry with backoff, cancel button, stuck-job reaper, graceful shutdown that finishes or re-queues work | M | ●●● |
| F6 | Soak and chaos tests: 1 h load, killing workers mid-training, DB failover | M | ●● |
| F7 | Blue/green or rolling deploys, readiness vs liveness probes, feature flags | M | ●● |
| F8 | Staging environment seeded with the benchmark datasets; preview environments per PR | M | ●● |

### G. UX and design
| # | Item | Size | Impact |
|---|---|---|---|
| G1 | **Mobile pass**: the sidebar fills the whole screen on phones; tables need a card layout; test at 375/768/1280 | M | ●●● |
| G2 | Accessibility audit (axe, keyboard-only, screen-reader), chart data tables as alternatives, contrast check in dark mode | M | ●●● |
| G3 | First-run onboarding: pick a goal → load a sample or connect data → see the first insight in one screen | M | ●●● |
| G4 | Plain-language metrics: every chart gets a "what this means for you" line and a threshold of good/bad | M | ●●● |
| G5 | Destructive-action safety: undo for reset/promote, typed confirmation for workspace reset | S | ●● |
| G6 | Toast queue and notification centre (jobs finished, alerts) | S | ●● |
| G7 | Performance budgets: LCP < 2 s, JS < 100 KB initial; Lighthouse in CI | S | ●● |
| G8 | Design-system documentation (tokens, components, states), visual-regression tests | M | ●● |
| G9 | Empty/error/loading states audit across every page | S | ●● |
| G10 | Saved layouts, density toggle, keyboard-first table navigation | M | ● |

### H. Data and evaluation assets
| # | Item | Size | Impact |
|---|---|---|---|
| H1 | Expand the benchmark: KKBox (event-level, large), more telecom/SaaS/subscription sets, time-split sets; nightly job publishes a leaderboard | L | ●●● |
| H2 | Dataset profiler shown *before* training (missingness, cardinality, drift between train/test periods, leakage suspects) | M | ●●● |
| H3 | Synthetic generator v2: uplift scenarios with known treatment effects to validate B4, event streams for B1 | L | ●●● |
| H4 | Regression-proof the schema inference with a corpus of anonymised real-world headers | M | ●● |

## 4. Sequenced plan (superseded by §10 after the decisions in §6; kept for reference)

**Phase 0: verify and de-risk (1–2 weeks).** A1, A2, A5, A7, F1, E1 (design), G1, D1 (measure first), quick wins below.
*Exit gate:* CI green on pinned versions + Docker/Postgres; migrations in place; multi-worker numbers published; no known cross-tenant path.

**Phase 1: reliable service (2–4 weeks).** D2, D3, F5, F2, F3, F4, D5, B7, D4.
*Exit gate:* training survives a web restart; explanations p95 < 500 ms at 20 concurrent; dashboards and alerts on the SLOs; restore drill passed.

**Phase 2: data in, value out automatically (4–8 weeks).** C1 (start with S3 + Postgres + Stripe), B2, C2, C4, H2, G3, C8.
*Exit gate:* a new workspace goes from connect → scheduled daily scoring → Slack alert with no manual upload; impact dashboard live.

**Phase 3: better predictions (6–10 weeks, overlaps Phase 2).** B1, B3, B9, B10, B12, H1, H3.
*Exit gate:* event-based features beat snapshot features on the KKBox benchmark and on at least one design-partner dataset; backtests in the UI; auto-retraining with safe promotion.

**Phase 4: decisions, not just scores (8–14 weeks).** C3, C7, B4, B5, C9, E3, E4, C10.
*Exit gate:* uplift model validated against a randomised holdout; roles/SSO shipped; data-deletion flow tested.

**Phase 5: scale-out and polish (ongoing).** D6–D12, E8–E9, G2, G4, G7–G10, B6, B8, B11, B13.

## 5. Quick wins (each under a day)
1. Add `pandas==2.2.2` to a CI matrix leg (A1).
2. Store library versions + seed in the model bundle and check them on load (A7).
3. Show `plan` (small/medium/large) and warnings in the model snapshot so users know why big uploads skip models.
4. Fix the mobile sidebar width and add `overflow-x` guards on tables (G1).
5. Typed confirmation for "Reset workspace" (G5).
6. Metric glossary tooltips on KPI tiles (G4/C12).
7. Cursor pagination for the customer table's "next/prev" (D9).
8. Add `Retry-After` handling and a friendly "server busy, you are 3rd in line" UI for 503s.
9. Nightly job that reruns `benchmark.py` on Telco and fails on regression (A6).
10. CSP header and `pip-audit`/`npm audit` in CI (E2, E6).

## 6. Decisions (resolved)

| Question | Decision | What it changes |
|---|---|---|
| Customer | **B2B** | Model at the *account* level with seats/users beneath it; renewal-centric features (days to renewal, contract end, auto-renew); both logo churn and **revenue churn (downgrade/contraction)**; NRR/GRR reporting; connectors for CRM (Salesforce, HubSpot), product analytics (Segment, Amplitude, Mixpanel), support (Zendesk, Intercom), billing (Stripe, Chargebee), warehouses. |
| Hosting | **Highly scalable** | Stateless API, separate workers, durable queue, Postgres + object storage, Kubernetes with autoscaling, per-tenant limits. Phase 1 is no longer optional. |
| Data sensitivity | **Highly sensitive and critical** | Security and reliability come *before* new features: no pickles, row-level security, envelope encryption, SSO/MFA/RBAC, immutable audit log, data deletion, DR with stated RPO/RTO, pen test, SOC 2 path, private-deployment option. |
| Design partner | **Unknown → decide for you** | Run a *design-partner program* in parallel (target 2–3 B2B SaaS companies, with a data-processing agreement and read-only sample exports). Until one exists we validate on KKBox (event-level) and a synthetic B2B/uplift simulator (H3). Uplift (B4) stays gated on real action data; everything else is not. |
| Pricing | **Per seat, with quotas and metering** | See below. |
| LLM features | **Yes** | Opt-in per tenant, behind guardrails (see below); built last. |

### Seats, quotas, metering (assumptions to confirm)
*Seat* = a platform user (CSM, admin, viewer). *Quotas* meter what costs us money. Proposed meters: scored accounts per month, training runs per month, API calls, connector syncs, stored history (retention), and LLM tokens. Plan = seats included + quota bundle; overage is soft-warn then hard-stop (never silently drop data).

Build order: (1) `usage_events` table written at the existing choke points (job admission, scoring, API gateway, connector sync); (2) nightly aggregation per workspace and plan; (3) enforcement in `jobs._admit` and the rate limiter (they already exist per workspace); (4) seat management: invites, roles, active-seat counting, SCIM provisioning; (5) usage page for admins; (6) Stripe subscriptions + usage records; (7) export of usage for finance. Metering must be append-only and idempotent so billing disputes can be audited.

### LLM guardrails (sensitive data)
* Off by default; per-tenant opt-in; per-field redaction (names, emails, free text) before any call.
* Only providers/endpoints with zero data retention and no training on inputs, regional endpoints, or a self-hosted open model for tenants that forbid external calls.
* **Ticket text and CRM notes are untrusted input**: treat them as potential prompt injection; the model may draft and summarise but never trigger actions, send messages or call tools on its own.
* Human approval for anything outward-facing; every prompt/response logged (redacted) and metered per tenant.
* First features: account situation summary, outreach drafts, ticket topic/sentiment features (B11), natural-language questions over the dashboard ("high-risk accounts renewing in 60 days"), plain-English metric explanations.

## 7. Risks
* **Overfitting to benchmarks:** public datasets are static snapshots; real gains come from event data (B1). Do not claim parity with large companies' predictors without a partner dataset.
* **Uplift without experiments:** observational action logs are confounded (CSMs contact the riskiest customers). B4 needs randomised holdouts (C7) before it can be trusted.
* **Operational sprawl:** Redis, workers, Postgres and object storage multiply failure modes. Add them in the order measurements demand, but given the scale requirement, do the queue and storage split early.
* **Silent quality decay:** a deployed model can degrade unnoticed. Monitoring ships with retraining, not after.
* **Sensitive data + LLMs:** the highest-impact privacy risk in the plan; keep it opt-in and last.

## 8. MLOps: what exists, what does not, and what we should build

### What exists today
A versioned **registry** (champion/challenger, promote, rollback), a **drift report** when a batch is scored, a model-quality **CI gate** on generated data, and reproducible benchmark scripts.

### What does not exist
* **No continuous training.** A model is trained only when someone uploads a file. There are no schedules, no triggers, and no retraining on data the system has received since.
* **No post-deployment performance monitoring.** Labels are read only from the training upload; when real outcomes arrive later, nothing compares them with what we predicted.
* **No shadow evaluation**, approval workflow, or lineage from model → dataset version → code version.
* **No feature store / point-in-time correctness**, so features at scoring time may not match training time.
* Artifacts are pickles with no integrity check; models do not record library versions.

### Are we training at inference time? No, and we should not
Training per request (or per scored row) is the wrong tool for churn:
* **Labels arrive late.** "Did they churn?" is known weeks after the prediction, so there is nothing to learn from at inference time.
* **Latency and cost:** a fit takes 30–80 s; scoring must stay in milliseconds.
* **Feedback loops and poisoning:** our own interventions change outcomes; a tenant could also feed adversarial data. Learning online from unvetted traffic is a security risk for sensitive data.
* **Reproducibility and audit:** regulated customers need to say exactly which model produced a score.

What is useful at inference time is *cheap adaptation without retraining*: **recalibration** on recently matured labels, and **prior-shift correction** when the base churn rate moves. Both are small, auditable, and safe.

### Target design: continuous training with guard-rails
1. **Ingest and mature labels:** scheduled score runs write predictions with a timestamp; a nightly job joins them to outcomes after the label window closes (e.g. 30/60/90 days).
2. **Monitor:** per-tenant dashboards for data drift (PSI), score drift, **realized PR-AUC / calibration on matured labels**, data-quality checks (missing columns, null spikes, schema changes), and alerts on thresholds.
3. **Trigger retraining** on a schedule (weekly/monthly), on drift, or on realized-performance decay. Retraining runs as a queued job: validate data → build point-in-time features → group-aware train → rolling-origin backtest → register.
4. **Shadow and compare:** the challenger scores live traffic silently for a period; promotion requires non-inferiority on the same recent window plus calibration and fairness checks.
5. **Promote with safety:** automatic for tenants who opt in, human approval (two-person for sensitive tenants) otherwise; instant rollback; every promotion audited.
6. **Reproducibility:** each version records data hash, code commit, library versions, seed, and feature definitions; artifacts are signed and stored as safe formats (skops/ONNX).
7. **Tenant isolation of learning:** models train on a tenant's data only. No cross-tenant pooling of raw data (sensitive). Cold start for small tenants uses generic priors validated on public/synthetic data, never other customers' records.

## 9. What top-tier churn predictors have that we do not

### Modelling and evaluation
1. **Group-aware validation (a real bug).** `training._split_indices` and every CV fold split by *row*. If a customer appears in several snapshots (normal for event/periodic data), the same customer lands in both train and test, inflating metrics. The demo has unique IDs so tests did not catch it. Top systems split by account and by time.
2. **Point-in-time correctness.** No guarantee features use only data known at the prediction date. This is the most common source of "great offline, poor in production".
3. **Explicit churn definition and prediction window.** We take a label as given. Top systems define churn (grace period, reactivation rules) and train multi-horizon models.
4. **Silent schema failure at scoring.** `prepare_features` fills any missing column with NaN without error, so a batch missing a key feature is scored with degraded accuracy and no warning. Needs schema contracts: block or warn loudly.
5. **Survival / time-to-event per customer**, not only per cohort.
6. **Revenue-aware B2B targets:** contraction/downgrade, expansion, CLV and margin. Our "expected loss" is probability × revenue × a fixed horizon.
7. **Uplift modelling and treatment recommendations:** "who responds to which action". We only log actions.
8. **Realized-performance monitoring** and rolling-origin backtests.
9. **Exact explanations (TreeSHAP)** and reason codes that map to playbooks. Ours perturb features to the median, which misattributes under correlated features, and covers only the top-12 features.
10. **Calibration by segment, confidence intervals, abstention** on out-of-distribution accounts.
11. **High-cardinality categorical handling** (target encoding, embeddings): we drop columns with > 50 distinct values (industry codes, regions, plan SKUs).
12. **Sample weighting, monotonic constraints, fairness reporting.**

### B2B data model
13. **Account hierarchy:** parent/child accounts, many users per account, seat-level engagement, champion/economic-buyer tracking, "key user left" signals.
14. **Renewal-centric workflow:** renewal calendar, risk by renewal date, QBR prep.
15. **Reporting:** GRR/NRR, logo vs revenue churn, forecasts of next-quarter churned ARR.

### Product and workflow
16. **Connectors and scheduled scoring**; today users upload CSVs by hand.
17. **Ownership and workflow:** account owners, queues by CSM, SLAs, snooze, deduplicated alerts across runs, notifications to Slack/email.
18. **Recommendations are keyword rules** (`service.ACTION_RULES` matches substrings of feature names). Top systems use reason codes tied to configurable playbooks and learn which action works (uplift).
19. **"Health score" is `100 − probability`.** Top tools offer configurable, explainable component scores that CSMs trust and tune.
20. **Impact measurement:** revenue saved, treated-vs-untreated comparison, experiments with holdouts.
21. **Public API, SDK, webhooks.**

### Platform, security, operations
22. **Pickled model artifacts** (unsafe to load, not portable); no signing or version checks.
23. **No migrations:** schema created with `create_all`; any column change breaks existing databases.
24. **No row-level security, per-tenant keys, SSO/MFA/RBAC, immutable audit, or data-deletion flow**, all essential for highly sensitive data.
25. **Jobs live in-process:** a restart loses running training; no retries, cancellation, or per-job resource limits.
26. **Rate limiting, caches and job queue are per-process** and will not work behind a load balancer.
27. **No tracing, error tracking, SLOs, backups or restore drills.**
28. **Wide `customers` rows** (attributes and reasons in every row): 13 s to write 100k scores and 3 s aggregates before caching.
29. **Explanations are CPU-bound in the web process** (3 s p95 under concurrency).

### UX
30. Metric explanations for non-data-scientists, onboarding, mobile layout (the sidebar fills the phone screen), accessibility audit, undo for destructive actions, side-by-side model comparison.

## 10. Revised phase order (given the decisions)

**Phase 0: Security, correctness, verification (3–4 weeks).** Fix the code bugs from §9 first: group-aware split and CV (#1), schema contract at scoring (#4), version-stamped safe artifacts (E1/A7). Then Postgres row-level security + tenant-isolation suite (A5), migrations (F1), SSO/MFA/RBAC (C9), envelope encryption design (per-tenant keys, KMS), immutable audit log, data-deletion flow (E4), PII controls (E3), threat model and scanning (E2), CI matrix and Docker/Postgres in CI (A1–A2).
*Exit:* a pen-test-ready baseline; no known cross-tenant path; metrics unaffected by duplicate customers.

**Phase 1: Scalable, reliable platform (3–5 weeks). STATUS: built and tested locally; cloud pieces unverified (see docs/BENCHMARKS.md, Phase 1).** D2 queue and workers, D3 Redis, D7 object storage and direct uploads, D5 narrow score table, D4/B7 TreeSHAP and precomputed reasons, F2–F5, multi-worker load test (D1), Kubernetes with autoscaling, multi-AZ database, DR with RPO/RTO targets.
*Exit:* training survives restarts; horizontal scaling demonstrated; restore drill passed.

**Phase 2: Seats, quotas, metering (2–3 weeks, can overlap Phase 1). STATUS: built and tested (api/churn/quotas.py, tests/test_phase2_quotas.py).** As in §6. Built: plans (trial/team/business/enterprise; the numbers are placeholders, not prices), seat limits counting members plus pending invitations, invitation list/revoke, monthly counters for accounts scored, training runs and API calls, hard limits for runs/scored accounts/upload size and a soft limit for API calls, 402 responses carrying the figures, `/api/usage` and CSV export, platform-admin plan changes (audited), usage card in the Admin page. **Not built:** invoicing/Stripe, self-serve upgrade, per-seat role pricing, quotas for connector syncs, retention days and LLM tokens (those metrics arrive with Phases 3 and 6). The seat definition (a person with an active membership) is still an assumption awaiting confirmation. The Admin-page usage card compiles but was not viewed in a browser (the local app requires a real Firebase sign-in).

**Phase 3: B2B data in (6–8 weeks). STATUS: built and tested locally against a synthetic B2B simulator and local stand-in servers; no vendor connector has run against a real service (see docs/ARCHITECTURE.md, B2B pipeline).** Connectors (Salesforce/HubSpot, Segment/Amplitude, Zendesk, Stripe, warehouse), account hierarchy and seat-level features, target-definition wizard (B2), event feature builder with point-in-time joins (B1), scheduled scoring, Slack/email alerts (C2).
*Exit:* a workspace connects, scores nightly, and alerts an owner with no manual upload.

**Phase 4: MLOps loop (4–6 weeks).** §8: label maturation, realized-performance monitoring, retraining pipeline, shadow evaluation, guarded promotion, rolling-origin backtests (B9/B10).
*Exit:* a model retrains and promotes itself safely on a design-partner or simulated workspace, with every step audited.

**Phase 5: B2B modelling and decisions (6–10 weeks).** Renewal-centric and revenue-churn models (contraction, CLV), multi-horizon survival (B3), reason codes → playbooks (C3), impact dashboard (C4), experiments with holdouts (C7), uplift (B4) when partner action data exists.

**Phase 6: LLM features (4–6 weeks).** §6 guardrails; start with ticket features and account summaries.

**Always-on:** UX/accessibility/mobile (G), benchmark expansion (H), cost and capacity reviews (D10).
