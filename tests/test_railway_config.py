"""Railway / hosted-URL helpers (no network)."""
from __future__ import annotations

import base64
import json
import os

import pytest


def test_normalize_database_url_railway_styles():
    from churn.config import normalize_database_url

    assert normalize_database_url('postgres://u:p@h:5432/db') == 'postgresql+psycopg://u:p@h:5432/db'
    assert normalize_database_url('postgresql://u:p@h:5432/db') == 'postgresql+psycopg://u:p@h:5432/db'
    assert normalize_database_url('postgresql+psycopg://u:p@h:5432/db') == 'postgresql+psycopg://u:p@h:5432/db'
    assert normalize_database_url('sqlite:///tmp/x.db') == 'sqlite:///tmp/x.db'


def test_database_url_reads_and_normalises(monkeypatch, tmp_path):
    from churn import config

    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('DATABASE_URL', 'postgres://u:p@h:5432/db')
    assert config.database_url() == 'postgresql+psycopg://u:p@h:5432/db'


def test_firebase_service_account_json_and_base64(monkeypatch):
    from churn import firebase_creds

    info = {
        'type': 'service_account',
        'project_id': 'demo-proj',
        'private_key_id': 'x',
        'private_key': '-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n',
        'client_email': 'x@demo-proj.iam.gserviceaccount.com',
        'client_id': '1',
        'token_uri': 'https://oauth2.googleapis.com/token',
    }
    monkeypatch.delenv('FIREBASE_PROJECT_ID', raising=False)
    monkeypatch.setenv('FIREBASE_SERVICE_ACCOUNT_JSON', json.dumps(info))
    assert firebase_creds.service_account_info()['project_id'] == 'demo-proj'
    assert os.environ.get('FIREBASE_PROJECT_ID') == 'demo-proj'

    monkeypatch.delenv('FIREBASE_PROJECT_ID', raising=False)
    monkeypatch.setenv('FIREBASE_SERVICE_ACCOUNT_JSON', base64.b64encode(json.dumps(info).encode()).decode())
    assert firebase_creds.service_account_info()['project_id'] == 'demo-proj'


def test_railway_sets_production(monkeypatch):
    monkeypatch.setenv('RAILWAY_ENVIRONMENT', 'production')
    monkeypatch.delenv('VERCEL', raising=False)
    monkeypatch.delenv('FLASK_ENV', raising=False)
    monkeypatch.delenv('AUTH_FAIL_CLOSED', raising=False)
    import importlib
    from churn import config
    importlib.reload(config)
    assert config.ON_RAILWAY
    assert config.PRODUCTION
    # Restore development defaults used by the rest of the suite.
    monkeypatch.delenv('RAILWAY_ENVIRONMENT', raising=False)
    monkeypatch.setenv('FLASK_ENV', 'development')
    importlib.reload(config)
