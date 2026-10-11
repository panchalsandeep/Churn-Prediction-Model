"""Benchmark many churn-model families on identical splits.

    python scripts/benchmark.py            # all datasets, 3 seeds each
    python scripts/benchmark.py telco bank # a subset

Every candidate gets the same preprocessing, the same Platt calibration and the same holdout, so the
differences are down to the model. Results land in datasets/results/benchmark.json (+ a printed table).
Sized for a laptop: datasets are small, n_jobs is capped, and nothing is parallelised across candidates.
"""
import json
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import (ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier,
                              StackingClassifier)
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import RandomizedSearchCV, StratifiedKFold, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))
from churn.demo_data import generate_demo  # noqa: E402
from churn.schema import infer_schema, model_columns, normalize_target, prepare_features  # noqa: E402

warnings.filterwarnings('ignore')
READY = os.path.join(ROOT, 'datasets', 'ready')
OUT = os.path.join(ROOT, 'datasets', 'results')
os.makedirs(OUT, exist_ok=True)
JOBS = min(4, os.cpu_count() or 1)


def load(name):
    if name == 'demo':
        return generate_demo(8000, seed=7)
    return pd.read_csv(os.path.join(READY, f'{name}.csv'), low_memory=False)


def pre(num, cat, scale=False):
    steps = [('imp', SimpleImputer(strategy='median', add_indicator=True))] + ([('sc', StandardScaler())] if scale else [])
    t = []
    if num:
        t.append(('num', Pipeline(steps), num))
    if cat:
        t.append(('cat', OneHotEncoder(handle_unknown='infrequent_if_exist', min_frequency=0.01, sparse_output=False), cat))
    return ColumnTransformer(t)


HGB = dict(max_iter=250, learning_rate=0.06, max_leaf_nodes=16, min_samples_leaf=30, l2_regularization=1.0,
           early_stopping=True, n_iter_no_change=20, validation_fraction=0.15, random_state=42)


def hgb_pipe(num, cat, **over):
    return Pipeline([('pre', pre(num, cat)), ('clf', HistGradientBoostingClassifier(**{**HGB, **over}))])


def lr_pipe(num, cat):
    return Pipeline([('pre', pre(num, cat, True)), ('clf', LogisticRegression(max_iter=2000, C=0.5))])


def rf_pipe(num, cat):
    return Pipeline([('pre', pre(num, cat)), ('clf', RandomForestClassifier(n_estimators=200, min_samples_leaf=8, max_features='sqrt',
                                                                            n_jobs=JOBS, random_state=42))])


def et_pipe(num, cat):
    return Pipeline([('pre', pre(num, cat)), ('clf', ExtraTreesClassifier(n_estimators=200, min_samples_leaf=8, max_features='sqrt',
                                                                          n_jobs=JOBS, random_state=42))])


def tuned_hgb(num, cat, X, y):
    space = {'clf__learning_rate': [0.02, 0.04, 0.06, 0.1], 'clf__max_leaf_nodes': [6, 10, 16, 24, 31],
             'clf__min_samples_leaf': [10, 20, 40, 80], 'clf__l2_regularization': [0, 0.5, 1, 3, 10],
             'clf__max_iter': [150, 300, 500]}
    search = RandomizedSearchCV(hgb_pipe(num, cat), space, n_iter=14, scoring='average_precision', refit=False,
                                cv=StratifiedKFold(3, shuffle=True, random_state=0), random_state=0, n_jobs=1)
    search.fit(X, y)
    return {k.replace('clf__', ''): v for k, v in search.best_params_.items()}


def stack_pipe(num, cat):
    return StackingClassifier([('lr', lr_pipe(num, cat)), ('hgb', hgb_pipe(num, cat)), ('rf', rf_pipe(num, cat))],
                              final_estimator=LogisticRegression(max_iter=1000), cv=3, n_jobs=1)


def add_ratio_features(X, num, y_hint, top=5):
    """Generic feature engineering: ratios/products among the numeric columns most related to churn."""
    scores = {}
    for c in num:
        v = X[c].fillna(X[c].median())
        if v.nunique() > 1:
            try:
                a = roc_auc_score(y_hint, v)
                scores[c] = abs(a - 0.5)
            except ValueError:
                pass
    cols = sorted(scores, key=scores.get, reverse=True)[:top]
    out = X.copy()
    new = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            out[f'{a}__div__{b}'] = X[a] / (X[b].abs() + 1e-6)
            out[f'{a}__mul__{b}'] = X[a] * X[b]
            new += [f'{a}__div__{b}', f'{a}__mul__{b}']
    return out, new


def calibrated(est, n):
    return CalibratedClassifierCV(est, method='sigmoid' if n < 8000 else 'isotonic', cv=3)


def lift10(y, p):
    order = np.argsort(-p)[: max(1, len(y) // 10)]
    return y[order].mean() / max(y.mean(), 1e-9)


def run_dataset(name, seeds):
    df = load(name)
    schema = infer_schema(df)
    keep = normalize_target(df[schema['target']]).notna().to_numpy()
    df = df.loc[keep].reset_index(drop=True)
    y = normalize_target(df[schema['target']]).astype(int).to_numpy()
    num, cat = model_columns(schema)
    X = prepare_features(df, schema)[num + cat]
    print(f'\n=== {name}: {len(df):,} rows, churn {y.mean():.1%}, {len(num)} numeric + {len(cat)} categorical'
          f'{", split=time" if schema["time_col"] else ""}', flush=True)

    results = {}
    for seed in seeds:
        if schema['time_col']:
            order = np.argsort(pd.to_datetime(df[schema['time_col']]).to_numpy(), kind='stable')
            cut = int(len(df) * 0.8)
            tr, te = order[:cut], order[cut:]
        else:
            tr, te = train_test_split(np.arange(len(df)), test_size=0.2, stratify=y, random_state=seed)
        Xtr, ytr, Xte, yte = X.iloc[tr], y[tr], X.iloc[te], y[te]

        t0 = time.time()
        params = tuned_hgb(num, cat, Xtr, ytr)
        tune_s = time.time() - t0
        Xfe_tr, new = add_ratio_features(Xtr, num, ytr)
        Xfe_te = Xte.copy()
        for c in new:
            a, op, b = c.split('__')[0], c.split('__')[1], c.split('__')[2]
            Xfe_te[c] = Xte[a] / (Xte[b].abs() + 1e-6) if op == 'div' else Xte[a] * Xte[b]
        num_fe = num + new

        cands = {
            'logistic': (lr_pipe(num, cat), Xtr, Xte),
            'hgb_default': (hgb_pipe(num, cat), Xtr, Xte),
            'hgb_balanced': (hgb_pipe(num, cat, class_weight='balanced'), Xtr, Xte),
            'hgb_tuned': (hgb_pipe(num, cat, **params), Xtr, Xte),
            'hgb_ratio_features': (hgb_pipe(num_fe, cat), Xfe_tr, Xfe_te),
            'random_forest': (rf_pipe(num, cat), Xtr, Xte),
            'extra_trees': (et_pipe(num, cat), Xtr, Xte),
            'stacking(lr+hgb+rf)': (stack_pipe(num, cat), Xtr, Xte),
        }
        for label, (est, a, b) in cands.items():
            t = time.time()
            model = calibrated(est, len(tr)).fit(a, ytr)
            p = model.predict_proba(b)[:, 1]
            r = results.setdefault(label, {'roc_auc': [], 'pr_auc': [], 'brier': [], 'lift10': [], 'seconds': []})
            r['roc_auc'].append(roc_auc_score(yte, p))
            r['pr_auc'].append(average_precision_score(yte, p))
            r['brier'].append(brier_score_loss(yte, p))
            r['lift10'].append(lift10(yte, p))
            r['seconds'].append(time.time() - t + (tune_s if label == 'hgb_tuned' else 0))
        print(f'  seed {seed} done ({time.time() - t0:.0f}s)  tuned params {params}', flush=True)

    summary = {k: {m: float(np.mean(v)) for m, v in r.items()} | {'roc_auc_sd': float(np.std(r['roc_auc']))} for k, r in results.items()}
    print(f'{"model":<22}{"ROC-AUC":>9}{"±sd":>7}{"PR-AUC":>9}{"Brier":>8}{"lift@10%":>10}{"sec":>7}')
    for k, s in sorted(summary.items(), key=lambda kv: -kv[1]['pr_auc']):
        print(f'{k:<22}{s["roc_auc"]:>9.4f}{s["roc_auc_sd"]:>7.4f}{s["pr_auc"]:>9.4f}{s["brier"]:>8.4f}{s["lift10"]:>10.2f}{s["seconds"]:>7.1f}')
    return {'rows': len(df), 'churn_rate': float(y.mean()), 'models': summary}


if __name__ == '__main__':
    names = sys.argv[1:] or ['orange', 'telco', 'bank', 'demo']
    out = {}
    for n in names:
        out[n] = run_dataset(n, seeds=[0] if n == 'demo' else [0, 1, 2])
        json.dump(out, open(os.path.join(OUT, 'benchmark.json'), 'w'), indent=1)
    print('\nSaved', os.path.join(OUT, 'benchmark.json'))
