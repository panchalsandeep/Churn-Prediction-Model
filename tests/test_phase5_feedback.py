"""Bug reports with screenshots: validated, encrypted, admin-only, forwarded safely."""
from __future__ import annotations

import base64
import json

import pytest

from churn import crypto, feedback, storage
from test_phase0_access import auth  # noqa: F401
from test_phase2_quotas import workspace  # noqa: F401
from test_phase3_notify import Receiver  # noqa: F401

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
SHOT = 'data:image/png;base64,' + base64.b64encode(PNG).decode()


def test_report_is_stored_encrypted_listed_for_admins_and_readable_back(client, env, auth):
    ws, boss, _ = workspace(client, auth, 'enterprise')
    r = client.post('/api/feedback', json={'message': 'The chart on Overview is blank for ACME-0042', 'screenshot': SHOT, 'page': '#overview', 'viewport': '390x844',
                                           'agent': 'test', 'app_version': 'x', 'request_ids': ['abc123']}, headers=boss)
    assert r.status_code == 201
    rid = r.get_json()['id']
    for key in storage.get_storage().list(f'feedback/{ws}'):
        blob = storage.get_storage().get(key)
        assert blob.startswith(crypto.MAGIC) and b'ACME-0042' not in blob and PNG[:8] not in blob          # nothing readable at rest
    listing = client.get('/api/feedback', headers=boss).get_json()['reports']
    assert listing[0]['id'] == rid and listing[0]['has_screenshot'] and listing[0]['page'] == '#overview' and 'screenshot' not in listing[0]
    full = client.get(f'/api/feedback/{rid}', headers=boss).get_json()
    assert full['message'].startswith('The chart') and base64.b64decode(full['screenshot']) == PNG


def test_validation_and_access_rules(client, env, auth):
    ws, boss, _ = workspace(client, auth, 'enterprise')
    bad = lambda body: client.post('/api/feedback', json=body, headers=boss).status_code   # noqa: E731
    assert bad({'message': 'hi'}) == 400
    assert bad({'message': 'long enough', 'screenshot': 'data:text/html;base64,PGI+'}) == 400
    assert bad({'message': 'long enough', 'screenshot': 'data:image/png;base64,' + base64.b64encode(b'not a png at all').decode()}) == 400
    assert bad({'message': 'long enough', 'screenshot': 'data:image/png;base64,' + base64.b64encode(PNG + b'0' * (feedback.MAX_IMAGE + 1)).decode()}) == 400
    assert bad({'message': 'works without a screenshot'}) == 201
    assert client.post('/api/feedback', json={'message': 'anonymous'}, headers={}).status_code == 401
    assert client.get('/api/feedback/../../etc/passwd', headers=boss).status_code == 404
    assert client.get('/api/feedback/ffffffffffff', headers=boss).status_code == 404


def test_reports_do_not_cross_workspaces(client, env, auth):
    ws1, boss1, _ = workspace(client, auth, 'enterprise')
    ws2, boss2, _ = workspace(client, auth, 'enterprise')
    rid = client.post('/api/feedback', json={'message': 'private to workspace one'}, headers=boss1).get_json()['id']
    assert client.get(f'/api/feedback/{rid}', headers=boss2).status_code == 404
    assert client.get('/api/feedback', headers=boss2).get_json()['reports'] == []


def test_forwarding_sends_text_and_metadata_but_not_the_screenshot_by_default(client, env, auth, monkeypatch):
    hook = Receiver()
    monkeypatch.setenv('SAFEHTTP_ALLOW_LOCAL', '1')
    monkeypatch.setenv('FEEDBACK_WEBHOOK_URL', hook.url)
    try:
        ws, boss, _ = workspace(client, auth, 'enterprise')
        assert client.post('/api/feedback', json={'message': 'something broke', 'screenshot': SHOT, 'page': '#alerts'}, headers=boss).status_code == 201
        sent = json.loads(hook.requests[-1]['body'])
        assert sent['message'] == 'something broke' and sent['page'] == '#alerts' and 'screenshot_base64' not in sent
        monkeypatch.setenv('FEEDBACK_FORWARD_SCREENSHOT', '1')
        client.post('/api/feedback', json={'message': 'again please', 'screenshot': SHOT}, headers=boss)
        assert 'screenshot_base64' in json.loads(hook.requests[-1]['body'])
        hook.status = 500                                                      # a failing forwarder never loses the report
        assert client.post('/api/feedback', json={'message': 'forwarder is down'}, headers=boss).status_code == 201
    finally:
        hook.close()
