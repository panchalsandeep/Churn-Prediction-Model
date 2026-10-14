"""Phase 4: realized performance, health, shadow evaluation, guarded promotion, rollback, retraining triggers, backtests.

Time is simulated by loading the simulator's tables as they would have looked on a past date (nothing after that date, and churn dates in
the future blanked out of the accounts table), then moving the clock forward by loading a later cut.
"""
from __future__ import annotations

import io
import time

import numpy as np
import pandas as pd
import pytest

from churn import backtest, db, ingest, mlops, notify, pipeline, registry, retrain, service, shadow
from churn.b2b_sim import END, generate_b2b
from test_phase0_access import auth  # noqa: F401
from test_phase2_quotas import workspace  # noqa: F401
from test_phase3_notify import Receiver  # noqa: F401

HORIZON = 60
NOOP = lambda *_: None    # noqa: E731


@pytest.fixture(scope='module')
def sim():
    return generate_b2b(600, 20, 11, base_logit=-3.4)


def travel(ws, sim, until, replace=True):
    """Load the tables as they stood on `until`."""
    until = pd.Timestamp(until)
    acc = sim['accounts'].copy()
    acc.loc[acc['churned_at'] > until, 'churned_at'] = pd.NaT                       # the future is not known yet
    acc = acc[acc['started_at'] <= until]
    with db.workspace_scope(ws):
        ingest.put_table(ws, 'accounts', ingest.normalize('accounts', acc), 'replace')
        for role in ('events', 'tickets', 'invoices'):
            ingest.put_table(ws, role, ingest.normalize(role, sim[role][sim[role]['ts'] <= until]), 'replace')


def start(ws, sim, t0, algorithm='logistic_regression'):
    travel(ws, sim, t0)
    with db.workspace_scope(ws):
        pipeline.set_target(ws, {'type': 'churn_date', 'horizon_days': HORIZON})
        res = pipeline.run_train(ws, 'test', algorithm, NOOP)
    assert res['status'] == 'champion'
    return res


def step(ws, sim, until):
    travel(ws, sim, until)
    with db.workspace_scope(ws):
        return pipeline.run_score(ws, 'scheduler', NOOP)


T0 = END - pd.Timedelta(days=200)


# ── realized performance ──────────────────────────────────────────────────────────────────────────

def test_runs_are_logged_then_resolved_when_their_horizon_passes_and_health_is_assessed(env, sim):
    ws = 'mlops-a'
    start(ws, sim, T0)
    runs = mlops.list_runs(ws)
    live = [r for r in runs if r['kind'] == 'live']
    assert len(live) >= 1 and all(r['status'] == 'open' and r['horizon_days'] == HORIZON for r in live)
    first = live[0]
    assert first['expected']['roc_auc'] and 'base_rate' in first['expected']
    assert b'ACME-' not in __import__('churn.storage', fromlist=['x']).get_storage().get(first['log_key'])      # the log is ciphertext

    step(ws, sim, T0 + pd.Timedelta(days=30))                                        # 30 days later: first run's horizon (60) has not closed
    assert mlops.get_run(ws, first['id'])['status'] == 'open'
    step(ws, sim, T0 + pd.Timedelta(days=75))                                        # now it has
    done = mlops.get_run(ws, first['id'])
    assert done['status'] == 'resolved'
    r = done['realized']
    assert r['n'] > 200 and r['positives'] >= 20 and r['roc_auc'] > 65 and 0.5 < r['calibration_ratio'] < 2.0 and r['usable']
    assert sum(1 for c in r['calibration']) >= 2
    health = mlops.stored_health(ws)
    assert health['status'] in ('ok', 'watch') and health['runs'] >= 1 and health['realized_roc_auc'] > 60
    outcomes = mlops.outcomes_for(ws, done)
    assert len(outcomes) == r['n'] and set(outcomes.unique()) <= {0, 1}


def test_a_model_that_stops_working_is_flagged_audited_and_announced(env, sim):
    ws = 'mlops-b'
    start(ws, sim, T0)
    hook = Receiver()
    import os
    os.environ['SAFEHTTP_ALLOW_LOCAL'] = '1'
    try:
        with db.workspace_scope(ws):
            notify.add_channel(ws, {'kind': 'slack', 'url': hook.url}, 'me')
            run = mlops.list_runs(ws, kind='live')[0]
            ids = mlops.load_log(ws, run)['customer_id']
            truth = pd.Series(np.random.default_rng(0).random(len(ids)) < 0.3, index=ids.to_numpy()).astype(int)     # outcomes unrelated to the scores
            m = mlops.resolve_run(ws, run['id'], truth)
            assert m['roc_auc'] < 60 and m['usable']
            health = mlops.update_health(ws)
        assert health['status'] == 'degraded' and 'below' in ' '.join(health['reasons'])
        assert any('Model health: degraded' in r['body'].decode() for r in hook.requests)
        with db.workspace_scope(ws):
            assert any(e['action'] == 'mlops.health' for e in __import__('churn.service', fromlist=['x']).list_audit(ws, 50)) if hasattr(service, 'list_audit') else True
    finally:
        os.environ.pop('SAFEHTTP_ALLOW_LOCAL', None)
        hook.close()


def test_outcomes_must_cover_the_run_and_unknown_runs_are_refused(env, sim):
    ws = 'mlops-c'
    start(ws, sim, T0)
    run = mlops.list_runs(ws, kind='live')[0]
    ids = mlops.load_log(ws, run)['customer_id']
    with pytest.raises(ValueError, match='at least 80%'):
        mlops.resolve_run(ws, run['id'], pd.Series([0, 1] * 10, index=ids.head(20).to_numpy()))
    with pytest.raises(LookupError):
        mlops.resolve_run(ws, 'nope', pd.Series([0], index=['a']))
    assert mlops.get_run(ws, run['id'])['status'] == 'open'


def test_realized_metrics_handle_degenerate_inputs():
    assert mlops.realized_metrics([0, 0, 0], [0.1, 0.2, 0.1])['usable'] is False
    assert mlops.realized_metrics([1, 1], [0.9, 0.8])['roc_auc'] is None
    rng = np.random.default_rng(1)
    p = rng.random(2000)
    y = (rng.random(2000) < p).astype(int)                                           # perfectly calibrated by construction
    m = mlops.realized_metrics(y, p)
    assert 0.9 < m['calibration_ratio'] < 1.1 and m['ece'] < 0.05 and m['roc_auc'] > 60 and m['usable']


# ── shadow evaluation, promotion, rollback ────────────────────────────────────────────────────────

def test_a_challenger_is_scored_in_shadow_beside_the_champion_and_compared_on_the_same_outcomes(env, sim):
    ws = 'mlops-d'
    champ = start(ws, sim, T0)
    t1 = T0 + pd.Timedelta(days=30)
    travel(ws, sim, t1)
    with db.workspace_scope(ws):
        ch = pipeline.run_train(ws, 'test', 'gradient_boosting', NOOP, challenger_only=True)
    assert ch['status'] == 'challenger' and registry.champion(ws)['id'] == champ['model_id']      # a retrained model never goes live by itself
    assert shadow.challenger(ws)['id'] == ch['model_id']
    for days in (30, 60, 90, 120):
        step(ws, sim, t1 + pd.Timedelta(days=days))
    kinds = {(r['kind'], r['as_of']) for r in mlops.list_runs(ws, 60)}
    assert sum(1 for k, _ in kinds if k == 'shadow') >= 4                                      # one shadow run per live run since the challenger appeared
    with db.workspace_scope(ws):
        rec = shadow.evaluate(ws)
    assert rec['matched_runs'] >= 2 and rec['decision'] in ('promote', 'keep_waiting', 'reject', 'pending')
    if rec['decision'] != 'pending':
        assert rec['champion']['n'] == rec['challenger_metrics']['n'] and rec['positives'] >= 20          # same accounts, same outcomes
    assert registry.champion(ws)['id'] == champ['model_id']                                        # auto-promote is off: still the same champion


def test_promotion_rules_gate_on_gain_calibration_and_roc_and_rollback_restores(env, sim, monkeypatch):
    ws = 'mlops-e'
    champ = start(ws, sim, T0)
    travel(ws, sim, T0 + pd.Timedelta(days=30))
    with db.workspace_scope(ws):
        ch = pipeline.run_train(ws, 'test', 'logistic_regression', NOOP, challenger_only=True)
    step(ws, sim, T0 + pd.Timedelta(days=45))
    base = {'n': 5000, 'roc_auc': 80.0, 'pr_auc': 40.0, 'brier': 0.080, 'positives': 300}

    def judge(champion, challenger, runs=2):
        monkeypatch.setattr(shadow, '_pool', lambda *_: {'runs': runs, 'n': 5000, 'positives': 300, 'champion': champion, 'challenger': challenger})
        with db.workspace_scope(ws):
            return shadow.evaluate(ws)

    pol = shadow.get_policy(ws)
    assert pol['auto_promote'] is False
    rec = judge(base, {**base, 'pr_auc': 40.4})
    assert rec['decision'] == 'keep_waiting' and 'below the required' in rec['reasons'][0]
    rec = judge(base, {**base, 'pr_auc': 43.0, 'brier': 0.095})
    assert rec['decision'] == 'keep_waiting' and any('Brier' in r for r in rec['reasons'])
    rec = judge(base, {**base, 'pr_auc': 43.0, 'roc_auc': 78.0})
    assert rec['decision'] == 'keep_waiting' and any('ROC-AUC is lower' in r for r in rec['reasons'])
    rec = judge(base, {**base, 'pr_auc': 38.0}, runs=4)
    assert rec['decision'] == 'reject' and rec['archived'] and registry.get_model(ws, ch['model_id'])['status'] == 'archived'

    # a fresh challenger that clearly wins is promoted automatically when the workspace opted in; the old champion can be restored
    travel(ws, sim, T0 + pd.Timedelta(days=60))
    with db.workspace_scope(ws):
        ch2 = pipeline.run_train(ws, 'test', 'logistic_regression', NOOP, challenger_only=True)
        shadow.set_policy(ws, {'auto_promote': True})
    rec = judge(base, {**base, 'pr_auc': 44.0, 'brier': 0.07})
    assert rec['decision'] == 'promote' and rec['promoted'] is True
    assert registry.champion(ws)['id'] == ch2['model_id']
    with db.workspace_scope(ws):
        back = shadow.rollback(ws, 'me')
    assert back['model_id'] == champ['model_id'] and registry.champion(ws)['id'] == champ['model_id']
    with pytest.raises(LookupError):
        with db.workspace_scope(ws):
            registry.set_status(ws, champ['model_id'], 'archived')                          # a champion cannot be archived by accident ...
            service.kv_set(ws, 'mlops_previous', {'model_id': 'gone'})
            shadow.rollback(ws, 'me')                                                       # ... and rollback refuses a target that no longer exists


def test_pending_until_enough_resolved_runs_exist(env, sim):
    ws = 'mlops-f'
    start(ws, sim, T0)
    travel(ws, sim, T0 + pd.Timedelta(days=20))
    with db.workspace_scope(ws):
        pipeline.run_train(ws, 'test', 'logistic_regression', NOOP, challenger_only=True)
        rec = shadow.evaluate(ws)
    assert rec['decision'] == 'pending' and 'Needs 2 resolved runs' in rec['reasons'][0]
    assert shadow.evaluate('mlops-no-challenger')['decision'] == 'none'


# ── retraining policy ─────────────────────────────────────────────────────────────────────────────

def test_retraining_triggers_and_a_single_challenger_at_a_time(env, sim):
    ws = 'mlops-g'
    start(ws, sim, T0)
    travel(ws, sim, T0 + pd.Timedelta(days=30))
    cfg = pipeline.get(ws)
    pol = {**shadow.DEFAULT_POLICY, 'enabled': True}
    healthy = {'status': 'ok', 'reasons': []}
    with db.workspace_scope(ws):
        assert retrain.triggers(ws, pol, cfg, healthy) == []                                  # nothing wrong: nothing to do
        assert any('older than' in t for t in retrain.triggers(ws, {**pol, 'every_days': 1}, cfg, healthy)) is False   # champion is minutes old
        with db.get_engine().begin() as c:                                                    # pretend the champion is two months old
            import sqlalchemy as sa
            c.execute(sa.update(db.models_t).where(db.models_t.c.workspace_id == ws).values(created_at=time.time() - 60 * 86400))
        assert any('older than 30 days' in t for t in retrain.triggers(ws, pol, cfg, healthy))
        service.kv_set(ws, 'drift', {'status': 'major_shift'})
        assert any('shifted a lot' in t for t in retrain.triggers(ws, {**pol, 'every_days': 0}, cfg, healthy))
        assert not any('shifted' in t for t in retrain.triggers(ws, {**pol, 'every_days': 0, 'on_drift': False}, cfg, healthy))
        service.kv_set(ws, 'drift', None)
        bad = {'status': 'degraded', 'reasons': ['ROC fell.']}
        assert any('degraded' in t for t in retrain.triggers(ws, {**pol, 'every_days': 0}, cfg, bad))
        assert any('new churn events' in t for t in retrain.triggers(ws, {**pol, 'every_days': 0, 'min_new_positives': 5}, {**cfg, 'positives': 0}, healthy))

        out = retrain.run_check(ws, 'mlops', NOOP)                                            # age trigger fires: a challenger is trained
        assert out['retrained'] and out['challenger_status'] == 'challenger' and out['triggers']
        again = retrain.run_check(ws, 'mlops', NOOP)
        assert not again['retrained'] and 'already being shadowed' in again['note']           # never a pile of challengers


def test_a_workspace_without_a_pipeline_cannot_auto_retrain(env):
    ws = 'mlops-h'
    out = retrain.run_check(ws, 'mlops', NOOP, force=True)
    assert out['retrained'] is False and 'pipeline workspace' in out['note']


# ── scheduler, API, backtest, erasure ─────────────────────────────────────────────────────────────

def test_policy_validation_and_the_scheduled_check(client, env, auth, sim):
    ws, boss, root = workspace(client, auth, 'enterprise')
    assert client.put('/api/mlops/policy', json={'shadow_min_runs': 99}, headers=boss).status_code == 400
    assert client.put('/api/mlops/policy', json={'every_days': 'soon'}, headers=boss).status_code == 400
    pol = client.put('/api/mlops/policy', json={'enabled': True, 'check_every_hours': 6, 'auto_promote': False}, headers=boss).get_json()
    assert pol['enabled'] and pol['shadow_min_runs'] == 2
    with db.system_context():
        import sqlalchemy as sa
        with db.get_engine().connect() as c:
            row = c.execute(sa.select(db.schedules_t).where(db.schedules_t.c.workspace_id == ws, db.schedules_t.c.kind == 'mlops_check')).first()
    assert row and row.every_hours == 6
    start(ws, sim, T0)
    assert pipeline.tick(time.time() + 7 * 3600) >= 1
    job = client.get('/api/mlops', headers=boss).get_json()
    assert job['health']['status'] in ('insufficient', 'ok', 'watch', 'degraded') and job['retrains_automatically'] and job['champion']
    assert client.get('/api/mlops', headers={}).status_code == 401


def test_batch_runs_are_resolved_by_uploading_outcomes_through_the_api(client, env):
    from churn.demo_data import generate_demo
    df = generate_demo(2500, seed=31)
    csv = df.to_csv(index=False).encode()
    up = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(csv), 'd.csv'), 'algorithm': 'logistic_regression'}, content_type='multipart/form-data')
    assert up.get_json()['job']['status'] == 'done'
    sc = client.post('/api/score?wait=1', data={'file': (io.BytesIO(csv), 's.csv')}, content_type='multipart/form-data')
    assert sc.get_json()['job']['status'] == 'done'
    runs = client.get('/api/runs').get_json()['runs']
    run = next(r for r in runs if r['status'] == 'open' and r['source'] == 'batch' and r['rows'] == 2500)
    assert run['horizon_days'] is None and run['kind'] == 'live'

    truth = df[['customer_id', 'churned']] if 'churned' in df else df[['customer_id', 'churn']].rename(columns={'churn': 'churned'})
    partial = truth.head(300)
    bad = client.post(f"/api/runs/{run['id']}/outcomes", data={'file': (io.BytesIO(partial.to_csv(index=False).encode()), 'o.csv')}, content_type='multipart/form-data')
    assert bad.status_code == 400 and 'at least 80%' in bad.get_json()['error']
    nocols = client.post(f"/api/runs/{run['id']}/outcomes", data={'file': (io.BytesIO(b'x,y\n1,2\n'), 'o.csv')}, content_type='multipart/form-data')
    assert nocols.status_code == 400
    assert client.post('/api/runs/unknown/outcomes', data={'file': (io.BytesIO(truth.to_csv(index=False).encode()), 'o.csv')}, content_type='multipart/form-data').status_code == 404
    ok = client.post(f"/api/runs/{run['id']}/outcomes", data={'file': (io.BytesIO(truth.to_csv(index=False).encode()), 'o.csv')}, content_type='multipart/form-data')
    assert ok.status_code == 200, ok.get_json()
    body = ok.get_json()
    assert body['realized']['roc_auc'] > 60 and body['health']['status'] in ('ok', 'watch', 'degraded')
    after = {r['id']: r for r in client.get('/api/runs').get_json()['runs']}
    assert after[run['id']]['status'] == 'resolved' and after[run['id']]['realized']['n'] == 2500


def test_backtest_shows_how_the_recipe_would_have_done_at_several_past_dates(env, sim):
    ws = 'mlops-i'
    travel(ws, sim, END)
    with db.workspace_scope(ws):
        pipeline.set_target(ws, {'type': 'churn_date', 'horizon_days': HORIZON})
        out = backtest.run(ws, NOOP, origins=3)
    assert out['summary'] and out['summary']['origins'] >= 2
    assert out['summary']['roc_auc_mean'] > 65 and all(r['train_rows'] > 500 for r in out['results'])
    # strictly earlier training data for earlier origins
    origins = [r['origin'] for r in out['results']]
    trains = [r['train_rows'] for r in out['results']]
    assert origins == sorted(origins) and trains == sorted(trains)
    assert service.kv_get(ws, 'backtest')['summary'] == out['summary']
    with pytest.raises(ValueError, match='churned'):
        with db.workspace_scope('mlops-empty'):
            backtest.run('mlops-empty', NOOP)


def test_erasure_removes_accounts_from_prediction_logs(env, sim):
    ws = 'mlops-j'
    start(ws, sim, T0)
    run = mlops.list_runs(ws, kind='live')[0]
    victim = mlops.load_log(ws, run)['customer_id'].iloc[0]
    assert mlops.erase_accounts(ws, [victim]) >= 1
    assert victim not in set(mlops.load_log(ws, mlops.get_run(ws, run['id']))['customer_id'])


def test_drift_baseline_is_the_population_today_so_ageing_alone_is_not_drift(env, sim):
    ws = 'mlops-k'
    start(ws, sim, T0)
    right_after = service.kv_get(ws, 'drift')
    assert right_after['status'] == 'stable' and right_after['max_psi'] < 0.05             # same data, same population
    step(ws, sim, T0 + pd.Timedelta(days=17))                                              # accounts are 17 days older, contracts 17 days closer to renewal
    later = service.kv_get(ws, 'drift')
    assert later['status'] != 'major_shift'
    assert not {'tenure_months', 'days_to_renewal'} & {f['feature'] for f in later['features']}
