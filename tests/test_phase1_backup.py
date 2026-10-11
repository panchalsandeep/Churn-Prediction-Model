"""Phase 1: backup and restore round trip, tamper detection, and "a backup is useless without the key"."""
from __future__ import annotations

import io
import os
import shutil

from cryptography.exceptions import InvalidTag

import pytest

from churn import backup, config, crypto, db, registry, service, storage


@pytest.fixture(scope='module')
def populated(client, env, demo_csv_bytes):
    res = client.post('/api/upload?wait=1', data={'file': (io.BytesIO(demo_csv_bytes), 'd.csv'), 'algorithm': 'logistic_regression'},
                      content_type='multipart/form-data')
    assert res.get_json()['job']['status'] == 'done'
    return client.get('/api/customers?limit=3').get_json()['customers']


def _point_at(data_dir, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(data_dir))
    monkeypatch.delenv('DATABASE_URL', raising=False)
    db.reset_engine()
    storage.reset()
    registry.forget()
    crypto.reset()


def test_backup_then_restore_into_a_fresh_data_dir_serves_the_same_data(populated, env, tmp_path, monkeypatch):
    original = os.environ['DATA_DIR']
    out = tmp_path / 'bk'
    manifest = backup.backup(str(out))
    assert manifest['database'] == 'sqlite' and any(k.startswith('blobs/models/') for k in manifest['files'])
    assert backup.verify(str(out))
    with pytest.raises(FileExistsError):
        backup.backup(str(out))                                  # never overwrite an earlier backup

    new_dir = tmp_path / 'restored'
    new_dir.mkdir()
    shutil.copy(os.path.join(original, 'master.key'), new_dir / 'master.key')    # the key is escrowed separately, not in the backup
    _point_at(new_dir, monkeypatch)
    try:
        backup.restore(str(out))
        row, bundle = registry.champion_bundle('local')
        assert bundle is not None and row['id']
        top = service.query_customers('local', limit=3)['customers']
        assert [c['id'] for c in top] == [c['id'] for c in populated]
        assert service.customer_detail('local', top[0]['id'])['attrs']                 # encrypted fields decrypt with the escrowed key
        assert db.verify_audit('local')['ok'] is True
    finally:
        _point_at(original, monkeypatch)


def test_a_restored_backup_cannot_be_read_without_the_master_key(populated, env, tmp_path, monkeypatch):
    original = os.environ['DATA_DIR']
    out = tmp_path / 'bk2'
    backup.backup(str(out))
    new_dir = tmp_path / 'nokey'
    new_dir.mkdir()
    _point_at(new_dir, monkeypatch)                              # a new data dir means a brand-new, unrelated dev key
    try:
        backup.restore(str(out))
        with pytest.raises(Exception) as e:
            registry.champion_bundle('local')
        assert isinstance(e.value, InvalidTag)
    finally:
        _point_at(original, monkeypatch)


def test_tampered_or_truncated_backups_are_refused(populated, env, tmp_path):
    out = tmp_path / 'bk3'
    backup.backup(str(out))
    victim = next(p for p in (out / 'blobs').rglob('*.cse'))
    victim.write_bytes(victim.read_bytes()[:-5] + b'XXXXX')
    with pytest.raises(ValueError, match='corrupt'):
        backup.verify(str(out))
    with pytest.raises(ValueError, match='corrupt'):
        backup.restore(str(out), force=True)


def test_restore_refuses_to_overwrite_existing_data_without_force(populated, env, tmp_path):
    out = tmp_path / 'bk4'
    backup.backup(str(out))
    with pytest.raises(FileExistsError):
        backup.restore(str(out))
