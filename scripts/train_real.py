"""Train the *product* pipeline (api/churn/training.py, algorithm='auto') on each real dataset and record the results.

    python scripts/train_real.py [telco bank orange]

This is what a user would get by uploading the same file: schema inference, model selection, tuning, calibration,
holdout evaluation. Output: datasets/results/product_run.json.
"""
import json
import os
import sys
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))
from churn.training import train_model  # noqa: E402

READY = os.path.join(ROOT, 'datasets', 'ready')
OUT = os.path.join(ROOT, 'datasets', 'results')
os.makedirs(OUT, exist_ok=True)

names = sys.argv[1:] or ['orange', 'telco', 'bank']
results = {}
for name in names:
    df = pd.read_csv(os.path.join(READY, f'{name}.csv'), low_memory=False)
    t = time.time()
    r = train_model(df, 'auto', progress=lambda s, p: print(f'  {name} {p:3d}% {s}', flush=True) if p in (15, 45, 70, 96) else None)
    m, ev = r['evaluation']['metrics'], r['evaluation']
    results[name] = {
        'rows': len(df), 'seconds': round(time.time() - t, 1), 'winner': r['meta']['algorithm_name'], 'split': r['meta']['split'],
        'comparison': r['meta']['comparison'], 'metrics': m, 'precision_at_k': ev['precision_at_k'],
        'ci': ev['confidence_intervals'], 'lift_top_decile': ev['lift'][0]['lift'],
        'top_features': [f['feature'] for f in r['importance'][:5]],
        'personas': len(r['personas']), 'survival_median_months': (r['survival'] or {}).get('median'),
    }
    print(f"{name}: winner {results[name]['winner']} · ROC-AUC {m['roc_auc']} · PR-AUC {m['pr_auc']} (base {m['pr_auc_baseline']}) · "
          f"Brier {m['brier']} · top-decile lift {results[name]['lift_top_decile']} · {results[name]['seconds']}s", flush=True)
    json.dump(results, open(os.path.join(OUT, 'product_run.json'), 'w'), indent=1)
print('saved', os.path.join(OUT, 'product_run.json'))
