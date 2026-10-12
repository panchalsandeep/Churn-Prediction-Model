"""In-app bug reports and questions, with an optional screenshot.

A screenshot of a analytics dashboard can contain customer data, so reports are stored like everything else here: encrypted with the
workspace key, visible to that workspace's admins only, deleted with the workspace. The screenshot is optional and the sender sees
exactly what will be attached. If the operator configures FEEDBACK_WEBHOOK_URL the text and page metadata are forwarded there
(through safehttp, never the screenshot unless FEEDBACK_FORWARD_SCREENSHOT=1).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import uuid

from . import crypto, db, safehttp, service, storage

log = logging.getLogger('churnsight.feedback')
MAX_MESSAGE = 4000
MAX_IMAGE = 2 * 1024 * 1024
KEEP = 100
_DATA_URL = re.compile(r'^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\s]+)$')
_MAGIC = {'png': b'\x89PNG\r\n\x1a\n', 'jpeg': b'\xff\xd8\xff', 'webp': b'RIFF'}


class FeedbackError(ValueError):
    pass


def _prefix(ws: str) -> str:
    return storage.model_prefix(ws).replace('models/', 'feedback/', 1)


def _clean(value, limit: int) -> str:
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value or ''))[:limit]


def submit(ws: str, actor: str | None, body: dict) -> dict:
    message = _clean(body.get('message'), MAX_MESSAGE).strip()
    if len(message) < 3:
        raise FeedbackError('Please describe what happened (a sentence is enough).')
    image = None
    shot = body.get('screenshot')
    if shot:
        m = _DATA_URL.match(str(shot)[:3_000_000])
        if not m:
            raise FeedbackError('The screenshot must be a PNG, JPEG or WebP image.')
        image = base64.b64decode(m.group(2))
        if len(image) > MAX_IMAGE or not image.startswith(_MAGIC[m.group(1)]):
            raise FeedbackError('The screenshot is too large or not a valid image.')
    rid = uuid.uuid4().hex[:12]
    meta = {'id': rid, 'at': time.time(), 'by': actor, 'page': _clean(body.get('page'), 200), 'viewport': _clean(body.get('viewport'), 40),
            'agent': _clean(body.get('agent'), 300), 'app_version': _clean(body.get('app_version'), 40), 'request_ids': [_clean(r, 64) for r in (body.get('request_ids') or [])[:5]]}
    record = {**meta, 'message': message, 'screenshot': base64.b64encode(image).decode() if image else None, 'screenshot_type': (m.group(1) if image else None)}
    storage.get_storage().put(f'{_prefix(ws)}/{rid}.cse', crypto.encrypt_bytes(ws, json.dumps(record).encode(), f'feedback|{rid}'))
    index = [{**meta, 'has_screenshot': bool(image), 'preview': message[:80]}] + (service.kv_get(ws, 'feedback', []) or [])
    for old in index[KEEP:]:
        storage.get_storage().delete(f'{_prefix(ws)}/{old["id"]}.cse')
    service.kv_set(ws, 'feedback', index[:KEEP])
    db.audit(ws, actor, 'feedback.submit', {'id': rid, 'screenshot': bool(image)})
    _forward(meta, message, image)
    return {'id': rid}


def _forward(meta: dict, message: str, image: bytes | None) -> None:
    url = os.environ.get('FEEDBACK_WEBHOOK_URL')
    if not url:
        return
    payload = {'event': 'feedback', **meta, 'message': message}
    if image and os.environ.get('FEEDBACK_FORWARD_SCREENSHOT') == '1':
        payload['screenshot_base64'] = base64.b64encode(image).decode()
    try:
        safehttp.request('POST', url, json.dumps(payload).encode(), {'Content-Type': 'application/json'}, 5)
    except Exception:  # noqa: BLE001  (a broken forwarder must never lose or block the report)
        log.warning('feedback forwarding failed', exc_info=True)


def list_reports(ws: str) -> list[dict]:
    return service.kv_get(ws, 'feedback', []) or []


def get_report(ws: str, rid: str) -> dict | None:
    if not re.fullmatch(r'[0-9a-f]{12}', rid or ''):
        return None
    try:
        raw = storage.get_storage().get(f'{_prefix(ws)}/{rid}.cse')
    except (FileNotFoundError, storage.StorageError):
        return None
    return json.loads(crypto.decrypt_bytes(ws, raw, f'feedback|{rid}'))
