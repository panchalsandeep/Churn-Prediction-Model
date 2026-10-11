# ChurnSight — Churn Prediction Dashboard

AI-assisted churn prediction for customer success teams: upload a labeled CSV, train a model, explore risk analytics, and score individual customers.

## Stack

- **Frontend:** Vite + vanilla JS (`index.html`, `src/main.js`, helpers in `src/lib/`), Chart.js and Firebase Auth (both lazy-loaded)
- **Backend:** Flask + scikit-learn (`api/index.py`)
- **Deploy:** Vercel (static build → `dist/`, Python serverless for `/api/*`)

The legacy `static/app.js` SPA is retired. Use the Vite entrypoint only.

## Features

- **Light/dark theme** (toggle in the top bar or press `d`); remembers your choice and follows the OS until you pick one.
- **Command palette** (`Ctrl/Cmd+K`): jump to any page, run actions, or find a customer by ID. Press `?` for all shortcuts (`g` then `o/h/c/a/n/t/p` navigates, `/` focuses search).
- **Customer table**: sortable columns, debounced search (ID, location, contract, payment), page-size picker, multi-select with floating bulk export, skeleton loading.
- **What-if simulator** (Predict Single): drag levers to re-score a customer live and see which features push risk up or down. Also reachable from a customer's detail drawer via *Simulate what-if*.
- **Alert workflow**: Acknowledge, Resolve and Assign actually move alerts between tabs.
- **Motion**: section/stagger transitions, sliding nav indicator, count-ups, animated gauge and charts, slide-over drawer. All of it respects `prefers-reduced-motion`.

## Quick start (local)

```bash
# Terminal 1 — API
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
npm run api

# Terminal 2 — frontend
npm install
npm run dev
```

Open the Vite URL (default `http://localhost:5173`). The dev server proxies `/api` to `http://localhost:5050`.

Copy `.env.example` to `.env` if you need to override Firebase web config.

## Sample data

`sample_data.csv` (also copied to `public/` for the Vite build) includes the required columns:

`customer_id, age, gender, location, tenure_months, monthly_charges, total_charges, num_products, num_logins_last30, support_tickets, last_login_days, contract_type, payment_method, churn`

## API overview

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Liveness + auth/model status |
| POST | `/api/upload` | Train model from CSV |
| POST | `/api/predict` | Score one customer (`"explain": true` adds per-feature impact vs a typical customer) |
| GET | `/api/results` | Last training payload |
| GET | `/api/customers` | Paginated risk list |
| GET/POST | `/api/admin/settings` | Risk thresholds & alert rules |
| GET | `/api/admin/model` | Model session status |

Reported metrics (`accuracy`, ROC, etc.) come from a **holdout test set**. Customer risk scores cover the full uploaded file.

## Auth behavior

- **Local/dev:** if Firebase Admin is not configured, API auth is bypassed so you can develop without credentials.
- **Production / Vercel:** auth **fails closed** (HTTP 503) until Firebase Admin credentials are configured. Set `ADMIN_EMAILS` for admin console access.

## Performance notes

- Chart.js and Firebase are code-split and loaded on demand; charts are built the first time their page is opened.
- API JSON is gzipped and carries ETags, so repeat `GET /api/results` calls revalidate with a 304.
- Hashed `/assets/*` are served with a one-year immutable cache header on Vercel.

## Persistence

Training artifacts are written under `.data/` locally (or `/tmp` on Vercel):

- `churn_model.pkl`
- `training_results.json`
- `admin_settings.json`

On Vercel, `/tmp` is **per-instance**. Same-instance restore works; for durable multi-instance storage, move these to object storage / a database.

## Regenerating the bundled model

```bash
pip install -r requirements.txt
npm run retrain-model
```

Keep `scikit-learn` pinned in `requirements.txt` in sync with the environment that writes `api/churn_model.pkl`.

## Tests

```bash
pip install -r requirements.txt
npm run test:api
```

## Deploy (Vercel)

1. Set env vars from `.env.example` (`FIREBASE_PROJECT_ID`, service account / `GOOGLE_APPLICATION_CREDENTIALS`, `ADMIN_EMAILS`).
2. Build uses `npm run build` → `dist/`; API routes to `api/index.py`.
