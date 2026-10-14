"""Branded transactional email: rendering, escaping, SMTP delivery, and the password-reset endpoint's safe defaults."""
from __future__ import annotations

import pytest

KINDS = {
    'invite': dict(name='Priya', workspace='Acme', role='analyst', inviter='boss@acme.com', token='tok123',
                   link='https://app.example/?invite=tok123'),
    'password_reset': dict(name='Priya', link='https://auth.example/reset?oob=1'),
    'verify_email': dict(name='Priya', link='https://auth.example/verify?oob=1'),
    'welcome': dict(name='Priya', workspace="Priya's workspace"),
}


@pytest.mark.parametrize('kind', list(KINDS))
def test_every_kind_renders_html_and_text(kind, monkeypatch):
    from churn import mailer
    monkeypatch.setenv('APP_URL', 'https://churnsight.example')
    subject, html, text = mailer.render(kind, email='priya@acme.com', **KINDS[kind])
    assert subject and 'ChurnSight' in subject
    assert html.startswith('<!doctype html>') and 'ChurnSight' in html and 'priya@acme.com' in html
    assert text.strip()
    if 'link' in KINDS[kind]:
        assert KINDS[kind]['link'].replace('&', '&amp;') in html and KINDS[kind]['link'] in text


def test_user_supplied_values_are_escaped():
    from churn import mailer
    _, html, _ = mailer.render('invite', email='x@y.z', name='<script>alert(1)</script>', workspace='<img src=x onerror=alert(1)>',
                               role='viewer', inviter='"><b>', token='t', link='https://a/?invite=t')
    assert '<script>alert(1)' not in html and '<img src=x' not in html and '"><b>' not in html
    assert '&lt;script&gt;' in html


def test_send_is_a_no_op_without_smtp(monkeypatch):
    from churn import mailer
    monkeypatch.delenv('SMTP_HOST', raising=False)
    assert mailer.send('welcome', 'a@b.c', name='A', workspace='W') is False


def test_send_uses_smtp_with_html_and_text_parts(monkeypatch):
    from churn import mailer
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host, self.port = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            sent.append(('login', user))

        def send_message(self, m):
            sent.append(('msg', m))

    monkeypatch.setattr(mailer.smtplib, 'SMTP', FakeSMTP)
    monkeypatch.setenv('SMTP_HOST', 'smtp.example')
    monkeypatch.setenv('SMTP_USER', 'mailer')
    monkeypatch.setenv('SMTP_FROM', 'no-reply@churnsight.example')
    assert mailer.send('password_reset', 'priya@acme.com', background=False, name='Priya', link='https://r') is True
    msg = [m for k, m in sent if k == 'msg'][0]
    assert msg['To'] == 'priya@acme.com' and 'ChurnSight' in msg['From'] and msg['Subject'] == 'Reset your ChurnSight password'
    types = sorted(p.get_content_type() for p in msg.iter_parts())
    assert types == ['text/html', 'text/plain']


def test_password_reset_endpoint_without_mail_tells_browser_to_fall_back(client, monkeypatch):
    monkeypatch.delenv('SMTP_HOST', raising=False)
    r = client.post('/api/auth/password-reset', json={'email': 'someone@example.com'})
    assert r.status_code == 501 and r.get_json()['code'] == 'email_not_configured'
