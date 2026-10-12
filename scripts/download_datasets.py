"""Fetch the public churn datasets used for benchmarking into datasets/raw, then prepare datasets/ready.

    python scripts/download_datasets.py

Sources (public GitHub mirrors of the original releases; check each licence before commercial use):
  telco   IBM Telco Customer Churn      7,043 rows   github.com/IBM/telco-customer-churn-on-icp4d
  bank    Bank Customer Churn (Kaggle) 10,000 rows   github.com/sagnikghoshcr7/Bank-Customer-Churn-Prediction
  orange  Orange Telecom (BigML)        3,333 rows   github.com/caroljmcdonald/mapr-sparkml-churn
Files are plain CSV, are only ever read as data, and are checked against the SHA-256 recorded when first downloaded.
"""
import hashlib
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, 'datasets', 'raw')
FILES = {
    'telco.csv': ('https://raw.githubusercontent.com/IBM/telco-customer-churn-on-icp4d/master/data/Telco-Customer-Churn.csv',
                  '16320c9c1ec72448db59aa0a26a0b95401046bef5d02fd3aeb906448e3055e91'),
    'bank.csv': ('https://raw.githubusercontent.com/sagnikghoshcr7/Bank-Customer-Churn-Prediction/HEAD/Churn_Modelling.csv',
                 'c9d2fe4ef6ff87b77a620c42a87a26a3afde2c5c7997be07ff182a2f8e4014a6'),
    'orange_80.csv': ('https://raw.githubusercontent.com/caroljmcdonald/mapr-sparkml-churn/HEAD/data/churn-bigml-80.csv',
                      'aa802987f9120781ac782aa64dd223b155ebab26895cb268336951f28fe3cbf0'),
    'orange_20.csv': ('https://raw.githubusercontent.com/caroljmcdonald/mapr-sparkml-churn/HEAD/data/churn-bigml-20.csv',
                      '0eb05b4a83fe0279278b681756944a6b436db71929b2899b8c2a4b8e5ed7f854'),
}

os.makedirs(RAW, exist_ok=True)
for name, (url, sha) in FILES.items():
    path = os.path.join(RAW, name)
    if not os.path.exists(path):
        print('downloading', name)
        urllib.request.urlretrieve(url, path)
    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    status = 'ok' if digest == sha else 'CHANGED upstream (inspect before trusting)'
    print(f'{name}: {os.path.getsize(path):,} bytes, sha256 {status}')
subprocess.check_call([sys.executable, os.path.join(ROOT, 'scripts', 'prepare_datasets.py')])
