"""Automatic schema inference so any churn CSV (Telco, bank, SaaS, ...) can be used.

The only hard requirement is a binary churn column. Everything else is detected:
id, dates, numeric vs categorical features, high-cardinality junk, leakage suspects, and
the semantic roles the UI cares about (revenue, tenure, last activity, signup cohort).
"""
from __future__ import annotations

import re
import warnings
from typing import Any

import numpy as np
import pandas as pd
from pandas.api import types as pdt
from sklearn.metrics import roc_auc_score

TARGET_NAMES = ['churn', 'churned', 'is_churn', 'exited', 'attrition', 'left', 'cancelled', 'canceled',
                'churn_label', 'churn_next_30d', 'customer_churn']
POSITIVE = {'1', 'yes', 'y', 'true', 't', 'churn', 'churned', 'left', 'exited', 'cancelled', 'canceled', 'attrited', 'attrited customer'}
NEGATIVE = {'0', 'no', 'n', 'false', 'f', 'retained', 'stayed', 'active', 'existing customer', 'not churned'}

REVENUE_HINTS = ['monthly_charges', 'monthlycharges', 'monthly_revenue', 'mrr', 'arpu', 'monthly_fee', 'monthly_spend', 'revenue']
TENURE_HINTS = ['tenure_months', 'tenure', 'months_active', 'months_as_customer', 'customer_age_months']
ACTIVITY_HINTS = ['last_login_days', 'days_since_last_login', 'days_since_last_activity', 'recency', 'last_active_days']
TIME_HINTS = ['snapshot', 'as_of', 'observation', 'scored_at', 'report_date']
SIGNUP_HINTS = ['signup', 'sign_up', 'start_date', 'join', 'created', 'registration', 'activation']


def _norm(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(name).strip().lower()).strip('_')


def _find_hint(columns: list[str], hints: list[str]) -> str | None:
    normed = {c: _norm(c) for c in columns}
    for hint in hints:                      # exact match first, in priority order
        for col, n in normed.items():
            if n == hint:
                return col
    for hint in hints:
        for col, n in normed.items():
            if hint in n:
                return col
    return None


def normalize_target(series: pd.Series) -> pd.Series:
    """Map yes/no/true/false/1/0/Churned/... to 1/0; unknown values become NaN."""
    if pdt.is_bool_dtype(series):
        return series.astype(float)
    if pdt.is_numeric_dtype(series):
        out = pd.Series(np.where(series.isna(), np.nan, (series.astype(float) > 0).astype(float)), index=series.index)
        return out

    def convert(value: Any) -> float:
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return np.nan
        text = str(value).strip().lower()
        if text in POSITIVE:
            return 1.0
        if text in NEGATIVE:
            return 0.0
        return np.nan

    return series.map(convert).astype(float)


def _detect_target(df: pd.DataFrame, explicit: str | None) -> str:
    if explicit:
        if explicit not in df.columns:
            raise ValueError(f'Target column "{explicit}" not found.')
        return explicit
    cols = list(df.columns)
    normed = {c: _norm(c) for c in cols}
    for name in TARGET_NAMES:
        for col, n in normed.items():
            if n == name:
                return col
    for col, n in normed.items():
        if 'churn' in n or 'attrition' in n:
            if normalize_target(df[col]).dropna().nunique() == 2:
                return col
    raise ValueError(
        'Could not find a churn column. Name it "churn" (values 0/1 or Yes/No), or pass target explicitly. '
        f'Columns seen: {", ".join(map(str, cols[:20]))}'
    )


def _as_datetime(series: pd.Series) -> pd.Series | None:
    if pdt.is_datetime64_any_dtype(series):
        return series
    if pdt.is_numeric_dtype(series):
        return None
    sample = series.dropna().astype(str).head(500)
    if sample.empty or sample.str.fullmatch(r'-?\d+(\.\d+)?').mean() > 0.5:
        return None                      # plain numbers are not dates, however leniently they parse
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        parsed = pd.to_datetime(sample, errors='coerce')
        if parsed.notna().mean() < 0.95:
            return None
        return pd.to_datetime(series, errors='coerce')


def _numeric_like(series: pd.Series) -> pd.Series | None:
    """Numbers stored as text (Telco's TotalCharges has blank strings)."""
    if pdt.is_numeric_dtype(series) and not pdt.is_bool_dtype(series):
        return series.astype(float)
    if pdt.is_bool_dtype(series):
        return series.astype(float)
    cleaned = pd.to_numeric(series.astype(str).str.strip().replace({'': np.nan, 'nan': np.nan, 'None': np.nan}), errors='coerce')
    non_null = series.notna() & (series.astype(str).str.strip() != '')
    if non_null.sum() and cleaned[non_null].notna().mean() >= 0.95:
        return cleaned
    return None


def infer_schema(df: pd.DataFrame, target: str | None = None) -> dict:
    """Return a JSON-serialisable description of how each column will be used."""
    if len(df) < 50:
        raise ValueError('Need at least 50 rows to train a meaningful model.')

    target_col = _detect_target(df, target)
    y = normalize_target(df[target_col])
    valid = y.notna()
    dropped_rows = int((~valid).sum())
    y = y[valid]
    if y.nunique() < 2:
        raise ValueError('Need both churn classes (churned and retained) in the data.')
    if int(y.value_counts().min()) < 10:
        raise ValueError('Need at least 10 examples of each churn class to train and evaluate reliably.')

    work = df.loc[valid].copy()
    n = len(work)
    feature_candidates = [c for c in work.columns if c != target_col]

    roles: dict[str, dict] = {}
    warnings: list[str] = []
    numeric: list[str] = []
    categorical: list[str] = []
    dates: list[str] = []
    id_col: str | None = None

    def put(col, role, reason=''):
        roles[col] = {'name': col, 'role': role, 'reason': reason}

    for col in feature_candidates:
        s = work[col]
        n_unique = int(s.nunique(dropna=True))
        norm = _norm(col)
        missing = float(s.isna().mean())

        dt = _as_datetime(s)
        if dt is not None and not (id_col is None and n_unique >= 0.9 * n and re.search(r'(^|_)id$', norm)):
            # Dates first: a constant "as of" snapshot date is a legitimate time reference, not junk.
            dates.append(col)
            put(col, 'date', 'parsed as date')
            work[col] = dt
        elif n_unique <= 1:
            put(col, 'drop', 'constant column')
        elif id_col is None and (re.search(r'(^|_)id$|^id$|customer_?id|account_?id|user_?id|^uuid$', norm)
                                 and n_unique >= 0.9 * n):
            id_col = col
            put(col, 'id', 'unique identifier')
        else:
            num = _numeric_like(s)
            if num is not None:
                work[col] = num
                if num.nunique(dropna=True) <= 1:
                    put(col, 'drop', 'constant column')
                elif (n_unique >= 0.98 * n and n > 200 and bool((num.dropna() % 1 == 0).all())
                      and not re.search(r'(charge|amount|revenue|spend|balance|salary|price|fee)', norm)):
                    put(col, 'drop', 'looks like an identifier')
                else:
                    numeric.append(col)
                    put(col, 'numeric')
            elif n_unique > max(50, 0.05 * n):
                put(col, 'drop', f'high-cardinality text ({n_unique} values)')
            else:
                categorical.append(col)
                put(col, 'categorical')
        roles[col]['missing_pct'] = round(missing * 100, 1)
        roles[col]['n_unique'] = n_unique
        roles[col]['dtype'] = str(df[col].dtype)

    # Fallback: if nothing looked like an ID, use a customer-ish column for display
    if id_col is None:
        for col in feature_candidates:
            if roles.get(col, {}).get('role') in ('drop', 'categorical') and re.search(r'customer|account|user|client', _norm(col)) \
                    and work[col].nunique() >= 0.9 * n:
                id_col = col
                put(col, 'id', 'unique identifier')
                if col in categorical:
                    categorical.remove(col)
                break

    time_col = _find_hint(dates, TIME_HINTS)
    signup_col = _find_hint(dates, SIGNUP_HINTS)
    if time_col is None and len(dates) == 1 and signup_col is None:
        time_col = dates[0]
    if time_col == signup_col:
        time_col = None

    revenue_col = _find_hint(numeric, REVENUE_HINTS)
    tenure_col = _find_hint(numeric, TENURE_HINTS)
    activity_col = _find_hint(numeric, ACTIVITY_HINTS)

    # Leakage screen: a single feature that nearly perfectly separates the classes is suspicious.
    yv = y.to_numpy()
    for col in numeric:
        vals = work.loc[y.index, col]
        if vals.notna().sum() > 30 and vals.nunique() > 1:
            filled = vals.fillna(vals.median())
            try:
                auc = roc_auc_score(yv, filled)
            except ValueError:
                continue
            strength = max(auc, 1 - auc)
            roles[col]['solo_auc'] = round(float(strength), 3)
            if strength > 0.97:
                warnings.append(f'"{col}" alone predicts churn with AUC {strength:.2f}; it may leak the outcome (e.g. a cancellation flag).')
    for col in categorical:
        grp = pd.DataFrame({'c': work.loc[y.index, col].fillna('Missing'), 'y': yv}).groupby('c')['y'].agg(['mean', 'size'])
        purity = float(((grp['mean'].clip(lower=1 - grp['mean']) * grp['size']).sum()) / grp['size'].sum())
        if purity > 0.995 and len(grp) > 1:
            warnings.append(f'"{col}" nearly determines churn by itself; check it is not derived from the outcome.')

    heavy_missing = [c for c in numeric + categorical if roles[c]['missing_pct'] > 40]
    if heavy_missing:
        warnings.append(f'Many missing values in: {", ".join(heavy_missing[:5])}. They are imputed and flagged with a missing-indicator.')
    rate = float(y.mean())
    if rate < 0.03 or rate > 0.7:
        warnings.append(f'Churn rate is {rate:.1%}; extreme imbalance makes accuracy misleading, so PR-AUC and lift are reported.')
    if dropped_rows:
        warnings.append(f'{dropped_rows} rows had an unreadable churn value and were ignored.')
    if not numeric and not categorical:
        raise ValueError('No usable feature columns were found after removing IDs and constants.')

    return {
        'target': target_col,
        'id': id_col,
        'time_col': time_col,
        'signup_col': signup_col,
        'dates': dates,
        'numeric': numeric,
        'categorical': categorical,
        'revenue_col': revenue_col,
        'tenure_col': tenure_col,
        'activity_col': activity_col,
        'columns': [roles[c] for c in feature_candidates if c in roles],
        'dropped': [{'name': c, 'reason': roles[c]['reason']} for c in feature_candidates if roles[c]['role'] == 'drop'],
        'warnings': warnings,
        'rows': int(n),
        'rows_ignored': dropped_rows,
        'positive_rate': round(rate, 4),
    }


def prepare_features(df: pd.DataFrame, schema: dict, time_ref: pd.Timestamp | None = None) -> pd.DataFrame:
    """Build the model input frame from raw data using a previously inferred schema.

    Date columns become "<col>_age_days" relative to the snapshot date (or the newest date),
    so the model learns tenure-like signals without memorising calendar dates.
    """
    out = pd.DataFrame(index=df.index)
    for col in schema['numeric']:
        out[col] = pd.to_numeric(df[col], errors='coerce') if col in df.columns else np.nan
    for col in schema['categorical']:
        if col in df.columns:
            out[col] = df[col].astype('string').fillna('Missing').astype(object)
        else:
            out[col] = 'Missing'

    ref = None
    time_col = schema.get('time_col')
    if time_col and time_col in df.columns:
        ref = pd.to_datetime(df[time_col], errors='coerce')
    for col in schema['dates']:
        if col == time_col:
            continue
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors='coerce')
            anchor = ref if ref is not None else (time_ref or parsed.max())
            out[f'{col}_age_days'] = (anchor - parsed).dt.days.astype(float)
        else:
            out[f'{col}_age_days'] = np.nan
    return out


def model_columns(schema: dict) -> tuple[list[str], list[str]]:
    """(numeric, categorical) column names as they appear in prepare_features output."""
    numeric = list(schema['numeric'])
    numeric += [f'{c}_age_days' for c in schema['dates'] if c != schema.get('time_col')]
    return numeric, list(schema['categorical'])
