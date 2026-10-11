"""Realized-performance monitoring, shadow evaluation and guarded promotion.

What the platform does NOT do: retrain on inference-time data. Predictions are only a promise until the outcome is known, so every
scoring run is logged (accounts and probabilities, encrypted) and *resolved* once its horizon has passed and the outcome can be read from
the workspace's tables (pipeline workspaces) or from outcomes the customer uploads (everyone else). Resolved runs give the numbers that
matter: how well the live model actually predicted, and whether its probabilities were honest.

Those numbers drive three decisions:
  health      ok / watch / degraded, from realized ROC-AUC against what the model promised at training time, and calibration
  retraining  a challenger is trained when the model is old, the data has drifted, health is degraded, or enough new churn has been seen
  promotion   a challenger is scored in shadow next to the champion on the same accounts; it replaces the champion only when, on the same
              resolved outcomes, it is clearly better and not worse-calibrated. Automatic promotion is opt-in; rollback is one call.
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import time

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from . import crypto, db, features, ingest, registry, storage

log = logging.getLogger('churnsight.mlops')
RUNS_KEPT = 60
MIN_POSITIVES = 20                  # below this a realized metric is mostly noise and is not used for decisions
MIN_COVERAGE = 0.8                  # share of logged accounts an outcome set must cover to resolve a run


def _prefix(ws: str) -> str:
    return storage.model_prefix(ws).replace('models/', 'predlog/', 1)


# ── prediction log ──────────────────────────────────────────────────────────────────────────────────

def log_run(ws: str, model_id: str, kind: str, ids, probs, *, as_of: str | None = None, horizon_days: int | None = None,
            source: str = 'batch') -> str:
    """Record one scoring run. The log is encrypted; the index row holds no customer data."""
    run_id = db.new_id()
    df = pd.DataFrame({'customer_id': np.asarray(ids).astype(str), 'prob': np.asarray(probs, dtype=float).round(6)})
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=3) as gz:
        gz.write(df.to_csv(index=False).encode())
    key = f'{_prefix(ws)}/{run_id}.cse'
    storage.get_storage().put(key, crypto.encrypt_bytes(ws, buf.getvalue(), f'predlog|{run_id}'))
    row = registry.get_model(ws, model_id)
    metrics = ((row or {}).get('meta', {}).get('evaluation') or {}).get('metrics') or {}
    expected = {'roc_auc': metrics.get('roc_auc'), 'pr_auc': metrics.get('pr_auc'), 'base_rate': (row or {}).get('meta', {}).get('base_rate')}
    with db.get_engine().begin() as c:
        c.execute(db.score_runs_t.insert().values(id=run_id, workspace_id=ws, model_id=model_id, kind=kind, source=source, as_of=as_of, horizon_days=horizon_days,
                                                  scored_at=time.time(), rows=int(len(df)), status='open', log_key=key, expected=db.dumps(expected)))
    _prune(ws)
    return run_id


def _prune(ws: str) -> None:
    t = db.score_runs_t
    with db.get_engine().begin() as c:
        old = c.execute(sa.select(t.c.id, t.c.log_key).where(t.c.workspace_id == ws).order_by(t.c.scored_at.desc()).offset(RUNS_KEPT)).all()
        for r in old:
            c.execute(sa.delete(t).where(t.c.id == r.id))
            if r.log_key:
                storage.get_storage().delete(r.log_key)


def _row(r) -> dict:
    d = dict(r._mapping)
    d['expected'] = db.loads(d.get('expected'), {})
    d['realized'] = db.loads(d.get('realized'))
    return d


def list_runs(ws: str, limit: int = 30, kind: str | None = None, status: str | None = None) -> list[dict]:
    t = db.score_runs_t
    q = sa.select(t).where(t.c.workspace_id == ws)
    if kind:
        q = q.where(t.c.kind == kind)
    if status:
        q = q.where(t.c.status == status)
    with db.get_engine().connect() as c:
        return [_row(r) for r in c.execute(q.order_by(t.c.scored_at.desc()).limit(limit))]


def get_run(ws: str, run_id: str) -> dict | None:
    t = db.score_runs_t
    with db.get_engine().connect() as c:
        r = c.execute(sa.select(t).where(t.c.workspace_id == ws, t.c.id == run_id)).first()
    return _row(r) if r else None


def siblings(ws: str, run: dict) -> list[dict]:
    """Runs logged by the same scoring event (a live run and its shadow run), so one outcomes upload resolves both."""
    return [r for r in list_runs(ws, RUNS_KEPT, status='open')
            if r['id'] != run['id'] and r['source'] == run['source'] and r['as_of'] == run['as_of'] and abs(r['scored_at'] - run['scored_at']) < 120]


def load_log(ws: str, run: dict) -> pd.DataFrame:
    raw = gzip.decompress(crypto.decrypt_bytes(ws, storage.get_storage().get(run['log_key']), f"predlog|{run['id']}"))
    return pd.read_csv(io.BytesIO(raw), dtype={'customer_id': str})


def _write_log(ws: str, run: dict, df: pd.DataFrame) -> None:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=3) as gz:
        gz.write(df.to_csv(index=False).encode())
    storage.get_storage().put(run['log_key'], crypto.encrypt_bytes(ws, buf.getvalue(), f"predlog|{run['id']}"))


def outcomes_for(ws: str, run: dict) -> pd.Series | None:
    """Known outcomes of a resolved run (customer_id -> 0/1), as stored with its predictions."""
    df = load_log(ws, run)
    if 'y' not in df:
        return None
    known = df.dropna(subset=['y'])
    return pd.Series(known['y'].astype(int).to_numpy(), index=known['customer_id'].to_numpy())


def erase_accounts(ws: str, ids: list[str]) -> int:
    """Right to erasure: rewrite prediction logs without these accounts."""
    gone, removed = {str(i) for i in ids}, 0
    for run in list_runs(ws, RUNS_KEPT):
        try:
            df = load_log(ws, run)
        except (FileNotFoundError, storage.StorageError):
            continue
        keep = df[~df['customer_id'].isin(gone)]
        if len(keep) == len(df):
            continue
        removed += len(df) - len(keep)
        _write_log(ws, run, keep)
    return removed


def drop_all(ws: str) -> None:
    storage.get_storage().delete_prefix(_prefix(ws).rstrip('/'))


# ── realized metrics ────────────────────────────────────────────────────────────────────────────────

def realized_metrics(y, p) -> dict:
    y, p = np.asarray(y, dtype=int), np.asarray(p, dtype=float)
    n, pos = int(len(y)), int(y.sum())
    out: dict = {'n': n, 'positives': pos, 'base_rate': round(pos / n, 4) if n else None, 'mean_predicted': round(float(p.mean()), 4) if n else None}
    if n == 0 or pos == 0 or pos == n:
        return {**out, 'roc_auc': None, 'pr_auc': None, 'brier': round(float(brier_score_loss(y, p)), 4) if n else None, 'lift_top10': None,
                'calibration': [], 'ece': None, 'calibration_ratio': None, 'usable': False}
    order = np.argsort(-p)
    top = y[order[:max(1, n // 10)]].mean()
    bins = pd.qcut(pd.Series(p).rank(method='first'), q=min(10, max(2, n // 20)), labels=False)
    cal = [{'predicted': round(float(p[bins == b].mean()), 4), 'observed': round(float(y[bins == b].mean()), 4), 'n': int((bins == b).sum())} for b in sorted(bins.unique())]
    ece = sum(c['n'] / n * abs(c['predicted'] - c['observed']) for c in cal)
    return {**out, 'roc_auc': round(float(roc_auc_score(y, p)) * 100, 2), 'pr_auc': round(float(average_precision_score(y, p)) * 100, 2),
            'brier': round(float(brier_score_loss(y, p)), 4), 'lift_top10': round(float(top / (pos / n)), 2), 'calibration': cal, 'ece': round(float(ece), 4),
            'calibration_ratio': round(float(p.mean() / (pos / n)), 3), 'usable': pos >= MIN_POSITIVES}


# ── resolving runs ──────────────────────────────────────────────────────────────────────────────────

def resolve_run(ws: str, run_id: str, outcomes: pd.Series) -> dict:
    """Join a run's logged probabilities with known outcomes (index = customer_id, values 0/1) and store the realized metrics."""
    run = get_run(ws, run_id)
    if not run:
        raise LookupError('Run not found.')
    log_df = load_log(ws, run)
    outcomes = outcomes[~outcomes.index.duplicated(keep='last')]
    known = log_df[log_df['customer_id'].isin(outcomes.index)]
    if len(known) < MIN_COVERAGE * len(log_df):
        raise ValueError(f'Outcomes cover {len(known):,} of the {len(log_df):,} accounts scored in this run; at least {int(MIN_COVERAGE * 100)}% are needed.')
    m = realized_metrics(outcomes.loc[known['customer_id']].to_numpy(), known['prob'].to_numpy())
    m['coverage'] = round(len(known) / max(len(log_df), 1), 3)
    log_df['y'] = log_df['customer_id'].map(outcomes)                   # keep the outcomes next to the predictions: shadow comparisons need them
    _write_log(ws, run, log_df)
    with db.get_engine().begin() as c:
        c.execute(sa.update(db.score_runs_t).where(db.score_runs_t.c.id == run_id).values(status='resolved', realized=db.dumps(m), resolved_at=time.time()))
    return m


def resolve_due_runs(ws: str, spec: dict | None, tables: dict | None = None) -> list[str]:
    """Resolve every open pipeline run whose horizon is fully observed, reading outcomes from the workspace tables."""
    if not spec:
        return []
    tables = tables if tables is not None else ingest.load_all(ws)
    if 'accounts' not in tables:
        return []
    through = features.observed_through(tables, spec)
    from . import service
    hashed = bool(service.get_settings(ws).get('privacy', {}).get('hash_ids'))
    back = {crypto.pseudonymize_id(ws, a): a for a in tables['accounts']['account_id']} if hashed else None     # logs hold pseudonyms; tables hold raw ids
    done = []
    for run in list_runs(ws, RUNS_KEPT, status='open'):
        if run['source'] != 'pipeline' or not run['as_of'] or not run['horizon_days']:
            continue
        if pd.Timestamp(run['as_of']) + pd.Timedelta(days=run['horizon_days']) > through:
            continue                                                    # the outcome is not knowable yet
        ids = load_log(ws, run)['customer_id']
        raw = ids.map(back).fillna(ids) if back else ids
        snap = pd.DataFrame({'customer_id': raw})
        y = features.labels(tables, snap, pd.Timestamp(run['as_of']), {**spec, 'horizon_days': run['horizon_days']}, through)
        try:
            resolve_run(ws, run['id'], pd.Series(y.to_numpy(), index=ids.to_numpy()).dropna())
            done.append(run['id'])
        except ValueError as exc:
            log.info('run %s not resolved: %s', run['id'], exc)
    return done


# ── health ──────────────────────────────────────────────────────────────────────────────────────────

def assess_health(ws: str) -> dict:
    """ok / watch / degraded / insufficient, from the champion's recent resolved live runs against what it promised."""
    champ = registry.champion(ws)
    if not champ:
        return {'status': 'insufficient', 'reasons': ['No model yet.']}
    runs = [r for r in list_runs(ws, 12, kind='live', status='resolved') if r['model_id'] == champ['id'] and (r['realized'] or {}).get('usable')]
    if not runs:
        return {'status': 'insufficient', 'reasons': [f'No resolved run has {MIN_POSITIVES}+ churn events yet. Realized performance appears once outcomes are known.'], 'runs': 0}
    runs = runs[:3]
    expected = champ['meta'].get('evaluation', {}).get('metrics', {}).get('roc_auc')
    pos = sum(r['realized']['positives'] for r in runs)
    roc = sum(r['realized']['roc_auc'] * r['realized']['positives'] for r in runs) / pos
    ratio = sum(r['realized']['calibration_ratio'] * r['realized']['positives'] for r in runs) / pos
    ece = sum(r['realized']['ece'] * r['realized']['positives'] for r in runs) / pos
    reasons, status = [], 'ok'
    drop = None if expected is None else expected - roc
    if drop is not None and drop > 10:
        status, reasons = 'degraded', reasons + [f'Realized ROC-AUC {roc:.1f} is {drop:.1f} points below the {expected:.1f} measured at training.']
    elif drop is not None and drop > 5:
        status, reasons = 'watch', reasons + [f'Realized ROC-AUC {roc:.1f} is {drop:.1f} points below the {expected:.1f} measured at training.']
    if not 0.5 <= ratio <= 2.0:
        status, reasons = 'degraded', reasons + [f'Probabilities are off by a factor of {max(ratio, 1 / max(ratio, 1e-9)):.1f} (predicted {ratio:.2f}x what happened).']
    elif (not 0.67 <= ratio <= 1.5 or ece > 0.08) and status == 'ok':
        status, reasons = 'watch', reasons + [f'Calibration is slipping (predicted {ratio:.2f}x observed, ECE {ece:.3f}).']
    return {'status': status, 'reasons': reasons or ['Realized performance is in line with training.'], 'runs': len(runs), 'realized_roc_auc': round(roc, 2),
            'expected_roc_auc': expected, 'calibration_ratio': round(ratio, 3), 'ece': round(ece, 4), 'positives': pos}


def stored_health(ws: str) -> dict:
    from . import service
    return service.kv_get(ws, 'mlops_health', None) or {'status': 'insufficient', 'reasons': ['Not assessed yet.']}


def update_health(ws: str, actor: str = 'mlops') -> dict:
    from . import notify, service
    new = {**assess_health(ws), 'updated_at': time.time()}
    old = service.kv_get(ws, 'mlops_health', None) or {}
    service.kv_set(ws, 'mlops_health', new)
    if new['status'] != old.get('status') and new['status'] in ('watch', 'degraded'):
        db.audit(ws, actor, 'mlops.health', {'status': new['status'], 'reasons': new['reasons']})
        if new['status'] == 'degraded':
            notify.broadcast(ws, 'Model health: degraded', ' '.join(new['reasons']))
    return new
