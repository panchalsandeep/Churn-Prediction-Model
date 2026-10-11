"""Alert notifications: Slack, a signed generic webhook, and email.

Safety rules, because this code makes the server call addresses that customers type in:
  * Destinations are validated and sealed (always encrypted) at rest, and never shown again in full.
  * Outbound HTTP only goes to public addresses. The hostname is resolved once, every resulting address must be public (no loopback,
    private, link-local, metadata or reserved ranges), and the connection is made to that exact address so a DNS answer cannot change
    between the check and the call. Redirects are never followed. Timeouts and response sizes are capped.
  * Messages are digests (one per scoring run per channel). Account identifiers and amounts are included only if the channel opted in:
    sending them to Slack or email means sending customer data to a third party.
  * A failing channel never fails scoring: errors are logged to the channel's delivery log and swallowed.

Not verified against live Slack or a live SMTP server in development: Slack uses the documented incoming-webhook JSON body, email uses
standard SMTP with STARTTLS. Both are exercised in tests against local stand-ins.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import ssl
import time
import uuid
from email.message import EmailMessage
from urllib.parse import urlsplit

from . import crypto, db, safehttp, service

log = logging.getLogger('churnsight.notify')
KINDS = ('slack', 'webhook', 'email')
MAX_CHANNELS = 5
SEVERITIES = ('Medium', 'High')
MAX_LINES = 10
TIMEOUT_S = 5
_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


class NotifyError(ValueError):
    """Bad channel settings or an unsafe destination (HTTP 400)."""


# ── safe outbound HTTP (see safehttp.py) ───────────────────────────────────────────────────────────

def resolve_safe(url: str):
    try:
        return safehttp.resolve_safe(url)
    except safehttp.UnsafeDestination as exc:
        raise NotifyError(str(exc)) from exc


def post_json(url: str, payload: dict, headers: dict | None = None) -> int:
    status, _ = safehttp.request('POST', url, json.dumps(payload).encode(), {'Content-Type': 'application/json', **(headers or {})}, TIMEOUT_S)
    return status                                        # 3xx is returned as-is: redirects are not followed


# ── channels ────────────────────────────────────────────────────────────────────────────────────────

def _load(ws: str) -> list[dict]:
    return service.kv_get(ws, 'channels', []) or []


def _store(ws: str, channels: list[dict]) -> None:
    service.kv_set(ws, 'channels', channels)


def _secret(ws: str, ch: dict) -> dict:
    return crypto.unseal_json(ws, ch['sealed'], f"channel|{ch['id']}")


def _mask(ws: str, ch: dict) -> dict:
    s = _secret(ws, ch)
    if ch['kind'] == 'email':
        shown = ', '.join(f"{r[0]}***@{r.split('@')[1]}" for r in s['to'])
    else:
        host = urlsplit(s['url']).hostname or ''
        shown = f'{host}/…'
    return {'id': ch['id'], 'kind': ch['kind'], 'name': ch['name'], 'min_severity': ch['min_severity'], 'include_accounts': ch['include_accounts'],
            'enabled': ch['enabled'], 'destination': shown, 'last': ch.get('last')}


def list_channels(ws: str) -> list[dict]:
    return [_mask(ws, c) for c in _load(ws)]


def add_channel(ws: str, body: dict, actor: str | None) -> dict:
    kind = body.get('kind')
    if kind not in KINDS:
        raise NotifyError(f'kind must be one of: {", ".join(KINDS)}.')
    channels = _load(ws)
    if len(channels) >= MAX_CHANNELS:
        raise NotifyError(f'At most {MAX_CHANNELS} channels per workspace.')
    sev = body.get('min_severity', 'High')
    if sev not in SEVERITIES:
        raise NotifyError('min_severity must be Medium or High.')
    cid = uuid.uuid4().hex[:12]
    if kind == 'email':
        to = [str(r).strip() for r in (body.get('to') or []) if str(r).strip()]
        if not to or len(to) > 10 or not all(_EMAIL.match(r) for r in to):
            raise NotifyError('Give between 1 and 10 valid email addresses in "to".')
        if not os.environ.get('SMTP_HOST'):
            raise NotifyError('Email is not configured on this server (SMTP_HOST).')
        secret = {'to': to}
    else:
        url = str(body.get('url') or '').strip()
        resolve_safe(url)                                   # reject unsafe destinations at configuration time, and again at send time
        secret = {'url': url}
        if kind == 'webhook':
            secret['signing_secret'] = 'whsec_' + uuid.uuid4().hex
    ch = {'id': cid, 'kind': kind, 'name': str(body.get('name') or kind).strip()[:60], 'min_severity': sev,
          'include_accounts': bool(body.get('include_accounts', False)), 'enabled': True,
          'sealed': crypto.seal_json(ws, secret, f'channel|{cid}')}
    channels.append(ch)
    _store(ws, channels)
    db.audit(ws, actor, 'channel.add', {'id': cid, 'kind': kind, 'include_accounts': ch['include_accounts']})
    out = _mask(ws, ch)
    if kind == 'webhook':
        out['signing_secret'] = secret['signing_secret']      # shown once so the receiver can verify signatures
    return out


def update_channel(ws: str, cid: str, body: dict, actor: str | None) -> dict | None:
    channels = _load(ws)
    ch = next((c for c in channels if c['id'] == cid), None)
    if not ch:
        return None
    if 'enabled' in body:
        ch['enabled'] = bool(body['enabled'])
    if body.get('min_severity') in SEVERITIES:
        ch['min_severity'] = body['min_severity']
    if 'include_accounts' in body:
        ch['include_accounts'] = bool(body['include_accounts'])
    _store(ws, channels)
    db.audit(ws, actor, 'channel.update', {'id': cid, 'enabled': ch['enabled'], 'include_accounts': ch['include_accounts']})
    return _mask(ws, ch)


def remove_channel(ws: str, cid: str, actor: str | None) -> bool:
    channels = _load(ws)
    keep = [c for c in channels if c['id'] != cid]
    if len(keep) == len(channels):
        return False
    _store(ws, keep)
    db.audit(ws, actor, 'channel.remove', {'id': cid})
    return True


# ── messages and delivery ───────────────────────────────────────────────────────────────────────────

def _app_url() -> str:
    return os.environ.get('APP_URL', '').rstrip('/')


def digest(ws_name: str, alerts: list[dict], include_accounts: bool) -> dict:
    total = sum(a.get('expected_loss') or 0 for a in alerts)
    high = [a for a in alerts if a['severity'] == 'High']
    title = f'{len(alerts)} new churn alert{"s" if len(alerts) != 1 else ""} in {ws_name}'
    lines = [f'{len(high)} high severity, about {total:,.0f} of revenue at risk across these accounts.']
    if include_accounts:
        for a in sorted(alerts, key=lambda a: -(a.get('expected_loss') or 0))[:MAX_LINES]:
            sig = (db.loads(a.get('signals'), []) or [])[:1]
            lines.append(f"• {a['customer_id']}: {a['title']} ({(a.get('expected_loss') or 0):,.0f} at risk)" + (f' — {sig[0]}' if sig else ''))
        if len(alerts) > MAX_LINES:
            lines.append(f'…and {len(alerts) - MAX_LINES} more.')
    else:
        lines.append('Account details are not included in this channel (turn on "include accounts" to add them).')
    if _app_url():
        lines.append(f'Open ChurnSight: {_app_url()}/#alerts')
    return {'title': title, 'text': '\n'.join(lines), 'count': len(alerts), 'high': len(high), 'revenue_at_risk': round(total, 2)}


def _send_email(to: list[str], msg: dict) -> None:
    host, port = os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT', '587'))
    m = EmailMessage()
    m['Subject'], m['From'], m['To'] = msg['title'], os.environ.get('SMTP_FROM', 'churnsight@localhost'), ', '.join(to)
    m.set_content(msg['text'])
    with smtplib.SMTP(host, port, timeout=TIMEOUT_S) as s:
        if os.environ.get('SMTP_STARTTLS', '1') == '1':
            s.starttls(context=ssl.create_default_context())
        if os.environ.get('SMTP_USER'):
            s.login(os.environ['SMTP_USER'], os.environ.get('SMTP_PASSWORD', ''))
        s.send_message(m)


def _deliver(ws: str, ch: dict, msg: dict) -> str:
    secret = _secret(ws, ch)
    if ch['kind'] == 'email':
        _send_email(secret['to'], msg)
        return 'sent'
    if ch['kind'] == 'slack':
        status = post_json(secret['url'], {'text': f"*{msg['title']}*\n{msg['text']}"})
    else:
        payload = {'event': 'churn.alerts', 'title': msg['title'], 'text': msg['text'], 'count': msg['count'], 'high': msg['high'],
                   'revenue_at_risk': msg['revenue_at_risk'], 'sent_at': int(time.time())}
        raw = json.dumps(payload)
        sig = hmac.new(secret['signing_secret'].encode(), raw.encode(), hashlib.sha256).hexdigest()
        status = post_json(secret['url'], payload, {'X-ChurnSight-Signature': f'sha256={sig}'})
    if not 200 <= status < 300:
        raise RuntimeError(f'destination answered HTTP {status}')
    return f'HTTP {status}'


def _record(ws: str, cid: str, ok: bool, detail: str) -> None:
    channels = _load(ws)
    for c in channels:
        if c['id'] == cid:
            c['last'] = {'at': time.time(), 'ok': ok, 'detail': detail[:160]}
    _store(ws, channels)


def send_to(ws: str, ch: dict, msg: dict) -> tuple[bool, str]:
    try:
        detail = _deliver(ws, ch, msg)
        ok = True
    except Exception as exc:  # noqa: BLE001  (network errors, unsafe destination, bad status)
        ok, detail = False, f'{type(exc).__name__}: {exc}'
        log.warning('channel %s (%s) failed: %s', ch['id'], ch['kind'], detail)
    _record(ws, ch['id'], ok, detail)
    return ok, detail


def test_channel(ws: str, cid: str) -> tuple[bool, str] | None:
    ch = next((c for c in _load(ws) if c['id'] == cid), None)
    if not ch:
        return None
    return send_to(ws, ch, {'title': 'ChurnSight test notification', 'text': 'If you can read this, the channel works.', 'count': 0, 'high': 0, 'revenue_at_risk': 0})


def on_new_alerts(ws: str, new_alerts: list[dict]) -> None:
    """Listener registered with service.regenerate_alerts: announce alerts created by this run, once per channel."""
    if not new_alerts:
        return
    channels = [c for c in _load(ws) if c.get('enabled')]
    if not channels:
        return
    from . import tenancy
    name = (tenancy.get_workspace(ws) or {}).get('name') or ws
    for ch in channels:
        wanted = [a for a in new_alerts if a['severity'] == 'High' or ch['min_severity'] == 'Medium']
        if wanted:
            send_to(ws, ch, digest(name, wanted, ch['include_accounts']))


service.alert_listeners.append(on_new_alerts)
