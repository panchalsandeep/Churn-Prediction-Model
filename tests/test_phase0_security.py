"""Phase 0 security: envelope encryption, crypto-shredding, safe artifacts, field protection."""
from __future__ import annotations

import io

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa
from cryptography.exceptions import InvalidTag

from churn import artifacts, config, crypto, db, registry
from churn.demo_data import generate_demo
from churn.training import train_model


@pytest.fixture(scope='module')
def trained_bundle():
    r = train_model(generate_demo(1200, seed=4), algorithm='logistic_regression')
    return r['bundle'], r['X'].head(40)


# ── envelope encryption ───────────────────────────────────────────────────────────────────────────

def test_encrypt_roundtrip_and_isolation_between_workspaces(env):
    blob = crypto.encrypt_bytes('tenant_a', b'secret customer data', 'ctx')
    assert blob.startswith(crypto.MAGIC) and b'secret' not in blob
    assert crypto.decrypt_bytes('tenant_a', blob, 'ctx') == b'secret customer data'
    crypto.dek_for('tenant_b')                                    # tenant B has its own key
    with pytest.raises(InvalidTag):
        crypto.decrypt_bytes('tenant_b', blob, 'ctx')            # B's key (and context binding) cannot open A's data


def test_ciphertext_is_bound_to_its_context_and_detects_tampering(env):
    blob = crypto.encrypt_bytes('tenant_a', b'row 1 attributes', 'C1|attrs')
    with pytest.raises(InvalidTag):
        crypto.decrypt_bytes('tenant_a', blob, 'C2|attrs')      # moving a value to another row fails
    flipped = bytearray(blob)
    flipped[-3] ^= 0x01
    with pytest.raises(InvalidTag):
        crypto.decrypt_bytes('tenant_a', bytes(flipped), 'C1|attrs')


def test_crypto_shredding_makes_data_unrecoverable(env):
    blob = crypto.encrypt_bytes('doomed', b'delete me forever', 'x')
    assert crypto.shred('doomed') is True
    crypto._deks.clear()
    with pytest.raises(crypto.KeyShredded):
        crypto.decrypt_bytes('doomed', blob, 'x')
    with pytest.raises(crypto.KeyShredded):
        crypto.encrypt_bytes('doomed', b'new data', 'x')          # and the key can't be silently recreated


def test_master_key_rotation_keeps_existing_data_readable(env):
    blob = crypto.encrypt_bytes('rotating', b'still here after rotation', 'ctx')
    old = crypto.provider()
    new = crypto.LocalKeyProvider(__import__('base64').urlsafe_b64decode(crypto.new_master_key()))
    assert crypto.rewrap_all(old, new) >= 1
    crypto._provider = new                                        # the deployment now holds only the new master key
    crypto._deks.clear()
    assert crypto.decrypt_bytes('rotating', blob, 'ctx') == b'still here after rotation'
    crypto.reset()


def test_production_refuses_to_invent_a_master_key(env, monkeypatch):
    monkeypatch.setattr(config, 'PRODUCTION', True)
    monkeypatch.delenv('CHURN_MASTER_KEY', raising=False)
    crypto.reset()
    with pytest.raises(crypto.CryptoConfigError):
        crypto.provider()
    crypto.reset()


def test_field_encryption_is_a_noop_unless_enabled(env, monkeypatch):
    monkeypatch.setattr(config, 'ENCRYPT_FIELDS', False)
    assert crypto.encrypt_text('t', '{"a":1}', 'c') == '{"a":1}'
    monkeypatch.setattr(config, 'ENCRYPT_FIELDS', True)
    enc = crypto.encrypt_text('t', '{"a":1}', 'c')
    assert enc.startswith('enc1:') and crypto.decrypt_text('t', enc, 'c') == '{"a":1}'


# ── safe artifacts ────────────────────────────────────────────────────────────────────────────────

def test_bundle_roundtrip_has_identical_predictions_and_no_pickle(trained_bundle):
    bundle, X = trained_bundle
    data = artifacts.dumps_bundle(bundle)
    assert data.startswith(artifacts.MAGIC) and b'\x80\x04' not in data[:16]       # not a pickle stream
    loaded = artifacts.loads_bundle(data)
    assert artifacts.same_predictions(bundle['model'], loaded['model'], X)
    assert loaded['schema']['target'] == 'churn' and loaded['provenance']['bundle_format'] == 3


def test_untrusted_types_are_refused_before_construction(trained_bundle):
    import skops.io as sio

    class Evil:
        pass

    bad = dict(trained_bundle[0], model=Evil())
    with pytest.raises(artifacts.ArtifactUntrusted):
        artifacts.loads_bundle(artifacts.dumps_bundle(bad))


def test_incompatible_library_versions_are_refused_with_a_clear_message(trained_bundle):
    bundle, _ = trained_bundle
    old = dict(bundle, provenance={**bundle['provenance'], 'scikit_learn': '0.24.2'})
    with pytest.raises(artifacts.ArtifactIncompatible, match='Retrain'):
        artifacts.loads_bundle(artifacts.dumps_bundle(old))


def test_legacy_pickles_and_garbage_are_rejected():
    with pytest.raises(artifacts.ArtifactError, match='pickles'):
        artifacts.loads_bundle(b'\x80\x04\x95 pretend pickle')


def test_stored_artifact_contains_no_customer_ids():
    """A fitted calibrator once held the grouped splitter, which carries every customer id. It must be scrubbed."""
    rng = np.random.default_rng(0)
    rows = [{'customer_id': f'SECRET-{i:04d}', 'snapshot_date': f'2026-0{s + 1}-28', 'usage': rng.normal(), 'tenure': int(rng.integers(1, 40)),
             'churn': int(rng.random() < 0.2)} for i in range(300) for s in range(3)]
    r = train_model(pd.DataFrame(rows), algorithm='logistic_regression')
    assert r['schema']['grouped']
    assert b'SECRET-' not in artifacts.dumps_bundle(r['bundle'])


# ── end to end: at-rest protection through the real API ──────────────────────────────────────────────

def test_artifacts_and_snapshots_are_encrypted_in_storage_and_bound_to_their_workspace(client, env, demo_csv_bytes):
    from churn import storage
    res = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(demo_csv_bytes), 'd.csv'), 'algorithm': 'logistic_regression'},
                      content_type='multipart/form-data')
    assert res.get_json()['job']['status'] == 'done'
    row = registry.champion('local')
    blobs = storage.get_storage()
    raw = blobs.get(row['artifact_path'])
    assert row['artifact_path'].endswith('.cse') and raw.startswith(crypto.MAGIC) and b'churn' not in raw
    snapshot = blobs.get(row['data_path'])
    assert snapshot.startswith(crypto.MAGIC) and b'CUST-' not in snapshot

    # An attacker who can write to storage copies tenant "local"'s artifact into another tenant's slot: it must not load.
    stolen_id = row['id'][:31] + 'x'                                     # ids are 32 characters; PostgreSQL enforces the column length
    stolen_key = f"{storage.model_prefix('intruder', stolen_id)}/model.cse"
    blobs.put(stolen_key, raw)
    with db.get_engine().begin() as c:
        c.execute(db.models_t.insert().values(id=stolen_id, workspace_id='intruder', created_at=1.0, status='champion',
                                              artifact_path=stolen_key, meta='{}'))
    crypto.dek_for('intruder')
    registry.forget()
    with pytest.raises(InvalidTag):
        registry.load_bundle('intruder', stolen_id)


def test_customer_fields_are_encrypted_in_the_database_but_still_served(client, env, monkeypatch):
    monkeypatch.setattr(config, 'ENCRYPT_FIELDS', True)
    df = generate_demo(800, seed=9)
    res = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(df.to_csv(index=False).encode()), 'e.csv'), 'algorithm': 'logistic_regression'},
                      content_type='multipart/form-data')
    assert res.get_json()['job']['status'] == 'done', res.get_json()
    with db.get_engine().connect() as c:
        rows = c.execute(sa.select(db.details_t.c.attrs, db.details_t.c.reasons).where(db.details_t.c.workspace_id == 'local').limit(30)).all()
    assert rows and all(r.attrs.startswith('enc1:') for r in rows)
    assert all((r.reasons is None) or r.reasons.startswith('enc1:') for r in rows)
    top = client.get('/api/customers?limit=1').get_json()['customers'][0]
    detail = client.get(f"/api/customers/{top['id']}").get_json()
    assert detail['attrs']['plan'] in ('Basic', 'Plus', 'Premium') and detail['reasons']
    assert client.get('/api/customers?q=CUST-000042').get_json()['total'] == 1           # ID search still works
    assert client.get('/api/customers?q=Premium').get_json()['total'] == 0                # attribute search is off by design
    assert client.get('/api/alerts?status=Open').get_json()['alerts'][0]['signals'] is not None
    seg = client.get('/api/results').get_json()['segments']
    assert seg and seg[0]['segments']                                                      # segments decrypt attributes in memory
