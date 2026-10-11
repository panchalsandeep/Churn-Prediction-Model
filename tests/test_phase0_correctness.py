"""Phase 0 correctness: customers must not straddle validation splits, and bad scoring batches must fail loudly."""
from __future__ import annotations

import io

import numpy as np
import pandas as pd

from churn.training import train_model


def multi_snapshot(n_customers=500, snapshots=5, seed=0, with_id=True):
    """Each customer appears in several snapshots with a customer-specific signature that predicts nothing real.

    The label is constant per customer and unrelated to every genuine feature, so an honest model must score ~0.5 AUC.
    A model that sees the same customer on both sides of the split can simply memorise the signature.
    """
    rng = np.random.default_rng(seed)
    signature = rng.normal(size=n_customers)
    label = (rng.random(n_customers) < 0.25).astype(int)
    rows = []
    for i in range(n_customers):
        for s in range(snapshots):
            rows.append({'customer_id': f'C{i:04d}', 'snapshot_date': f'2026-0{s + 1}-28', 'signature': signature[i],
                         'noise': rng.normal(), 'usage': rng.normal(), 'churn': label[i]})
    df = pd.DataFrame(rows)
    return df if with_id else df.drop(columns=['customer_id'])


def test_repeated_customers_are_detected_and_validated_by_group():
    df = multi_snapshot()
    r = train_model(df, algorithm='gradient_boosting')
    assert r['schema']['grouped'] and r['schema']['n_groups'] == 500
    assert r['meta']['validation']['grouped'] is True
    assert any('snapshots each' in w for w in r['schema']['warnings'])
    # Honest validation: nothing in the data predicts churn, so the holdout must not look good.
    assert r['evaluation']['metrics']['roc_auc'] < 62, r['evaluation']['metrics']

def test_the_bug_is_real_without_an_id_column():
    """Same data without an ID column cannot be grouped, and memorisation inflates the score. Documents *why* grouping matters."""
    r = train_model(multi_snapshot(with_id=False), algorithm='gradient_boosting')
    assert r['schema']['grouped'] is False
    assert r['evaluation']['metrics']['roc_auc'] > 65, r['evaluation']['metrics']   # nothing real predicts churn here; honest is ~50


def test_time_split_removes_customers_seen_in_the_future_from_training():
    rng = np.random.default_rng(1)
    rows = []
    for i in range(900):                                    # 600 early-only customers, 300 appear in the late period too
        first = {'customer_id': f'C{i:04d}', 'snapshot_date': ['2026-01-31', '2026-02-28', '2026-03-31'][i % 3], 'usage': rng.normal(), 'tenure': rng.integers(1, 40)}
        rows.append(first)
        if i % 3 == 0:
            rows.append({**first, 'snapshot_date': '2026-05-31'})
        if i < 400:
            rows.append({'customer_id': f'N{i:04d}', 'snapshot_date': '2026-05-31', 'usage': rng.normal(), 'tenure': rng.integers(1, 40)})
    df = pd.DataFrame(rows)
    df['churn'] = (df['usage'] + rng.normal(0, 1, len(df)) < -0.3).astype(int)
    r = train_model(df, algorithm='logistic_regression')
    v = r['meta']['validation']
    assert r['meta']['split'] == 'time' and v['grouped']
    assert v['rows_dropped_for_leakage'] > 0


def test_latest_snapshot_wins_when_deduplicating_for_the_customer_list(client, env):
    df = multi_snapshot(n_customers=120, snapshots=3)
    res = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(df.to_csv(index=False).encode()), 'm.csv'), 'algorithm': 'logistic_regression'},
                      content_type='multipart/form-data')
    assert res.get_json()['job']['status'] == 'done', res.get_json()
    assert client.get('/api/customers?limit=1').get_json()['total'] == 120          # one row per customer
    detail = client.get('/api/customers/C0007').get_json()
    assert detail['attrs']['snapshot_date'] == '2026-03-28'                         # the newest of its three snapshots


def test_scoring_batch_missing_important_columns_fails_with_names(client, env, demo_csv_bytes):
    df = pd.read_csv(io.BytesIO(demo_csv_bytes))
    ok = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(demo_csv_bytes), 'd.csv')}, content_type='multipart/form-data')
    assert ok.get_json()['job']['status'] == 'done'
    broken = df.drop(columns=['login_trend', 'unresolved_tickets'])
    res = client.post('/api/score?wait=1', data={'file': (io.BytesIO(broken.to_csv(index=False).encode()), 'b.csv')},
                      content_type='multipart/form-data')
    job = res.get_json()['job']
    assert job['status'] == 'error'
    assert 'login_trend' in job['error'] and 'relies' in job['error']
    # explicit override scores anyway and reports what was missing
    res = client.post('/api/score?wait=1', data={'file': (io.BytesIO(broken.to_csv(index=False).encode()), 'b.csv'), 'allow_missing': '1'},
                      content_type='multipart/form-data')
    job = res.get_json()['job']
    assert job['status'] == 'done'
    assert set(job['result']['schema_check']['missing']) == {'login_trend', 'unresolved_tickets'}


def test_scoring_warns_about_null_spikes_and_unreadable_numbers(client, env, demo_csv_bytes):
    df = pd.read_csv(io.BytesIO(demo_csv_bytes)).head(800)
    df['logins_30d'] = np.nan                                              # was fully populated in training
    df['monthly_charges'] = 'n/a'
    res = client.post('/api/score?wait=1', data={'file': (io.BytesIO(df.to_csv(index=False).encode()), 'w.csv'), 'allow_missing': '1'},
                      content_type='multipart/form-data')
    warnings = ' '.join(res.get_json()['job']['result']['schema_check']['warnings'])
    assert 'logins_30d' in warnings and 'monthly_charges' in warnings


def test_prediction_reports_which_inputs_were_defaulted(client, env):
    form = client.get('/api/model/schema').get_json()
    keep = {f['name']: f['default'] for f in form['fields'][:5]}
    body = client.post('/api/predict', json={'values': keep}).get_json()
    assert set(body['defaults_used']) == {f['name'] for f in form['fields'][5:]}


def test_models_carry_provenance(client, env):
    from churn import registry
    row, bundle = registry.champion_bundle('local')
    p = bundle['provenance']
    assert p['scikit_learn'] and p['seed'] == 42 and p['data_hash'] and p['bundle_format'] == 3
    assert 0 <= min(bundle['null_rates'].values()) <= 1
