"""End-to-end API behaviour: upload → results → customers → predict → alerts → scoring → registry."""
from __future__ import annotations

import io

import pytest


def test_health(client):
    body = client.get('/api/health').get_json()
    assert body['ok'] and body['db'] and body['production'] is False


def test_no_model_returns_404_with_guidance(client, env):
    res = client.get('/api/results')
    assert res.status_code == 404 and 'demo' in res.get_json()['error'].lower()


def test_preview_describes_columns_before_training(client, demo_csv_bytes):
    res = client.post('/api/preview', data={'file': (io.BytesIO(demo_csv_bytes), 'd.csv')}, content_type='multipart/form-data')
    body = res.get_json()
    assert res.status_code == 200
    assert body['schema']['target'] == 'churn' and body['schema']['id'] == 'customer_id'
    assert body['schema']['time_col'] == 'snapshot_date' and body['schema']['signup_col'] == 'signup_date'
    assert body['rows'] == 3000 and len(body['sample']) == 5


def test_bad_uploads_are_rejected_cleanly(client):
    assert client.post('/api/upload', data={}, content_type='multipart/form-data').status_code == 400
    res = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(b'a,b\n1,2\n3,4\n'), 'x.csv')}, content_type='multipart/form-data')
    job = res.get_json()['job']
    assert job['status'] == 'error' and 'rows' in job['error'].lower()


def test_training_registers_a_champion(trained, client):
    assert trained['status'] == 'champion'
    models = client.get('/api/models').get_json()['models']
    assert len(models) == 1 and models[0]['status'] == 'champion' and models[0]['pr_auc'] > 0


def test_results_payload_shape(trained, client):
    r = client.get('/api/results').get_json()
    for key in ('metrics', 'lift', 'calibration', 'roc_curve', 'feature_importance', 'cohorts', 'segments', 'summary', 'thresholds',
                'economics', 'top_risk', 'schema', 'model', 'precision_at_k', 'confidence_intervals'):
        assert key in r, key
    assert r['summary']['total_customers'] == 3000
    assert 'customers' not in r                                   # the full list is paged, never shipped in one response
    assert r['cohorts']['signup']['rows'] and r['cohorts']['signup']['cols']
    assert r['thresholds']['medium'] <= r['thresholds']['high']


def test_customers_are_paged_sorted_and_filtered_server_side(trained, client):
    first = client.get('/api/customers?limit=10&sort=prob&dir=desc').get_json()
    assert first['total'] == 3000 and len(first['customers']) == 10 and first['pages'] == 300
    probs = [c['churn_prob'] for c in first['customers']]
    assert probs == sorted(probs, reverse=True)
    assert client.get('/api/customers?limit=10&sort=prob&dir=asc').get_json()['customers'][0]['churn_prob'] <= probs[0]
    high = client.get('/api/customers?tier=High&limit=5').get_json()
    assert high['total'] > 0 and all(c['risk_level'] == 'High' for c in high['customers'])
    search = client.get('/api/customers?q=CUST-000042').get_json()
    assert search['total'] == 1 and search['customers'][0]['id'] == 'CUST-000042'


def test_customer_detail_has_reasons_and_recommendations(trained, client):
    top = client.get('/api/customers?limit=1').get_json()['customers'][0]
    d = client.get(f"/api/customers/{top['id']}").get_json()
    assert d['reasons'] and d['recommendations'] and 'attrs' in d
    low = client.get('/api/customers?limit=1&sort=prob&dir=asc').get_json()['customers'][0]
    d2 = client.get(f"/api/customers/{low['id']}").get_json()          # beyond the pre-explained top-N: explained on demand
    assert d2['reasons']
    assert client.get('/api/customers/NOPE').status_code == 404


def test_export_is_streamed_and_formula_safe(trained, client):
    res = client.get('/api/customers/export?tier=High')
    text = res.get_data(as_text=True)
    assert res.mimetype == 'text/csv' and text.startswith('customer_id,risk_level')
    assert len(text.splitlines()) > 2
    sel = client.post('/api/customers/export', json={'ids': ['CUST-000001', 'CUST-000002']}).get_data(as_text=True)
    assert len(sel.strip().splitlines()) == 3


def test_form_schema_and_prediction_with_explanations(trained, client):
    form = client.get('/api/model/schema').get_json()
    assert any(f['kind'] == 'category' for f in form['fields']) and any(f['kind'] == 'number' for f in form['fields'])
    values = {f['name']: f['default'] for f in form['fields']}
    base = client.post('/api/predict', json={'values': values}).get_json()
    values['login_trend'], values['unresolved_tickets'], values['payment_failures_6m'] = 0.1, 4, 3
    risky = client.post('/api/predict', json={'values': values}).get_json()
    assert risky['churn_probability'] > base['churn_probability']
    assert risky['impacts'] and risky['recommendation'] and 'expected_loss' in risky
    assert client.post('/api/predict', json={}).status_code == 400


def test_settings_retier_customers_and_validate_input(trained, client):
    before = client.get('/api/results').get_json()['summary']['high_risk']
    res = client.post('/api/settings', json={'thresholds': {'mode': 'manual', 'high': 0.9, 'medium': 0.5}})
    assert res.status_code == 200
    after = client.get('/api/results').get_json()['summary']
    assert after['high_risk'] < before and after['thresholds']['mode'] == 'manual'
    assert client.post('/api/settings', json={'thresholds': {'mode': 'manual', 'high': 0.3, 'medium': 0.6}}).status_code == 400
    assert client.post('/api/settings', json={'economics': {'outreach_cost': 'abc'}}).status_code == 400
    client.post('/api/settings', json={'thresholds': {'mode': 'auto'}, 'economics': {'outreach_cost': 400}})
    assert client.get('/api/results').get_json()['economics']['outreach_cost'] == 400
    client.post('/api/settings', json={'economics': {'outreach_cost': 25}})


def test_alert_workflow_persists(trained, client):
    data = client.get('/api/alerts?status=Open').get_json()
    assert data['counts']['Open'] > 0 and len(data['alerts']) > 0
    a = data['alerts'][0]
    assert a['severity'] in ('High', 'Medium') and a['signals'] is not None
    assert client.patch(f"/api/alerts/{a['id']}", json={'status': 'Acknowledged', 'assignee': '@me'}).status_code == 200
    ack = client.get('/api/alerts?status=Acknowledged').get_json()
    assert any(x['id'] == a['id'] and x['assignee'] for x in ack['alerts'])
    assert client.patch('/api/alerts/missing', json={'status': 'Resolved'}).status_code == 404
    assert client.patch(f"/api/alerts/{a['id']}", json={}).status_code == 400


def test_interventions_close_the_loop(trained, client):
    cid = client.get('/api/customers?limit=1').get_json()['customers'][0]['id']
    res = client.post(f'/api/customers/{cid}/interventions', json={'kind': 'call', 'notes': 'Spoke with the champion.'})
    assert res.status_code == 201
    iid = res.get_json()['id']
    assert client.patch(f'/api/interventions/{iid}', json={'outcome': 'saved'}).status_code == 200
    assert client.patch(f'/api/interventions/{iid}', json={'outcome': 'bogus'}).status_code == 400
    summary = client.get('/api/interventions/summary').get_json()
    assert summary['saved'] == 1 and summary['save_rate'] == 100.0
    assert client.get(f'/api/customers/{cid}').get_json()['interventions'][0]['outcome'] == 'saved'
    assert client.post('/api/customers/NOPE/interventions', json={'kind': 'call'}).status_code == 404


def test_batch_scoring_reports_drift_and_risk_jumps(trained, client):
    import pandas as pd
    from churn.demo_data import generate_demo
    batch = generate_demo(1500, seed=99)
    batch['logins_30d'] = (batch['logins_30d'] * 0.3).round()       # simulate a usage collapse
    batch['login_trend'] = batch['login_trend'] * 0.4
    res = client.post('/api/score?wait=1', data={'file': (io.BytesIO(batch.to_csv(index=False).encode()), 'b.csv')},
                      content_type='multipart/form-data')
    job = res.get_json()['job']
    assert job['status'] == 'done', job
    drift = client.get('/api/monitoring/drift').get_json()['drift']
    assert drift['status'] in ('moderate_shift', 'major_shift')
    assert any(f['feature'] in ('logins_30d', 'login_trend') for f in drift['features'][:4])
    assert client.get('/api/results').get_json()['summary']['total_customers'] == 1500


def test_retrain_creates_second_version_and_rollback_restores_scores(trained, client, demo_csv_bytes):
    res = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(demo_csv_bytes), 'again.csv'), 'promote': 'false'},
                      content_type='multipart/form-data')
    assert res.get_json()['job']['result']['status'] == 'challenger'
    models = client.get('/api/models').get_json()['models']
    assert len(models) == 2
    original = next(m for m in models if m['status'] == 'champion')
    challenger = next(m for m in models if m['status'] == 'challenger')
    assert client.post(f"/api/models/{challenger['id']}/promote").status_code == 200
    assert client.post(f"/api/models/{original['id']}/promote").status_code == 200
    assert client.get('/api/results').get_json()['summary']['total_customers'] == 3000     # snapshot of that model restored
    assert client.post('/api/models/nope/promote').status_code == 404


def test_workspaces_are_isolated(client, env, monkeypatch):
    import index
    monkeypatch.setattr(index, 'workspace_for', lambda user: 'other_tenant')
    assert client.get('/api/results').status_code == 404            # the other tenant has no model
    assert client.get('/api/customers').get_json()['total'] == 0
    assert client.get('/api/models').get_json()['models'] == []


def test_rate_limiting(client, env, monkeypatch):
    from churn import config
    from churn.security import limiter
    limiter.reset()
    monkeypatch.setattr(config, 'RATE_LIMIT_PER_MIN', 3)
    codes = [client.get('/api/models').status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429
    limiter.reset()


def test_auth_fail_closed_in_production(client, monkeypatch):
    import index
    monkeypatch.setenv('AUTH_FAIL_CLOSED', 'true')
    monkeypatch.setattr(index, 'firebase_initialized', False)
    assert client.get('/api/results').status_code == 503


def test_security_headers_and_request_id(client):
    res = client.get('/api/health')
    assert res.headers['X-Content-Type-Options'] == 'nosniff' and res.headers['X-Frame-Options'] == 'DENY'
    assert res.headers['X-Request-ID']


def test_results_are_gzipped_and_revalidate_with_etag(trained, client):
    res = client.get('/api/results', headers={'Accept-Encoding': 'gzip'})
    assert res.headers.get('Content-Encoding') == 'gzip'
    again = client.get('/api/results', headers={'If-None-Match': res.headers['ETag']})
    assert again.status_code == 304


def test_workspace_reset(client, env):
    assert client.post('/api/workspace/reset').status_code == 200
    assert client.get('/api/results').status_code == 404
    assert client.get('/api/alerts').get_json()['counts']['Open'] == 0


def test_telco_style_csv_trains_end_to_end(client, env):
    """A foreign schema (Telco column names, Yes/No target, blank TotalCharges, no dates) works without configuration."""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(3)
    n = 1200
    tenure = rng.integers(1, 72, n)
    monthly = np.round(rng.uniform(20, 110, n), 2)
    contract = rng.choice(['Month-to-month', 'One year', 'Two year'], n, p=[0.55, 0.25, 0.2])
    z = -0.8 - 0.04 * tenure + 1.1 * (contract == 'Month-to-month') + 0.01 * monthly + rng.normal(0, 0.8, n)
    churn = rng.random(n) < 1 / (1 + np.exp(-z))
    total = (tenure * monthly).astype(object)
    total[:6] = ' '
    df = pd.DataFrame({'customerID': [f'{i:04d}-XYZ' for i in range(n)], 'gender': rng.choice(['Male', 'Female'], n), 'tenure': tenure,
                       'Contract': contract, 'MonthlyCharges': monthly, 'TotalCharges': total, 'Churn': np.where(churn, 'Yes', 'No')})
    res = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(df.to_csv(index=False).encode()), 'telco.csv')},
                      content_type='multipart/form-data')
    job = res.get_json()['job']
    assert job['status'] == 'done', job
    results = client.get('/api/results').get_json()
    assert results['schema']['id'] == 'customerID' and results['schema']['revenue_col'] == 'MonthlyCharges'
    assert results['model']['split'] == 'random'                     # no date column, so a stratified split
    assert results['metrics']['roc_auc'] > 65
    assert client.get('/api/customers?limit=3').get_json()['total'] == n
    top = client.get('/api/customers?limit=1').get_json()['customers'][0]
    assert client.get(f"/api/customers/{top['id']}").get_json()['reasons']


def test_job_admission_one_per_workspace_and_busy_server(client, env, monkeypatch):
    """Back-pressure: a workspace can't pile up jobs, and a saturated server answers 503 with Retry-After."""
    import sqlalchemy as sa
    from churn import config, db, jobs
    # Pretend this workspace already has a job running.
    with db.get_engine().begin() as c:
        c.execute(db.jobs_t.insert().values(id='stuck1', workspace_id='local', kind='train', status='running', progress=10,
                                            stage='x', created_at=__import__('time').time()))
    res = client.post('/api/upload', data={'file': (io.BytesIO(b'a,b\n1,2\n'), 'x.csv')}, content_type='multipart/form-data')
    assert res.status_code == 409 and 'in progress' in res.get_json()['error']
    with db.get_engine().begin() as c:
        c.execute(sa.delete(db.jobs_t).where(db.jobs_t.c.id == 'stuck1'))
    monkeypatch.setattr(config, 'MAX_QUEUED_JOBS', 0)
    res = client.post('/api/upload', data={'file': (io.BytesIO(b'a,b\n1,2\n'), 'x.csv')}, content_type='multipart/form-data')
    assert res.status_code == 503 and res.headers['Retry-After']


def test_deep_paging_is_capped_but_export_streams_everything(trained, client, monkeypatch):
    from churn import config
    monkeypatch.setattr(config, 'MAX_OFFSET', 100)
    ok = client.get('/api/customers?page=5&limit=20')            # offset 80
    assert ok.status_code == 200
    deep = client.get('/api/customers?page=20&limit=20')         # offset 380
    assert deep.status_code == 400 and 'not reachable' in deep.get_json()['error']
    total = client.get('/api/results').get_json()['summary']['total_customers']
    text = client.get('/api/customers/export?sort=loss').get_data(as_text=True)
    assert len(text.strip().splitlines()) == total + 1           # header + every customer, via keyset paging


def test_tier_counts_are_cached_and_stay_correct(trained, client):
    r = client.get('/api/results').get_json()['summary']
    total = client.get('/api/customers?limit=1').get_json()['total']
    assert total == r['total_customers'] == r['high_risk'] + r['medium_risk'] + r['low_risk']
    assert client.get('/api/customers?tier=High&limit=1').get_json()['total'] == r['high_risk']
