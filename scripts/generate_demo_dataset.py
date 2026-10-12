"""Write the demo CSV served at /sample_data.csv.

    python scripts/generate_demo_dataset.py [rows]

The generator lives in api/churn/demo_data.py: realistic SaaS churn data with rolling-window behaviour,
nine monthly snapshots, missing values, an unobserved customer effect and ~11% base churn.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))

from churn.demo_data import generate_demo  # noqa: E402

rows = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
out = os.path.join(ROOT, 'public', 'sample_data.csv')
os.makedirs(os.path.dirname(out), exist_ok=True)
df = generate_demo(rows)
df.to_csv(out, index=False)
print(f'Wrote {len(df):,} rows to {out} (churn rate {df.churn.mean():.1%})')
