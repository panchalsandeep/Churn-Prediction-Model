"""Point-in-time feature building and target definition for B2B accounts.

The rule that matters: a feature computed "as of" a date may only use records stamped on or before that date. Training rows are built at
several past dates (month ends) and labelled by what happened in the *following* horizon; the same builder, run as of today, produces
the rows that get scored. That is what keeps the model honest about the future and makes training rows look like scoring rows.

Known limit: account attributes (plan, size, region, MRR, contract end) are whatever the accounts table said when it was last uploaded.
They are not versioned, so a customer who changed plan last week looks like they were on the new plan all along. Event, ticket and invoice
features do not have this problem. Upload dated history if static attributes matter to you.

Targets (what "churned" means) are explicit and validated before training:
  churn_date   the account has a churned_at date; positive if it falls inside (as_of, as_of + horizon]
  inactivity   positive if the account logs no product events at all inside (as_of, as_of + horizon]
A snapshot is only labelled once its whole horizon has been observed; newer snapshots are scored, never trained on.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WINDOWS = (7, 30, 90)
FAILED = {'failed', 'past_due', 'unpaid', 'declined', 'uncollectible'}
TARGET_TYPES = ('churn_date', 'inactivity')
MAX_EVENT_TYPES = 5


class TargetError(ValueError):
    """The target definition cannot be applied to this data (HTTP 400)."""


def observed_through(tables: dict, spec: dict | None = None) -> pd.Timestamp:
    """The last date we have data for. Labels beyond it are unknown, not zero."""
    if spec and spec.get('observed_through'):
        return pd.Timestamp(spec['observed_through']).normalize()
    stamps = [tables[r]['ts'].max() for r in ('events', 'tickets', 'invoices') if r in tables and len(tables[r])]
    if not stamps and 'accounts' in tables:
        a = tables['accounts']
        stamps = [d for d in (a['started_at'].max(), a['churned_at'].max() if 'churned_at' in a else pd.NaT) if pd.notna(d)]
    if not stamps:
        raise TargetError('There is no dated data yet.')
    return pd.Timestamp(max(stamps)).normalize()


def validate_spec(spec: dict, tables: dict) -> dict:
    kind = (spec or {}).get('type')
    if kind not in TARGET_TYPES:
        raise TargetError(f'Target type must be one of: {", ".join(TARGET_TYPES)}.')
    try:
        horizon = int(spec.get('horizon_days', 90))
    except (TypeError, ValueError):
        raise TargetError('horizon_days must be a whole number of days.') from None
    if not 14 <= horizon <= 365:
        raise TargetError('horizon_days must be between 14 and 365.')
    if 'accounts' not in tables:
        raise TargetError('Upload the accounts table first.')
    if kind == 'churn_date' and ('churned_at' not in tables['accounts'] or tables['accounts']['churned_at'].notna().sum() == 0):
        raise TargetError('This target needs a churned_at date on the accounts table (at least some accounts must have churned).')
    if kind == 'inactivity' and 'events' not in tables:
        raise TargetError('The inactivity target needs the events table.')
    return {**spec, 'type': kind, 'horizon_days': horizon}


def event_vocabulary(tables: dict) -> list[str]:
    ev = tables.get('events')
    if ev is None or 'event' not in ev:
        return []
    return [str(e) for e in ev['event'].value_counts().head(MAX_EVENT_TYPES).index]


def _count_windows(df: pd.DataFrame, as_of: pd.Timestamp, prefix: str, windows=WINDOWS, mask=None) -> pd.DataFrame:
    out = {}
    base = df if mask is None else df[mask]
    for w in windows:
        sel = base[(base['ts'] > as_of - pd.Timedelta(days=w)) & (base['ts'] <= as_of)]
        out[f'{prefix}_{w}d'] = sel.groupby('account_id').size()
    return pd.DataFrame(out)


def build_snapshot(tables: dict, as_of: pd.Timestamp, vocab: list[str] | None = None) -> pd.DataFrame:
    """One row per account that was live at `as_of`, using only data stamped at or before it."""
    as_of = pd.Timestamp(as_of).normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59)
    acc = tables['accounts']
    live = acc[(acc['started_at'] <= as_of) & (acc['churned_at'].isna() | (acc['churned_at'] > as_of) if 'churned_at' in acc else True)]
    if live.empty:
        return pd.DataFrame()
    f = pd.DataFrame({'customer_id': live['account_id'].to_numpy()}).set_index('customer_id')
    f['snapshot_date'] = as_of.normalize().strftime('%Y-%m-%d')
    f['tenure_months'] = ((as_of - live['started_at']).dt.days / 30.4).to_numpy().round(2)
    if 'mrr' in live:
        f['monthly_revenue'] = live['mrr'].to_numpy()
    if 'seats_licensed' in live:
        f['seats_licensed'] = live['seats_licensed'].to_numpy()
    if 'contract_end' in live:
        f['days_to_renewal'] = np.clip((live['contract_end'] - as_of).dt.days.to_numpy(), -365, 730)
    for c in live.columns:
        if c not in ('account_id', 'started_at', 'churned_at', 'contract_end', 'mrr', 'seats_licensed'):
            f[c] = live[c].to_numpy()

    ev = tables.get('events')
    if ev is not None:
        ev = ev[ev['ts'] <= as_of]
        w = _count_windows(ev, as_of, 'events')
        f = f.join(w)
        for c in w.columns:
            f[c] = f[c].fillna(0)
        f['events_trend'] = (f['events_30d'] / np.maximum(1.0, (f['events_90d'] - f['events_30d']) / 2.0)).round(3)
        last = ev.groupby('account_id')['ts'].max()
        f['days_since_last_activity'] = ((as_of - last.reindex(f.index)).dt.days).astype(float)
        f['days_since_last_activity'] = f['days_since_last_activity'].fillna((as_of - live.set_index('account_id')['started_at'].reindex(f.index)).dt.days.clip(lower=0))
        recent = ev[ev['ts'] > as_of - pd.Timedelta(days=30)]
        f['active_days_30d'] = recent.assign(d=recent['ts'].dt.normalize()).groupby('account_id')['d'].nunique().reindex(f.index).fillna(0)
        if 'user_id' in ev:
            for wd in (30, 90):
                s = ev[ev['ts'] > as_of - pd.Timedelta(days=wd)]
                f[f'active_users_{wd}d'] = s.groupby('account_id')['user_id'].nunique().reindex(f.index).fillna(0)
            if 'seats_licensed' in f:
                f['seat_utilization_30d'] = (f['active_users_30d'] / np.maximum(f['seats_licensed'], 1)).clip(upper=1.5).round(3)
        if 'event' in ev and vocab:
            r30 = ev[ev['ts'] > as_of - pd.Timedelta(days=30)]
            for name in vocab:
                col = 'ev_' + ''.join(ch if ch.isalnum() else '_' for ch in name.lower())[:30] + '_30d'
                f[col] = r30[r30['event'].astype(str) == name].groupby('account_id').size().reindex(f.index).fillna(0)
        if 'value' in ev:
            f['event_value_30d'] = recent.groupby('account_id')['value'].sum().reindex(f.index).fillna(0)

    tk = tables.get('tickets')
    if tk is not None:
        tk = tk[tk['ts'] <= as_of]
        for wd in (30, 90):
            s = tk[tk['ts'] > as_of - pd.Timedelta(days=wd)]
            f[f'tickets_{wd}d'] = s.groupby('account_id').size().reindex(f.index).fillna(0)
        if 'severity' in tk:
            s = tk[(tk['ts'] > as_of - pd.Timedelta(days=90)) & (tk['severity'].astype(str).str.lower().isin(['high', 'critical', 'urgent', 'p1']))]
            f['high_severity_tickets_90d'] = s.groupby('account_id').size().reindex(f.index).fillna(0)
        if 'status' in tk:
            s = tk[tk['status'].astype(str).str.lower().isin(['open', 'pending', 'new'])]
            f['open_tickets'] = s.groupby('account_id').size().reindex(f.index).fillna(0)
        if 'csat' in tk:
            s = tk[tk['ts'] > as_of - pd.Timedelta(days=90)]
            f['avg_csat_90d'] = s.groupby('account_id')['csat'].mean().reindex(f.index).round(2)

    inv = tables.get('invoices')
    if inv is not None:
        inv = inv[inv['ts'] <= as_of]
        s90 = inv[inv['ts'] > as_of - pd.Timedelta(days=90)]
        f['invoices_90d'] = s90.groupby('account_id').size().reindex(f.index).fillna(0)
        if 'status' in inv:
            bad = s90[s90['status'].astype(str).str.lower().isin(FAILED)]
            f['failed_payments_90d'] = bad.groupby('account_id').size().reindex(f.index).fillna(0)
        recent_amt = inv[inv['ts'] > as_of - pd.Timedelta(days=45)].groupby('account_id')['amount'].mean()
        prior_amt = inv[(inv['ts'] <= as_of - pd.Timedelta(days=45)) & (inv['ts'] > as_of - pd.Timedelta(days=135))].groupby('account_id')['amount'].mean()
        f['billing_change'] = (recent_amt.reindex(f.index) / prior_amt.reindex(f.index).replace(0, np.nan)).round(3)
        if 'monthly_revenue' not in f:
            f['monthly_revenue'] = recent_amt.reindex(f.index)
    return f.reset_index()


def labels(tables: dict, snap: pd.DataFrame, as_of: pd.Timestamp, spec: dict, through: pd.Timestamp) -> pd.Series:
    """1 = churned inside the horizon, 0 = survived it, NaN = the horizon is not fully observed yet."""
    horizon = int(spec['horizon_days'])
    as_of = pd.Timestamp(as_of).normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59)
    end = as_of + pd.Timedelta(days=horizon)
    if end.normalize() > through:
        return pd.Series(np.nan, index=snap.index)
    ids = snap['customer_id']
    if spec['type'] == 'churn_date':
        acc = tables['accounts'].set_index('account_id')['churned_at']
        c = ids.map(acc)
        return ((c > as_of) & (c <= end)).astype(float)
    ev = tables['events']
    seen = set(ev.loc[(ev['ts'] > as_of) & (ev['ts'] <= end), 'account_id'])
    return (~ids.isin(seen)).astype(float)


def snapshot_dates(tables: dict, spec: dict, through: pd.Timestamp, max_snapshots: int = 12) -> list[pd.Timestamp]:
    """Month ends from the point accounts have enough history up to the last date whose horizon is fully observed."""
    first = tables['accounts']['started_at'].min() + pd.Timedelta(days=60)
    last = through - pd.Timedelta(days=int(spec['horizon_days']))
    if last < first:
        return []
    dates = [d for d in pd.date_range(first, last, freq='ME')]
    return dates[-max_snapshots:]


def preview_target(tables: dict, spec: dict, max_snapshots: int = 12) -> dict:
    """What the target definition would produce: per-snapshot label rates, plus the warnings a data scientist would raise."""
    spec = validate_spec(spec, tables)
    through = observed_through(tables, spec)
    vocab = event_vocabulary(tables)
    rows, pos_total, n_total = [], 0, 0
    for d in snapshot_dates(tables, spec, through, max_snapshots):
        snap = build_snapshot(tables, d, vocab)
        if snap.empty:
            continue
        y = labels(tables, snap, d, spec, through)
        pos, n = int(y.sum()), int(y.notna().sum())
        pos_total, n_total = pos_total + pos, n_total + n
        rows.append({'as_of': d.strftime('%Y-%m-%d'), 'accounts': n, 'churned': pos, 'rate': round(pos / n, 4) if n else None})
    warnings: list[str] = []
    if not rows:
        warnings.append(f'No snapshot has a fully observed {spec["horizon_days"]}-day horizon yet. Data runs through {through.date()}. '
                        'Try a shorter horizon or upload more history.')
    else:
        rate = pos_total / max(n_total, 1)
        if pos_total < 50:
            warnings.append(f'Only {pos_total} churn events across all snapshots. That is too few to train a trustworthy model (aim for 100+).')
        if rate > 0.4:
            warnings.append(f'{rate:.0%} of account-snapshots are labelled churned. Check the definition: a horizon this long or an inactivity rule this strict may be mislabelling.')
        if rate == 0:
            warnings.append('No account is labelled churned under this definition.')
        if len(rows) < 3:
            warnings.append('Fewer than 3 labelled snapshots: the time-based validation will be coarse.')
    return {'spec': spec, 'observed_through': through.strftime('%Y-%m-%d'), 'snapshots': rows, 'positives': pos_total, 'labelled_rows': n_total,
            'base_rate': round(pos_total / n_total, 4) if n_total else None, 'warnings': warnings, 'ok': bool(rows) and pos_total > 0}


def build_training_frame(tables: dict, spec: dict, max_snapshots: int = 12) -> tuple[pd.DataFrame, dict]:
    """Stack labelled snapshots into one training table (customer_id, snapshot_date, features..., churned)."""
    spec = validate_spec(spec, tables)
    through = observed_through(tables, spec)
    vocab = event_vocabulary(tables)
    frames = []
    for d in snapshot_dates(tables, spec, through, max_snapshots):
        snap = build_snapshot(tables, d, vocab)
        if snap.empty:
            continue
        snap['churned'] = labels(tables, snap, d, spec, through).to_numpy()
        frames.append(snap[snap['churned'].notna()])
    if not frames:
        raise TargetError('No labelled training rows could be built. Run the target preview to see why.')
    df = pd.concat(frames, ignore_index=True)
    df['churned'] = df['churned'].astype(int)
    if df['churned'].nunique() < 2:
        raise TargetError('Every row has the same label under this definition, so there is nothing to learn from.')
    return df, {'vocab': vocab, 'observed_through': through.strftime('%Y-%m-%d'), 'snapshots': int(df['snapshot_date'].nunique()), 'rows': int(len(df))}


def build_scoring_frame(tables: dict, spec: dict, vocab: list[str] | None = None) -> tuple[pd.DataFrame, dict]:
    """Rows for scoring today: the same builder, as of the end of the observed data (no label)."""
    spec = validate_spec(spec, tables)
    through = observed_through(tables, spec)
    snap = build_snapshot(tables, through, vocab if vocab is not None else event_vocabulary(tables))
    if snap.empty:
        raise TargetError('No live accounts to score as of the latest data.')
    return snap, {'as_of': through.strftime('%Y-%m-%d'), 'accounts': int(len(snap))}
