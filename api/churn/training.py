"""Model training: candidate selection, probability calibration, honest evaluation.

Key choices (and why):
  * Gradient-boosted trees are the main model; logistic regression is the transparent baseline,
    random forest the third opinion. The winner is picked on cross-validated PR-AUC, not accuracy.
  * Probabilities are calibrated (isotonic/sigmoid) so "30%" really means ~30%, which the
    cost-based thresholds depend on.
  * If the data has a snapshot date the holdout is the *latest* 20% in time, mirroring production.
  * Customers are scored with out-of-fold predictions, so the risk list is never the model
    grading its own homework.
"""
from __future__ import annotations

import hashlib
import os
import time
import warnings
from typing import Callable

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score
from sklearn.model_selection import (RandomizedSearchCV, StratifiedGroupKFold, StratifiedKFold, cross_val_predict,
                                     cross_val_score)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from . import economics, metrics as M
from .config import RANDOM_STATE, TRAIN_TIME_BUDGET_S
from .schema import infer_schema, model_columns, normalize_target, prepare_features

warnings.filterwarnings('ignore')

ALGORITHMS = ['auto', 'gradient_boosting', 'logistic_regression', 'random_forest']   # what users may force; 'auto' also tries the tuned variant
PRETTY = {
    'gradient_boosting': 'Gradient Boosting',
    'gradient_boosting_tuned': 'Gradient Boosting (tuned)',
    'logistic_regression': 'Logistic Regression',
    'random_forest': 'Random Forest',
}
TENURE_BANDS = [(0, 3, '0-3m'), (3, 6, '3-6m'), (6, 12, '6-12m'), (12, 24, '12-24m'), (24, 10_000, '24m+')]

Progress = Callable[[str, int], None]

# Isotonic calibration overfits small samples (it can output literal 0%/100%); use Platt scaling below this.
ISOTONIC_MIN_ROWS = 8000
PROB_FLOOR, PROB_CEIL = 0.002, 0.995


def clip_probs(p):
    """Calibrated models should never claim certainty about a customer."""
    return np.clip(p, PROB_FLOOR, PROB_CEIL)


def plan_for(n: int) -> dict:
    """Size-aware training plan: public hosting means one big upload must not starve everyone else.

    small  (< 25k)    the full zoo, small search, 4-fold out-of-fold scoring
    medium (< 150k)   linear + boosted (+ forest below 60k), selection on a 40k sample
    large  (>= 150k)  linear + boosted only, selection on a 30k sample, 3-fold OOF with lighter calibration
    """
    if n < 25_000:
        return {'tier': 'small', 'kinds': ['gradient_boosting', 'gradient_boosting_tuned', 'logistic_regression', 'random_forest'],
                'select_rows': 60_000, 'oof_outer': 4, 'oof_inner': 2, 'tune_iter': 12}
    if n < 150_000:
        kinds = ['gradient_boosting', 'logistic_regression'] + (['gradient_boosting_tuned', 'random_forest'] if n < 60_000 else [])
        return {'tier': 'medium', 'kinds': kinds, 'select_rows': 40_000, 'oof_outer': 3, 'oof_inner': 2, 'tune_iter': 8}
    return {'tier': 'large', 'kinds': ['gradient_boosting', 'logistic_regression'], 'select_rows': 30_000, 'oof_outer': 3, 'oof_inner': 2, 'tune_iter': 0}


def _noop(stage: str, pct: int) -> None:  # pragma: no cover
    pass


# ── Pipelines ──────────────────────────────────────────────────────────────────

HGB_SPACE = {
    'clf__learning_rate': [0.02, 0.04, 0.06, 0.1], 'clf__max_leaf_nodes': [6, 10, 16, 24, 31],
    'clf__min_samples_leaf': [10, 20, 40, 80], 'clf__l2_regularization': [0, 0.5, 1, 3, 10], 'clf__max_iter': [150, 300, 500],
}


def tune_hgb(X: pd.DataFrame, y: np.ndarray, num_cols: list[str], cat_cols: list[str], n_iter: int, cv=None) -> dict:
    """Small randomised search for the boosted model, scored on PR-AUC. Benchmarks showed this is the one tuning step that pays."""
    search = RandomizedSearchCV(make_pipeline('gradient_boosting', num_cols, cat_cols), HGB_SPACE, n_iter=n_iter,
                                scoring='average_precision', refit=False, random_state=0, n_jobs=1,
                                cv=cv if cv is not None else StratifiedKFold(3, shuffle=True, random_state=0))
    search.fit(X, y)
    return {k.replace('clf__', ''): v for k, v in search.best_params_.items()}


def make_pipeline(kind: str, num_cols: list[str], cat_cols: list[str], hgb_params: dict | None = None) -> Pipeline:
    num_steps = [('impute', SimpleImputer(strategy='median', add_indicator=True))]
    if kind == 'logistic_regression':
        num_steps.append(('scale', StandardScaler()))
    transformers = []
    if num_cols:
        transformers.append(('num', Pipeline(num_steps), num_cols))
    if cat_cols:
        transformers.append(('cat', OneHotEncoder(handle_unknown='infrequent_if_exist', min_frequency=0.01, sparse_output=False), cat_cols))
    pre = ColumnTransformer(transformers, remainder='drop')

    if kind == 'logistic_regression':
        clf = LogisticRegression(max_iter=2000, C=0.5, random_state=RANDOM_STATE)
    elif kind == 'random_forest':
        clf = RandomForestClassifier(n_estimators=150, min_samples_leaf=8, max_features='sqrt', n_jobs=min(4, os.cpu_count() or 1),
                                     random_state=RANDOM_STATE)
    else:
        params = dict(max_iter=250, learning_rate=0.06, max_leaf_nodes=16, min_samples_leaf=30, l2_regularization=1.0,
                      early_stopping=True, n_iter_no_change=20, validation_fraction=0.15, random_state=RANDOM_STATE)
        if kind == 'gradient_boosting_tuned' and hgb_params:
            params.update(hgb_params)
        clf = HistGradientBoostingClassifier(**params)
    return Pipeline([('pre', pre), ('clf', clf)])


class GroupedCV:
    """Stratified K-fold that never splits a customer across folds.

    Group labels are looked up through the *index* of the data being split, so one object works for every subset
    (selection sample, training part, calibration folds) that keeps the original row labels.
    """

    def __init__(self, groups: pd.Series, n_splits: int = 3, seed: int = RANDOM_STATE):
        self.groups, self.n_splits, self.seed = groups, n_splits, seed

    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        return self.n_splits

    def split(self, X, y=None, groups=None):
        g = self.groups.loc[X.index].to_numpy()
        yield from StratifiedGroupKFold(self.n_splits, shuffle=True, random_state=self.seed).split(X, y, g)


def _calibrated(pipe: Pipeline, n_train: int, inner_cv=3) -> CalibratedClassifierCV:
    return CalibratedClassifierCV(pipe, method='isotonic' if n_train >= ISOTONIC_MIN_ROWS else 'sigmoid', cv=inner_cv)


# ── Helpers ────────────────────────────────────────────────────────────────────

def provenance(data_hash: str, schema_sig: str) -> dict:
    """Everything needed to reproduce, audit, or safely refuse to load a model later."""
    import platform
    import subprocess
    import sys as _sys

    import scipy
    import sklearn

    sha = os.environ.get('GIT_SHA')
    if not sha:
        try:
            sha = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=3,
                                 cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip() or None
        except Exception:  # noqa: BLE001
            sha = None
    return {
        'python': platform.python_version(), 'scikit_learn': sklearn.__version__, 'numpy': np.__version__,
        'pandas': pd.__version__, 'scipy': scipy.__version__, 'code_version': sha, 'seed': RANDOM_STATE,
        'trained_at': time.time(), 'data_hash': data_hash, 'schema_signature': schema_sig, 'bundle_format': 3,
    }


def dataset_hash(df: pd.DataFrame) -> str:
    h = hashlib.sha256(pd.util.hash_pandas_object(df, index=False).values.tobytes())
    h.update(','.join(map(str, df.columns)).encode())
    return h.hexdigest()[:16]


def schema_signature(schema: dict) -> str:
    key = '|'.join(sorted(schema['numeric']) + ['#'] + sorted(schema['categorical']) + ['#'] + sorted(schema['dates']))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _split_indices(df: pd.DataFrame, schema: dict, y: np.ndarray, groups: pd.Series | None = None) -> tuple[np.ndarray, np.ndarray, str, int]:
    """Time-based split when possible, otherwise stratified random. Customers never straddle the split.

    Returns (train_idx, test_idx, kind, rows_dropped_to_prevent_leakage).
    """
    n = len(df)
    time_col = schema.get('time_col')
    if time_col and time_col in df.columns:
        t = pd.to_datetime(df[time_col], errors='coerce')
        if t.notna().mean() > 0.95 and t.nunique() > 3:
            order = np.argsort(t.fillna(t.min()).to_numpy(), kind='stable')
            cut = int(n * 0.8)
            train_idx, test_idx = order[:cut], order[cut:]
            dropped = 0
            if groups is not None:           # a customer seen in the future must not also be in training
                seen_in_test = set(groups.iloc[test_idx])
                keep_train = np.array([g not in seen_in_test for g in groups.iloc[train_idx]])
                dropped = int((~keep_train).sum())
                train_idx = train_idx[keep_train]
            if y[test_idx].sum() >= 5 and (len(test_idx) - y[test_idx].sum()) >= 5 and y[train_idx].sum() >= 10:
                return train_idx, test_idx, 'time', dropped
    if groups is not None:
        sgkf = StratifiedGroupKFold(5, shuffle=True, random_state=RANDOM_STATE)
        train_idx, test_idx = next(sgkf.split(np.zeros(n), y, groups.to_numpy()))
        return np.sort(train_idx), np.sort(test_idx), 'random', 0
    rng = np.random.default_rng(RANDOM_STATE)
    train_idx, test_idx = [], []
    for cls in (0, 1):
        idx = np.where(y == cls)[0]
        rng.shuffle(idx)
        cut = int(len(idx) * 0.8)
        train_idx.extend(idx[:cut])
        test_idx.extend(idx[cut:])
    return np.array(sorted(train_idx)), np.array(sorted(test_idx)), 'random', 0


def _baseline(X: pd.DataFrame, num_cols: list[str], cat_cols: list[str]) -> dict:
    base = {}
    for c in num_cols:
        med = X[c].median()
        base[c] = float(med) if pd.notna(med) else 0.0
    for c in cat_cols:
        mode = X[c].mode()
        base[c] = str(mode.iloc[0]) if len(mode) else 'Missing'
    return base


def _feature_ranges(X: pd.DataFrame, num_cols: list[str], cat_cols: list[str]) -> dict:
    ranges = {}
    for c in num_cols:
        col = X[c].dropna()
        if col.empty:
            ranges[c] = {'min': 0, 'max': 1, 'integer': False}
            continue
        lo, hi = float(col.quantile(0.01)), float(col.quantile(0.99))
        if hi <= lo:
            lo, hi = float(col.min()), float(col.max()) if col.max() > col.min() else float(col.min()) + 1
        ranges[c] = {'min': round(lo, 3), 'max': round(hi, 3), 'integer': bool((col.round() == col).all())}
    for c in cat_cols:
        ranges[c] = {'options': [str(v) for v in X[c].value_counts().head(15).index]}
    return ranges


def _correlated_pairs(X: pd.DataFrame, num_cols: list[str], threshold: float = 0.7, limit: int = 20) -> list[list]:
    """Pairs of numeric features that normally move together. The what-if simulator warns when a scenario pulls them apart."""
    cols = [c for c in num_cols if X[c].nunique() > 3]
    if len(cols) < 2:
        return []
    sample = X[cols].sample(min(len(X), 20000), random_state=RANDOM_STATE)
    corr = sample.corr(method='spearman').abs()
    out = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            r = corr.loc[a, b]
            if pd.notna(r) and r >= threshold:
                out.append([a, b, round(float(r), 2)])
    return sorted(out, key=lambda p: -p[2])[:limit]


def _drift_reference(X: pd.DataFrame, probs: np.ndarray, num_cols: list[str], cat_cols: list[str]) -> dict:
    ref = {'numeric': {}, 'categorical': {}}
    for c in num_cols:
        col = X[c].dropna()
        if col.nunique() < 3:
            continue
        edges = np.unique(np.quantile(col, np.linspace(0, 1, 11)))
        if len(edges) < 3:
            continue
        inner = edges[1:-1]
        counts = np.bincount(np.digitize(col, inner), minlength=len(inner) + 1)
        ref['numeric'][c] = {'edges': inner.tolist(), 'p': (counts / counts.sum()).tolist()}
    for c in cat_cols:
        vc = X[c].value_counts(normalize=True)
        top = vc.head(10)
        ref['categorical'][c] = {'values': [str(v) for v in top.index], 'p': top.round(6).tolist(), 'other': round(float(1 - top.sum()), 6)}
    counts, _ = np.histogram(probs, bins=10, range=(0, 1))
    ref['score'] = (counts / max(counts.sum(), 1)).tolist()
    return ref


def drift_report(ref: dict, X_new: pd.DataFrame, probs_new: np.ndarray) -> dict:
    """PSI per feature plus score drift between the training reference and a new batch."""
    feats = []
    for c, r in ref.get('numeric', {}).items():
        if c in X_new.columns:
            col = X_new[c].dropna()
            if len(col) >= 30:
                counts = np.bincount(np.digitize(col, r['edges']), minlength=len(r['edges']) + 1)
                feats.append({'feature': c, 'psi': round(M.psi(np.array(r['p']), counts / counts.sum()), 4)})
    for c, r in ref.get('categorical', {}).items():
        if c in X_new.columns and len(X_new) >= 30:
            vc = X_new[c].astype(str).value_counts(normalize=True)
            actual = [float(vc.get(v, 0)) for v in r['values']]
            actual.append(max(0.0, 1 - sum(actual)))
            feats.append({'feature': c, 'psi': round(M.psi(np.array(r['p'] + [r['other']]), np.array(actual)), 4)})
    counts, _ = np.histogram(probs_new, bins=10, range=(0, 1))
    score_psi = round(M.psi(np.array(ref['score']), counts / max(counts.sum(), 1)), 4)
    feats.sort(key=lambda f: -f['psi'])
    worst = max([score_psi] + [f['psi'] for f in feats])
    status = 'major_shift' if worst > 0.25 else 'moderate_shift' if worst > 0.1 else 'stable'
    return {'score_psi': score_psi, 'features': feats[:15], 'status': status, 'max_psi': round(worst, 4)}


# ── Explanations ───────────────────────────────────────────────────────────────

def explain_rows(model, X: pd.DataFrame, baseline: dict, cols: list[str], top_n: int = 5, batch: int = 400) -> list[list[dict]]:
    """Per-customer drivers: change in churn probability when one feature is set to the typical value.

    Positive impact = this feature is pushing the customer's risk up. Model-agnostic, so it works for
    the calibrated ensembles used here without extra dependencies.
    """
    out: list[list[dict]] = []
    for start in range(0, len(X), batch):
        chunk = X.iloc[start:start + batch].reset_index(drop=True)
        base_p = clip_probs(model.predict_proba(chunk)[:, 1])
        frames = []
        for c in cols:
            alt = chunk.copy()
            alt[c] = baseline[c]
            frames.append(alt)
        probs = clip_probs(model.predict_proba(pd.concat(frames, ignore_index=True))[:, 1]).reshape(len(cols), len(chunk))
        for i in range(len(chunk)):
            impacts = [
                {'feature': c, 'value': _py(chunk.iloc[i][c]), 'baseline': _py(baseline[c]), 'impact': round(float(base_p[i] - probs[j, i]) * 100, 2)}
                for j, c in enumerate(cols)
            ]
            impacts.sort(key=lambda d: -abs(d['impact']))
            out.append(impacts[:top_n])
    return out


def _py(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if np.isnan(v) else round(float(v), 3)
    return v


# ── Cohorts & segments ────────────────────────────────────────────────────────

def _tenure_months(df: pd.DataFrame, X: pd.DataFrame, schema: dict) -> pd.Series | None:
    if schema.get('tenure_col') and schema['tenure_col'] in df.columns:
        return pd.to_numeric(df[schema['tenure_col']], errors='coerce')
    if schema.get('signup_col'):
        col = f"{schema['signup_col']}_age_days"
        if col in X.columns:
            return X[col] / 30.0
    return None


def build_cohorts(df: pd.DataFrame, X: pd.DataFrame, schema: dict, y: np.ndarray, p: np.ndarray) -> dict:
    """Real churn rate / mean predicted risk by cohort × tenure band, from the labelled data."""
    tenure = _tenure_months(df, X, schema)
    if tenure is not None:
        band = pd.Series(pd.NA, index=df.index, dtype='object')
        for lo, hi, label in TENURE_BANDS:
            band[(tenure >= lo) & (tenure < hi)] = label
        band_labels = [b[2] for b in TENURE_BANDS]
    else:
        band = pd.Series('All', index=df.index, dtype='object')
        band_labels = ['All']

    dims: dict[str, pd.Series] = {}
    labels: dict[str, str] = {}
    if schema.get('signup_col') and schema['signup_col'] in df.columns:
        s = pd.to_datetime(df[schema['signup_col']], errors='coerce')
        dims['signup'] = s.dt.to_period('Q').astype(str).where(s.notna())
        labels['signup'] = 'Signup quarter'
    cats = sorted(schema['categorical'], key=lambda c: X[c].nunique())
    for c in [c for c in cats if 2 <= X[c].nunique() <= 12][:4]:
        dims[c] = X[c].astype(str)
        labels[c] = c.replace('_', ' ').title()

    out = {}
    for key, series in dims.items():
        frame = pd.DataFrame({'row': series.values, 'band': band.values, 'y': y, 'p': p})
        frame = frame.dropna(subset=['row'])
        if key == 'signup':
            rows = sorted(frame['row'].unique())[-8:]
        else:
            rows = list(frame['row'].value_counts().head(8).index)
        rate, risk, count = [], [], []
        for r in rows:
            sub = frame[frame['row'] == r]
            rr, kk, cc = [], [], []
            for b in band_labels:
                cell = sub[sub['band'] == b]
                if len(cell) >= 10:
                    rr.append(round(float(cell['y'].mean()) * 100, 1))
                    kk.append(round(float(cell['p'].mean()) * 100, 1))
                else:
                    rr.append(None)
                    kk.append(None)
                cc.append(int(len(cell)))
            rate.append(rr)
            risk.append(kk)
            count.append(cc)
        out[key] = {'label': labels[key], 'rows': [str(r) for r in rows], 'cols': band_labels, 'rate': rate, 'risk': risk, 'count': count}
    return out


def build_segments(scored: pd.DataFrame, attrs_cols: list[str]) -> list[dict]:
    """Segments with the largest expected revenue loss, per categorical attribute."""
    out = []
    for c in attrs_cols:
        if c not in scored.columns:
            continue
        g = scored.groupby(c).agg(customers=('prob', 'size'), avg_risk=('prob', 'mean'), expected_loss=('expected_loss', 'sum'),
                                  high_risk=('tier', lambda s: int((s == 'High').sum())))
        g = g[g['customers'] >= 5].sort_values('expected_loss', ascending=False).head(6)
        out.append({'attribute': c, 'segments': [
            {'value': str(idx), 'customers': int(r.customers), 'avg_risk': round(float(r.avg_risk) * 100, 1),
             'high_risk': int(r.high_risk), 'expected_loss': round(float(r.expected_loss), 2)} for idx, r in g.iterrows()]})
    return out



# ── Survival (time-to-churn) ──────────────────────────────────────────────────

def kaplan_meier(durations: np.ndarray, events: np.ndarray, max_points: int = 60) -> tuple[list[float], list[float]]:
    """Kaplan–Meier survival curve: probability a customer is still active after t."""
    order = np.argsort(durations, kind='stable')
    d, e = durations[order], events[order]
    s = 1.0
    at_risk = len(d)
    out_t, out_s = [0.0], [1.0]
    idx = 0
    for t in np.unique(d):
        end = np.searchsorted(d, t, side='right')
        ev = int(e[idx:end].sum())
        if at_risk > 0 and ev:
            s *= 1 - ev / at_risk
        at_risk -= end - idx
        idx = end
        out_t.append(float(t))
        out_s.append(round(float(s), 4))
    if len(out_t) > max_points:                     # thin evenly, always keep the last point
        keep = np.unique(np.linspace(0, len(out_t) - 1, max_points).astype(int))
        out_t, out_s = [out_t[i] for i in keep], [out_s[i] for i in keep]
    return out_t, out_s


def _surv_at(t: list[float], s: list[float], at: float) -> float | None:
    if not t or at > t[-1]:
        return None
    return s[int(np.searchsorted(t, at, side='right')) - 1]


def build_survival(df: pd.DataFrame, X: pd.DataFrame, schema: dict, y: np.ndarray) -> dict | None:
    """Retention-by-tenure curves. Needs a tenure measure; groups come from low-cardinality categoricals."""
    tenure = _tenure_months(df, X, schema)
    if tenure is None:
        return None
    ok = tenure.notna().to_numpy() & (tenure.to_numpy() >= 0)
    if ok.sum() < 100:
        return None
    dur, ev = tenure.to_numpy()[ok].astype(float), y[ok].astype(int)
    t, s = kaplan_meier(dur, ev)
    median = next((tt for tt, ss in zip(t, s) if ss <= 0.5), None)
    groups = {}
    cats = sorted(schema['categorical'], key=lambda c: X[c].nunique())
    for c in [c for c in cats if 2 <= X[c].nunique() <= 6][:3]:
        curves = []
        col = X[c].astype(str).to_numpy()[ok]
        for value in pd.Series(col).value_counts().head(5).index:
            m = col == value
            if m.sum() >= 30:
                gt, gs = kaplan_meier(dur[m], ev[m], max_points=40)
                curves.append({'name': str(value), 't': gt, 's': gs, 'n': int(m.sum())})
        if len(curves) >= 2:
            groups[c] = {'label': c.replace('_', ' ').title(), 'curves': curves}
    return {
        'unit': 'months', 'n': int(ok.sum()), 'median': median,
        's6': _surv_at(t, s, 6), 's12': _surv_at(t, s, 12), 's24': _surv_at(t, s, 24),
        'overall': {'t': t, 's': s}, 'groups': groups,
    }


# ── Personas (clustering) ─────────────────────────────────────────────────────

def build_personas(X: pd.DataFrame, num_cols: list[str], cat_cols: list[str], y: np.ndarray, p: np.ndarray,
                   revenue: np.ndarray | None) -> list[dict]:
    """Discover customer personas with k-means and describe each by what makes it different.

    Unsupervised on purpose: it can reveal groups the churn label alone does not (for example a quiet,
    low-spend segment that rarely complains and rarely leaves).
    """
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    n = len(X)
    if n < 300 or len(num_cols) + len(cat_cols) < 3:
        return []
    rng = np.random.default_rng(RANDOM_STATE)
    num = X[num_cols].apply(pd.to_numeric, errors='coerce')
    num = num.fillna(num.median())
    keep = [c for c in num_cols if num[c].std() > 0]
    if len(keep) < 2:
        return []
    z = (num[keep] - num[keep].mean()) / num[keep].std()
    parts = [z.clip(-4, 4)]
    top_cats = [c for c in cat_cols if X[c].nunique() <= 8][:6]
    if top_cats:
        parts.append(pd.get_dummies(X[top_cats].astype(str)).astype(float) * 0.6)
    M = pd.concat(parts, axis=1).to_numpy(dtype=float)

    sample = rng.choice(n, min(n, 6000), replace=False)
    best_k, best_score = 3, -1.0
    for k in (3, 4, 5, 6):
        km = KMeans(n_clusters=k, n_init=3, random_state=RANDOM_STATE).fit(M[sample])
        sc = silhouette_score(M[sample], km.labels_, sample_size=min(len(sample), 3000), random_state=RANDOM_STATE)
        if sc > best_score:
            best_k, best_score = k, sc
    labels = KMeans(n_clusters=best_k, n_init=5, random_state=RANDOM_STATE).fit(M[sample]).predict(M)

    overall_rate = float(y.mean())
    total_rev = float(revenue.sum()) if revenue is not None and revenue.sum() > 0 else None
    personas = []
    for c in range(best_k):
        m = labels == c
        if m.sum() < max(20, 0.02 * n):
            continue
        diffs = ((num.loc[m, keep].mean() - num[keep].mean()) / num[keep].std()).sort_values(key=lambda s_: -s_.abs())
        traits = [{'feature': f, 'direction': 'high' if d > 0 else 'low', 'z': round(float(d), 2),
                   'value': round(float(num.loc[m, f].mean()), 2), 'overall': round(float(num[f].mean()), 2)}
                  for f, d in diffs.head(4).items() if abs(d) >= 0.25]
        cat_traits = []
        for cc in top_cats:
            share = X.loc[m, cc].astype(str).value_counts(normalize=True)
            base = X[cc].astype(str).value_counts(normalize=True)
            top = share.index[0]
            if share.iloc[0] >= 0.45 and share.iloc[0] - base.get(top, 0) >= 0.15:
                cat_traits.append({'feature': cc, 'value': str(top), 'share': round(float(share.iloc[0]), 2)})
        rate = float(y[m].mean())
        personas.append({
            'size': int(m.sum()), 'share': round(float(m.mean()) * 100, 1),
            'churn_rate': round(rate * 100, 1), 'lift_vs_overall': round(rate / max(overall_rate, 1e-9), 2),
            'avg_risk': round(float(p[m].mean()) * 100, 1),
            'revenue_share': round(float(revenue[m].sum()) / total_rev * 100, 1) if total_rev else None,
            'traits': traits, 'categories': cat_traits,
        })
    personas.sort(key=lambda d: -d['churn_rate'])
    for i, d in enumerate(personas):
        d['id'] = i + 1
    return personas


# ── Main entry ─────────────────────────────────────────────────────────────────

def train_model(df: pd.DataFrame, algorithm: str = 'auto', econ: dict | None = None, progress: Progress = _noop,
                target: str | None = None, exclude: list[str] | None = None, allow_pii: list[str] | None = None,
                drift_df: pd.DataFrame | None = None, drift_skip: list[str] | None = None) -> dict:
    t0 = time.time()
    econ = economics.merged(econ)
    algorithm = algorithm if algorithm in ALGORITHMS else 'auto'

    progress('Detecting schema', 5)
    schema = infer_schema(df, target, exclude, allow_pii)
    keep = normalize_target(df[schema['target']]).notna().to_numpy()
    df = df.loc[keep].reset_index(drop=True)
    y = normalize_target(df[schema['target']]).astype(int).to_numpy()

    # Newest snapshot last, so "keep the latest row per customer" is really the latest.
    if schema.get('time_col') and schema['time_col'] in df.columns:
        order = np.argsort(pd.to_datetime(df[schema['time_col']], errors='coerce').to_numpy(), kind='stable')
        df = df.iloc[order].reset_index(drop=True)
        y = normalize_target(df[schema['target']]).astype(int).to_numpy()
    groups = df[schema['id']].astype(str) if schema.get('grouped') and schema.get('id') in df.columns else None

    X = prepare_features(df, schema)
    num_cols, cat_cols = model_columns(schema)
    cols = num_cols + cat_cols
    X = X[cols]

    train_idx, test_idx, split, leak_dropped = _split_indices(df, schema, y, groups)
    make_cv = (lambda k, seed=RANDOM_STATE: GroupedCV(groups, k, seed)) if groups is not None \
        else (lambda k, seed=RANDOM_STATE: StratifiedKFold(k, shuffle=True, random_state=seed))
    Xtr, ytr = X.iloc[train_idx], y[train_idx]
    Xte, yte = X.iloc[test_idx], y[test_idx]

    # 1) Candidate selection on the training part only
    progress('Comparing models', 15)
    plan = plan_for(len(df))
    kinds = plan['kinds'] if algorithm == 'auto' else [algorithm]
    sel_X, sel_y = Xtr, ytr
    if len(Xtr) > plan['select_rows']:
        sub = np.random.default_rng(RANDOM_STATE).choice(len(Xtr), plan['select_rows'], replace=False)
        sel_X, sel_y = Xtr.iloc[sub], ytr[sub]
    cv = make_cv(3)
    hp: dict = {}                                    # tuned boosting hyper-parameters, filled in when that candidate runs
    comparison = []
    for i, kind in enumerate(kinds):
        if comparison and time.time() - t0 > TRAIN_TIME_BUDGET_S * 0.4:
            break                                    # out of selection budget: go with what has been compared
        if kind == 'gradient_boosting_tuned':
            progress('Tuning gradient boosting', 15 + int(25 * i / len(kinds)))
            tune_rows = min(len(sel_X), 20_000)
            hp = tune_hgb(sel_X.iloc[:tune_rows], sel_y[:tune_rows], num_cols, cat_cols, plan['tune_iter'], cv=make_cv(3, 0))
            # Score it with different folds than the search used, so tuning can't flatter itself.
            fold_cv = make_cv(3, RANDOM_STATE + 1)
        else:
            fold_cv = cv
        score = cross_val_score(make_pipeline(kind, num_cols, cat_cols, hp), sel_X, sel_y, cv=fold_cv, scoring='average_precision')
        comparison.append({'algorithm': kind, 'name': PRETTY[kind], 'cv_pr_auc': round(float(score.mean()) * 100, 2),
                           'cv_std': round(float(score.std()) * 100, 2)})
        progress('Comparing models', 15 + int(25 * (i + 1) / len(kinds)))
    best = max(comparison, key=lambda r: r['cv_pr_auc'])['algorithm']

    # 2) Calibrated model on train, evaluated on the untouched holdout
    progress('Calibrating probabilities', 45)
    holdout_model = _calibrated(make_pipeline(best, num_cols, cat_cols, hp), len(Xtr), make_cv(3)).fit(Xtr, ytr)
    p_test = clip_probs(holdout_model.predict_proba(Xte)[:, 1])

    base_rate = float(y.mean())
    revenue = None
    if schema.get('revenue_col') and schema['revenue_col'] in df.columns:
        revenue = pd.to_numeric(df[schema['revenue_col']], errors='coerce').fillna(0).clip(lower=0).to_numpy()
    avg_rev = float(revenue.mean()) if revenue is not None and revenue.mean() > 0 else 1.0
    thresholds = economics.derive_thresholds(base_rate, avg_rev, econ)

    progress('Evaluating on holdout', 55)
    evaluation = M.evaluate(yte, p_test, threshold=thresholds['high'])
    evaluation['metrics']['threshold_f1'] = round(M.best_f1_threshold(yte, p_test), 4)
    evaluation['metrics']['threshold_breakeven'] = thresholds['breakeven']
    # Metrics at the operating point retention teams would actually use.
    f1_thr = evaluation['metrics']['threshold_f1']
    alt = M.evaluate(yte, p_test, threshold=f1_thr)
    evaluation['metrics_at_f1'] = {k: alt['metrics'][k] for k in ('accuracy', 'precision', 'recall', 'f1_score')}
    evaluation['confusion_matrix_f1'] = alt['confusion_matrix']

    # 3) Importance on the holdout (permutation, PR-AUC drop)
    progress('Measuring feature importance', 62)
    imp_X, imp_y = Xte, yte
    if len(Xte) > 3000:
        s = np.random.default_rng(RANDOM_STATE).choice(len(Xte), 3000, replace=False)
        imp_X, imp_y = Xte.iloc[s], yte[s]
    perm = permutation_importance(holdout_model, imp_X, imp_y, scoring='average_precision', n_repeats=3,
                                  random_state=RANDOM_STATE, n_jobs=1)
    raw = np.clip(perm.importances_mean, 0, None)
    total = raw.sum() or 1.0
    importance = sorted(({'feature': c, 'importance': round(float(v / total), 4), 'pr_auc_drop': round(float(v) * 100, 3)}
                         for c, v in zip(cols, raw)), key=lambda d: -d['importance'])

    # 4) Honest per-customer scores: out-of-fold predictions
    progress('Scoring customers (out-of-fold)', 70)
    outer = plan['oof_outer']
    oof_model = _calibrated(make_pipeline(best, num_cols, cat_cols, hp), len(X) * (outer - 1) // outer, make_cv(plan['oof_inner']))
    p_oof = cross_val_predict(oof_model, X, y, cv=make_cv(outer),
                              method='predict_proba')[:, 1]
    p_oof = clip_probs(p_oof)

    # 5) Deployable model on all the data
    progress('Fitting final model', 82)
    final_model = _calibrated(make_pipeline(best, num_cols, cat_cols, hp), len(X), make_cv(3)).fit(X, y)
    final_model.set_params(cv=3)      # the fitted splitter holds every customer id: never let it into a stored artifact

    baseline = _baseline(X, num_cols, cat_cols)
    ranges = _feature_ranges(X, num_cols, cat_cols)
    correlated = _correlated_pairs(X, num_cols)
    ref_X, ref_p = X, p_oof
    if schema.get('time_col') and schema['time_col'] in df.columns:
        # With several snapshots, features that grow with time (tenure, days to renewal) differ between the pooled history and any
        # new batch. Compare new data with the newest snapshot instead, or every new batch would look like drift.
        stamp = pd.to_datetime(df[schema['time_col']], errors='coerce')
        newest = (stamp == stamp.max()).to_numpy()
        if newest.sum() >= max(200, 0.03 * len(df)):
            ref_X, ref_p = X[newest], p_oof[newest]
    if drift_df is not None and len(drift_df) >= 30:
        # The population as it looks today (same builder as scoring): later batches are compared with this, so drift means change since training.
        ref_X = prepare_features(drift_df, schema).reindex(columns=cols)
        for c in cat_cols:
            ref_X[c] = ref_X[c].fillna('Missing')
        ref_p = clip_probs(final_model.predict_proba(ref_X)[:, 1])
    drift_ref = _drift_reference(ref_X, ref_p, [c for c in num_cols if c not in set(drift_skip or [])], cat_cols)      # skip features that only age with the calendar
    cohorts = build_cohorts(df, X, schema, y, p_oof)
    progress('Finding customer personas', 86)
    survival = build_survival(df, X, schema, y)
    personas = build_personas(X, num_cols, cat_cols, y, p_oof, revenue)

    progress('Explaining high-risk customers', 90)
    top_idx = np.argsort(-p_oof)[:300]
    reasons = explain_rows(final_model, X.iloc[top_idx], baseline, cols)
    reasons_by_pos = {int(i): r for i, r in zip(top_idx, reasons)}

    bundle = {
        'model': final_model, 'schema': schema, 'baseline': baseline, 'ranges': ranges,
        'num_cols': num_cols, 'cat_cols': cat_cols, 'cols': cols, 'econ': econ, 'thresholds': thresholds,
        'importance': importance, 'drift_ref': drift_ref, 'version': 2, 'correlated': correlated,
        'base_rate': base_rate, 'avg_revenue': avg_rev,
        'null_rates': {c: round(float(X[c].isna().mean()), 4) for c in cols},
        'provenance': provenance(dataset_hash(df), schema_signature(schema)),
    }
    meta = {
        'algorithm': best, 'algorithm_name': PRETTY[best], 'comparison': comparison, 'split': split,
        'train_rows': int(len(train_idx)), 'test_rows': int(len(test_idx)), 'rows': int(len(df)),
        'base_rate': round(base_rate * 100, 2), 'features': cols, 'feature_count': len(cols),
        'warnings': schema['warnings'], 'seconds': round(time.time() - t0, 1),
        'data_hash': dataset_hash(df), 'schema_signature': schema_signature(schema),
        'calibration_method': 'isotonic' if len(Xtr) >= ISOTONIC_MIN_ROWS else 'sigmoid',
        'plan': plan['tier'],
        'validation': {'grouped': groups is not None, 'groups': int(groups.nunique()) if groups is not None else None,
                       'rows_dropped_for_leakage': int(leak_dropped), 'split': split},
    }
    progress('Finalising', 96)
    return {
        'bundle': bundle, 'meta': meta, 'evaluation': evaluation, 'importance': importance, 'cohorts': cohorts,
        'schema': schema, 'survival': survival, 'personas': personas, 'thresholds': thresholds, 'econ': econ, 'df': df, 'X': X, 'y': y, 'p_oof': p_oof,
        'reasons_by_pos': reasons_by_pos, 'revenue': revenue,
    }
