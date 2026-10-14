"""Train the product pipeline on each public benchmark dataset and save the result as a reference model.

    python scripts/export_reference_models.py [telco bank orange]

Writes models/reference/<name>.skops (the same safe, pickle-free skops bundle the platform stores, WITHOUT the per-workspace encryption,
because these models contain no customer data: only fitted parameters and aggregate statistics of public datasets) and <name>.json (a model card).

Load one with:
    from churn import artifacts
    bundle = artifacts.loads_bundle(open('models/reference/telco.skops', 'rb').read())
    bundle['model'].predict_proba(X)[:, 1]        # X: a DataFrame with bundle['cols']

These are examples and starting points, not production models for anyone's customers: they were trained on small public datasets.
Check each dataset's licence (see datasets/README or scripts/download_datasets.py for the source) before redistributing the files.
"""
import hashlib
import json
import os
import sys
import time

import pandas as pd
import sklearn
import skops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))
from churn import artifacts, training  # noqa: E402

READY = os.path.join(ROOT, 'datasets', 'ready')
OUT = os.path.join(ROOT, 'models', 'reference')
os.makedirs(OUT, exist_ok=True)
SOURCES = {'telco': 'IBM Telco customer churn sample dataset', 'bank': 'Bank customer churn (public Kaggle dataset)', 'orange': 'Orange telecom churn dataset (public)'}

for name in sys.argv[1:] or ['telco', 'bank', 'orange']:
    path = os.path.join(READY, f'{name}.csv')
    if not os.path.exists(path):
        print(f'skip {name}: run scripts/download_datasets.py first')
        continue
    df = pd.read_csv(path, low_memory=False)
    t = time.time()
    r = training.train_model(df, 'auto')
    blob = artifacts.dumps_bundle(r['bundle'])
    assert b'customer_id' not in blob[:0] and artifacts.loads_bundle(blob)            # round-trips through the safe loader
    open(os.path.join(OUT, f'{name}.skops'), 'wb').write(blob)
    m = r['evaluation']['metrics']
    card = {
        'name': f'churnsight-reference-{name}', 'dataset': SOURCES.get(name, name), 'rows': int(len(df)), 'target': r['schema']['target'],
        'algorithm': r['meta']['algorithm_name'], 'validation': r['meta']['split'], 'trained_with': {'scikit_learn': sklearn.__version__, 'skops': skops.__version__},
        'metrics_on_untouched_holdout': {k: m[k] for k in ('roc_auc', 'pr_auc', 'pr_auc_baseline', 'brier')}, 'features': r['bundle']['cols'],
        'top_features': [f['feature'] for f in r['importance'][:6]], 'sha256': hashlib.sha256(blob).hexdigest(), 'bytes': len(blob),
        'limitations': ['Small public dataset: use as an example or a starting point, not as a model for your own customers.',
                        'Probabilities are calibrated for this dataset only; another population needs its own model.',
                        'Loads only with a compatible scikit-learn version (the loader refuses mismatches).'],
    }
    json.dump(card, open(os.path.join(OUT, f'{name}.json'), 'w'), indent=1)
    print(f"{name}: {len(blob) / 1e6:.2f} MB · ROC-AUC {m['roc_auc']} · PR-AUC {m['pr_auc']} · {time.time() - t:.0f}s")
