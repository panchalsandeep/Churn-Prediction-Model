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


def data_dir() -> str:
    """Local .data by default; /tmp on Vercel (ephemeral). Override with DATA_DIR."""
    path = os.environ.get('DATA_DIR') or ('/tmp/churnsight' if ON_VERCEL else os.path.join(BASE_DIR, '.data'))
    os.makedirs(path, exist_ok=True)
    return path


def database_url() -> str:
    return os.environ.get('DATABASE_URL') or f"sqlite:///{os.path.join(data_dir(), 'churnsight.db').replace(os.sep, '/')}"


# Uploads
MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', '25'))
MAX_ROWS = int(os.environ.get('MAX_ROWS', '500000'))

# Jobs run in a worker thread by default. Serverless platforms freeze threads once the
# response is sent, so there they run inline (set SYNC_JOBS=0 to force threads).
SYNC_JOBS = _flag('SYNC_JOBS', ON_VERCEL)

# 'user' (one workspace per account), 'domain' (per email domain) or 'shared' (everyone).
WORKSPACE_MODE = os.environ.get('WORKSPACE_MODE', 'user')

RATE_LIMIT_PER_MIN = int(os.environ.get('RATE_LIMIT_PER_MIN', '240'))
HEAVY_RATE_LIMIT_PER_MIN = int(os.environ.get('HEAVY_RATE_LIMIT_PER_MIN', '12'))

RANDOM_STATE = 42
