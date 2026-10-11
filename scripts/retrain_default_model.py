"""Train and write api/churn_model.pkl from sample_data.csv using pinned sklearn."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))

import pandas as pd  # noqa: E402
import joblib  # noqa: E402
import sklearn  # noqa: E402

# Import after path setup
from index import build_pipeline, preprocess_dataframe, NUMERICAL_FEATURES, CATEGORICAL_FEATURES  # noqa: E402


def main():
    csv_path = os.path.join(ROOT, 'sample_data.csv')
    out_path = os.path.join(ROOT, 'api', 'churn_model.pkl')
    df_raw = pd.read_csv(csv_path)
    df = preprocess_dataframe(df_raw)
    X = df[NUMERICAL_FEATURES + CATEGORICAL_FEATURES]
    y = df['churn'].astype(int)
    pipeline = build_pipeline('random_forest')
    pipeline.fit(X, y)
    joblib.dump(pipeline, out_path)
    print(f'Wrote {out_path}')
    print(f'scikit-learn={sklearn.__version__}')


if __name__ == '__main__':
    main()
