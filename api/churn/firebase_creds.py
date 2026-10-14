"""Load Firebase Admin credentials from env (Railway-friendly) or ADC file path."""
from __future__ import annotations

import base64
import json
import os
from typing import Any


def service_account_info() -> dict[str, Any] | None:
    """Parse FIREBASE_SERVICE_ACCOUNT_JSON (raw JSON or base64) if set."""
    raw = (os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON') or os.environ.get('FIREBASE_CREDENTIALS') or '').strip()
    if not raw:
        return None
    if not raw.startswith('{'):
        raw = base64.b64decode(raw).decode('utf-8')
    info = json.loads(raw)
    if not isinstance(info, dict) or info.get('type') != 'service_account':
        raise ValueError('FIREBASE_SERVICE_ACCOUNT_JSON must be a Firebase service-account JSON object')
    if info.get('project_id') and not os.environ.get('FIREBASE_PROJECT_ID'):
        os.environ['FIREBASE_PROJECT_ID'] = str(info['project_id'])
    return info


def credential():
    """Return a firebase_admin.credentials.Certificate, or None to use application default credentials."""
    from firebase_admin import credentials

    info = service_account_info()
    if info is not None:
        return credentials.Certificate(info)
    path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if path and os.path.isfile(path):
        return credentials.Certificate(path)
    return None


def credentials_configured() -> bool:
    try:
        if service_account_info() is not None:
            return True
    except Exception:  # noqa: BLE001
        return False
    if os.environ.get('FIREBASE_CONFIG') or os.environ.get('FIREBASE_PROJECT_ID'):
        return True
    path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    return bool(path and os.path.isfile(path))
