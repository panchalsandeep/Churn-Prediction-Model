"""Admin console login (/api/admin/login): disabled by default, constant credentials check, signed expiring token, rate limit."""
from __future__ import annotations

import pytest

SECRET = 'x' * 40


@pytest.fixture()
def console(client, monkeypatch):
    from churn.security import limiter
    monkeypatch.setenv('ADMIN_PASSWORD', 'correct horse battery staple')
    monkeypatch.setenv('ADMIN_TOKEN_SECRET', SECRET)
    limiter.reset()
    yield client
    limiter.reset()


def _login(c, username='admin', password='correct horse battery staple'):
    return c.post('/api/admin/login', json={'username': username, 'password': password})


def test_disabled_without_password_or_secret(client, monkeypatch):
    monkeypatch.delenv('ADMIN_PASSWORD', raising=False)
    monkeypatch.setenv('ADMIN_TOKEN_SECRET', SECRET)
    assert client.post('/api/admin/login', json={'username': 'admin', 'password': ''}).status_code == 503
    monkeypatch.setenv('ADMIN_PASSWORD', 'pw')
    monkeypatch.setenv('ADMIN_TOKEN_SECRET', 'too-short')
    monkeypatch.delenv('FLASK_SECRET_KEY', raising=False)
    assert client.post('/api/admin/login', json={'username': 'admin', 'password': 'pw'}).status_code == 503
    assert client.get('/api/admin/session').get_json()['enabled'] is False


def test_wrong_credentials_rejected(console):
    assert _login(console, password='nope').status_code == 401
    assert _login(console, username='root').status_code == 401


def test_token_grants_platform_admin_routes(console):
    r = _login(console)
    assert r.status_code == 200
    token = r.get_json()['token']
    s = console.get('/api/admin/session', headers={'X-Admin-Token': token}).get_json()
    assert s['is_admin'] is True and s['username'] == 'admin'
    # Platform-admin route accepts the console token (Firebase is not configured in tests, so it answers 501, not 401/403).
    assert console.get('/api/admin/users', headers={'X-Admin-Token': token}).status_code == 501


def test_tampered_or_expired_token_rejected(console, monkeypatch):
    import index
    token = _login(console).get_json()['token']
    payload, sig = token.rsplit('.', 1)
    forged = payload + '.' + ('0' if sig[0] != '0' else '1') + sig[1:]
    assert console.get('/api/admin/users', headers={'X-Admin-Token': forged}).status_code == 401
    monkeypatch.setattr(index, 'ADMIN_TOKEN_TTL_S', -1)
    expired = index.create_console_token('admin')
    assert index.verify_console_token(expired) is None
    # Rotating the secret signs every session out.
    monkeypatch.setenv('ADMIN_TOKEN_SECRET', 'y' * 40)
    assert index.verify_console_token(token) is None


def test_login_is_rate_limited(console, monkeypatch):
    import index
    monkeypatch.setattr(index, 'ADMIN_LOGIN_PER_MIN', 3)
    codes = [_login(console, password='bad').status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3] == 429
