"""Runtime configuration, read from the environment once."""
from __future__ import annotations

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


ON_VERCEL = os.environ.get('VERCEL') == '1'
PRODUCTION = ON_VERCEL or os.environ.get('FLASK_ENV') == 'production' or _flag('AUTH_FAIL_CLOSED')
# Encrypt per-customer attributes and explanations at rest (breaks substring search over attributes, by design).
ENCRYPT_FIELDS = _flag('ENCRYPT_FIELDS', PRODUCTION)


def data_dir() -> str:
    """Local .data by default; /tmp on Vercel (ephemeral). Override with DATA_DIR."""
    path = os.environ.get('DATA_DIR') or ('/tmp/churnsight' if ON_VERCEL else os.path.join(BASE_DIR, '.data'))
    os.makedirs(path, exist_ok=True)
    return path


def database_url() -> str:
    return os.environ.get('DATABASE_URL') or f"sqlite:///{os.path.join(data_dir(), 'churnsight.db').replace(os.sep, '/')}"


# Uploads
MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', '25'))
MAX_ROWS = int(os.environ.get('MAX_ROWS', '250000'))   # ~0.55 GB RSS at 100k rows; size this to the host

# Jobs run in a worker thread by default. Serverless platforms freeze threads once the
# response is sent, so there they run inline (set SYNC_JOBS=0 to force threads).
SYNC_JOBS = _flag('SYNC_JOBS', ON_VERCEL)

# 'user' (one workspace per account), 'domain' (per email domain) or 'shared' (everyone).
WORKSPACE_MODE = os.environ.get('WORKSPACE_MODE', 'user')

RATE_LIMIT_PER_MIN = int(os.environ.get('RATE_LIMIT_PER_MIN', '240'))
HEAVY_RATE_LIMIT_PER_MIN = int(os.environ.get('HEAVY_RATE_LIMIT_PER_MIN', '12'))

# ── Scale controls (public hosting) ────────────────────────────────────────────
MAX_CONCURRENT_JOBS = int(os.environ.get('MAX_CONCURRENT_JOBS', '2'))      # training/scoring jobs running at once
MAX_QUEUED_JOBS = int(os.environ.get('MAX_QUEUED_JOBS', '20'))             # beyond this, new jobs are refused (503)
JOB_RETENTION_DAYS = int(os.environ.get('JOB_RETENTION_DAYS', '7'))
KEEP_MODELS = int(os.environ.get('KEEP_MODELS', '5'))                      # model versions kept per workspace
TRAIN_TIME_BUDGET_S = int(os.environ.get('TRAIN_TIME_BUDGET_S', '900'))    # stop adding candidates after this long
ATTRS_MAX_ROWS = int(os.environ.get('ATTRS_MAX_ROWS', '150000'))           # keep per-customer attributes for all rows up to this
ATTRS_TOP_ROWS = int(os.environ.get('ATTRS_TOP_ROWS', '50000'))            # ...above it, only for the riskiest rows
MAX_OFFSET = int(os.environ.get('MAX_OFFSET', '50000'))                    # deepest page reachable by OFFSET paging
SEARCH_SCAN_MAX = int(os.environ.get('SEARCH_SCAN_MAX', '30000'))             # above this many customers, search is ID-prefix only
MAX_CONCURRENT_EXPLAIN = int(os.environ.get('MAX_CONCURRENT_EXPLAIN', '3'))   # CPU-heavy inference requests running at once
EXPLAIN_TOP_FEATURES = int(os.environ.get('EXPLAIN_TOP_FEATURES', '12'))     # features perturbed per explanation
RESULTS_CACHE_TTL_S = int(os.environ.get('RESULTS_CACHE_TTL_S', '30'))
DB_POOL_SIZE = int(os.environ.get('DB_POOL_SIZE', '10'))

RANDOM_STATE = 42
