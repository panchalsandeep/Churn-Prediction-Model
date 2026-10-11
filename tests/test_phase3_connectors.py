"""Phase 3: HTTP pull connectors and the full hands-off loop (pull -> train -> schedule -> score -> alert)."""
from __future__ import annotations

import http.server
import json
import os
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest

from churn import connectors, ingest, notify, pipeline, safehttp, service
from churn.b2b_sim import generate_b2b
from test_phase0_access import auth  # noqa: F401
from test_phase2_quotas import workspace  # noqa: F401
from test_phase3_notify import Receiver  # noqa: F401

TOKEN = 'vendor-token-123'
PAGE = 4000


class Vendor:
    """A fake vendor API serving the simulator's tables: JSON with nesting and pagination, plus a CSV endpoint. Needs a bearer token."""

    def __init__(self, data):
        self.data, self.requests, self.fail = data, [], False
        outer = self
        recs = lambda df: json.loads(df.assign(**{c: df[c].astype(str) for c in df.columns if str(df[c].dtype).startswith('datetime')}).to_json(orient='records'))   # noqa: E731

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                outer.requests.append(self.path)
                if self.headers.get('Authorization') != f'Bearer {TOKEN}':
                    return self.reply(401, b'{}')
                if outer.fail:
                    return self.reply(500, b'oops')
                if u.path == '/v1/accounts':
                    return self.reply(200, json.dumps({'data': {'items': recs(outer.data['accounts'])}}).encode())
                if u.path == '/v1/events':
                    df = outer.data['events']
                    since = q.get('updated_after', [None])[0]
                    if since:
                        df = df[df['ts'] > pd.Timestamp(since)]
                    page = int(q.get('page', ['0'])[0])
                    chunk = df.iloc[page * PAGE:(page + 1) * PAGE]
                    nxt = f'http://127.0.0.1:{outer.port}/v1/events?page={page + 1}' + (f'&updated_after={since}' if since else '') if (page + 1) * PAGE < len(df) else None
                    return self.reply(200, json.dumps({'items': recs(chunk), 'meta': {'next': nxt}}).encode())
                if u.path == '/v1/tickets.csv':
                    return self.reply(200, outer.data['tickets'].to_csv(index=False).encode(), 'text/csv')
                if u.path == '/v1/invoices':
                    return self.reply(200, json.dumps(recs(outer.data['invoices'])).encode())
                if u.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', 'http://169.254.169.254/latest/meta-data')
                    self.end_headers()
                    return None
                return self.reply(404, b'{}')

            def reply(self, status, body, ctype='application/json'):
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.port = self.server.server_port
        self.base = f'http://127.0.0.1:{self.port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture(scope='module')
def data():
    d = generate_b2b(220, 13, 4)
    d['accounts'] = d['accounts'].copy()
    return d


@pytest.fixture()
def vendor(data, monkeypatch):
    monkeypatch.setenv('SAFEHTTP_ALLOW_LOCAL', '1')
    v = Vendor(data)
    yield v
    v.close()


def src(vendor, role, path, **kw):
    return {'role': role, 'url': vendor.base + path, 'headers': {'Authorization': f'Bearer {TOKEN}'}, **kw}


def test_json_pagination_nesting_mapping_and_credentials(env, vendor, data):
    ws = 'conn-1'
    acc = connectors.add_source(ws, src(vendor, 'accounts', '/v1/accounts', records_path='data.items', name='crm'), 'me')
    ev = connectors.add_source(ws, src(vendor, 'events', '/v1/events', records_path='items', next_path='meta.next', name='product'), 'me')
    assert 'Bearer' not in json.dumps(connectors.list_sources(ws)) and acc['host'] == '127.0.0.1'      # the token is never returned
    results = [connectors.sync(ws, acc['id']), connectors.sync(ws, ev['id'])]
    assert all(r['ok'] for r in results), results
    assert len(ingest.load_table(ws, 'accounts')) == len(data['accounts'])
    assert len(ingest.load_table(ws, 'events')) == len(data['events'].drop_duplicates())          # every page was followed
    assert sum('page=' in r for r in vendor.requests) >= len(data['events']) // PAGE

    # a wrong token is reported on the source, not raised
    bad = connectors.add_source(ws, {**src(vendor, 'invoices', '/v1/invoices'), 'headers': {'Authorization': 'Bearer nope'}}, 'me')
    out = connectors.sync(ws, bad['id'])
    assert out['ok'] is False and 'credentials' in out['detail']
    assert [s for s in connectors.list_sources(ws) if s['id'] == bad['id']][0]['last']['ok'] is False


def test_csv_source_and_column_mapping(env, vendor, data):
    ws = 'conn-2'
    renamed = data['tickets'].rename(columns={'account_id': 'Customer', 'ts': 'Opened'})
    vendor.data = {**data, 'tickets': renamed}
    s = connectors.add_source(ws, src(vendor, 'tickets', '/v1/tickets.csv', format='csv', mapping={'account_id': 'Customer', 'ts': 'Opened'}), 'me')
    out = connectors.sync(ws, s['id'])
    assert out['ok'], out
    assert len(ingest.load_table(ws, 'tickets')) == len(data['tickets'])


def test_incremental_sync_asks_only_for_what_is_new(env, vendor, data):
    ws = 'conn-3'
    half = len(data['events']) // 2
    ordered = data['events'].sort_values('ts').reset_index(drop=True)
    vendor.data = {**data, 'events': ordered.iloc[:half]}
    s = connectors.add_source(ws, src(vendor, 'events', '/v1/events', records_path='items', next_path='meta.next', since_param='updated_after'), 'me')
    assert connectors.sync(ws, s['id'])['rows'] == half
    vendor.requests.clear()
    vendor.data = {**data, 'events': ordered}
    out = connectors.sync(ws, s['id'])
    assert any('updated_after=' in r for r in vendor.requests)                                     # the second call carried a cursor
    assert out['rows'] == len(ordered) - half
    assert len(ingest.load_table(ws, 'events')) == len(ordered)


def test_unsafe_or_broken_sources_are_rejected_or_reported(env, vendor, monkeypatch):
    ws = 'conn-4'
    with pytest.raises(connectors.SourceError, match='private or internal'):
        monkeypatch.delenv('SAFEHTTP_ALLOW_LOCAL')
        connectors.add_source(ws, {'role': 'events', 'url': 'https://localhost/x'}, 'me')
    monkeypatch.setenv('SAFEHTTP_ALLOW_LOCAL', '1')
    for bad in ({'role': 'payroll', 'url': vendor.base}, {'role': 'events', 'url': vendor.base, 'format': 'xml'}, {'role': 'events', 'url': vendor.base, 'headers': {'Host': 'x'}},
                {'role': 'events', 'url': vendor.base, 'headers': {'a': 1}}, {'role': 'events', 'url': 'ftp://x/y'}):
        with pytest.raises(connectors.SourceError):
            connectors.add_source(ws, bad, 'me')
    redirect = connectors.add_source(ws, src(vendor, 'events', '/redirect'), 'me')
    out = connectors.sync(ws, redirect['id'])
    assert out['ok'] is False and '302' in out['detail']                                           # redirect not followed (it points at the metadata service)
    vendor.fail = True
    s = connectors.add_source(ws, src(vendor, 'invoices', '/v1/invoices'), 'me')
    assert 'HTTP 500' in connectors.sync(ws, s['id'])['detail']
    vendor.fail = False
    wrong_path = connectors.add_source(ws, src(vendor, 'accounts', '/v1/accounts', records_path='data.nothing'), 'me')
    assert 'No list of records' in connectors.sync(ws, wrong_path['id'])['detail']
    for _ in range(connectors.MAX_SOURCES):
        try:
            connectors.add_source(ws, src(vendor, 'events', '/v1/events', records_path='items'), 'me')
        except connectors.SourceError as exc:
            assert 'At most' in str(exc)
            break
    else:
        pytest.fail('source limit not enforced')


def test_response_size_is_capped(env, vendor, monkeypatch):
    monkeypatch.setattr(connectors, 'MAX_BYTES', 2000)
    s = connectors.add_source('conn-5', src(vendor, 'accounts', '/v1/accounts', records_path='data.items'), 'me')
    out = connectors.sync('conn-5', s['id'])
    assert out['ok'] is False and 'larger than' in out['detail']


def test_hands_off_loop_pull_train_schedule_score_and_alert(client, env, auth, vendor, data):
    """The Phase 3 exit criterion: connect sources once; after that scoring and alerting need no human."""
    import sqlalchemy as sa
    from churn import db
    with db.system_context(), db.get_engine().begin() as c:
        c.execute(sa.delete(db.schedules_t))
    ws, boss, root = workspace(client, auth, 'enterprise')
    hook = Receiver()
    last = data['events']['ts'].max()
    cutoff = last - pd.Timedelta(days=45)
    vendor.data = {**data, **{k: data[k][data[k]['ts'] <= cutoff] for k in ('events', 'tickets', 'invoices')}}     # the vendor as it was 45 days ago
    try:
        for role, path, kw in (('accounts', '/v1/accounts', {'records_path': 'data.items'}),
                               ('events', '/v1/events', {'records_path': 'items', 'next_path': 'meta.next', 'since_param': 'updated_after'}),
                               ('tickets', '/v1/tickets.csv', {'format': 'csv'}), ('invoices', '/v1/invoices', {})):
            r = client.post('/api/sources', json=src(vendor, role, path, **kw), headers=boss)
            assert r.status_code == 201, r.get_json()
            assert client.post(f"/api/sources/{r.get_json()['id']}/sync", headers=boss).get_json()['ok'] is True
        assert client.post('/api/pipeline/target', json={'type': 'churn_date', 'horizon_days': 90, 'save': True}, headers=boss).status_code == 200
        job = client.post('/api/pipeline/run?wait=1', json={'algorithm': 'logistic_regression'}, headers=boss).get_json()['job']
        assert job['status'] == 'done' and job['result']['status'] == 'champion'

        os.environ['SAFEHTTP_ALLOW_LOCAL'] = '1'
        ch = client.post('/api/channels', json={'kind': 'slack', 'url': hook.url, 'min_severity': 'Medium'}, headers=boss)
        assert ch.status_code == 201
        assert client.put('/api/pipeline/schedule', json={'enabled': True, 'every_hours': 24}, headers=boss).status_code == 200

        # the vendor's data moves on: tomorrow a batch of accounts is in trouble. Nobody uploads anything.
        ev = data['events']
        live = set(data['accounts'].loc[data['accounts']['churned_at'].isna(), 'account_id'])
        busiest = ev[(ev['ts'] > cutoff - pd.Timedelta(days=30)) & (ev['ts'] <= cutoff) & ev['account_id'].isin(live)]['account_id'].value_counts().head(40).index
        newer = ev[ev['ts'] > cutoff]
        silent = newer[newer['account_id'].isin(busiest)]
        # 45 days pass. The vendor now has newer data, but 40 of the busiest accounts logged nothing. Nobody uploads anything.
        vendor.data = {**data, 'events': ev.drop(silent.index)}
        hook.requests.clear()
        assert pipeline.tick(time.time() + 25 * 3600) == 1
        s = pipeline.get_schedule(ws)
        done = client.get(f"/api/jobs/{s['last_job_id']}", headers=boss).get_json()
        assert done['status'] == 'done' and done['result']['rows'] > 100 and len(done['result']['sources']) == 4
        assert [x for x in done['result']['sources'] if not x['ok']] == []
        assert any(json.loads(r['body'])['text'] for r in hook.requests), 'the owner was not alerted'
        assert 'new churn alert' in json.loads(hook.requests[-1]['body'])['text']
    finally:
        os.environ.pop('SAFEHTTP_ALLOW_LOCAL', None)
        hook.close()
