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
| GET · POST | `/api/jobs/<id>` · `/api/jobs/<id>/cancel` | Job stage and progress; cancel (admin). Send `Idempotency-Key` on POST to de-duplicate retries |
| POST · GET · DELETE | `/api/data/<table>` · `/api/pipeline` · `/api/data` | B2B tables (accounts, events, tickets, invoices): upload CSV, pipeline status, drop tables (owner) |
| POST | `/api/pipeline/target` · `/api/pipeline/run` | Preview or save the churn definition; train from the tables and score today |
| PUT | `/api/pipeline/schedule` | Score automatically every N hours |
| POST | `/api/ingest/<table>` | Push rows from your systems with an ingest API key (`Authorization: Bearer cs_live_…`) |
| GET · POST · DELETE | `/api/api-keys` · `/api/sources` · `/api/channels` | Ingest keys, pull sources (JSON/CSV over HTTP), alert channels (Slack, signed webhook, email) |
| GET · PUT · POST | `/api/mlops` · `/api/mlops/policy` · `/api/mlops/check` · `/api/mlops/backtest` · `/api/mlops/promote` · `/api/mlops/rollback` | Model health from realized outcomes, retraining policy, shadow challenger, backtest, promote and roll back |
| GET · POST | `/api/runs` · `/api/runs/<id>/outcomes` | Scoring-run log; upload what actually happened (customer_id, churned) to grade a run |
| GET | `/api/usage` · `/api/usage/export` | Plan, seats, monthly usage against limits (admin); usage CSV for billing |
| GET · DELETE | `/api/invites` · `/api/invites/<id>` | Pending invitations; revoke one (frees its seat) |
| PUT | `/api/platform/workspaces/<id>/plan` | Platform admin only: set a workspace's plan |
| GET | `/api/health` · `/api/ready` · `/metrics` | Liveness; readiness (db, schema, storage, keys); Prometheus (needs `METRICS_TOKEN` as a Bearer header) |
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

## Auth, roles and tenancy

- **Roles per workspace:** `viewer` (read) < `analyst` (+ export, work alerts, log actions) < `admin` (+ train, score, settings, manage people) < `owner` (+ admins, SSO, delete workspace). New people join by **single-use invitation**; platform administrators (`ADMIN_EMAILS`) provision workspaces.
- **MFA and SSO:** admin/owner require a second factor in production (`REQUIRE_MFA_ROLES`); a workspace can require SAML/OIDC single sign-on. Both are configured in Firebase/Identity Platform and enforced here.
- **Encryption and privacy:** per-workspace keys, encrypted artifacts and fields, personal-data columns excluded and never stored, optional ID pseudonymisation, erasure and subject-access export, workspace deletion with key destruction. **Production needs `CHURN_MASTER_KEY`** (or KMS).
- **Audit:** hash-chained and append-only; view and export it under Settings.
- Details and residual risks: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). Plan: [docs/ROADMAP.md](docs/ROADMAP.md).

### Auth behaviour

- **Local/dev:** without Firebase Admin credentials, auth is bypassed and everything runs in the `local` workspace.
- **Production:** auth **fails closed** (HTTP 503) until Firebase Admin is configured. Set `ADMIN_EMAILS` for admin features.
- **Account emails:** with `SMTP_HOST` (and `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `APP_URL`) set, ChurnSight sends its own branded emails for workspace invitations, password reset, email verification and welcome (`api/churn/mailer.py`). Without SMTP the app falls back to Firebase's default reset and verification emails, and invitations are shared as links.
- **Admin console (`/admin`, optional):** username + password from `ADMIN_USERNAME` / `ADMIN_PASSWORD`, sessions signed with `ADMIN_TOKEN_SECRET` (32+ characters) and valid for 8 hours. Grants the platform-administrator routes (Firebase user management, workspace provisioning, plans) without a Firebase account. Off unless both variables are set; login is rate-limited and audited under the `_platform` audit chain.
- **Workspaces** are per account by default (`WORKSPACE_MODE=user`); `domain` shares by company email domain, `shared` is one workspace for everyone.

## Configuration

See [.env.example](.env.example): `DATABASE_URL` (SQLite by default, Postgres for anything shared), `DATA_DIR`, upload and rate limits, `CORS_ORIGINS`, `METRICS_TOKEN`, and for scale-out `EMBEDDED_WORKER`, `REDIS_URL`, `STORAGE_BACKEND`/`S3_BUCKET`, `SENTRY_DSN`.

## Run it as a service

```bash
docker compose up --build        # app + worker + Postgres + Redis at http://localhost:8080
```

To run workers on their own (any number, same database and blob storage): `EMBEDDED_WORKER=0` on the web processes and `python -m churn.worker` for the workers. Back up with `python -m churn.backup backup --out DIR` (the master key is deliberately not included). Kubernetes reference manifests are in `deploy/k8s/`; they and the compose file have not been run.

The image builds the frontend and serves it with the API behind gunicorn. `FLASK_ENV=production` makes auth fail closed, so provide Firebase Admin credentials (see the comments in `docker-compose.yml`) or drop that variable for a local trial.

**Railway:** connect the repo (Dockerfile + `railway.toml`). Add a Postgres plugin, mount a volume at `/data` (or set `STORAGE_BACKEND=s3`), and set `CHURN_MASTER_KEY`, `FIREBASE_SERVICE_ACCOUNT_JSON` (service-account JSON or base64), and `ADMIN_EMAILS`. `DATABASE_URL` from the plugin is accepted as-is (`postgres://` is normalised). Production mode is detected via `RAILWAY_ENVIRONMENT`.

**Vercel** still works for the frontend and light API use, but sklearn + pandas + firebase-admin is close to the function size limit, `/tmp` is per-instance (so models do not persist reliably), and training runs inline. Use a container for real workloads.

## Benchmarks on real data

The shipped pipeline (`algorithm='auto'`, random stratified split except where noted) on public churn datasets downloaded with `scripts/download_datasets.py`. Full model-family comparison and methodology: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

| Dataset | Rows | Churn | ROC-AUC | PR-AUC (baseline) | Top-decile lift |
|---|---|---|---|---|---|
| Orange telecom | 3,333 | 14.5% | 94.1 | 90.8 (14.5) | 6.7× |
| Bank customers | 10,000 | 20.4% | 86.1 | 69.6 (20.4) | 4.0× |
| IBM Telco | 7,043 | 26.5% | 84.2 | 66.5 (26.5) | 2.8× |

## Model evaluation, reference models, bug reports

* `python scripts/eval.py [--check|--update-baseline]` records ROC-AUC, PR-AUC, Brier and lift per dataset against a no-skill and a logistic-regression floor, appends to `evals/history.jsonl` and gates CI on `evals/baseline.json` (see [docs/BENCHMARKS.md](docs/BENCHMARKS.md#evaluation-history-and-the-regression-gate)).
* `models/reference/` holds models trained by the shipped pipeline on the public telco, bank and orange datasets (safe skops format, model cards, test that they load). Re-create with `python scripts/export_reference_models.py`. Workspace models are different: they are encrypted per workspace, never committed, and versioned in the registry, where any version can be promoted or rolled back.
* The bug icon in the top bar captures the page (edges flash, a card slides in with the screenshot and a message box). Reports are stored encrypted per workspace and listed for admins at `GET /api/feedback`; set `FEEDBACK_WEBHOOK_URL` to forward text and metadata to the operator (the screenshot only with `FEEDBACK_FORWARD_SCREENSHOT=1`).

## Scale

Designed to be hosted publicly: size-aware training, a bounded job queue with per-workspace limits, SQL paging with cached counts, keyset exports, capped deep paging, results caching, retention limits. Details and the measured 100,000-row run are in [docs/BENCHMARKS.md](docs/BENCHMARKS.md#scale); `scripts/loadtest.py` and `scripts/scale_test.py` reproduce them.

## Tests and CI

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q        # ~8 min; includes the model-quality gate, security, RBAC and migration tests
npm run build
```

CI also runs the suite on PostgreSQL (row-level security, experimental until its first green run), scans dependencies and the image, and builds the container.

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs both on every push and pull request. The quality gate fails the build if ROC-AUC, PR-AUC lift, top-decile lift or calibration fall below fixed floors on generated data.

## Performance notes

- Chart.js and Firebase are code-split and loaded on demand; charts build when their page is first opened.
- The customer table is paged, sorted and filtered in SQL. API JSON is gzipped with ETags; hashed assets are immutable-cached.
- Training is a background job with real progress; scores come from out-of-fold predictions; the champion model is cached in memory per workspace.
