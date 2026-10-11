# ChurnSight architecture

## System

```
Browser (Vite SPA, vanilla JS)
   │  Firebase ID token            JSON over HTTPS
   ▼
Flask API  (api/index.py)  ── auth → workspace → rate limit → handler
   │
   ├─ churn/service.py   scoring, summaries, alerts, interventions, drift
   ├─ churn/training.py  schema → selection → calibration → evaluation → explanations
   ├─ churn/registry.py  versioned models on disk, champion/challenger in the DB
   ├─ churn/jobs.py      background jobs with persisted progress
   └─ churn/db.py        SQLAlchemy Core (SQLite by default, Postgres via DATABASE_URL)
```

* **Tenancy.** Every row and artifact is keyed by `workspace_id`, derived from the Firebase user (`WORKSPACE_MODE=user|domain|shared`). Two accounts never see each other's models, customers, alerts or settings.
* **Jobs.** Training and batch scoring run as jobs: the API returns a job id immediately and the UI polls `/api/jobs/<id>` for real stage/percent progress. With threads (local, container) they run on a small pool; on serverless (`SYNC_JOBS`) they run inline because threads don't outlive the response.
* **Data path.** The full customer list is never sent to the browser. `/api/customers` pages, sorts and filters in SQL; `/api/results` carries aggregates, charts data and the 50 riskiest accounts.
* **Registry.** Each training run is a version: `models/<workspace>/<id>/model.joblib` plus a scored snapshot. The champion serves predictions; challengers can be promoted or rolled back (the snapshot restores that version's scores). Retraining on *identical* data with a worse PR-AUC is kept as a challenger; a different dataset always goes live.
* **Monitoring.** `POST /api/score` scores a fresh batch with the champion and stores PSI per feature plus score-distribution PSI against the training reference.
* **Closed loop.** Alerts and logged interventions (action, outcome) are persisted. They are the labelled treatment data that a future uplift model needs.
* **Security.** Fail-closed auth in production, per-user rate limits (stricter for training), upload size/row caps, CSV formula-injection neutralised on export, security headers, request ids and structured logs, optional `/metrics`.

## Security architecture

* **Identity and roles.** Firebase proves who you are (password, SAML/OIDC SSO, MFA). *We* decide what you may do: workspaces, memberships and four roles (`viewer < analyst < admin < owner`) live in our database and are enforced per route. Invitations are single-use, expiring, hashed at rest and bound to the invited email. SSO and MFA can be required per workspace and per role.
* **Tenant isolation, three layers.** (1) The workspace comes from the verified token plus membership, never from a client-supplied id. (2) Every query is scoped. (3) On PostgreSQL, forced row-level security with a fail-closed default backs both up; the tenant is set per transaction from a context variable that is cleared after every request. Cross-tenant operations run in an explicit `system_context`.
* **Encryption.** Each workspace has its own data key, wrapped by a master key (environment or KMS). Model artifacts and scored snapshots are always encrypted; customer attributes and explanations are encrypted per field in production. Ciphertext is bound to its workspace and context, so swapping blobs fails. Deleting a workspace destroys its key (crypto-shredding), which makes every surviving copy unreadable.
* **No pickles.** Models are serialised with `skops` and every referenced type is checked against an allow-list before anything is built; libraries are version-stamped and incompatible versions are refused.
* **Tamper-evident audit.** Hash-chained, append-only (database triggers), verifiable, exportable.
* **Privacy by default.** Personal-data columns are detected and neither modelled nor stored; IDs can be pseudonymised; erasure and subject-access export are built in.
* **Schema discipline.** Alembic migrations, a drift test between models and migrations, and automatic adoption of pre-migration databases.

See [THREAT_MODEL.md](THREAT_MODEL.md) for the threats, the tests that cover each, and what remains unproven.

## Model

| Decision | Why |
|---|---|
| Schema inference instead of a fixed schema | Any churn CSV works; IDs, dates, numeric/categorical, junk and leakage suspects are detected and shown before training. |
| Gradient boosting vs logistic regression vs random forest, chosen on 3-fold CV **PR-AUC** | Churn is imbalanced; accuracy rewards predicting "stays". The baseline keeps the boosted model honest. |
| Probability calibration (Platt below 8k rows, isotonic above) and clipping to [0.2%, 99.5%] | Cost-based decisions need probabilities that mean what they say; isotonic on small data produces literal 0%/100%. |
| Time-based holdout when a snapshot date exists | Mirrors production (train on the past, predict the future); random splits flatter the model. |
| Out-of-fold scores for the customer list | A model scoring its own training rows looks perfect and is useless. |
| Permutation importance + per-customer "set to typical" impacts | Model-agnostic and works for calibrated ensembles; no extra dependency. |
| Economics → thresholds | `P(contact is worth it) = outreach_cost / (save_rate × revenue × horizon)`; tiers derive from it and the base rate. |

### Why not deep learning or a GenAI model?
On tabular customer snapshots of this size, gradient-boosted trees match or beat neural networks, train in seconds, calibrate well and explain themselves. Deep sequence models (LSTM/Transformer) earn their keep with raw event streams at millions of rows (e.g. KKBox); the right next step there is a feature store and rolling-window features, which the demo generator already imitates. An LLM would help write retention messages, not predict churn.

## Running it for the public

| Concern | What the code does |
|---|---|
| One huge upload starving everyone | `plan_for(n)` picks the work by data size: small data gets the full zoo plus tuning, 60k+ rows drop the forest, 150k+ rows use linear + boosted only on a 30k selection sample. A wall-clock budget (`TRAIN_TIME_BUDGET_S`) stops adding candidates. |
| Job pile-ups | At most `MAX_CONCURRENT_JOBS` run at once; one active job per workspace (409); a bounded queue (`MAX_QUEUED_JOBS`, then 503 + `Retry-After`); queue position is shown in the UI; finished jobs expire after `JOB_RETENTION_DAYS`. |
| Big tables | Paging, sorting, filtering are SQL with composite indexes; per-tier counts are cached so paging never runs `COUNT(*)`; `OFFSET` is capped (`MAX_OFFSET`); exports use keyset pagination (O(n), not O(n²)); on workspaces over 100k customers search is ID-prefix only because substring scans do not scale. |
| Per-customer storage | Raw attributes are kept for every customer up to `ATTRS_MAX_ROWS` and, above that, for the riskiest `ATTRS_TOP_ROWS`. Scores, tiers and alerts always cover everyone. |
| Repeated dashboard loads | The results payload is cached for a few seconds, keyed by a revision stored in the database, so it invalidates across processes. |
| Disk growth | Only `KEEP_MODELS` versions per workspace are retained; each version's scored snapshot goes with it. |
| Abuse | Auth required, per-user rate limits (stricter for heavy endpoints), upload byte and row caps, formula-safe exports. |

Still single-node by design: rate limiting is in-process (put Redis behind several instances), jobs run in-process (move to a worker queue such as RQ/Celery when you outgrow one box), and SQLite should give way to Postgres (`DATABASE_URL`) for anything shared.

## More than one way to look at churn

* **Predictive model** (calibrated, tuned boosting chosen against linear and forest baselines) for "who, how likely".
* **Survival analysis** (Kaplan–Meier by tenure, split by contract-like attributes) for "when, and how long do customers last".
* **Personas** (k-means on behaviour, ignoring the label) for "what kinds of customers do we have, and which are fragile".
* **Cohorts** (observed churn by group × tenure band) for "where is it actually happening".
* **Drift** (PSI) for "has the world changed since we trained".

## What is deliberately not here yet
Event-level ingestion and a feature store, survival (time-to-churn) and uplift models (the intervention log exists to enable them), SSO/SAML and fine-grained roles, shared rate limiting (Redis) behind multiple instances, a Playwright end-to-end suite, and warehouse/CRM connectors.
