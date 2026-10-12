"""Phase 2: seats, plan limits and usage metering."""
from __future__ import annotations

import csv
import io
import json

import pytest

from churn import quotas
from test_phase0_access import auth, small_csv, upload  # noqa: F401  (fixtures/helpers shared with the access tests)

_n = [0]


def workspace(client, auth, plan=None):
    """A fresh workspace with one owner. Returns (ws_id, owner_headers, platform_headers)."""
    _n[0] += 1
    n = _n[0]
    root = auth.token('root', 'root@platform.io')
    res = client.post('/api/workspaces', json={'name': f'Quota {n}', 'owner_email': f'owner{n}@q.com'}, headers=root)
    assert res.status_code == 201, res.get_json()
    ws = res.get_json()['workspace_id']
    boss = auth.token(f'qowner{n}', f'owner{n}@q.com')
    assert client.post('/api/invites/accept', json={'token': res.get_json()['invite']['token']}, headers=boss).status_code == 200
    if plan:
        assert client.put(f'/api/platform/workspaces/{ws}/plan', json={'plan': plan}, headers=root).status_code == 200
    return ws, boss, root


def invite(client, boss, n, role='viewer'):
    return client.post('/api/invites', json={'email': f'p{n}-{_n[0]}@q.com', 'role': role}, headers=boss)


def test_seat_limit_counts_members_and_pending_invitations_and_frees_up_on_revoke_or_upgrade(client, env, auth):
    ws, boss, root = workspace(client, auth)                      # trial: 3 seats, 1 used by the owner
    assert invite(client, boss, 1).status_code == 201 and invite(client, boss, 2).status_code == 201
    blocked = invite(client, boss, 3)
    assert blocked.status_code == 402
    body = blocked.get_json()
    assert body['code'] == 'quota_exceeded' and body['metric'] == 'seats' and body['limit'] == 3 and body['used'] == 3

    seats = client.get('/api/usage', headers=boss).get_json()['seats']
    assert seats == {'used': 1, 'pending': 2, 'limit': 3, 'available': 0}

    pending = client.get('/api/invites', headers=boss).get_json()['invites']
    assert len(pending) == 2 and 'token' not in json.dumps(pending)                    # the secret is never listed
    assert client.delete(f"/api/invites/{pending[0]['id']}", headers=boss).status_code == 200
    assert invite(client, boss, 4).status_code == 201                                  # revoking freed the seat
    assert invite(client, boss, 5).status_code == 402

    assert client.put(f'/api/platform/workspaces/{ws}/plan', json={'plan': 'team'}, headers=root).status_code == 200
    quotas.forget_plans()
    assert invite(client, boss, 6).status_code == 201                                  # upgraded: more room
    assert client.delete('/api/invites/doesnotexist0000000', headers=boss).status_code == 404


def test_only_platform_admins_change_plans_and_only_to_known_plans(client, env, auth):
    ws, boss, root = workspace(client, auth)
    assert client.put(f'/api/platform/workspaces/{ws}/plan', json={'plan': 'enterprise'}, headers=boss).status_code == 403   # a customer cannot upgrade themselves
    assert client.put(f'/api/platform/workspaces/{ws}/plan', json={'plan': 'free-forever'}, headers=root).status_code == 400
    assert client.put(f'/api/platform/workspaces/{ws}/plan', json={'plan': 'dev'}, headers=root).status_code == 400          # the unlimited dev plan is not sellable
    assert client.put('/api/platform/workspaces/nope/plan', json={'plan': 'team'}, headers=root).status_code == 404
    assert client.put(f'/api/platform/workspaces/{ws}/plan', json={'plan': 'team'}, headers={}).status_code == 401


def test_training_run_limit_is_hard_the_demo_is_free_and_failures_are_not_charged(client, env, auth, monkeypatch):
    monkeypatch.setenv('PLAN_LIMITS_JSON', json.dumps({'trial': {'training_runs': 2}}))
    ws, boss, root = workspace(client, auth)
    assert upload(client, boss).get_json()['job']['status'] == 'done'
    bad = client.post('/api/upload?wait=1', headers=boss, content_type='multipart/form-data',
                      data={'file': (io.BytesIO(b'a,b\n1,2\n3,4\n'), 'tiny.csv'), 'algorithm': 'logistic_regression'})
    assert bad.status_code in (200, 400) and quotas.usage(ws)['training_runs'] == 1       # a rejected/failed dataset did not use a run
    demo = client.post('/api/demo?wait=1', json={'rows': 1000, 'algorithm': 'logistic_regression'}, headers=boss)
    assert demo.get_json()['job']['status'] == 'done' and quotas.usage(ws)['training_runs'] == 1   # demo is unmetered
    assert upload(client, boss).get_json()['job']['status'] == 'done' and quotas.usage(ws)['training_runs'] == 2
    third = upload(client, boss)
    assert third.status_code == 402 and third.get_json()['metric'] == 'training_runs' and third.get_json()['limit'] == 2


def test_upload_size_and_monthly_scored_account_limits(client, env, auth, monkeypatch):
    monkeypatch.setenv('PLAN_LIMITS_JSON', json.dumps({'trial': {'rows_per_upload': 1000, 'scored_accounts': 1500}}))
    ws, boss, root = workspace(client, auth)
    big = upload(client, boss, small_csv(1200))
    assert big.status_code == 402 and big.get_json()['metric'] == 'rows_per_upload'
    assert upload(client, boss, small_csv(900)).get_json()['job']['status'] == 'done'
    assert quotas.usage(ws)['scored_accounts'] == 900
    again = upload(client, boss, small_csv(900, seed=2))                                      # 900 + 900 > 1500
    assert again.status_code == 402 and again.get_json()['metric'] == 'scored_accounts'
    assert quotas.usage(ws)['scored_accounts'] == 900                                         # rejected work is not billed
    summary = client.get('/api/usage', headers=boss).get_json()
    assert summary['metrics']['scored_accounts'] == {'used': 900, 'limit': 1500, 'hard': True, 'percent': 60.0}


def test_api_calls_are_metered_but_never_blocked(client, env, auth, monkeypatch):
    monkeypatch.setenv('PLAN_LIMITS_JSON', json.dumps({'trial': {'api_calls': 3}}))
    ws, boss, root = workspace(client, auth)
    statuses = [client.get('/api/me', headers=boss).status_code for _ in range(8)]
    assert statuses == [200] * 8                                                              # over the soft limit and still served
    quotas.flush()
    used = quotas.usage(ws)['api_calls']
    assert used >= 8 and quotas.api_over_limit(ws)
    assert client.get('/api/usage', headers=boss).get_json()['metrics']['api_calls']['percent'] > 100


def test_usage_is_private_to_the_workspace_and_to_admins(client, env, auth):
    ws1, boss1, _ = workspace(client, auth, 'enterprise')
    ws2, boss2, _ = workspace(client, auth, 'enterprise')
    assert upload(client, boss1).get_json()['job']['status'] == 'done'
    assert client.get('/api/usage', headers=boss1).get_json()['metrics']['training_runs']['used'] == 1
    assert client.get('/api/usage', headers=boss2).get_json()['metrics']['training_runs']['used'] == 0
    assert client.get('/api/usage', headers={}).status_code == 401

    export = client.get('/api/usage/export', headers=boss1)
    rows = list(csv.DictReader(io.StringIO(export.get_data(as_text=True))))
    assert {'period', 'metric', 'value', 'workspace'} <= set(rows[0]) and {r['workspace'] for r in rows} == {ws1}
    by_metric = {r['metric']: int(r['value']) for r in rows}
    assert by_metric['training_runs'] == 1 and by_metric['scored_accounts'] == 900


def test_unlimited_plans_and_dev_workspaces_have_no_caps(client, env):
    assert quotas.limits('local')['plan'] == 'dev' and quotas.limits('local')['seats'] is None
    quotas.check_job('local', 'train', 10_000_000)                                           # no exception
    assert quotas.summary('local')['metrics']['training_runs']['percent'] is None


def test_unparseable_plan_overrides_are_ignored_not_fatal(monkeypatch):
    monkeypatch.setenv('PLAN_LIMITS_JSON', '{not json')
    assert quotas.plans()['trial']['seats'] == 3
