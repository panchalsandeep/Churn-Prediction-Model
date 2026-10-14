"""Print the shape, target balance and missingness of each downloaded dataset (paths come from datasets/raw)."""
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'datasets', 'raw')

for name in sorted(os.listdir(RAW)):
    path = os.path.join(RAW, name)
    header = None if name.startswith('orange') else 'infer'
    df = pd.read_csv(path, header=header, low_memory=False)
    print(f'{name}: {df.shape[0]:,} rows x {df.shape[1]} cols, {df.isna().mean().mean():.1%} missing')
    for col in ('Churn', 'Exited'):
        if col in df:
            print('   target', col, df[col].value_counts(dropna=False).head(4).to_dict())
    if header is None:
        print('   last column values', df.iloc[:, -1].value_counts().to_dict())
