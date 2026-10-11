"""The committed reference models still load through the safe loader and predict sensibly."""
from __future__ import annotations

import hashlib
import json
import os

import pandas as pd
import pytest

from churn import artifacts

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models', 'reference')
NAMES = ['telco', 'bank', 'orange']


@pytest.mark.parametrize('name', NAMES)
def test_reference_model_matches_its_card_and_predicts(name):
    blob = open(os.path.join(DIR, f'{name}.skops'), 'rb').read()
    card = json.load(open(os.path.join(DIR, f'{name}.json')))
    assert hashlib.sha256(blob).hexdigest() == card['sha256']                      # the file is the one the card describes
    bundle = artifacts.loads_bundle(blob)
    row = pd.DataFrame([{c: bundle['baseline'][c] for c in bundle['cols']}])
    for c in bundle['cat_cols']:
        row[c] = row[c].astype(str)
    p = float(bundle['model'].predict_proba(row[bundle['cols']])[0, 1])
    assert 0.0 < p < 1.0 and card['metrics_on_untouched_holdout']['roc_auc'] > 80
