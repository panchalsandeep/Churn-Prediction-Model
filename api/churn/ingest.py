"""B2B data tables: accounts, events, tickets, invoices.

Raw tables are stored per workspace as encrypted, gzipped CSV batches in blob storage (`tables/<ws>/<role>/<batch>.cse`), never in
the query database, so they are crypto-shredded with the workspace key and need no schema migration when a customer's columns differ.
Batch metadata (row counts, date ranges) lives in the workspace key-value store so status pages never decrypt anything.

Each table has a small canonical schema. Customers map their own column names onto it (`mapping`); anything else in `accounts` is kept
as a static attribute (personal-data columns are dropped, as for uploads).
"""
from __future__ import annotations

import gzip
import io
import os
import re
import time
import uuid

import pandas as pd

from . import config, crypto, mapping as colmap, service, storage
from .schema import detect_pii

ROLES: dict[str, dict] = {
    'accounts': {'required': ['account_id', 'started_at'], 'dates': ['started_at', 'churned_at', 'contract_end'],
                 'numeric': ['mrr', 'seats_licensed'], 'optional': ['churned_at', 'contract_end', 'mrr', 'seats_licensed']},
    'events': {'required': ['account_id', 'ts'], 'dates': ['ts'], 'numeric': ['value'], 'optional': ['event', 'user_id', 'value']},
    'tickets': {'required': ['account_id', 'ts'], 'dates': ['ts'], 'numeric': ['csat'], 'optional': ['severity', 'status', 'csat']},
    'invoices': {'required': ['account_id', 'ts', 'amount'], 'dates': ['ts'], 'numeric': ['amount'], 'optional': ['status']},
}
MAX_EXTRA_ATTRS = 25
MAX_ROWS_PER_TABLE = int(os.environ.get('MAX_TABLE_ROWS', '5000000'))


class IngestError(ValueError):
    """A problem with the data the caller sent (HTTP 400)."""


def _norm(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(name).strip().lower()).strip('_')


def normalize(role: str, df: pd.DataFrame, mapping: dict[str, str] | None = None, auto: bool = True) -> pd.DataFrame:
    """Map customer columns onto the canonical schema and validate. Returns a clean frame; raises IngestError with a human message.

    Columns the caller did not map are matched automatically when the match is confident (see mapping.py); weaker matches are never applied silently."""
    if role not in ROLES:
        raise IngestError(f'Unknown table "{role}". Use one of: {", ".join(ROLES)}.')
    spec = ROLES[role]
    mapping = {k: v for k, v in (mapping or {}).items() if v}
    if auto:
        found = colmap.suggest(role, df, spec)['applied']
        used = set(mapping.values())
        mapping = {**{k: v for k, v in found.items() if v not in used}, **mapping}      # the caller's choices always win
    cols = {_norm(c): c for c in df.columns}
    rename: dict[str, str] = {}
    for canon in spec['required'] + spec['optional']:
        src = mapping.get(canon)
        if src is not None:
            if src not in df.columns:
                raise IngestError(f'Mapped column "{src}" for {canon} is not in the file. Columns: {", ".join(map(str, df.columns[:15]))}.')
            rename[src] = canon
        elif canon in cols:
            rename[cols[canon]] = canon
    missing = [c for c in spec['required'] if c not in rename.values()]
    if missing:
        raise IngestError(f'The {role} table needs {", ".join(missing)}. Map your columns or rename them. Columns found: {", ".join(map(str, df.columns[:15]))}.')
    out = df.rename(columns=rename).copy()
    keep = list(dict.fromkeys(list(rename.values())))
    extras: list[str] = []
    if role == 'accounts':                                    # static attributes: kept, minus personal data
        for c in out.columns:
            if c in keep or len(extras) >= MAX_EXTRA_ATTRS:
                continue
            if detect_pii(out[c], str(c)):
                continue
            extras.append(c)
    out = out[keep + extras]
    out['account_id'] = out['account_id'].astype(str).str.strip()
    out = out[out['account_id'].ne('') & out['account_id'].ne('nan')]
    for c in spec['dates']:
        if c in out:
            parsed = pd.to_datetime(out[c], errors='coerce', utc=True, format='mixed').dt.tz_localize(None)
            blank = out[c].isna() | out[c].astype(str).str.strip().str.lower().isin(['', 'nat', 'nan', 'none', 'null', 'n/a', '-'])      # "no date" spelled in the usual ways
            bad = parsed.isna() & ~blank
            if len(out) and bad.mean() > 0.2:
                raise IngestError(f'{int(bad.sum()):,} of {len(out):,} values in "{c}" are not dates (for example {out[c][bad].iloc[0]!r}).')
            out[c] = parsed
    for c in spec['numeric']:
        if c in out:
            out[c] = pd.to_numeric(out[c], errors='coerce')
    if role == 'accounts':
        out = out.dropna(subset=['started_at'])
    else:
        out = out.dropna(subset=['ts'])
    if out.empty:
        raise IngestError(f'No usable rows were found for the {role} table after cleaning.')
    for c in out.columns:                                     # strings only for the free-text roles
        if out[c].dtype == object:
            out[c] = out[c].astype(str).str.slice(0, 200).where(out[c].notna(), None)
    return out.reset_index(drop=True)


def _prefix(ws: str, role: str) -> str:
    return storage.table_prefix(ws, role)


def _meta(ws: str) -> dict:
    return service.kv_get(ws, 'tables', {}) or {}


def put_table(ws: str, role: str, df: pd.DataFrame, mode: str = 'append') -> dict:
    """Store a normalized batch. `replace` swaps the whole table; `append` adds (events/tickets/invoices) or upserts (accounts, last wins)."""
    if mode not in ('append', 'replace'):
        raise IngestError('mode must be "append" or "replace".')
    meta = _meta(ws)
    prior = 0 if mode == 'replace' else int(meta.get(role, {}).get('rows', 0))
    if prior + len(df) > MAX_ROWS_PER_TABLE:
        raise IngestError(f'This would take the {role} table past {MAX_ROWS_PER_TABLE:,} rows. Send a smaller window or replace the table.')
    batch = f'{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}'
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=3) as gz:
        gz.write(df.to_csv(index=False).encode())
    key = f'{_prefix(ws, role)}/{batch}.cse'
    store = storage.get_storage()
    store.put(key, crypto.encrypt_bytes(ws, buf.getvalue(), f'table|{role}|{batch}'))
    if mode == 'replace':
        for old in store.list(_prefix(ws, role)):
            if old != key:
                store.delete(old)
    date_col = 'started_at' if role == 'accounts' else 'ts'
    entry = meta.get(role, {}) if mode == 'append' else {}
    lo, hi = df[date_col].min(), df[date_col].max()
    meta[role] = {'rows': prior + len(df), 'batches': int(entry.get('batches', 0)) + 1, 'updated_at': time.time(),
                  'min_ts': min(filter(None, [entry.get('min_ts'), lo.isoformat()])), 'max_ts': max(filter(None, [entry.get('max_ts'), hi.isoformat()]))}
    service.kv_set(ws, 'tables', meta)
    return {'role': role, 'rows_added': int(len(df)), 'rows_total': meta[role]['rows'], 'mode': mode}


def load_table(ws: str, role: str) -> pd.DataFrame | None:
    store = storage.get_storage()
    keys = sorted(store.list(_prefix(ws, role)))
    if not keys:
        return None
    frames = []
    for key in keys:
        batch = key.rsplit('/', 1)[1][:-4]
        raw = gzip.decompress(crypto.decrypt_bytes(ws, store.get(key), f'table|{role}|{batch}'))
        frames.append(pd.read_csv(io.BytesIO(raw), keep_default_na=True, low_memory=False, dtype={'account_id': str}))
    df = pd.concat(frames, ignore_index=True)
    for c in ROLES[role]['dates']:
        if c in df:
            df[c] = pd.to_datetime(df[c], errors='coerce')
    if role == 'accounts':
        df = df.drop_duplicates('account_id', keep='last')
    else:
        df = df.drop_duplicates()
    return df.reset_index(drop=True)


def load_all(ws: str) -> dict[str, pd.DataFrame]:
    return {r: t for r in ROLES if (t := load_table(ws, r)) is not None}


def table_info(ws: str) -> dict:
    meta = _meta(ws)
    return {r: meta[r] for r in ROLES if r in meta}


def drop_all(ws: str) -> None:
    storage.get_storage().delete_prefix(storage.table_prefix(ws))
    service.kv_set(ws, 'tables', {})


def erase_accounts(ws: str, ids: list[str]) -> int:
    """Right to erasure: rewrite every table without these accounts (raw events and tickets would otherwise outlive a deletion)."""
    gone = {str(i) for i in ids}
    removed = 0
    for role in ROLES:
        df = load_table(ws, role)
        if df is None:
            continue
        keep = df[~df['account_id'].astype(str).isin(gone)]
        if len(keep) == len(df):
            continue
        removed += len(df) - len(keep)
        if keep.empty:
            storage.get_storage().delete_prefix(_prefix(ws, role))
            meta = _meta(ws)
            meta.pop(role, None)
            service.kv_set(ws, 'tables', meta)
        else:
            put_table(ws, role, keep, 'replace')
    return removed
