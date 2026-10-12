"""Model evaluation with history and a regression gate.

    python scripts/eval.py                     # evaluate every available dataset, append to evals/history.jsonl
    python scripts/eval.py --check             # also compare with evals/baseline.json; exit 1 on a regression
    python scripts/eval.py --update-baseline   # accept the current numbers as the new baseline
    python scripts/eval.py demo b2b            # a subset

What is measured, per dataset, with the shipped pipeline (algorithm "auto", honest splits as in production):
  model     ROC-AUC, PR-AUC, Brier score and top-decile lift on the untouched holdout
  floor     what knowing nothing gives (PR-AUC = churn rate, ROC-AUC = 50) and a plain logistic regression on its own split,
            so every score can be read against "how much did the model add?"
Datasets: `demo` and `b2b` (synthetic, regenerated offline, used by CI) plus telco / bank / orange when
datasets/ready has them (python scripts/download_datasets.py). Every run is appended to evals/history.jsonl with the git commit.

The gate: a metric may not fall more than its tolerance below the baseline (PR-AUC 2.0 points, ROC-AUC 1.5, lift 0.3, Brier may not
rise by more than 0.01). Baselines only move when you run --update-baseline and commit the result, so a regression is a visible decision.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import warnings

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import train_test_split

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))
from churn import features, training  # noqa: E402
from churn.b2b_sim import generate_b2b  # noqa: E402
from churn.demo_data import generate_demo  # noqa: E402
from churn.schema import infer_schema, model_columns, normalize_target, prepare_features  # noqa: E402

warnings.filterwarnings('ignore')
EVALS = os.path.join(ROOT, 'evals')
READY = os.path.join(ROOT, 'datasets', 'ready')
TOLERANCE = {'pr_auc': 2.0, 'roc_auc': 1.5, 'lift_top10': 0.3, 'brier': 0.01}


def load(name):
    if name == 'demo':
        return generate_demo(8000, seed=7)
    if name == 'b2b':
        d = generate_b2b(600, 20, 11, base_logit=-3.4)
        df, _ = features.build_training_frame({k: d[k] for k in ('accounts', 'events', 'tickets', 'invoices')}, {'type': 'churn_date', 'horizon_days': 60})
        return df
    path = os.path.join(READY, f'{name}.csv')
    return pd.read_csv(path, low_memory=False) if os.path.exists(path) else None


def logistic_floor(df):
    schema = infer_schema(df)
    keep = normalize_target(df[schema['target']]).notna().to_numpy()
    df = df.loc[keep].reset_index(drop=True)
    y = normalize_target(df[schema['target']]).astype(int).to_numpy()
    num, cat = model_columns(schema)
    X = prepare_features(df, schema)[num + cat]
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, stratify=y, random_state=0)
    pipe = training.make_pipeline('logistic_regression', num, cat).fit(Xtr, ytr)
    p = pipe.predict_proba(Xte)[:, 1]
    return {'roc_auc': round(roc_auc_score(yte, p) * 100, 2), 'pr_auc': round(average_precision_score(yte, p) * 100, 2), 'brier': round(brier_score_loss(yte, p), 4)}


def evaluate(name):
    df = load(name)
    if df is None:
        return None
    t = time.time()
    r = training.train_model(df, 'auto')
    m, ev = r['evaluation']['metrics'], r['evaluation']
    base = float(r['y'].mean())
    return {'dataset': name, 'rows': int(len(df)), 'base_rate': round(base * 100, 2), 'split': r['meta']['split'], 'winner': r['meta']['algorithm_name'],
            'model': {'roc_auc': m['roc_auc'], 'pr_auc': m['pr_auc'], 'brier': m['brier'], 'lift_top10': ev['lift'][0]['lift']},
            'floor': {'no_skill_pr_auc': round(base * 100, 2), 'no_skill_roc_auc': 50.0, 'logistic_regression': logistic_floor(df)},
            'seconds': round(time.time() - t, 1)}


def git_sha():
    try:
        return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return 'unknown'


def compare(result, baseline):
    problems = []
    for k, tol in TOLERANCE.items():
        new, old = result['model'][k], baseline['model'][k]
        worse = (new - old > tol) if k == 'brier' else (old - new > tol)
        if worse:
            problems.append(f"{result['dataset']}: {k} {new} vs baseline {old} (tolerance {tol})")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('datasets', nargs='*', default=['demo', 'b2b', 'telco', 'bank', 'orange'])
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--update-baseline', action='store_true')
    args = ap.parse_args()
    os.makedirs(EVALS, exist_ok=True)
    base_path = os.path.join(EVALS, 'baseline.json')
    baseline = json.load(open(base_path)) if os.path.exists(base_path) else {}
    results, problems = [], []
    print(f'{"dataset":<8}{"rows":>8}{"base":>7}  {"ROC":>6}{"PR-AUC":>8}{"Brier":>8}{"lift":>6}   floor: LR ROC/PR   baseline PR')
    for name in args.datasets:
        r = evaluate(name)
        if r is None:
            print(f'{name:<8} (not available: run scripts/download_datasets.py)')
            continue
        results.append(r)
        m, lr = r['model'], r['floor']['logistic_regression']
        old = baseline.get(name, {}).get('model', {}).get('pr_auc')
        print(f'{name:<8}{r["rows"]:>8,}{r["base_rate"]:>6.1f}%  {m["roc_auc"]:>6.1f}{m["pr_auc"]:>8.1f}{m["brier"]:>8.4f}{m["lift_top10"]:>5.1f}x   {lr["roc_auc"]:>5.1f}/{lr["pr_auc"]:<5.1f}       {old if old is not None else "-"}', flush=True)
        if args.check and name in baseline:
            problems += compare(r, baseline[name])
    stamp = {'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'commit': git_sha()}
    with open(os.path.join(EVALS, 'history.jsonl'), 'a', encoding='utf-8') as f:
        for r in results:
            f.write(json.dumps({**stamp, **r}) + '\n')
    if args.update_baseline:
        baseline.update({r['dataset']: {'model': r['model'], 'floor': r['floor'], 'rows': r['rows'], 'recorded': stamp} for r in results})
        json.dump(baseline, open(base_path, 'w'), indent=1)
        print('baseline updated:', base_path)
    if problems:
        print('\nREGRESSION:\n  ' + '\n  '.join(problems))
        sys.exit(1)
    if args.check:
        print('\nno regression against the baseline')


if __name__ == '__main__':
    main()
