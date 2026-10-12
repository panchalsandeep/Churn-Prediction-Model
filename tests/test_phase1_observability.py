"""Phase 1: readiness, metrics, request ids."""
from __future__ import annotations

import pytest

from churn import observability, storage


def test_readiness_is_ok_when_everything_works(client):
    r = client.get('/api/ready')
    assert r.status_code == 200 and r.get_json()['ready'] is True
    assert set(r.get_json()['checks']) == {'database', 'schema', 'storage', 'keys'}


def test_readiness_goes_red_when_storage_breaks(client, monkeypatch):
    class Broken:
        def put(self, *a):
            raise OSError('disk full')

        def delete(self, *a):
            pass

    monkeypatch.setattr(storage, '_storage', Broken())
    r = client.get('/api/ready')
    assert r.status_code == 503 and r.get_json()['checks']['storage'].startswith('failed')
    assert 'disk full' not in r.get_data(as_text=True)               # no internals leaked to the caller
    storage.reset()


def test_metrics_need_a_header_token_not_a_url_token(client, monkeypatch):
    monkeypatch.setenv('METRICS_TOKEN', 's3cret-token')
    assert client.get('/metrics').status_code == 404
    assert client.get('/metrics?token=s3cret-token').status_code == 404          # URL tokens leak into access logs
    assert client.get('/metrics', headers={'Authorization': 'Bearer wrong'}).status_code == 404
    ok = client.get('/metrics', headers={'Authorization': 'Bearer s3cret-token'})
    assert ok.status_code == 200
    text = ok.get_data(as_text=True)
    assert 'churnsight_jobs{status="queued"}' in text and 'churnsight_queue_oldest_age_seconds' in text and 'churnsight_requests_total' in text


def test_request_id_is_propagated_but_sanitised(client):
    assert client.get('/api/health', headers={'X-Request-ID': 'trace-abc_1.2'}).headers['X-Request-ID'] == 'trace-abc_1.2'
    evil = client.get('/api/health', headers={'X-Request-ID': 'x" y\u0000<script>'}).headers['X-Request-ID']
    assert evil == 'xyscript'
    assert observability.clean_request_id('!!!', 'fallback') == 'fallback'
    assert len(observability.clean_request_id('a' * 500, 'f')) == 64
