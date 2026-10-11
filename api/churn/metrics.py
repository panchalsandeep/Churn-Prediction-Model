"""Evaluation metrics that matter for imbalanced churn: PR-AUC, lift, calibration, precision@k."""
from __future__ import annotations

import numpy as np
from sklearn.metrics import (
    accuracy_score, average_precision_score, brier_score_loss, confusion_matrix,
    f1_score, precision_score, recall_score, roc_auc_score, roc_curve, precision_recall_curve,
)


def _round(x, nd=4):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def lift_table(y, p, bins: int = 10) -> list[dict]:
    """Decile table: customers sorted by risk, churn rate and lift vs the base rate per decile."""
    y = np.asarray(y)
    order = np.argsort(-p, kind='stable')
    ys = y[order]
    base = y.mean() or 1e-9
    total_pos = ys.sum() or 1
    edges = np.linspace(0, len(ys), bins + 1).astype(int)
    rows, cum = [], 0
    for i in range(bins):
        chunk = ys[edges[i]:edges[i + 1]]
        if not len(chunk):
            continue
        cum += chunk.sum()
        rate = chunk.mean()
        rows.append({
            'decile': i + 1,
            'customers': int(len(chunk)),
            'churn_rate': _round(rate * 100, 2),
            'lift': _round(rate / base, 2),
            'cumulative_capture': _round(cum / total_pos * 100, 2),
        })
    return rows


def calibration_curve_points(y, p, bins: int = 10) -> list[dict]:
    """Quantile-binned reliability curve: mean predicted vs observed churn rate."""
    y = np.asarray(y)
    order = np.argsort(p, kind='stable')
    edges = np.linspace(0, len(p), bins + 1).astype(int)
    pts = []
    for i in range(bins):
        idx = order[edges[i]:edges[i + 1]]
        if len(idx):
            pts.append({'predicted': _round(p[idx].mean() * 100, 2), 'observed': _round(y[idx].mean() * 100, 2), 'n': int(len(idx))})
    return pts


def precision_at_fraction(y, p, fractions=(0.01, 0.05, 0.10, 0.20)) -> dict:
    y = np.asarray(y)
    order = np.argsort(-p, kind='stable')
    out = {}
    for f in fractions:
        k = max(1, int(round(len(y) * f)))
        top = y[order[:k]]
        out[f'{int(f * 100)}%'] = {'precision': _round(top.mean() * 100, 2), 'recall': _round(top.sum() / max(y.sum(), 1) * 100, 2), 'customers': int(k)}
    return out


def bootstrap_ci(y, p, n_boot: int = 200, seed: int = 0) -> dict:
    """95% bootstrap confidence intervals for ROC-AUC and PR-AUC."""
    rng = np.random.default_rng(seed)
    y = np.asarray(y)
    aucs, aps = [], []
    n = len(y)
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        if y[idx].min() == y[idx].max():
            continue
        aucs.append(roc_auc_score(y[idx], p[idx]))
        aps.append(average_precision_score(y[idx], p[idx]))
    if not aucs:
        return {}
    lo = lambda a: _round(np.percentile(a, 2.5))
    hi = lambda a: _round(np.percentile(a, 97.5))
    return {'roc_auc': [lo(aucs), hi(aucs)], 'pr_auc': [lo(aps), hi(aps)]}


def best_f1_threshold(y, p) -> float:
    prec, rec, thr = precision_recall_curve(y, p)
    f1 = 2 * prec * rec / np.maximum(prec + rec, 1e-9)
    if not len(thr):
        return 0.5
    return float(thr[int(np.argmax(f1[:-1]))])


def evaluate(y, p, threshold: float) -> dict:
    """Full holdout evaluation at an operating threshold."""
    y = np.asarray(y).astype(int)
    p = np.asarray(p, dtype=float)
    pred = (p >= threshold).astype(int)
    cm = confusion_matrix(y, pred, labels=[0, 1]).tolist()
    fpr, tpr, _ = roc_curve(y, p)
    step = max(1, len(fpr) // 120)  # keep payload small
    sel = np.unique(np.r_[np.arange(0, len(fpr), step), len(fpr) - 1])
    base = float(y.mean())
    return {
        'metrics': {
            'roc_auc': _round(roc_auc_score(y, p) * 100, 2),
            'pr_auc': _round(average_precision_score(y, p) * 100, 2),
            'pr_auc_baseline': _round(base * 100, 2),
            'brier': _round(brier_score_loss(y, p)),
            'accuracy': _round(accuracy_score(y, pred) * 100, 2),
            'precision': _round(precision_score(y, pred, zero_division=0) * 100, 2),
            'recall': _round(recall_score(y, pred, zero_division=0) * 100, 2),
            'f1_score': _round(f1_score(y, pred, zero_division=0) * 100, 2),
            'threshold': _round(threshold),
            'base_rate': _round(base * 100, 2),
        },
        'confusion_matrix': cm,
        'roc_curve': {'fpr': [_round(x) for x in fpr[sel]], 'tpr': [_round(x) for x in tpr[sel]]},
        'lift': lift_table(y, p),
        'calibration': calibration_curve_points(y, p),
        'precision_at_k': precision_at_fraction(y, p),
        'confidence_intervals': bootstrap_ci(y, p),
        'score_distribution': score_histogram(p),
    }


def score_histogram(p, bins: int = 20) -> dict:
    counts, edges = np.histogram(p, bins=bins, range=(0, 1))
    return {'labels': [f'{int(edges[i] * 100)}-{int(edges[i + 1] * 100)}' for i in range(bins)], 'counts': counts.astype(int).tolist()}


def psi(expected: np.ndarray, actual: np.ndarray, eps: float = 1e-4) -> float:
    """Population Stability Index between two proportion vectors (<0.1 stable, >0.25 major shift)."""
    e = np.clip(np.asarray(expected, dtype=float), eps, None)
    a = np.clip(np.asarray(actual, dtype=float), eps, None)
    e, a = e / e.sum(), a / a.sum()
    return float(np.sum((a - e) * np.log(a / e)))
