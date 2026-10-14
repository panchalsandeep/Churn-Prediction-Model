"""Turn the raw downloads in datasets/raw into train-ready CSVs in datasets/ready (nothing is executed from the data)."""
import os

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW, READY = os.path.join(ROOT, 'datasets', 'raw'), os.path.join(ROOT, 'datasets', 'ready')
os.makedirs(READY, exist_ok=True)

ORANGE_COLS = ['State', 'Account length', 'Area code', 'International plan', 'Voice mail plan', 'Number vmail messages',
               'Total day minutes', 'Total day calls', 'Total day charge', 'Total eve minutes', 'Total eve calls',
               'Total eve charge', 'Total night minutes', 'Total night calls', 'Total night charge', 'Total intl minutes',
               'Total intl calls', 'Total intl charge', 'Customer service calls', 'Churn']

orange = pd.concat([pd.read_csv(os.path.join(RAW, f), header=None, names=ORANGE_COLS) for f in ('orange_80.csv', 'orange_20.csv')],
                   ignore_index=True)
orange['Area code'] = orange['Area code'].astype(str)          # a label, not a quantity
orange.to_csv(os.path.join(READY, 'orange.csv'), index=False)

for name in ('telco', 'bank'):
    pd.read_csv(os.path.join(RAW, f'{name}.csv'), low_memory=False).to_csv(os.path.join(READY, f'{name}.csv'), index=False)

for f in sorted(os.listdir(READY)):
    print(f, pd.read_csv(os.path.join(READY, f)).shape)
