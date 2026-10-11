"""Phase 0 access control: roles, invitations, MFA/SSO, tenant isolation, audit chain, erasure, PII."""
from __future__ import annotations

import io

import pandas as pd
import pytest
import sqlalchemy as sa

from churn import crypto, db, tenancy
from churn.demo_data import generate_demo


@pytest.fixture()
def auth(monkeypatch):
    """Turn real authentication on with fake Firebase tokens. `auth.token(uid, email, ...)` returns a bearer header."""
    import index
    claims = {}

    def verify(token):
        if token not in claims:
            raise ValueError('invalid token')
        return claims[token]

    monkeypatch.setattr(index, 'auth_configured', lambda: True)
    monkeypatch.setattr(index.auth, 'verify_id_token', verify)
    monkeypatch.setattr(index, 'ADMIN_EMAILS', ['root@platform.io'])
    monkeypatch.setattr(index, 'AUTO_PROVISION', False)
    monkeypatch.delenv('REQUIRE_MFA_ROLES', raising=False)

    class Auth:
        def token(self, uid, email, provider='password', mfa=False, ws=None, **extra):
            t = f'tok-{uid}-{provider}-{int(mfa)}'
            fb = {'sign_in_provider': provider}
            if mfa:
                fb['sign_in_second_factor'] = 'phone'
            claims[t] = {'uid': uid, 'email': email, 'firebase': fb, **extra}
            h = {'Authorization': f'Bearer {t}'}
            if ws:
                h['X-Workspace'] = ws
            return h

    return Auth()


_team_counter = [0]


def make_team(client, auth):
    """Platform admin provisions a workspace; the owner accepts, then invites an admin, analyst and viewer.

    Every team gets unique user ids and emails, so tests never inherit memberships from each other.
    """
    _team_counter[0] += 1
    n = _team_counter[0]
    root = auth.token('root', 'root@platform.io')
    boss_email = f'boss{n}@acme.com'
    res = client.post('/api/workspaces', json={'name': f'Acme {n}', 'owner_email': boss_email}, headers=root)
    assert res.status_code == 201, res.get_json()
    ws, token = res.get_json()['workspace_id'], res.get_json()['invite']['token']
    assert client.put(f'/api/platform/workspaces/{ws}/plan', json={'plan': 'enterprise'}, headers=root).status_code == 200   # no seat cap in these tests
    boss = auth.token(f'boss{n}', boss_email)
    assert client.post('/api/invites/accept', json={'token': token}, headers=boss).status_code == 200
    people = {}
    for base, role in (('adam', 'admin'), ('ana', 'analyst'), ('vic', 'viewer')):
        email = f'{base}{n}@acme.com'
        inv = client.post('/api/invites', json={'email': email, 'role': role}, headers=boss)
        assert inv.status_code == 201, inv.get_json()
        h = auth.token(f'{base}{n}', email)
        assert client.post('/api/invites/accept', json={'token': inv.get_json()['token']}, headers=h).status_code == 200
        people[base] = h
    people['boss'] = boss
    people['_n'] = n
    return ws, people


def small_csv(rows=900, seed=1):
    return generate_demo(rows, seed=seed).to_csv(index=False).encode()


def upload(client, headers, payload=None, **form):
    return client.post('/api/upload?wait=1', headers=headers, content_type='multipart/form-data',
                       data={'file': (io.BytesIO(payload or small_csv()), 'd.csv'), 'algorithm': 'logistic_regression', **form})


# ── authentication basics ────────────────────────────────────────────────────────────────────────

def test_requests_without_valid_tokens_are_rejected(client, env, auth):
    assert client.get('/api/results').status_code == 401
    assert client.get('/api/results', headers={'Authorization': 'Bearer nonsense'}).status_code == 401
    assert client.get('/api/results', headers={'Authorization': 'Basic abc'}).status_code == 401


def test_every_api_route_requires_authentication(client, env, auth):
    """Walks the URL map: nothing under /api may answer an anonymous caller except the explicit public few."""
    import index
    public = {'/api/health', '/api/ready', '/api/demo.csv'}
    for rule in index.app.url_map.iter_rules():
        if not rule.rule.startswith('/api/') or rule.rule in public:
            continue
        path = rule.rule.replace('<path:customer_id>', 'x').replace('<job_id>', 'x').replace('<model_id>', 'x').replace('<alert_id>', 'x') \
            .replace('<intervention_id>', 'x').replace('<uid>', 'x')
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            res = client.open(path, method=method)
            assert res.status_code == 401, f'{method} {rule.rule} answered {res.status_code} to an anonymous caller'


def test_new_account_without_a_workspace_is_told_to_ask_for_an_invite(client, env, auth):
    res = client.get('/api/results', headers=auth.token('stranger', 'stranger@x.com'))
    assert res.status_code == 403 and res.get_json()['code'] == 'no_workspace'


# ── invitations ──────────────────────────────────────────────────────────────────────────────────

def test_invites_work_once_for_the_right_email_only(client, env, auth):
    root = auth.token('root', 'root@platform.io')
    created = client.post('/api/workspaces', json={'name': 'Beta Co', 'owner_email': 'cfo@beta.com'}, headers=root).get_json()
    token = created['invite']['token']
    wrong = client.post('/api/invites/accept', json={'token': token}, headers=auth.token('mallory', 'mallory@evil.com'))
    assert wrong.status_code == 403 and wrong.get_json()['code'] == 'wrong_email'
    good = client.post('/api/invites/accept', json={'token': token}, headers=auth.token('cfo', 'cfo@beta.com'))
    assert good.status_code == 200 and good.get_json()['role'] == 'owner'
    again = client.post('/api/invites/accept', json={'token': token}, headers=auth.token('cfo2', 'cfo@beta.com'))
    assert again.status_code == 400 and again.get_json()['code'] == 'invalid_invite'
    assert client.post('/api/invites/accept', json={'token': 'made-up'}, headers=auth.token('cfo', 'cfo@beta.com')).status_code == 400
    assert client.post('/api/workspaces', json={'name': 'X', 'owner_email': 'a@b.co'}, headers=auth.token('nobody', 'n@x.com')).status_code == 403


def test_invite_tokens_are_stored_only_as_hashes(client, env, auth):
    ws, team = make_team(client, auth)
    token = client.post('/api/invites', json={'email': 'new@acme.com', 'role': 'viewer'}, headers=team['boss']).get_json()['token']
    with db.system_context(), db.get_engine().connect() as c:
        stored = [r.token_hash for r in c.execute(sa.select(db.invites_t.c.token_hash))]
    assert token not in stored and all(len(h) == 64 for h in stored)


def test_expired_invites_are_refused(client, env, auth):
    ws, team = make_team(client, auth)
    inv = client.post('/api/invites', json={'email': 'late@acme.com', 'role': 'viewer'}, headers=team['boss']).get_json()
    with db.system_context(), db.get_engine().begin() as c:
        c.execute(sa.update(db.invites_t).values(expires_at=1.0))
    res = client.post('/api/invites/accept', json={'token': inv['token']}, headers=auth.token('late', 'late@acme.com'))
    assert res.status_code == 400


# ── roles ────────────────────────────────────────────────────────────────────────────────────────

def test_role_matrix(client, env, auth):
    ws, team = make_team(client, auth)
    assert upload(client, team['adam']).get_json()['job']['status'] == 'done'      # an admin trains the first model

    can = lambda who, method, path, **kw: client.open(path, method=method, headers=team[who], **kw).status_code
    # viewers read
    for path in ('/api/results', '/api/customers', '/api/alerts', '/api/models', '/api/settings', '/api/model/schema'):
        assert can('vic', 'GET', path) == 200, path
    assert can('vic', 'POST', '/api/predict', json={'values': {}}) == 200
    # …but cannot export or act
    assert can('vic', 'GET', '/api/customers/export') == 403
    assert can('vic', 'PATCH', '/api/alerts/x', json={'status': 'Resolved'}) == 403
    assert can('vic', 'POST', '/api/customers/x/interventions', json={'kind': 'call'}) == 403
    # analysts export and work alerts, but cannot train, change settings, or manage people
    assert can('ana', 'GET', '/api/customers/export') == 200
    assert can('ana', 'PATCH', '/api/alerts/missing', json={'status': 'Resolved'}) == 404           # allowed; the alert just doesn't exist
    assert can('ana', 'POST', '/api/upload') == 403
    assert can('ana', 'POST', '/api/settings', json={}) == 403
    assert can('ana', 'GET', '/api/members') == 403
    assert can('ana', 'GET', '/api/audit') == 403
    # admins train, configure, manage members, read the audit log — but cannot reset or delete the workspace
    assert can('adam', 'POST', '/api/settings', json={'economics': {'outreach_cost': 30}}) == 200
    assert can('adam', 'GET', '/api/members') == 200 and can('adam', 'GET', '/api/audit') == 200
    assert can('adam', 'POST', '/api/workspace/reset') == 403
    assert client.delete('/api/workspace', json={'confirm': ws}, headers=team['adam']).status_code == 403
    # owners can do everything, including reset
    assert can('boss', 'POST', '/api/workspace/reset') == 200


def test_people_management_rules(client, env, auth):
    ws, team = make_team(client, auth)
    # admins can invite analysts/viewers, but not admins or owners
    assert client.post('/api/invites', json={'email': 'x@acme.com', 'role': 'analyst'}, headers=team['adam']).status_code == 201
    assert client.post('/api/invites', json={'email': 'x@acme.com', 'role': 'admin'}, headers=team['adam']).status_code == 403
    assert client.post('/api/invites', json={'email': 'x@acme.com', 'role': 'owner'}, headers=team['adam']).status_code == 403
    # admins cannot demote the owner or promote themselves
    assert client.patch(f"/api/members/boss{team['_n']}", json={'role': 'viewer'}, headers=team['adam']).status_code == 403
    assert client.patch(f"/api/members/adam{team['_n']}", json={'role': 'owner'}, headers=team['adam']).status_code == 403
    # an admin can promote a viewer to analyst but not to admin
    assert client.patch(f"/api/members/vic{team['_n']}", json={'role': 'analyst'}, headers=team['adam']).status_code == 200
    assert client.patch(f"/api/members/vic{team['_n']}", json={'role': 'admin'}, headers=team['adam']).status_code == 403
    # the last owner cannot be demoted or removed, even by themselves
    assert client.patch(f"/api/members/boss{team['_n']}", json={'role': 'admin'}, headers=team['boss']).status_code == 409
    assert client.delete(f"/api/members/boss{team['_n']}", headers=team['boss']).status_code == 409
    # removing a member takes effect immediately
    assert client.delete(f"/api/members/ana{team['_n']}", headers=team['adam']).status_code == 200
    assert client.get('/api/results', headers=team['ana']).status_code == 403


# ── MFA and SSO ──────────────────────────────────────────────────────────────────────────────────

def test_mfa_is_required_for_privileged_roles_when_configured(client, env, auth, monkeypatch):
    ws, team = make_team(client, auth)
    monkeypatch.setenv('REQUIRE_MFA_ROLES', 'admin,owner')
    blocked = client.get('/api/members', headers=team['adam'])
    assert blocked.status_code == 403 and blocked.get_json()['code'] == 'mfa_required'
    assert client.get('/api/results', headers=team['vic']).status_code in (200, 404)     # viewers are not forced
    with_mfa = auth.token(f"adam{team['_n']}", f"adam{team['_n']}@acme.com", mfa=True)
    assert client.get('/api/members', headers=with_mfa).status_code == 200


def test_sso_enforcement_blocks_password_logins(client, env, auth):
    ws, team = make_team(client, auth)
    assert client.post('/api/workspace/sso', json={'required': True}, headers=team['boss']).status_code == 200
    denied = client.get('/api/results', headers=team['boss'])
    assert denied.status_code == 403 and denied.get_json()['code'] == 'sso_required'
    saml = auth.token(f"boss{team['_n']}", f"boss{team['_n']}@acme.com", provider='saml.acme')
    assert client.get('/api/me', headers=saml).status_code == 200
    assert client.post('/api/workspace/sso', json={'required': False}, headers=saml).status_code == 200
    assert client.get('/api/me', headers=team['boss']).status_code == 200


# ── tenant isolation ─────────────────────────────────────────────────────────────────────────────

def test_one_tenant_cannot_see_or_reach_another_tenants_data(client, env, auth):
    ws_a, team_a = make_team(client, auth)
    assert upload(client, team_a['adam']).get_json()['job']['status'] == 'done'
    first = client.get('/api/customers?limit=1', headers=team_a['adam']).get_json()['customers'][0]['id']
    job_id = client.get('/api/models', headers=team_a['adam']).get_json()['models'][0]['id']

    root = auth.token('root', 'root@platform.io')
    inv = client.post('/api/workspaces', json={'name': 'Rival', 'owner_email': 'ceo@rival.com'}, headers=root).get_json()
    ws_b = inv['workspace_id']
    rival = auth.token('ceo', 'ceo@rival.com')
    client.post('/api/invites/accept', json={'token': inv['invite']['token']}, headers=rival)

    assert client.get('/api/results', headers=rival).status_code == 404                  # no model in B
    assert client.get('/api/customers', headers=rival).get_json()['total'] == 0
    assert client.get(f'/api/customers/{first}', headers=rival).status_code == 404
    assert client.get('/api/models', headers=rival).get_json()['models'] == []
    assert client.get('/api/alerts', headers=rival).get_json()['alerts'] == []
    assert client.get(f'/api/privacy/export/{first}', headers=rival).status_code == 404
    export = client.get('/api/customers/export', headers=rival).get_data(as_text=True)
    assert len(export.strip().splitlines()) == 1                                         # header only
    assert client.post(f'/api/models/{job_id}/promote', headers=rival).status_code == 404
    assert client.post('/api/interventions/x', headers=rival).status_code in (404, 405)
    # asking for A's workspace explicitly is refused
    stolen = dict(rival, **{'X-Workspace': ws_a})
    assert client.get('/api/results', headers=stolen).status_code == 403
    # and B's owner cannot read A's job through the jobs endpoint
    some_job = client.post('/api/upload?wait=1', headers=team_a['adam'], content_type='multipart/form-data',
                           data={'file': (io.BytesIO(small_csv(500, 3)), 'x.csv'), 'algorithm': 'logistic_regression'}).get_json()['job_id']
    assert client.get(f'/api/jobs/{some_job}', headers=rival).status_code == 404


# ── audit log ────────────────────────────────────────────────────────────────────────────────────

def test_audit_log_is_chained_and_detects_tampering(client, env, auth):
    ws, team = make_team(client, auth)
    client.post('/api/settings', json={'economics': {'outreach_cost': 40}}, headers=team['adam'])
    body = client.get('/api/audit', headers=team['adam']).get_json()
    actions = [e['action'] for e in body['events']]
    assert 'invite.create' in actions and 'invite.accept' in actions and 'settings.update' in actions
    assert body['chain']['ok'] is True and body['chain']['checked'] >= 5

    with db.system_context(), db.get_engine().begin() as c:                    # the database itself refuses edits
        with pytest.raises(Exception, match='append-only'):
            c.execute(sa.text("UPDATE audit_log SET actor = 'someone-else'"))
    with db.system_context(), db.get_engine().begin() as c:                    # a DBA who drops the trigger is still caught
        c.execute(sa.text('DROP TRIGGER audit_log_no_update ON audit_log' if c.dialect.name == 'postgresql' else 'DROP TRIGGER audit_log_no_update'))
        c.execute(sa.text("UPDATE audit_log SET actor = 'forged' WHERE id = "
                          "(SELECT id FROM audit_log WHERE workspace_id = :w ORDER BY id LIMIT 1 OFFSET 2)"), {'w': ws})
    verdict = client.get('/api/audit', headers=team['adam']).get_json()['chain']
    assert verdict['ok'] is False and verdict['first_bad_id'] is not None
    export = client.get('/api/audit/export', headers=team['adam'])
    assert export.mimetype == 'application/x-ndjson' and b'"hash"' in export.data


# ── privacy: erasure, export, deletion, PII ───────────────────────────────────────────────────────

def test_erasure_removes_a_customer_everywhere_including_rollback_snapshots(client, env, auth):
    ws, team = make_team(client, auth)
    assert upload(client, team['adam']).get_json()['job']['status'] == 'done'
    top = client.get('/api/customers?limit=2', headers=team['adam']).get_json()['customers']
    victim, bystander = top[0]['id'], top[1]['id']
    client.post(f'/api/customers/{victim}/interventions', json={'kind': 'call', 'notes': 'n'}, headers=team['ana'])

    dump = client.get(f'/api/privacy/export/{victim}', headers=team['adam']).get_json()
    assert dump['customer_id'] == victim and dump['attributes'] and dump['interventions']

    res = client.post('/api/privacy/erase', json={'ids': [victim]}, headers=team['adam'])
    assert res.status_code == 200 and res.get_json()['erased'] == 1 and res.get_json()['snapshots_rewritten'] >= 1
    assert client.get(f'/api/customers/{victim}', headers=team['adam']).status_code == 404
    assert client.get(f'/api/customers/{bystander}', headers=team['adam']).status_code == 200
    model_id = client.get('/api/models', headers=team['adam']).get_json()['models'][0]['id']
    client.post(f'/api/models/{model_id}/promote', headers=team['adam'])         # rollback re-reads the snapshot
    assert client.get(f'/api/customers/{victim}', headers=team['adam']).status_code == 404
    assert client.post('/api/privacy/erase', json={'ids': []}, headers=team['adam']).status_code == 400


def test_workspace_deletion_destroys_data_and_the_key(client, env, auth):
    ws, team = make_team(client, auth)
    assert upload(client, team['adam']).get_json()['job']['status'] == 'done'
    bad = client.delete('/api/workspace', json={'confirm': 'nope'}, headers=team['boss'])
    assert bad.status_code == 400
    res = client.delete('/api/workspace', json={'confirm': ws}, headers=team['boss'])
    assert res.status_code == 200 and res.get_json()['key_destroyed'] is True
    assert client.get('/api/results', headers=team['boss']).status_code == 403            # membership is gone
    with pytest.raises(crypto.KeyShredded):
        crypto.encrypt_bytes(ws, b'x', 'ctx')                                              # and the key cannot come back
    assert tenancy.get_workspace(ws)['status'] == 'deleted'
    with db.system_context(), db.get_engine().connect() as c:
        left = c.execute(sa.select(sa.func.count()).select_from(db.customers_t).where(db.customers_t.c.workspace_id == ws)).scalar()
    assert left == 0


def pii_csv(rows=700):
    df = generate_demo(rows, seed=6)
    df['contact_email'] = [f'person{i}@example.com' for i in range(rows)]
    df['phone_number'] = [f'+1555{i:07d}' for i in range(rows)]
    df['first_name'] = ['Ada'] * rows
    df['notes_internal'] = 'confidential'
    return df.to_csv(index=False).encode()


def test_personal_data_columns_are_detected_unused_and_never_stored(client, env, auth):
    ws, team = make_team(client, auth)
    pre = client.post('/api/preview', headers=team['adam'], content_type='multipart/form-data',
                      data={'file': (io.BytesIO(pii_csv()), 'p.csv'), 'exclude_columns': 'notes_internal'}).get_json()
    assert set(pre['schema']['pii']) == {'contact_email', 'phone_number', 'first_name'}
    assert any('Personal data detected' in w for w in pre['schema']['warnings'])
    assert not any('person0@example.com' in str(row) for row in pre['sample'])             # not even in the preview sample
    reasons = {d['name']: d['reason'] for d in pre['schema']['dropped']}
    assert reasons['notes_internal'] == 'excluded by you'

    assert upload(client, team['adam'], pii_csv(), exclude_columns='notes_internal').get_json()['job']['status'] == 'done'
    top = client.get('/api/customers?limit=1', headers=team['adam']).get_json()['customers'][0]['id']
    attrs = client.get(f'/api/customers/{top}', headers=team['adam']).get_json()['attrs']
    assert not ({'contact_email', 'phone_number', 'first_name', 'notes_internal'} & set(attrs))
    with db.get_engine().connect() as c:
        blob = ' '.join(r.attrs or '' for r in c.execute(sa.select(db.details_t.c.attrs).where(db.details_t.c.workspace_id == ws).limit(50)))
    assert 'example.com' not in blob and 'confidential' not in blob
    model = client.get('/api/model/schema', headers=team['adam']).get_json()
    assert not any(f['name'] in ('contact_email', 'phone_number', 'first_name') for f in model['fields'])

    allowed = client.post('/api/preview', headers=team['adam'], content_type='multipart/form-data',
                          data={'file': (io.BytesIO(pii_csv()), 'p.csv'), 'allow_pii': 'first_name'}).get_json()
    assert 'first_name' not in allowed['schema']['pii']                                     # explicit override for a false positive


def test_customer_ids_can_be_pseudonymised_stably_per_workspace(client, env, auth):
    ws, team = make_team(client, auth)
    client.post('/api/settings', json={'privacy': {'hash_ids': True}}, headers=team['adam'])
    assert upload(client, team['adam']).get_json()['job']['status'] == 'done'
    ids = [c['id'] for c in client.get('/api/customers?limit=20', headers=team['adam']).get_json()['customers']]
    assert ids and all(i.startswith('h_') and 'CUST-' not in i for i in ids)
    lookup = client.post('/api/privacy/lookup', json={'ids': ['CUST-000001']}, headers=team['adam']).get_json()['ids']['CUST-000001']
    assert lookup.startswith('h_') and lookup == crypto.pseudonymize_id(ws, 'CUST-000001')
    assert crypto.pseudonymize_id(ws, 'CUST-000001') != crypto.pseudonymize_id('another-workspace', 'CUST-000001')
