# ChurnSight

Churn prediction for customer-success teams. Upload any churn CSV (or use the built-in demo), and ChurnSight detects the schema, compares models, calibrates probabilities, evaluates honestly, ranks accounts by **revenue at risk**, explains each score, and tracks what your team did about it.

Architecture and modelling decisions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What it does

- **Any CSV.** One requirement: a churn column (`0/1`, `Yes/No`, `Churned`…). IDs, dates, numeric/categorical features, junk columns and possible target leakage are detected and shown before training (Telco, bank and SaaS exports work as-is).
- **Honest modelling.** Gradient boosting vs logistic regression vs random forest, chosen on cross-validated **PR-AUC**; calibrated probabilities; time-based holdout when a snapshot date exists; out-of-fold scores for the customer list; bootstrap confidence intervals.
- **Business-aware.** Outreach cost, save rate and value horizon set the "worth contacting" threshold and the High/Medium/Low tiers. Customers rank by expected loss, not just probability.
- **Explainable.** Per-customer drivers ("why this score"), global permutation importance, recommended actions, and a **what-if simulator** that re-scores live.
- **Operational.** Persisted alerts (Open → Acknowledged → Resolved, assign, jump detection), a **closed-loop action log** with outcomes and save rates, batch scoring with **drift monitoring** (PSI), a **model registry** with promote/rollback, and an audit log.
- **UX.** Light/dark theme, command palette (`Ctrl/Cmd+K`), keyboard shortcuts (`?`), server-paged sortable table with bulk export, animated transitions that respect `prefers-reduced-motion`.

## Quick start

```bash
# API  (Python 3.12+)
python -m venv .venv && .venv\Scripts\activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
npm run api                                          # http://localhost:5050

# Frontend
npm install
npm run dev                                          # http://localhost:5173 (proxies /api)
```

Open the app and click **Try the demo dataset**: it trains on 8,000 synthetic SaaS customers in under a minute. Locally, with no Firebase credentials configured, API auth is bypassed so you can develop without an account (the login card still appears; the API does not require it).

`npm run demo-data` rewrites `public/sample_data.csv` (the "Download demo CSV" link).

## The data it expects

Anything tabular with a churn label. Best results come from behavioural features (rolling usage, tickets, payment failures) and a snapshot date for the time-based holdout; a revenue column (`monthly_charges`, `mrr`, …) enables revenue-at-risk. Public sets such as the IBM Telco and bank-churn CSVs map automatically. The demo generator ([api/churn/demo_data.py](api/churn/demo_data.py)) shows the shape.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/preview` | Show how a CSV will be interpreted |
| POST | `/api/upload` · `/api/demo` | Train (async job; `?wait=1` blocks) |
| POST | `/api/score` | Score a new batch with the champion, report drift |
| GET | `/api/jobs/<id>` | Job stage and progress |
| GET | `/api/results` | Metrics, charts data, cohorts, segments, summary (no customer list) |
| GET | `/api/customers` | Server-side paging: `tier, q, sort, dir, page, limit` |
| GET/POST | `/api/customers/export` | Streamed, formula-safe CSV (POST with `ids`) |
| GET | `/api/customers/<id>` | 360 view: reasons, actions, activity |
| GET · POST | `/api/model/schema` · `/api/predict` | Dynamic form fields; score one customer with explanations |
| GET · POST | `/api/settings` | Economics, thresholds, alert rules |
| GET · POST | `/api/models` · `/api/models/<id>/promote` | Registry |
| GET | `/api/monitoring/drift` | Latest batch drift |
| GET · PATCH | `/api/alerts` · `/api/alerts/<id>` | Alert workflow |
| POST · PATCH · GET | `/api/customers/<id>/interventions` · `/api/interventions/<id>` · `/api/interventions/summary` | Closed loop |
| GET · PATCH | `/api/admin/users` · `/api/admin/audit` | Admin only |

## Auth and tenancy

- **Local/dev:** without Firebase Admin credentials, auth is bypassed and everything runs in the `local` workspace.
- **Production:** auth **fails closed** (HTTP 503) until Firebase Admin is configured. Set `ADMIN_EMAILS` for admin features.
- **Workspaces** are per account by default (`WORKSPACE_MODE=user`); `domain` shares by company email domain, `shared` is one workspace for everyone.

## Configuration

See [.env.example](.env.example): `DATABASE_URL` (SQLite by default, Postgres for anything shared), `DATA_DIR`, upload and rate limits, `CORS_ORIGINS`, `METRICS_TOKEN`.

## Run it as a service

```bash
docker compose up --build        # app + Postgres at http://localhost:8080
```

The image builds the frontend and serves it with the API behind gunicorn. `FLASK_ENV=production` makes auth fail closed, so provide Firebase Admin credentials (see the comments in `docker-compose.yml`) or drop that variable for a local trial.

**Vercel** still works for the frontend and light API use, but sklearn + pandas + firebase-admin is close to the function size limit, `/tmp` is per-instance (so models do not persist reliably), and training runs inline. Use a container for real workloads.

## Tests and CI

```bash
python -m pytest tests -q        # ~3 min; includes the model-quality gate
npm run build
```

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs both on every push and pull request. The quality gate fails the build if ROC-AUC, PR-AUC lift, top-decile lift or calibration fall below fixed floors on generated data.

## Performance notes

- Chart.js and Firebase are code-split and loaded on demand; charts build when their page is first opened.
- The customer table is paged, sorted and filtered in SQL. API JSON is gzipped with ETags; hashed assets are immutable-cached.
- Training is a background job with real progress; scores come from out-of-fold predictions; the champion model is cached in memory per workspace.
