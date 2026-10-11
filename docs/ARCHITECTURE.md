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

## What is deliberately not here yet
Event-level ingestion and a feature store, survival (time-to-churn) and uplift models (the intervention log exists to enable them), SSO/SAML and fine-grained roles, shared rate limiting (Redis) behind multiple instances, a Playwright end-to-end suite, and warehouse/CRM connectors.
