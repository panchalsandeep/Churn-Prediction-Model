"""Phase 3: alert channels (Slack, signed webhook, email) and the safety rules around outbound calls."""
from __future__ import annotations

import hashlib
import hmac
import http.server
import io
import json
import socket
import threading

import pytest

from churn import crypto, notify, safehttp, service
from churn.demo_data import generate_demo


class Receiver:
    """A local HTTP server that records what it is sent and answers with a configurable status."""

    def __init__(self):
        self.requests, self.status, self.location = [], 200, None
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                outer.requests.append({'path': self.path, 'headers': dict(self.headers), 'body': body})
                self.send_response(outer.status)
                if outer.location:
                    self.send_header('Location', outer.location)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.server.server_port}/hook'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def hook(monkeypatch):
    monkeypatch.setenv('SAFEHTTP_ALLOW_LOCAL', '1')
    r = Receiver()
    yield r
    r.close()


@pytest.fixture()
def fresh_channels(client, env):
    """Each test starts with no channels in the dev workspace."""
    service.kv_set('local', 'channels', [])
    yield
    service.kv_set('local', 'channels', [])


# ── outbound safety ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('ip', ['127.0.0.1', '10.1.2.3', '192.168.0.9', '172.16.5.5', '169.254.169.254', '::1', 'fe80::1', '0.0.0.0', '::ffff:10.0.0.1',
                                '100.64.1.1', '::ffff:100.64.0.1', '198.18.0.1', 'fd12::1'])
def test_internal_addresses_are_never_public(ip):
    import ipaddress
    assert safehttp.is_public(ipaddress.ip_address(ip)) is False


def test_destinations_are_validated(monkeypatch):
    monkeypatch.delenv('SAFEHTTP_ALLOW_LOCAL', raising=False)
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
    mixed = public + [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.5', 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: public)
    assert notify.resolve_safe('https://hooks.example.com/services/T0/B0/xyz')[4] == '93.184.216.34'
    for bad in ('http://hooks.example.com/x', 'ftp://hooks.example.com/x', 'https://user:pw@hooks.example.com/x', 'https:///nohost', '', 'javascript:alert(1)'):
        with pytest.raises(notify.NotifyError):
            notify.resolve_safe(bad)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: mixed)                     # one internal answer taints the whole name (DNS rebinding)
    with pytest.raises(notify.NotifyError, match='private or internal'):
        notify.resolve_safe('https://sneaky.example.com/x')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 443))])
    with pytest.raises(notify.NotifyError, match='private or internal'):
        notify.resolve_safe('https://metadata.example.com/latest/meta-data')
    with pytest.raises(notify.NotifyError, match='private or internal'):
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
        notify.resolve_safe('https://localhost/x')


def test_redirects_are_not_followed_and_failures_are_reported(client, env, hook, fresh_channels):
    ch = notify.add_channel('local', {'kind': 'slack', 'url': hook.url, 'name': 'ops'}, 'me')
    hook.status, hook.location = 302, 'http://169.254.169.254/latest'
    ok, detail = notify.test_channel('local', ch['id'])
    assert ok is False and '302' in detail and len(hook.requests) == 1                    # one request: the redirect target was never contacted
    hook.status, hook.location = 500, None
    assert notify.test_channel('local', ch['id'])[0] is False
    hook.status = 200
    assert notify.test_channel('local', ch['id']) == (True, 'HTTP 200')
    assert notify.list_channels('local')[0]['last']['ok'] is True


# ── channel configuration ─────────────────────────────────────────────────────────────────────────

def test_destinations_are_sealed_masked_and_validated(client, env, hook, fresh_channels, monkeypatch):
    out = notify.add_channel('local', {'kind': 'slack', 'url': hook.url + '?secret=abc123', 'name': 'ops'}, 'me')
    assert 'abc123' not in json.dumps(out) and out['destination'].startswith('127.0.0.1')
    stored = json.dumps(service.kv_get('local', 'channels'))
    assert 'abc123' not in stored and '127.0.0.1' not in stored and 'sealed1:' in stored   # encrypted at rest
    assert 'abc123' not in json.dumps(client.get('/api/channels').get_json())

    monkeypatch.delenv('SAFEHTTP_ALLOW_LOCAL')
    with pytest.raises(notify.NotifyError):
        notify.add_channel('local', {'kind': 'slack', 'url': hook.url}, 'me')                # loopback refused at configuration time
    with pytest.raises(notify.NotifyError, match='kind'):
        notify.add_channel('local', {'kind': 'sms', 'url': 'https://x.example.com'}, 'me')
    monkeypatch.setenv('SAFEHTTP_ALLOW_LOCAL', '1')
    with pytest.raises(notify.NotifyError, match='min_severity'):
        notify.add_channel('local', {'kind': 'slack', 'url': hook.url, 'min_severity': 'Low'}, 'me')
    for _ in range(notify.MAX_CHANNELS - 1):
        notify.add_channel('local', {'kind': 'slack', 'url': hook.url}, 'me')
    with pytest.raises(notify.NotifyError, match='At most'):
        notify.add_channel('local', {'kind': 'slack', 'url': hook.url}, 'me')


def test_signed_webhooks_can_be_verified_by_the_receiver(client, env, hook, fresh_channels):
    ch = notify.add_channel('local', {'kind': 'webhook', 'url': hook.url}, 'me')
    secret = ch['signing_secret']
    assert 'signing_secret' not in notify.list_channels('local')[0]                           # shown once
    assert notify.test_channel('local', ch['id'])[0] is True
    req = hook.requests[-1]
    expected = 'sha256=' + hmac.new(secret.encode(), req['body'], hashlib.sha256).hexdigest()
    assert req['headers']['X-ChurnSight-Signature'] == expected
    assert json.loads(req['body'])['event'] == 'churn.alerts'


# ── what gets sent ────────────────────────────────────────────────────────────────────────────────

def alerts(n=12):
    return [{'customer_id': f'ACME-{i:03d}', 'severity': 'High' if i % 3 else 'Medium', 'title': f'High churn risk ({60 + i}%)',
             'expected_loss': 1000.0 * (n - i), 'signals': json.dumps([f'tickets_90d = {i} (+4.0 pts)'])} for i in range(n)]


def test_digests_hide_accounts_unless_the_channel_opted_in():
    quiet = notify.digest('Acme', alerts(), include_accounts=False)
    assert 'ACME-' not in quiet['text'] and quiet['count'] == 12 and quiet['revenue_at_risk'] == 78000.0
    loud = notify.digest('Acme', alerts(), include_accounts=True)
    assert 'ACME-000' in loud['text'] and 'and 2 more' in loud['text'] and loud['text'].count('ACME-') == notify.MAX_LINES
    assert 'tickets_90d' in loud['text']


def test_severity_filter_and_one_digest_per_run_through_the_real_alert_flow(client, env, hook, fresh_channels):
    high_only = notify.add_channel('local', {'kind': 'slack', 'url': hook.url, 'name': 'high', 'min_severity': 'High'}, 'me')
    both = notify.add_channel('local', {'kind': 'slack', 'url': hook.url + '2', 'name': 'all', 'min_severity': 'Medium', 'include_accounts': True}, 'me')
    notify.on_new_alerts('local', alerts(6))
    assert len(hook.requests) == 2                                                             # exactly one message per channel
    texts = {r['path']: json.loads(r['body'])['text'] for r in hook.requests}
    assert '4 high severity' in texts['/hook'] and 'ACME-' not in texts['/hook']
    assert 'ACME-000' in texts['/hook2']
    hook.requests.clear()
    notify.on_new_alerts('local', [])
    notify.on_new_alerts('local', [a for a in alerts(6) if a['severity'] == 'Medium'])        # only Medium: the High-only channel stays quiet
    assert [r['path'] for r in hook.requests] == ['/hook2']
    notify.update_channel('local', both['id'], {'enabled': False}, 'me')
    hook.requests.clear()
    notify.on_new_alerts('local', alerts(3))
    assert [r['path'] for r in hook.requests] == ['/hook']


def test_scoring_announces_only_new_alerts_and_survives_a_broken_channel(client, env, hook, fresh_channels, monkeypatch):
    notify.add_channel('local', {'kind': 'slack', 'url': hook.url, 'name': 'ops', 'include_accounts': True}, 'me')
    csv = generate_demo(1500, seed=21).to_csv(index=False).encode()
    res = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(csv), 'd.csv'), 'algorithm': 'logistic_regression'}, content_type='multipart/form-data')
    assert res.get_json()['job']['status'] == 'done'
    assert len(hook.requests) == 1 and 'new churn alert' in json.loads(hook.requests[0]['body'])['text']
    hook.requests.clear()
    service.regenerate_alerts('local', service.get_settings('local'))                           # same scores again: the alerts already exist
    assert hook.requests == []                                                                  # nothing new to say, so nothing is sent

    hook.status = 500                                                                           # a failing destination must not break scoring
    service.kv_set('local', 'channels', service.kv_get('local', 'channels'))
    notify.on_new_alerts('local', alerts(3))
    assert notify.list_channels('local')[0]['last']['ok'] is False


# ── email ─────────────────────────────────────────────────────────────────────────────────────────

class FakeSMTP:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.messages = []
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        f = conn.makefile('rwb')
        send = lambda line: (f.write(line.encode() + b'\r\n'), f.flush())    # noqa: E731
        send('220 fake ESMTP')
        data, in_data = [], False
        while True:
            line = f.readline()
            if not line:
                break
            text = line.decode().rstrip('\r\n')
            if in_data:
                if text == '.':
                    in_data = False
                    self.messages.append('\n'.join(data))
                    send('250 queued')
                else:
                    data.append(text)
                continue
            cmd = text.upper()
            if cmd.startswith('EHLO') or cmd.startswith('HELO'):
                send('250 fake')
            elif cmd.startswith('DATA'):
                in_data = True
                send('354 go')
            elif cmd.startswith('QUIT'):
                send('221 bye')
                break
            else:
                send('250 ok')
        conn.close()


def test_email_channel_sends_a_digest_through_smtp(client, env, fresh_channels, monkeypatch):
    smtp = FakeSMTP()
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', str(smtp.port))
    monkeypatch.setenv('SMTP_STARTTLS', '0')
    monkeypatch.setenv('SMTP_FROM', 'alerts@churnsight.test')
    with pytest.raises(notify.NotifyError, match='valid email'):
        notify.add_channel('local', {'kind': 'email', 'to': ['not-an-address']}, 'me')
    ch = notify.add_channel('local', {'kind': 'email', 'to': ['owner@acme.com'], 'include_accounts': True}, 'me')
    assert ch['destination'] == 'o***@acme.com'
    ok, detail = notify.test_channel('local', ch['id'])
    assert ok, detail
    msg = smtp.messages[0]
    assert 'Subject: ChurnSight test notification' in msg and 'To: owner@acme.com' in msg and 'alerts@churnsight.test' in msg


def test_email_is_refused_when_the_server_has_no_smtp(client, env, fresh_channels, monkeypatch):
    monkeypatch.delenv('SMTP_HOST', raising=False)
    with pytest.raises(notify.NotifyError, match='not configured'):
        notify.add_channel('local', {'kind': 'email', 'to': ['a@b.co']}, 'me')


# ── permissions ───────────────────────────────────────────────────────────────────────────────────

def test_only_admins_manage_channels(client, env, hook, fresh_channels):
    from test_phase0_access import auth  # noqa: F401
    r = client.post('/api/channels', json={'kind': 'slack', 'url': hook.url})
    assert r.status_code == 201                                                                  # dev mode acts as the owner
    cid = r.get_json()['id']
    assert client.patch(f'/api/channels/{cid}', json={'enabled': False}).get_json()['enabled'] is False
    assert client.post(f'/api/channels/{cid}/test').get_json()['ok'] is True
    assert client.delete(f'/api/channels/{cid}').status_code == 200
    assert client.delete(f'/api/channels/{cid}').status_code == 404
