"""Model registry: versioned artifacts on disk, metadata in the database, champion/challenger."""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from collections import OrderedDict

import sqlalchemy as sa

from . import artifacts, config, crypto, db
KEEP_MODELS = config.KEEP_MODELS

_cache: OrderedDict[tuple[str, str], dict] = OrderedDict()
_cache_lock = threading.Lock()
CACHE_SIZE = 6


def safe(ws: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]', '_', ws)[:80]


def model_dir(ws: str, model_id: str) -> str:
    path = os.path.join(config.data_dir(), 'models', safe(ws), model_id)
    os.makedirs(path, exist_ok=True)
    return path


def save_bundle(ws: str, model_id: str, bundle: dict) -> str:
    """Safe (non-pickle) format, encrypted with the workspace key and bound to this workspace and model id."""
    path = os.path.join(model_dir(ws, model_id), 'model.cse')
    with open(path, 'wb') as f:
        f.write(crypto.encrypt_bytes(ws, artifacts.dumps_bundle(bundle), f'model|{model_id}'))
    return path


def load_bundle(ws: str, model_id: str) -> dict | None:
    key = (ws, model_id)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    row = get_model(ws, model_id)
    if not row or not row['artifact_path'] or not os.path.exists(row['artifact_path']):
        return None
    if not row['artifact_path'].endswith('.cse'):
        raise artifacts.ArtifactError('This model uses the legacy pickle format, which is no longer loaded. Retrain it.')
    with open(row['artifact_path'], 'rb') as f:
        bundle = artifacts.loads_bundle(crypto.decrypt_bytes(ws, f.read(), f'model|{model_id}'))
    with _cache_lock:
        _cache[key] = bundle
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return bundle


def forget(ws: str | None = None) -> None:
    with _cache_lock:
        for key in [k for k in _cache if ws is None or k[0] == ws]:
            _cache.pop(key, None)


def _row(r) -> dict:
    d = dict(r._mapping)
    d['meta'] = db.loads(d.get('meta'), {})
    return d


def get_model(ws: str, model_id: str) -> dict | None:
    with db.get_engine().connect() as c:
        r = c.execute(sa.select(db.models_t).where(db.models_t.c.workspace_id == ws, db.models_t.c.id == model_id)).first()
    return _row(r) if r else None


def champion(ws: str) -> dict | None:
    with db.get_engine().connect() as c:
        r = c.execute(sa.select(db.models_t).where(db.models_t.c.workspace_id == ws, db.models_t.c.status == 'champion')).first()
    return _row(r) if r else None


def champion_bundle(ws: str) -> tuple[dict, dict] | tuple[None, None]:
    row = champion(ws)
    if not row:
        return None, None
    bundle = load_bundle(ws, row['id'])
    return (row, bundle) if bundle else (None, None)


def list_models(ws: str) -> list[dict]:
    with db.get_engine().connect() as c:
        rows = c.execute(sa.select(db.models_t).where(db.models_t.c.workspace_id == ws).order_by(db.models_t.c.created_at.desc())).all()
    out = []
    for r in rows:
        d = _row(r)
        meta = d['meta']
        out.append({
            'id': d['id'], 'status': d['status'], 'created_at': d['created_at'], 'algorithm': meta.get('algorithm_name'),
            'pr_auc': d['pr_auc'], 'roc_auc': d['roc_auc'], 'rows': meta.get('rows'), 'split': meta.get('split'),
            'data_hash': meta.get('data_hash'), 'source': meta.get('source'),
        })
    return out


def register(ws: str, model_id: str, bundle: dict, meta: dict, metrics: dict, snapshot_path: str | None, promote: bool | None) -> tuple[str, str]:
    """Persist a trained model. Returns (status, reason)."""
    artifact = save_bundle(ws, model_id, bundle)
    pr, roc = metrics.get('pr_auc'), metrics.get('roc_auc')
    current = champion(ws)
    if promote is False:
        status, reason = 'challenger', 'kept as challenger (promotion disabled)'
    elif current is None:
        status, reason = 'champion', 'first model for this workspace'
    elif current['meta'].get('data_hash') == meta.get('data_hash') and pr is not None and current['pr_auc'] is not None \
            and promote is None and pr < current['pr_auc'] - 1.0:
        # Same data, worse score: the metrics are comparable, so protect the champion. A different dataset is a
        # different problem (scores can't be compared across datasets), so an explicit upload always goes live.
        status, reason = 'challenger', (f'PR-AUC {pr:.1f} is more than 1 point below the champion ({current["pr_auc"]:.1f}) '
                                        'on the same data; promote it manually if you disagree')
    else:
        status, reason = 'champion', 'promoted: new data, or equal/better PR-AUC on the same data'
    with db.get_engine().begin() as conn:
        if status == 'champion':
            conn.execute(sa.update(db.models_t).where(db.models_t.c.workspace_id == ws, db.models_t.c.status == 'champion')
                         .values(status='archived'))
        conn.execute(db.models_t.insert().values(
            id=model_id, workspace_id=ws, created_at=time.time(), algorithm=meta.get('algorithm'), status=status,
            pr_auc=pr, roc_auc=roc, schema_signature=meta.get('schema_signature'), artifact_path=artifact,
            data_path=snapshot_path, meta=db.dumps(meta)))
    _prune(ws)
    return status, reason


def set_champion(ws: str, model_id: str) -> dict | None:
    target = get_model(ws, model_id)
    if not target:
        return None
    with db.get_engine().begin() as conn:
        conn.execute(sa.update(db.models_t).where(db.models_t.c.workspace_id == ws, db.models_t.c.status == 'champion')
                     .values(status='archived'))
        conn.execute(sa.update(db.models_t).where(db.models_t.c.workspace_id == ws, db.models_t.c.id == model_id)
                     .values(status='champion'))
    return target


def _prune(ws: str) -> None:
    with db.get_engine().begin() as conn:
        rows = conn.execute(sa.select(db.models_t.c.id, db.models_t.c.status).where(db.models_t.c.workspace_id == ws)
                            .order_by(db.models_t.c.created_at.desc())).all()
        for r in rows[KEEP_MODELS:]:
            if r.status == 'champion':
                continue
            conn.execute(sa.delete(db.models_t).where(db.models_t.c.id == r.id))
            shutil.rmtree(model_dir(ws, r.id), ignore_errors=True)
            with _cache_lock:
                _cache.pop((ws, r.id), None)


def wipe(ws: str) -> None:
    with db.get_engine().begin() as conn:
        for t in (db.models_t, db.customers_t, db.alerts_t, db.interventions_t, db.jobs_t, db.score_batches_t, db.kv_t):
            conn.execute(sa.delete(t).where(t.c.workspace_id == ws))
    shutil.rmtree(os.path.join(config.data_dir(), 'models', safe(ws)), ignore_errors=True)
    forget(ws)
