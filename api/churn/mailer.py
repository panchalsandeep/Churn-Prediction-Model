"""Branded transactional email: invitation, password reset, email verification, welcome.

Each message is rendered as table-based HTML with inline styles (what Gmail, Outlook and Apple Mail render reliably) plus a
plain-text alternative, and sent over SMTP using the same settings as the alert channels:
SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_STARTTLS (1), SMTP_FROM, and APP_URL for links.
Optional: EMAIL_FROM_NAME (display name, default "ChurnSight"); EMAIL_LOGO_URL / EMAIL_LOGO_REVERSE_URL to override the logo
images, which default to APP_URL/brand/churnsight-logo-email(-reverse).png served by the app.

Sending never blocks or fails a request: messages go out on a background thread and failures are logged.
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

log = logging.getLogger('churnsight.mailer')

TIMEOUT_S = 15

# Brand palette (ChurnSight brand guide). Contrast on white checked to WCAG AA:
# Electric Blue 4.65:1 (buttons with white text, links), Slate 4.76:1 (secondary text), Deep Indigo 16.6:1 (text).
NAVY = '#071426'        # Midnight Navy: dark surfaces (footer)
INDIGO = '#10194B'      # Deep Indigo: headings and primary text
BLUE = '#176BFA'        # Electric Blue: primary actions and links
BRIGHT = '#1688F8'      # Bright Blue: wordmark and highlights (large text only on white)
CYAN = '#09C9E8'        # Cyan: data highlights and accents
TEAL = '#13D9B4'        # Teal: AI / intelligence accents
VIOLET = '#6338F5'      # Violet: gradient start, secondary accent
PAGE = '#F7F9FC'        # Page background
LINE = '#DCE4EE'        # Borders and dividers
MUTED = '#64748B'       # Slate: secondary text
INK = INDIGO
BOX = PAGE
BRAND_GRADIENT = f'linear-gradient(90deg,{VIOLET} 0%,{BLUE} 50%,{CYAN} 100%)'

FEATURES = [
    ('Risk scoring', 'Calibrated churn probability for every account, ranked by revenue at risk'),
    ('Why this score', 'Plain-language drivers behind each prediction, plus a what-if simulator'),
    ('Alerts &amp; playbooks', 'Open, assign and resolve risks; log what your team did and what it saved'),
    ('Model health', 'Drift monitoring, realized accuracy and safe promote / rollback'),
]


def configured() -> bool:
    return bool(os.environ.get('SMTP_HOST'))


def app_url() -> str:
    return (os.environ.get('APP_URL') or '').rstrip('/')


def _e(value) -> str:
    return html.escape(str(value), quote=True)


# ── layout ──────────────────────────────────────────────────────────────────────────────────────────

def _logo_url(reverse: bool = False) -> str:
    """PNG logo for email (Gmail and Outlook do not render SVG). Served by the app at /brand/; override with EMAIL_LOGO_URL."""
    if not reverse and os.environ.get('EMAIL_LOGO_URL'):
        return os.environ['EMAIL_LOGO_URL']
    if reverse and os.environ.get('EMAIL_LOGO_REVERSE_URL'):
        return os.environ['EMAIL_LOGO_REVERSE_URL']
    if not app_url():
        return ''
    return f"{app_url()}/brand/churnsight-logo-email{'-reverse' if reverse else ''}.png"


def _wordmark(color_churn: str, color_sight: str, size: int) -> str:
    return (f'<span style="font:700 {size}px/1.2 Helvetica,Arial,sans-serif;color:{color_churn};letter-spacing:-0.5px;">'
            f'Churn<span style="color:{color_sight};">Sight</span></span>')


def _logo_html(reverse: bool = False, height: int = 52) -> str:
    """Logo image with a text wordmark as alt/fallback (shown when images are blocked or no URL is configured)."""
    url = _logo_url(reverse)
    if url:
        width = round(height * 440 / 103)
        return (f'<img src="{_e(url)}" alt="ChurnSight" width="{width}" height="{height}" '
                f'style="display:block;margin:0 auto;width:{width}px;height:{height}px;border:0;outline:none;text-decoration:none;'
                f'font:700 26px/1.2 Helvetica,Arial,sans-serif;color:{"#ffffff" if reverse else INDIGO};">')
    return (_wordmark('#ffffff', CYAN, 24) if reverse else _wordmark(INDIGO, BRIGHT, 30))


def _button(label: str, url: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:28px auto 8px;"><tr>'
        f'<td style="background:{BLUE};border-radius:10px;">'
        f'<a href="{_e(url)}" style="display:inline-block;padding:15px 34px;font:700 16px/1 Helvetica,Arial,sans-serif;'
        f'color:#ffffff;text-decoration:none;border-radius:10px;">{label} &rarr;</a></td></tr></table>'
    )


def _info_box(rows: list[tuple[str, str, str]]) -> str:
    """rows: (label, value_html, kind) where kind is 'link', 'email', 'code' or 'text'."""
    cells = []
    for label, value, kind in rows:
        if kind == 'code':
            val = (f'<div style="display:inline-block;margin-top:6px;padding:12px 18px;background:#ffffff;border:1px solid {LINE};'
                   f'border-radius:8px;font:700 20px/1.2 \'SFMono-Regular\',Menlo,Consolas,monospace;letter-spacing:1px;color:{INK};">{value}</div>')
        elif kind == 'link':
            val = f'<div style="margin-top:4px;font:600 16px/1.4 Helvetica,Arial,sans-serif;color:{BLUE};word-break:break-all;">{value}</div>'
        elif kind == 'email':
            val = f'<div style="margin-top:4px;font:600 16px/1.4 Helvetica,Arial,sans-serif;color:{INK};word-break:break-all;">{value}</div>'
        else:
            val = f'<div style="margin-top:4px;font:600 16px/1.4 Helvetica,Arial,sans-serif;color:{INK};">{value}</div>'
        cells.append(f'<tr><td style="padding:10px 0;"><div style="font:400 14px/1.4 Helvetica,Arial,sans-serif;color:{MUTED};">{_e(label)}</div>{val}</td></tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:24px 0;background:{BOX};'
            f'border:1px solid {LINE};border-radius:12px;"><tr><td style="padding:14px 24px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{"".join(cells)}</table></td></tr></table>')


def _features() -> str:
    rows = []
    for i, (title, text) in enumerate(FEATURES):
        border = f'border-top:1px solid {LINE};' if i else ''
        rows.append(
            f'<tr><td style="{border}padding:14px 20px;"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td valign="top" style="padding-top:3px;"><div style="width:14px;height:14px;border-radius:7px;background:{CYAN};background-image:linear-gradient(135deg,{BLUE},{TEAL});"></div></td>'
            f'<td style="padding-left:16px;"><div style="font:700 16px/1.4 Helvetica,Arial,sans-serif;color:{INK};">{title}</div>'
            f'<div style="font:400 14px/1.5 Helvetica,Arial,sans-serif;color:{MUTED};">{text}</div></td></tr></table></td></tr>')
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 4px;background:{BOX};'
            f'border-radius:12px;">{"".join(rows)}</table>')


def _p(text_html: str, size: int = 17, color: str = INK, margin: str = '0 0 16px') -> str:
    return f'<p style="margin:{margin};font:400 {size}px/1.6 Helvetica,Arial,sans-serif;color:{color};">{text_html}</p>'


def _layout(preheader: str, title: str, subtitle: str, body_html: str, footnote: str) -> str:
    year = time.gmtime().tm_year
    host = (app_url().split('://', 1)[-1] or 'churnsight.up.railway.app')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>{_e(title)}</title></head>
<body style="margin:0;padding:0;background:{PAGE};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_e(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PAGE};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:18px;overflow:hidden;border:1px solid {LINE};">
  <tr><td style="height:6px;line-height:6px;font-size:0;background:{BLUE};background-image:{BRAND_GRADIENT};">&nbsp;</td></tr>
  <tr><td style="padding:36px 40px 0;background:{PAGE};background-image:radial-gradient(circle at 92% 0%,rgba(9,201,232,0.16) 0,rgba(9,201,232,0) 45%),radial-gradient(circle at 8% 100%,rgba(99,56,245,0.10) 0,rgba(99,56,245,0) 40%);">
    {_logo_html()}
    <div style="height:28px;line-height:28px;">&nbsp;</div>
  </td></tr>
  <tr><td style="padding:0 40px;"><div style="border-top:1px solid {LINE};"></div></td></tr>
  <tr><td style="padding:32px 40px 8px;text-align:center;">
    <h1 style="margin:0;font:800 30px/1.25 Helvetica,Arial,sans-serif;color:{INDIGO};letter-spacing:-0.5px;">{title}</h1>
    <p style="margin:12px 0 0;font:400 15px/1.5 Helvetica,Arial,sans-serif;color:{MUTED};">{subtitle}</p>
  </td></tr>
  <tr><td style="padding:24px 40px 8px;">{body_html}</td></tr>
  <tr><td style="padding:8px 40px 32px;text-align:center;">
    <p style="margin:0;font:400 13px/1.6 Helvetica,Arial,sans-serif;color:{MUTED};">{footnote}</p>
  </td></tr>
  <tr><td style="background:{NAVY};padding:26px 40px;text-align:center;">
    {_logo_html(reverse=True, height=38)}
    <div style="margin-top:8px;font:400 12px/1.5 Helvetica,Arial,sans-serif;color:#8FA3BF;">&copy; {year} ChurnSight &middot; {_e(host)}</div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


# ── messages ────────────────────────────────────────────────────────────────────────────────────────

ROLE_TEXT = {'viewer': 'Viewer (read-only)', 'analyst': 'Analyst', 'admin': 'Admin', 'owner': 'Owner'}


def render(kind: str, **c) -> tuple[str, str, str]:
    """Return (subject, html, text) for one message kind."""
    name = _e(c.get('name') or 'there')
    app = app_url()
    if kind == 'invite':
        ws, role, inviter = _e(c['workspace']), _e(ROLE_TEXT.get(c.get('role'), c.get('role', ''))), _e(c.get('inviter') or 'An administrator')
        link = c['link']
        days = c.get('expires_days', 7)
        subject = f"You're invited to {c['workspace']} on ChurnSight"
        body = (_p(f'Hi {name},')
                + _p(f'{inviter} has invited you to join the <strong>{ws}</strong> workspace in <strong>ChurnSight</strong>, '
                     'the platform that predicts which customers are about to churn, explains why, and tracks what your team does about it.')
                + _info_box([('Workspace', ws, 'text'), ('Your role', role, 'text'), ('Invited email', _e(c['email']), 'email'),
                             ('Invitation code', _e(c['token']), 'code')])
                + _p(f'Click the button and sign in (or create an account) with <strong>{_e(c["email"])}</strong>. The invitation works once, '
                     f'only for this email address, and expires in {days} days.', 15, MUTED)
                + _features() + _button('Accept invitation', link))
        html_ = _layout(f'{c.get("inviter") or "An administrator"} invited you to {c["workspace"]}', "You're invited",
                        f'{ws} &middot; ChurnSight', body,
                        'If you weren’t expecting this invitation, you can ignore this email. Nothing happens unless you accept it.')
        text = (f"Hi {c.get('name') or 'there'},\n\n{c.get('inviter') or 'An administrator'} has invited you to the {c['workspace']} workspace "
                f"in ChurnSight as {ROLE_TEXT.get(c.get('role'), c.get('role', ''))}.\n\nAccept: {link}\nInvitation code: {c['token']}\n"
                f"Sign in with {c['email']}. The invitation works once and expires in {days} days.\n")
    elif kind == 'password_reset':
        link = c['link']
        subject = 'Reset your ChurnSight password'
        body = (_p(f'Hi {name},')
                + _p('We received a request to reset the password for your ChurnSight account. Click the button below to choose a new one.')
                + _info_box([('Account', _e(c['email']), 'email'), ('Sign in at', _e(app or 'ChurnSight'), 'link')])
                + _p('The link works once and expires in 1 hour. If it has expired, request a new one from the sign-in page with '
                     '<em>Forgot your password?</em>', 15, MUTED)
                + _button('Reset password', link))
        html_ = _layout('Choose a new password for your ChurnSight account', 'Reset your password', 'ChurnSight account security', body,
                        'Didn’t ask for this? You can ignore this email — your password stays the same. '
                        'If you keep getting these, tell your administrator.')
        text = (f"Hi {c.get('name') or 'there'},\n\nReset your ChurnSight password: {link}\n\nThe link works once and expires in 1 hour. "
                "If you didn't ask for this, ignore this email; your password stays the same.\n")
    elif kind == 'verify_email':
        link = c['link']
        subject = 'Confirm your email for ChurnSight'
        body = (_p(f'Hi {name},')
                + _p('Please confirm that this is your email address. Confirming protects your workspace and lets you use invitations, '
                     'password reset and alerts.')
                + _info_box([('Email', _e(c['email']), 'email')])
                + _button('Confirm email', link)
                + _p('The link expires after a while; you can request a new one from the app.', 14, MUTED, '20px 0 0'))
        html_ = _layout('Confirm your email address for ChurnSight', 'Confirm your email', 'One quick step to secure your account', body,
                        'Didn’t create a ChurnSight account? Ignore this email and the address won’t be confirmed.')
        text = f"Hi {c.get('name') or 'there'},\n\nConfirm your email for ChurnSight: {link}\n\nDidn't sign up? Ignore this email.\n"
    elif kind == 'welcome':
        ws = _e(c.get('workspace') or 'Your workspace')
        subject = 'Your ChurnSight workspace is ready'
        body = (_p(f'Hi {name},')
                + _p(f'Welcome to <strong>ChurnSight</strong>. Your workspace <strong>{ws}</strong> is ready. Upload a customer CSV '
                     '(or try the built-in demo with 8,000 sample customers) and get a ranked list of accounts at risk in about a minute.')
                + _info_box([('Sign in at', _e(app or 'ChurnSight'), 'link'), ('Email', _e(c['email']), 'email'),
                             ('Workspace', ws, 'text')])
                + _features() + _button('Open ChurnSight', app or '#'))
        html_ = _layout('Your ChurnSight workspace is ready', 'Your workspace is ready', f'{ws} &middot; ChurnSight', body,
                        'You’re receiving this because an account was created with this email address. '
                        'If that wasn’t you, reply to this email.')
        text = (f"Hi {c.get('name') or 'there'},\n\nWelcome to ChurnSight. Your workspace {c.get('workspace') or ''} is ready.\n"
                f"Sign in: {app}\nEmail: {c['email']}\n")
    else:
        raise ValueError(f'Unknown email kind: {kind}')
    return subject, html_, text


# ── delivery ────────────────────────────────────────────────────────────────────────────────────────

def _smtp_send(to: str, subject: str, html_: str, text: str) -> None:
    host, port = os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT', '587'))
    sender = os.environ.get('SMTP_FROM', 'churnsight@localhost')
    m = EmailMessage()
    m['Subject'] = subject
    m['From'] = formataddr((os.environ.get('EMAIL_FROM_NAME', 'ChurnSight'), sender))
    m['To'] = to
    m['Message-ID'] = make_msgid(domain=sender.split('@')[-1] or None)
    m.set_content(text)
    m.add_alternative(html_, subtype='html')
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_S, context=ssl.create_default_context()) as s:
            if os.environ.get('SMTP_USER'):
                s.login(os.environ['SMTP_USER'], os.environ.get('SMTP_PASSWORD', ''))
            s.send_message(m)
        return
    with smtplib.SMTP(host, port, timeout=TIMEOUT_S) as s:
        if os.environ.get('SMTP_STARTTLS', '1') == '1':
            s.starttls(context=ssl.create_default_context())
        if os.environ.get('SMTP_USER'):
            s.login(os.environ['SMTP_USER'], os.environ.get('SMTP_PASSWORD', ''))
        s.send_message(m)


def send(kind: str, to: str, *, background: bool = True, **context) -> bool:
    """Render and send one message. Returns False (and logs) when email is not configured."""
    if not configured() or not to:
        log.info('email %s not sent (SMTP_HOST not configured)', kind)
        return False
    subject, html_, text = render(kind, email=to, **context)

    def run():
        try:
            _smtp_send(to, subject, html_, text)
            log.info('email %s sent', kind)
        except Exception as exc:  # noqa: BLE001  never let mail break the caller
            log.warning('email %s failed: %s', kind, type(exc).__name__)

    if background:
        threading.Thread(target=run, name=f'mail-{kind}', daemon=True).start()
    else:
        run()
    return True
