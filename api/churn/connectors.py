"""HTTP pull connectors: fetch JSON or CSV from a URL on a schedule and load it into one of the workspace's data tables.

This is the generic building block. Product-specific connectors (CRM, billing, support desk) are configurations of it: the URL and
headers of that vendor's list endpoint, `records_path` pointing at the array in its response, `next_path` at its pagination link, and a
`mapping` from its field names to ours. None of those vendor configurations has been run against the real services in development; what
is tested is the mechanism, against a local server.

The URL and request headers (which usually hold an API token) are sealed with the workspace key, never returned by the API, and every
request goes through safehttp (public addresses only, no redirects, size and time limits).
"""
from __future__ import annotations

import io
import json
import logging
import time
import uuid
from urllib.parse import quote, urlsplit

import pandas as pd

from . import crypto, db, ingest, safehttp, service

log = logging.getLogger('churnsight.connectors')
MAX_SOURCES = 5
MAX_PAGES = 20
MAX_BYTES = 20 * 1024 * 1024
TIMEOUT_S = 20
FORMATS = ('json', 'csv')


class SourceError(ValueError):
    """Bad source settings or a failed fetch (HTTP 400 when configuring, recorded on the source when syncing)."""


def _load(ws: str) -> list[dict]:
    return service.kv_get(ws, 'sources', []) or []


def _store(ws: str, sources: list[dict]) -> None:
    service.kv_set(ws, 'sources', sources)


def _secret(ws: str, src: dict) -> dict:
    return crypto.unseal_json(ws, src['sealed'], f"source|{src['id']}")


def _public(ws: str, src: dict) -> dict:
    host = urlsplit(_secret(ws, src)['url']).hostname or ''
    return {k: src[k] for k in ('id', 'name', 'role', 'format', 'records_path', 'next_path', 'since_param', 'mapping', 'mode', 'enabled', 'last')} | {'host': host}


def list_sources(ws: str) -> list[dict]:
    return [_public(ws, s) for s in _load(ws)]


def add_source(ws: str, body: dict, actor: str | None) -> dict:
    sources = _load(ws)
    if len(sources) >= MAX_SOURCES:
        raise SourceError(f'At most {MAX_SOURCES} sources per workspace.')
    role = body.get('role')
    if role not in ingest.ROLES:
        raise SourceError(f'role must be one of: {", ".join(ingest.ROLES)}.')
    fmt = body.get('format', 'json')
    if fmt not in FORMATS:
        raise SourceError('format must be json or csv.')
    mode = body.get('mode', 'replace' if role == 'accounts' else 'append')
    if mode not in ('append', 'replace'):
        raise SourceError('mode must be append or replace.')
    url = str(body.get('url') or '').strip()
    try:
        safehttp.resolve_safe(url)
    except safehttp.UnsafeDestination as exc:
        raise SourceError(str(exc)) from exc
    headers = body.get('headers') or {}
    if not isinstance(headers, dict) or len(headers) > 10 or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise SourceError('headers must be an object of up to 10 text values.')
    if any(k.lower() in ('host', 'content-length', 'transfer-encoding', 'connection') for k in headers):
        raise SourceError('That header cannot be set.')
    mapping = body.get('mapping') or {}
    if not isinstance(mapping, dict):
        raise SourceError('mapping must be an object.')
    sid = uuid.uuid4().hex[:12]
    src = {'id': sid, 'name': str(body.get('name') or role).strip()[:60], 'role': role, 'format': fmt, 'records_path': str(body.get('records_path') or '')[:200],
           'next_path': str(body.get('next_path') or '')[:200], 'since_param': str(body.get('since_param') or '')[:40],
           'mapping': {str(k): str(v) for k, v in mapping.items()}, 'mode': mode, 'enabled': True, 'last': None,
           'sealed': crypto.seal_json(ws, {'url': url, 'headers': headers}, f'source|{sid}')}
    sources.append(src)
    _store(ws, sources)
    db.audit(ws, actor, 'source.add', {'id': sid, 'role': role, 'format': fmt})
    return _public(ws, src)


def remove_source(ws: str, sid: str, actor: str | None) -> bool:
    sources = _load(ws)
    keep = [s for s in sources if s['id'] != sid]
    if len(keep) == len(sources):
        return False
    _store(ws, keep)
    db.audit(ws, actor, 'source.remove', {'id': sid})
    return True


def set_enabled(ws: str, sid: str, enabled: bool) -> dict | None:
    sources = _load(ws)
    src = next((s for s in sources if s['id'] == sid), None)
    if not src:
        return None
    src['enabled'] = bool(enabled)
    _store(ws, sources)
    return _public(ws, src)


# ── fetching ────────────────────────────────────────────────────────────────────────────────────────

def _dig(obj, path: str):
    for part in [p for p in path.split('.') if p]:
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def _fetch(url: str, headers: dict) -> bytes:
    try:
        status, data = safehttp.request('GET', url, None, {'Accept': 'application/json, text/csv', **headers}, TIMEOUT_S, MAX_BYTES)
    except safehttp.UnsafeDestination as exc:
        raise SourceError(str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise SourceError(f'Request failed: {exc}') from exc
    if status in (401, 403):
        raise SourceError(f'The source refused our credentials (HTTP {status}).')
    if not 200 <= status < 300:
        raise SourceError(f'The source answered HTTP {status}.')
    return data


def _with_since(url: str, param: str, since: str | None) -> str:
    if not param or not since:
        return url
    sep = '&' if '?' in url else '?'
    return f'{url}{sep}{param}={quote(since)}'


def fetch_frame(ws: str, src: dict) -> pd.DataFrame:
    secret = _secret(ws, src)
    since = (ingest.table_info(ws).get(src['role']) or {}).get('max_ts') if src['mode'] == 'append' else None
    url, frames, pages = _with_since(secret['url'], src['since_param'], since), [], 0
    while url and pages < MAX_PAGES:
        raw = _fetch(url, secret['headers'])
        pages += 1
        if src['format'] == 'csv':
            try:
                frames.append(pd.read_csv(io.BytesIO(raw), low_memory=False))
            except Exception as exc:  # noqa: BLE001
                raise SourceError(f'Could not parse the CSV: {exc}') from exc
            break
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise SourceError('The response is not valid JSON.') from exc
        records = _dig(doc, src['records_path']) if src['records_path'] else doc
        if not isinstance(records, list):
            raise SourceError(f'No list of records found at "{src["records_path"] or "(top level)"}".')
        if records:
            frames.append(pd.json_normalize(records, max_level=1))
        nxt = _dig(doc, src['next_path']) if src['next_path'] else None
        url = nxt if isinstance(nxt, str) and nxt else None
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def sync(ws: str, sid: str) -> dict:
    """Fetch one source into its table. Returns {ok, rows|detail}; the outcome is also stored on the source."""
    sources = _load(ws)
    src = next((s for s in sources if s['id'] == sid), None)
    if not src:
        raise SourceError('Source not found.')
    try:
        df = fetch_frame(ws, src)
        if df.empty:
            out = {'ok': True, 'rows': 0, 'detail': 'Nothing new.'}
        else:
            norm = ingest.normalize(src['role'], df, src['mapping'])
            res = ingest.put_table(ws, src['role'], norm, src['mode'])
            out = {'ok': True, 'rows': res['rows_added'], 'detail': f"{res['rows_added']:,} rows into {src['role']}."}
    except (SourceError, ingest.IngestError) as exc:
        out = {'ok': False, 'rows': 0, 'detail': str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001
        log.warning('source %s failed: %s', sid, exc, exc_info=True)
        out = {'ok': False, 'rows': 0, 'detail': f'Unexpected error: {type(exc).__name__}'}
    sources = _load(ws)                                          # re-read: another sync may have written meanwhile
    for s in sources:
        if s['id'] == sid:
            s['last'] = {'at': time.time(), **out}
    _store(ws, sources)
    return out


def sync_all(ws: str) -> list[dict]:
    """Sync every enabled source. Failures are reported, not raised: scoring can still run on the data we have."""
    results = []
    for s in _load(ws):
        if s.get('enabled'):
            results.append({'id': s['id'], 'name': s['name'], 'role': s['role'], **sync(ws, s['id'])})
    return results
