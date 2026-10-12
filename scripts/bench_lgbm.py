"""Is LightGBM worth adding next to scikit-learn's HistGradientBoosting?  It would give exact per-customer
explanations (pred_contrib) in milliseconds instead of the perturbation method.

    python scripts/bench_lgbm.py

Same preprocessing, same splits, same (uncalibrated) comparison for both models; 3 seeds per dataset.
Prints PR-AUC / ROC-AUC / fit seconds, then the cost of explaining 1,000 customers each way on the largest dataset.
"""
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))
from churn.demo_data import generate_demo  # noqa: E402
from churn.schema import infer_schema, model_columns, normalize_target, prepare_features  # noqa: E402
from churn import training  # noqa: E402

warnings.filterwarnings('ignore')
READY = os.path.join(ROOT, 'datasets', 'ready')


def load(name):
    if name.startswith('demo'):
        return generate_demo(int(name[4:] or 8000), seed=7)
    return pd.read_csv(os.path.join(READY, f'{name}.csv'), low_memory=False)


def prep(num, cat):
    t = []
    if num:
        t.append(('num', SimpleImputer(strategy='median', add_indicator=True), num))
    if cat:
        t.append(('cat', OneHotEncoder(handle_unknown='infrequent_if_exist', min_frequency=0.01, sparse_output=False), cat))
    return ColumnTransformer(t)


def hgb():
    return HistGradientBoostingClassifier(max_iter=250, learning_rate=0.06, max_leaf_nodes=16, min_samples_leaf=30, l2_regularization=1.0,
                                          early_stopping=True, n_iter_no_change=20, validation_fraction=0.15, random_state=42)


def lgbm():
    return LGBMClassifier(n_estimators=250, learning_rate=0.06, num_leaves=16, min_child_samples=30, reg_lambda=1.0, subsample=0.8,
                          subsample_freq=1, colsample_bytree=0.9, random_state=42, n_jobs=4, verbose=-1)


def main():
    print(f'{"dataset":<10}{"rows":>8}  {"model":<9}{"PR-AUC":>8}{"ROC-AUC":>9}{"fit s":>8}')
    last = None
    for name in ('telco', 'bank', 'orange', 'demo8000', 'demo100000'):
        df = load(name)
        schema = infer_schema(df)
        keep = normalize_target(df[schema['target']]).notna().to_numpy()
        df = df.loc[keep].reset_index(drop=True)
        y = normalize_target(df[schema['target']]).astype(int).to_numpy()
        num, cat = model_columns(schema)
        X = prepare_features(df, schema)[num + cat]
        for label, make in (('HGB', hgb), ('LightGBM', lgbm)):
            pr, roc, secs = [], [], []
            for seed in (0, 1, 2):
                Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.3, stratify=y, random_state=seed)
                pipe = Pipeline([('pre', prep(num, cat)), ('clf', make())])
                t = time.time()
                pipe.fit(Xtr, ytr)
                secs.append(time.time() - t)
                p = pipe.predict_proba(Xte)[:, 1]
                pr.append(average_precision_score(yte, p))
                roc.append(roc_auc_score(yte, p))
            print(f'{name:<10}{len(df):>8,}  {label:<9}{np.mean(pr) * 100:>8.2f}{np.mean(roc) * 100:>9.2f}{np.mean(secs):>8.2f}', flush=True)
        last = (X, y, num, cat)

    X, y, num, cat = last
    sample = X.iloc[:1000]
    pre = prep(num, cat).fit(X)
    names = list(pre.get_feature_names_out())
    clf = lgbm().fit(pre.transform(X), y)
    t = time.time()
    contrib = clf.predict(pre.transform(sample), pred_contrib=True)
    t_lgbm = time.time() - t
    pipe = Pipeline([('pre', prep(num, cat)), ('clf', hgb())]).fit(X, y)
    baseline = training._baseline(X, num, cat)
    t = time.time()
    training.explain_rows(pipe, sample, baseline, num + cat)
    t_perturb = time.time() - t
    print(f'\nexplain 1,000 customers on {len(X):,}-row model: LightGBM pred_contrib {t_lgbm * 1000:.0f} ms ({contrib.shape[1] - 1} transformed features, '
          f'{len(names)} names) vs perturbation {t_perturb * 1000:.0f} ms')


if __name__ == '__main__':
    main()
