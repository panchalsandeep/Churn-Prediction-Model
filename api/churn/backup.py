"""Backup and restore:  python -m churn.backup backup --out DIR   |   python -m churn.backup restore --src DIR [--force]

What is saved: the database (SQLite online-backup API, or pg_dump for PostgreSQL) and, for local storage, the blob tree
(encrypted model artifacts, snapshots, queued uploads). With STORAGE_BACKEND=s3 use bucket versioning / replication for
blobs instead; this tool only records that fact in the manifest.

What is deliberately NOT saved: the master key. Everything above is ciphertext, so a backup alone is useless to a thief,
and equally useless to you without the key. Keep the key in a separate, access-controlled place (KMS, a secrets manager,
an offline copy) and test restores with it. A backup whose key is lost is unrecoverable by design.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time

from sqlalchemy.engine import make_url

from . import config, db, storage

MANIFEST = 'manifest.json'


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_path() -> str | None:
    url = make_url(config.database_url())
    return url.database if url.get_backend_name() == 'sqlite' else None


def backup(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    if os.listdir(out_dir):
        raise FileExistsError(f'{out_dir} is not empty; backups never overwrite each other')
    db.get_engine()                                  # make sure the schema exists and is current
    manifest: dict = {'created_at': time.time(), 'files': {}, 'storage': storage.get_storage().name}
    src = _sqlite_path()
    if src:
        dest = os.path.join(out_dir, 'database.sqlite')
        with sqlite3.connect(src) as source, sqlite3.connect(dest) as target:
            source.backup(target)                    # consistent snapshot even while the app is writing
        manifest['database'] = 'sqlite'
    else:
        if not shutil.which('pg_dump'):
            raise RuntimeError('pg_dump is required to back up PostgreSQL (or use your provider\'s snapshots / PITR).')
        dest = os.path.join(out_dir, 'database.dump')
        subprocess.run(['pg_dump', '--format=custom', '--no-owner', f'--file={dest}', config.database_url().replace('+psycopg', '')], check=True)
        manifest['database'] = 'postgresql'
    blob_root = getattr(storage.get_storage(), 'root', None)
    if blob_root and os.path.isdir(blob_root):
        shutil.copytree(blob_root, os.path.join(out_dir, 'blobs'))
    for d, _, files in os.walk(out_dir):
        for f in files:
            p = os.path.join(d, f)
            manifest['files'][os.path.relpath(p, out_dir).replace(os.sep, '/')] = _sha256(p)
    with open(os.path.join(out_dir, MANIFEST), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)
    return manifest


def verify(src_dir: str) -> dict:
    with open(os.path.join(src_dir, MANIFEST), encoding='utf-8') as f:
        manifest = json.load(f)
    bad = [name for name, digest in manifest['files'].items()
           if not os.path.isfile(os.path.join(src_dir, name)) or _sha256(os.path.join(src_dir, name)) != digest]
    if bad:
        raise ValueError(f'Backup is corrupt or incomplete: {bad[:5]}')
    return manifest


def restore(src_dir: str, force: bool = False) -> dict:
    """Restore into the current DATA_DIR (SQLite + local blobs). PostgreSQL dumps are restored with pg_restore by the operator."""
    manifest = verify(src_dir)
    if manifest['database'] != 'sqlite':
        raise RuntimeError('This backup is a PostgreSQL dump: restore it with pg_restore into an empty database, then copy blobs/ back.')
    target_db = _sqlite_path()
    if target_db is None:
        raise RuntimeError('DATABASE_URL points at PostgreSQL but the backup is SQLite.')
    if os.path.exists(target_db) and not force:
        raise FileExistsError(f'{target_db} already exists; pass --force to replace it')
    db.reset_engine()
    for ext in ('', '-wal', '-shm'):
        if os.path.exists(target_db + ext):
            os.remove(target_db + ext)
    os.makedirs(os.path.dirname(target_db), exist_ok=True)
    shutil.copy2(os.path.join(src_dir, 'database.sqlite'), target_db)
    blobs = os.path.join(src_dir, 'blobs')
    if os.path.isdir(blobs):
        dest = os.path.join(config.data_dir(), 'blobs')
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(blobs, dest)
        storage.reset()
    db.get_engine()                                  # applies any newer migrations to an older backup
    return manifest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('backup')
    b.add_argument('--out', required=True)
    r = sub.add_parser('restore')
    r.add_argument('--src', required=True)
    r.add_argument('--force', action='store_true')
    v = sub.add_parser('verify')
    v.add_argument('--src', required=True)
    a = p.parse_args()
    if a.cmd == 'backup':
        m = backup(a.out)
        print(f'backed up {len(m["files"])} files to {a.out} (the master key is NOT included; store it separately)')
    elif a.cmd == 'restore':
        restore(a.src, a.force)
        print('restored; now run `python -m churn.backup verify`-style checks: log in, open the dashboard, GET /api/audit to verify the chain')
    else:
        verify(a.src)
        print('backup checksums OK')


if __name__ == '__main__':
    main()
