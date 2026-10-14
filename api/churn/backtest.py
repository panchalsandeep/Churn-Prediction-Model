"""Rolling-origin backtest: how well would this recipe have predicted churn at several points in the past?

For each recent origin date o the model is trained only on snapshots whose outcome window had already closed by o, then scores the accounts
live at o and is compared with what happened in the following horizon. Several origins show both the typical performance to expect from a live
model and how much it moves from period to period, which a single holdout split cannot.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from . import features, ingest, mlops, pipeline, service, training
from .schema import prepare_features

MIN_TRAIN_SNAPSHOTS = 3
MIN_TRAIN_POSITIVES = 30


def run(ws: str, progress, origins: int = 4, algorithm: str = 'logistic_regression') -> dict:
    cfg = pipeline.get(ws)
    if not cfg['target']:
        raise ValueError('Define what "churned" means first (target definition).')
    progress('Loading tables', 3)
    tables = ingest.load_all(ws)
    spec = features.validate_spec(cfg['target'], tables)
    through = features.observed_through(tables, spec)
    horizon = pd.Timedelta(days=spec['horizon_days'])
    vocab = cfg['vocab'] if cfg['vocab'] is not None else features.event_vocabulary(tables)
    dates = features.snapshot_dates(tables, spec, through, max_snapshots=36)
    if len(dates) < MIN_TRAIN_SNAPSHOTS + 1:
        raise ValueError('Not enough history for a backtest: it needs at least four labelled monthly snapshots.')
    cache: dict[pd.Timestamp, pd.DataFrame] = {}

    def frame(d: pd.Timestamp) -> pd.DataFrame:
        if d not in cache:
            snap = features.build_snapshot(tables, d, vocab)
            snap['churned'] = features.labels(tables, snap, d, spec, through).to_numpy()
            cache[d] = snap
        return cache[d]

    results, skipped = [], []
    chosen = dates[-origins:]
    for i, o in enumerate(chosen):
        progress(f'Backtest origin {o.date()}', 5 + int(90 * i / len(chosen)))
        train_dates = [d for d in dates if d + horizon <= o]
        if len(train_dates) < MIN_TRAIN_SNAPSHOTS:
            skipped.append({'origin': o.strftime('%Y-%m-%d'), 'reason': 'too little earlier history'})
            continue
        train = pd.concat([frame(d) for d in train_dates], ignore_index=True).dropna(subset=['churned'])
        train['churned'] = train['churned'].astype(int)
        if train['churned'].sum() < MIN_TRAIN_POSITIVES or train['churned'].nunique() < 2:
            skipped.append({'origin': o.strftime('%Y-%m-%d'), 'reason': f'only {int(train["churned"].sum())} churn events before this date'})
            continue
        test = frame(o).dropna(subset=['churned'])
        bundle = training.train_model(train, algorithm)['bundle']
        X = prepare_features(test, bundle['schema']).reindex(columns=bundle['cols'])
        for c in bundle['cat_cols']:
            X[c] = X[c].fillna('Missing')
        p = training.clip_probs(bundle['model'].predict_proba(X)[:, 1])
        m = mlops.realized_metrics(test['churned'].astype(int).to_numpy(), p)
        results.append({'origin': o.strftime('%Y-%m-%d'), 'train_rows': int(len(train)), 'train_positives': int(train['churned'].sum()),
                        **{k: m[k] for k in ('n', 'positives', 'base_rate', 'roc_auc', 'pr_auc', 'brier', 'lift_top10', 'calibration_ratio')}})
    usable = [r for r in results if r['roc_auc'] is not None]
    summary = None
    if usable:
        roc, pr = np.array([r['roc_auc'] for r in usable]), np.array([r['pr_auc'] for r in usable])
        summary = {'origins': len(usable), 'roc_auc_mean': round(float(roc.mean()), 2), 'roc_auc_std': round(float(roc.std()), 2), 'roc_auc_min': round(float(roc.min()), 2),
                   'pr_auc_mean': round(float(pr.mean()), 2), 'pr_auc_std': round(float(pr.std()), 2),
                   'lift_top10_mean': round(float(np.mean([r['lift_top10'] for r in usable if r['lift_top10']] or [0])), 2)}
    out = {'created_at': time.time(), 'algorithm': algorithm, 'horizon_days': spec['horizon_days'], 'results': results, 'skipped': skipped, 'summary': summary}
    service.kv_set(ws, 'backtest', out)
    return out
