"""Phase 3: B2B tables, point-in-time features, target definitions, pipeline runs, API-key ingestion and scheduling."""
from __future__ import annotations

import io
import json
import time

import numpy as np
import pandas as pd
import pytest

from churn import apikeys, crypto, features, ingest, jobs, pipeline, quotas, storage
from churn.b2b_sim import generate_b2b
from test_phase0_access import auth  # noqa: F401  (fixture)
from test_phase2_quotas import workspace  # noqa: F401


@pytest.fixture(scope='module')
def sim():
    return generate_b2b(300, 14, 5)


@pytest.fixture(scope='module')
def tables(sim):
    return {k: sim[k] for k in ('accounts', 'events', 'tickets', 'invoices')}


def csv_bytes(df):
    return df.to_csv(index=False).encode()


def post_table(client, role, df, headers=None, **form):
    return client.post(f'/api/data/{role}', data={'file': (io.BytesIO(csv_bytes(df)), f'{role}.csv'), **form}, content_type='multipart/form-data',
                       headers=headers or {})


# ── normalisation ─────────────────────────────────────────────────────────────────────────────────

def test_columns_are_mapped_validated_and_personal_data_is_dropped():
    n = 10
    raw = pd.DataFrame({'Customer Ref': [f'a{i}' for i in range(n)], 'Signed Up': ['2025-01-05'] * (n - 1) + ['garbage'], 'ARR/12': np.arange(n) * 10.0 + 100,
                        'contact_email': ['x@y.com'] * n, 'industry': ['retail'] * n})
    out = ingest.normalize('accounts', raw, {'account_id': 'Customer Ref', 'started_at': 'Signed Up', 'mrr': 'ARR/12'})
    assert 'contact_email' not in out and 'industry' in out and 'mrr' in out
    assert len(out) == n - 1                                                       # one unparseable date: that row is dropped, not guessed
    with pytest.raises(ingest.IngestError, match='needs account_id'):
        ingest.normalize('accounts', pd.DataFrame({'x': [1]}))
    with pytest.raises(ingest.IngestError, match='not in the file'):
        ingest.normalize('events', raw, {'account_id': 'nope', 'ts': 'Signed Up'})
    with pytest.raises(ingest.IngestError, match='not dates'):
        ingest.normalize('events', pd.DataFrame({'account_id': list('abcde'), 'ts': ['soon'] * 5}))
    with pytest.raises(ingest.IngestError, match='Unknown table'):
        ingest.normalize('payroll', raw)


# ── point-in-time correctness ─────────────────────────────────────────────────────────────────────

def test_features_never_see_the_future(tables):
    as_of = pd.Timestamp('2026-01-31')
    vocab = features.event_vocabulary(tables)
    before = features.build_snapshot(tables, as_of, vocab).set_index('customer_id').sort_index()

    rng = np.random.default_rng(0)
    future_events = tables['events'][tables['events']['ts'] > as_of + pd.Timedelta(days=1)].copy()
    poisoned = dict(tables)
    poisoned['events'] = pd.concat([tables['events'], future_events.assign(ts=future_events['ts'] + pd.Timedelta(days=5))], ignore_index=True)
    poisoned['tickets'] = pd.concat([tables['tickets'], tables['tickets'].assign(ts=as_of + pd.Timedelta(days=3))], ignore_index=True)
    poisoned['invoices'] = pd.concat([tables['invoices'], tables['invoices'].assign(ts=as_of + pd.Timedelta(days=3), status='failed', amount=1.0)], ignore_index=True)
    after = features.build_snapshot(poisoned, as_of, vocab).set_index('customer_id').sort_index()
    pd.testing.assert_frame_equal(before, after)                                    # identical: nothing after as_of leaked in

    # an account is only a row if it was live that day
    acc = tables['accounts']
    ids = set(before.index)
    assert not (set(acc.loc[acc['started_at'] > as_of, 'account_id']) & ids)
    assert not (set(acc.loc[acc['churned_at'] <= as_of, 'account_id']) & ids)


def test_labels_wait_for_their_whole_horizon_and_training_never_uses_unlabelled_rows(tables):
    spec = {'type': 'churn_date', 'horizon_days': 90}
    through = features.observed_through(tables)
    df, info = features.build_training_frame(tables, spec)
    assert df['churned'].isin([0, 1]).all() and pd.to_datetime(df['snapshot_date']).max() <= through - pd.Timedelta(days=90)
    score, meta = features.build_scoring_frame(tables, spec, info['vocab'])
    assert meta['as_of'] == through.strftime('%Y-%m-%d') and 'churned' not in score
    assert set(df.columns) - {'churned'} == set(score.columns)                       # training and scoring rows have identical columns

    # label correctness on a hand-made case
    acc = pd.DataFrame({'account_id': ['a', 'b', 'c'], 'started_at': pd.to_datetime(['2025-01-01'] * 3),
                        'churned_at': pd.to_datetime(['2025-04-15', '2025-08-01', None])})
    ev = pd.DataFrame({'account_id': ['a', 'b', 'c'], 'ts': pd.to_datetime(['2025-03-01', '2025-03-01', '2025-12-31'])})
    t = {'accounts': acc, 'events': ev}
    snap = features.build_snapshot(t, pd.Timestamp('2025-03-31'))
    y = features.labels(t, snap, pd.Timestamp('2025-03-31'), {'type': 'churn_date', 'horizon_days': 90}, pd.Timestamp('2025-12-31'))
    assert dict(zip(snap['customer_id'], y)) == {'a': 1.0, 'b': 0.0, 'c': 0.0}       # b churns after the horizon: not a positive
    young = features.labels(t, snap, pd.Timestamp('2025-03-31'), {'type': 'churn_date', 'horizon_days': 90}, pd.Timestamp('2025-05-01'))
    assert young.isna().all()                                                         # horizon not observed: unknown, not zero


def test_inactivity_target_needs_events_and_labels_silence():
    acc = pd.DataFrame({'account_id': ['a', 'b'], 'started_at': pd.to_datetime(['2025-01-01'] * 2), 'churned_at': pd.NaT})
    ev = pd.DataFrame({'account_id': ['a', 'a', 'b'], 'ts': pd.to_datetime(['2025-03-01', '2025-05-01', '2025-03-01'])})
    t = {'accounts': acc, 'events': ev}
    snap = features.build_snapshot(t, pd.Timestamp('2025-03-31'))
    y = features.labels(t, snap, pd.Timestamp('2025-03-31'), {'type': 'inactivity', 'horizon_days': 60}, pd.Timestamp('2025-12-31'))
    assert dict(zip(snap['customer_id'], y)) == {'a': 0.0, 'b': 1.0}
    with pytest.raises(features.TargetError, match='events table'):
        features.validate_spec({'type': 'inactivity'}, {'accounts': acc})


def test_target_preview_explains_problems_before_training(tables):
    good = features.preview_target(tables, {'type': 'churn_date', 'horizon_days': 90})
    assert good['ok'] and good['positives'] > 100 and len(good['snapshots']) >= 6 and 0 < good['base_rate'] < 0.4
    with pytest.raises(features.TargetError, match='between 14 and 365'):
        features.preview_target(tables, {'type': 'churn_date', 'horizon_days': 5})
    with pytest.raises(features.TargetError, match='Target type'):
        features.preview_target(tables, {'type': 'vibes'})
    no_churn = dict(tables, accounts=tables['accounts'].assign(churned_at=pd.NaT))
    with pytest.raises(features.TargetError, match='churned_at'):
        features.preview_target(no_churn, {'type': 'churn_date'})
    tiny = {k: v[v['account_id'].isin(tables['accounts']['account_id'].head(25))] for k, v in tables.items()}
    warned = features.preview_target(tiny, {'type': 'churn_date', 'horizon_days': 90})
    assert any('too few' in w.lower() for w in warned['warnings'])
    early = (tables['accounts']['started_at'].min() + pd.Timedelta(days=130)).strftime('%Y-%m-%d')          # data that has barely begun
    short = features.preview_target(tables, {'type': 'churn_date', 'horizon_days': 90, 'observed_through': early})
    assert len(short['snapshots']) < 3 and any('No snapshot' in w or 'Fewer than 3' in w for w in short['warnings'])


# ── storage, isolation, erasure ───────────────────────────────────────────────────────────────────

def test_tables_are_encrypted_in_storage_and_erasure_removes_raw_rows(client, env, tables):
    ws = 'tbl-ws'
    ingest.put_table(ws, 'accounts', ingest.normalize('accounts', tables['accounts']), 'replace')
    ingest.put_table(ws, 'events', ingest.normalize('events', tables['events'].head(5000)), 'replace')
    for key in storage.get_storage().list(storage.table_prefix(ws)):
        blob = storage.get_storage().get(key)
        assert blob.startswith(crypto.MAGIC) and b'ACME-' not in blob               # ciphertext only
    victim = tables['events']['account_id'].iloc[0]
    assert (ingest.load_table(ws, 'events')['account_id'] == victim).any()
    removed = ingest.erase_accounts(ws, [victim])
    assert removed >= 2 and not (ingest.load_table(ws, 'events')['account_id'] == victim).any()
    assert not (ingest.load_table(ws, 'accounts')['account_id'] == victim).any()
    assert ingest.load_table('other-ws', 'events') is None                          # another workspace sees nothing
    ingest.drop_all(ws)
    assert ingest.load_table(ws, 'accounts') is None and storage.get_storage().list(storage.table_prefix(ws)) == []


def test_append_and_replace_semantics(env, tables):
    ws = 'sem-ws'
    ev = ingest.normalize('events', tables['events'].head(100))
    ingest.put_table(ws, 'events', ev, 'replace')
    ingest.put_table(ws, 'events', ev, 'append')                                     # exact duplicates collapse on load
    assert len(ingest.load_table(ws, 'events')) == 100
    ingest.put_table(ws, 'events', ingest.normalize('events', tables['events'].iloc[100:150]), 'append')
    assert len(ingest.load_table(ws, 'events')) == 150 and ingest.table_info(ws)['events']['rows'] == 250   # info counts rows received
    ingest.put_table(ws, 'events', ev, 'replace')
    assert len(ingest.load_table(ws, 'events')) == 100
    with pytest.raises(ingest.IngestError):
        ingest.put_table(ws, 'events', ev, 'upsert')


# ── the whole pipeline through the API ────────────────────────────────────────────────────────────

def test_upload_define_target_train_and_score_end_to_end(client, env, sim):
    small = generate_b2b(260, 14, 9)
    for role in ('accounts', 'events', 'tickets', 'invoices'):
        res = post_table(client, role, small[role])
        assert res.status_code == 201, res.get_json()
    st = client.get('/api/pipeline').get_json()
    assert st['tables']['accounts']['rows'] == 260 and not st['ready_to_train']

    bad = client.post('/api/pipeline/target', json={'type': 'churn_date', 'horizon_days': 3})
    assert bad.status_code == 400
    pv = client.post('/api/pipeline/target', json={'type': 'churn_date', 'horizon_days': 90})
    assert pv.status_code == 200 and pv.get_json()['ok'] and client.get('/api/pipeline').get_json()['target'] is None   # preview does not save
    assert client.post('/api/pipeline/target', json={'type': 'churn_date', 'horizon_days': 90, 'save': True}).status_code == 200
    assert client.get('/api/pipeline').get_json()['ready_to_train']

    run = client.post('/api/pipeline/run?wait=1', json={'algorithm': 'gradient_boosting'})
    job = run.get_json()['job']
    assert job['status'] == 'done', job
    result = job['result']
    assert result['status'] == 'champion' and result['features']['snapshots'] >= 6
    assert result['metrics']['roc_auc'] > 70                                         # the planted behaviour is recoverable, validated on a time split
    assert result['scored_today']['as_of'] == result['features']['observed_through']

    top = client.get('/api/customers?limit=3&sort=loss').get_json()
    assert top['total'] > 150 and top['customers'][0]['id'].startswith('ACME-')
    results = client.get('/api/results').get_json()
    assert results['metrics']['roc_auc'] > 70
    drivers = {r['feature'] for c in top['customers'] for r in client.get(f"/api/customers/{c['id']}").get_json()['reasons']}
    assert drivers & {'events_trend', 'days_since_last_activity', 'events_30d', 'active_users_30d', 'failed_payments_90d', 'seat_utilization_30d',
                      'tickets_90d', 'days_to_renewal', 'billing_change', 'active_days_30d', 'events_90d', 'events_7d'}   # explanations speak the customer's language


def test_pipeline_requires_the_prerequisites(client, env):
    ws_tables = ingest.table_info('local')
    if ws_tables:
        client.delete('/api/data')
    assert client.post('/api/pipeline/target', json={'type': 'churn_date', 'horizon_days': 90, 'save': True}).status_code == 400   # no accounts yet
    job = client.post('/api/pipeline/run?wait=1', json={}).get_json()['job']
    assert job['status'] == 'error' and 'accounts table' in job['error'].lower()
    assert client.put('/api/pipeline/schedule', json={'enabled': True, 'every_hours': 24}).status_code in (200, 400)


# ── API keys and ingestion ────────────────────────────────────────────────────────────────────────

def test_api_key_ingestion_is_scoped_hashed_revocable_and_limited(client, env, auth, monkeypatch):
    ws, boss, root = workspace(client, auth, 'enterprise')
    ws2, boss2, _ = workspace(client, auth, 'enterprise')
    made = client.post('/api/api-keys', json={'name': 'event pipeline'}, headers=boss)
    assert made.status_code == 201
    key = made.get_json()['key']
    assert key.startswith('cs_live_') and 'key' not in client.get('/api/api-keys', headers=boss).get_json()['keys'][0]
    import sqlalchemy as sa
    from churn import db
    with db.system_context(), db.get_engine().connect() as c:
        stored = c.execute(sa.select(db.api_keys_t.c.key_hash)).scalars().all()
    assert key not in stored and apikeys._hash(key) in stored                         # only the hash is stored

    rows = [{'account_id': 'A1', 'ts': '2026-01-01T10:00:00', 'event': 'login'}, {'account_id': 'A2', 'ts': '2026-01-02T10:00:00', 'event': 'export'}]
    bearer = {'Authorization': f'Bearer {key}'}
    ok = client.post('/api/ingest/events', json={'rows': rows}, headers=bearer)
    assert ok.status_code == 202 and ok.get_json()['rows_added'] == 2
    assert len(ingest.load_table(ws, 'events')) == 2 and ingest.load_table(ws2, 'events') is None   # landed in its own workspace only

    assert client.post('/api/ingest/events', json={'rows': rows}).status_code == 401
    assert client.post('/api/ingest/events', json={'rows': rows}, headers={'Authorization': 'Bearer cs_live_wrong'}).status_code == 401
    assert client.post('/api/ingest/events', json={'rows': []}, headers=bearer).status_code == 400
    assert client.post('/api/ingest/events', json={'rows': [{'x': 1}]}, headers=bearer).status_code == 400            # missing required columns
    assert client.post('/api/ingest/payroll', json={'rows': rows}, headers=bearer).status_code == 400
    monkeypatch.setattr('index.MAX_INGEST_ROWS', 1)
    assert client.post('/api/ingest/events', json={'rows': rows}, headers=bearer).status_code == 413
    monkeypatch.setattr('index.MAX_INGEST_ROWS', 5000)

    # a key opens nothing else
    assert client.get('/api/results', headers=bearer).status_code == 401
    assert client.get('/api/api-keys', headers=bearer).status_code == 401
    # a viewer cannot mint keys
    assert client.post('/api/api-keys', json={'name': 'x'}, headers={}).status_code == 401

    kid = client.get('/api/api-keys', headers=boss).get_json()['keys'][0]['id']
    assert client.delete(f'/api/api-keys/{kid}', headers=boss2).status_code == 404       # another workspace cannot revoke it
    assert client.delete(f'/api/api-keys/{kid}', headers=boss).status_code == 200
    assert client.post('/api/ingest/events', json={'rows': rows}, headers=bearer).status_code == 401


def test_ingest_rate_limit_applies_per_key(client, env, auth, monkeypatch):
    ws, boss, root = workspace(client, auth, 'enterprise')
    key = client.post('/api/api-keys', json={'name': 'k'}, headers=boss).get_json()['key']
    monkeypatch.setenv('INGEST_RATE_LIMIT_PER_MIN', '3')
    rows = [{'account_id': 'A1', 'ts': '2026-01-01', 'event': 'login'}]
    codes = [client.post('/api/ingest/events', json={'rows': rows}, headers={'Authorization': f'Bearer {key}'}).status_code for _ in range(5)]
    assert codes == [202, 202, 202, 429, 429]


# ── scheduling ────────────────────────────────────────────────────────────────────────────────────

def test_schedule_runs_due_pipelines_once_and_records_why_it_skipped(client, env, auth):
    small = generate_b2b(220, 13, 4)
    from churn import db
    import sqlalchemy as sa
    with db.system_context(), db.get_engine().begin() as c:
        c.execute(sa.delete(db.schedules_t))                                           # other tests in this module may have left schedules
    ws, boss, root = workspace(client, auth, 'enterprise')
    for role in ('accounts', 'events', 'tickets', 'invoices'):
        assert post_table(client, role, small[role], headers=boss).status_code == 201
    assert client.put('/api/pipeline/schedule', json={'enabled': True, 'every_hours': 24}, headers=boss).status_code == 400   # nothing trained yet
    assert client.post('/api/pipeline/target', json={'type': 'churn_date', 'horizon_days': 90, 'save': True}, headers=boss).status_code == 200
    assert client.post('/api/pipeline/run?wait=1', json={'algorithm': 'logistic_regression'}, headers=boss).get_json()['job']['status'] == 'done'

    assert client.put('/api/pipeline/schedule', json={'enabled': True, 'every_hours': 0.1}, headers=boss).status_code == 400    # too frequent
    sched = client.put('/api/pipeline/schedule', json={'enabled': True, 'every_hours': 24}, headers=boss).get_json()
    assert sched['enabled'] and sched['next_run_at'] > time.time() + 23 * 3600
    assert pipeline.tick() == 0                                                       # not due yet

    later = time.time() + 25 * 3600
    assert pipeline.tick(later) == 1
    assert pipeline.tick(later) == 0                                                  # a second worker in the same instant does nothing
    s = pipeline.get_schedule(ws)
    assert s['last_job_id'] and s['last_status'] == 'queued'
    job = client.get(f"/api/jobs/{s['last_job_id']}", headers=boss).get_json()
    assert job['status'] == 'done' and job['kind'] == 'pipeline_score' and job['result']['rows'] > 100

    # over quota: the run is skipped, not crashed, and the reason is visible
    import os
    os.environ['PLAN_LIMITS_JSON'] = json.dumps({'enterprise': {'scored_accounts': 10}})
    try:
        assert pipeline.tick(later + 25 * 3600) == 0
        assert pipeline.get_schedule(ws)['last_status'].startswith('skipped:') and 'allowance' in pipeline.get_schedule(ws)['last_status']
    finally:
        os.environ.pop('PLAN_LIMITS_JSON')
