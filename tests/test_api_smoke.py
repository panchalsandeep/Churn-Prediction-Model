"""Smoke tests for ChurnSight API (dev auth bypass)."""
from __future__ import annotations

import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))

# Ensure local/dev fail-open so tests do not need Firebase
os.environ.pop('VERCEL', None)
os.environ.pop('AUTH_FAIL_CLOSED', None)
os.environ.pop('AUTH_REQUIRED', None)
os.environ['FLASK_ENV'] = 'development'

from index import app, classify_risk, ADMIN_SETTINGS  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('FLASK_ENV', 'development')
    # Point persistence at a temp directory
    import index as api

    monkeypatch.setattr(api, 'WRITABLE_DIR', str(tmp_path))
    monkeypatch.setattr(api, 'MODEL_PATH', str(tmp_path / 'churn_model.pkl'))
    monkeypatch.setattr(api, 'RESULTS_PATH', str(tmp_path / 'training_results.json'))
    monkeypatch.setattr(api, 'ADMIN_SETTINGS_FILE', str(tmp_path / 'admin_settings.json'))
    api.trained_model = None
    api.training_results = None
    api.customer_predictions = None
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def test_health(client):
    res = client.get('/api/health')
    assert res.status_code == 200
    body = res.get_json()
    assert body['ok'] is True
    assert body['production'] is False


def test_upload_predict_persist_roundtrip(client):
    csv_path = os.path.join(ROOT, 'sample_data.csv')
    with open(csv_path, 'rb') as f:
        data = {'file': (io.BytesIO(f.read()), 'sample_data.csv'), 'algorithm': 'random_forest'}
        res = client.post('/api/upload', data=data, content_type='multipart/form-data')
    assert res.status_code == 200, res.get_json()
    payload = res.get_json()
    assert payload['metrics_source'] == 'holdout_test_set'
    assert 'test_size' in payload['summary']
    customer = payload['customers'][0]
    assert 'contract_type' in customer
    assert 'location' in customer
    assert 'payment_method' in customer

    # Simulate cold instance: clear memory, reload from disk
    import index as api
    api.trained_model = None
    api.training_results = None
    api.customer_predictions = None

    results = client.get('/api/results')
    assert results.status_code == 200
    assert results.get_json()['summary']['total_customers'] == payload['summary']['total_customers']

    pred = client.post('/api/predict', json={
        'age': 40,
        'tenure_months': 6,
        'monthly_charges': 90,
        'total_charges': 540,
        'num_products': 1,
        'num_logins_last30': 2,
        'support_tickets': 5,
        'last_login_days': 30,
        'gender': 'Female',
        'location': 'New York',
        'contract_type': 'Month-to-month',
        'payment_method': 'Electronic check',
    })
    assert pred.status_code == 200
    body = pred.get_json()
    assert 'churn_probability' in body
    assert body['risk_level'] in ('High', 'Medium', 'Low')


def test_risk_thresholds_applied():
    ADMIN_SETTINGS['risk_thresholds']['high'] = 0.9
    ADMIN_SETTINGS['risk_thresholds']['medium'] = 0.5
    assert classify_risk(0.95) == 'High'
    assert classify_risk(0.7) == 'Medium'
    assert classify_risk(0.2) == 'Low'
    # restore defaults for other tests
    ADMIN_SETTINGS['risk_thresholds']['high'] = 0.7
    ADMIN_SETTINGS['risk_thresholds']['medium'] = 0.4


def test_auth_fail_closed_in_production(client, monkeypatch):
    import index as api

    monkeypatch.setenv('AUTH_FAIL_CLOSED', 'true')
    monkeypatch.setattr(api, 'firebase_initialized', False)
    res = client.get('/api/results')
    assert res.status_code == 503


SCENARIO = {
    'age': 40, 'tenure_months': 6, 'monthly_charges': 90, 'total_charges': 540,
    'num_products': 1, 'num_logins_last30': 2, 'support_tickets': 5,
    'last_login_days': 30, 'gender': 'Female', 'location': 'New York',
    'contract_type': 'Month-to-month', 'payment_method': 'Electronic check',
}


def test_predict_explain_returns_ranked_impacts(client):
    plain = client.post('/api/predict', json=SCENARIO)
    assert plain.status_code == 200
    assert 'impacts' not in plain.get_json()

    res = client.post('/api/predict', json={**SCENARIO, 'explain': True})
    assert res.status_code == 200
    impacts = res.get_json()['impacts']
    assert len(impacts) == 12
    magnitudes = [abs(i['impact']) for i in impacts]
    assert magnitudes == sorted(magnitudes, reverse=True)
    assert {'feature', 'value', 'baseline', 'impact'} <= set(impacts[0])


def test_results_are_gzipped_and_revalidate_with_etag(client):
    with open(os.path.join(ROOT, 'sample_data.csv'), 'rb') as f:
        up = client.post('/api/upload', data={'file': (io.BytesIO(f.read()), 's.csv')},
                         content_type='multipart/form-data')
    assert up.status_code == 200
    res = client.get('/api/results', headers={'Accept-Encoding': 'gzip'})
    assert res.status_code == 200
    assert res.headers.get('Content-Encoding') == 'gzip'
    etag = res.headers.get('ETag')
    assert etag
    again = client.get('/api/results', headers={'If-None-Match': etag})
    assert again.status_code == 304
