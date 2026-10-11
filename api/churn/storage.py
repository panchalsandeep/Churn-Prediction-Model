"""Blob storage behind one small interface, so API servers and workers do not need to share a disk.

Everything stored here (model artifacts, scored snapshots, uploaded datasets) is already encrypted by crypto.py before it
arrives, so the backend only ever sees ciphertext.

    STORAGE_BACKEND=local   directory under DATA_DIR (default)
    STORAGE_BACKEND=s3      S3 or any S3-compatible service (needs boto3; S3_BUCKET, optional S3_PREFIX, S3_ENDPOINT_URL)

The S3 backend is written against the documented boto3 API and is not exercised in this repository's test run
(no AWS credentials or S3 emulator available during development). The local backend is fully tested.
"""
from __future__ import annotations

import os
import shutil
import threading
from typing import Iterable

from . import config


class StorageError(Exception):
    pass


def _check_key(key: str) -> str:
    if not key or key.startswith(('/', '\\')) or '..' in key.replace('\\', '/').split('/') or ':' in key or '\x00' in key:
        raise StorageError(f'Illegal storage key: {key!r}')
    return key.replace('\\', '/')


class LocalStorage:
    name = 'local'

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key: str) -> str:
        path = os.path.abspath(os.path.join(self.root, *_check_key(key).split('/')))
        if os.path.commonpath([path, self.root]) != self.root:           # belt and braces against traversal
            raise StorageError(f'Key escapes the storage root: {key!r}')
        return path

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'wb') as f:                                       # write-then-rename: readers never see a half-written blob
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return key

    def get(self, key: str) -> bytes:
        with open(self._path(key), 'rb') as f:
            return f.read()

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._path(key))

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def delete_prefix(self, prefix: str) -> int:
        path = self._path(prefix.rstrip('/'))
        n = sum(len(files) for _, _, files in os.walk(path)) if os.path.isdir(path) else 0
        shutil.rmtree(path, ignore_errors=True)
        return n

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix.rstrip('/'))
        out: list[str] = []
        for d, _, files in os.walk(base):
            out += [os.path.relpath(os.path.join(d, f), self.root).replace(os.sep, '/') for f in files]
        return sorted(out)

    def presign_put(self, key: str, ttl: int = 900) -> dict | None:
        return None          # local storage has no direct-upload URL; clients send bytes through the API


class S3Storage:
    name = 's3'

    def __init__(self, bucket: str, prefix: str = '', endpoint_url: str | None = None, client=None):
        if client is None:
            import boto3  # noqa: PLC0415 (optional dependency)
            client = boto3.client('s3', endpoint_url=endpoint_url)
        self._c, self._bucket, self._prefix = client, bucket, prefix.strip('/')

    def _k(self, key: str) -> str:
        key = _check_key(key)
        return f'{self._prefix}/{key}' if self._prefix else key

    def put(self, key: str, data: bytes) -> str:
        self._c.put_object(Bucket=self._bucket, Key=self._k(key), Body=data, ServerSideEncryption='AES256')
        return key

    def get(self, key: str) -> bytes:
        try:
            return self._c.get_object(Bucket=self._bucket, Key=self._k(key))['Body'].read()
        except self._c.exceptions.NoSuchKey as exc:
            raise FileNotFoundError(key) from exc

    def exists(self, key: str) -> bool:
        try:
            self._c.head_object(Bucket=self._bucket, Key=self._k(key))
            return True
        except Exception:  # noqa: BLE001  (botocore ClientError 404)
            return False

    def delete(self, key: str) -> None:
        self._c.delete_object(Bucket=self._bucket, Key=self._k(key))

    def _keys(self, prefix: str) -> Iterable[str]:
        token = None
        while True:
            kwargs = {'Bucket': self._bucket, 'Prefix': self._k(prefix.rstrip('/')) + '/'}
            if token:
                kwargs['ContinuationToken'] = token
            page = self._c.list_objects_v2(**kwargs)
            for obj in page.get('Contents', []):
                yield obj['Key']
            token = page.get('NextContinuationToken')
            if not token:
                return

    def delete_prefix(self, prefix: str) -> int:
        keys = list(self._keys(prefix))
        for i in range(0, len(keys), 1000):
            self._c.delete_objects(Bucket=self._bucket, Delete={'Objects': [{'Key': k} for k in keys[i:i + 1000]]})
        return len(keys)

    def list(self, prefix: str) -> list[str]:
        strip = len(self._prefix) + 1 if self._prefix else 0
        return sorted(k[strip:] for k in self._keys(prefix))

    def presign_put(self, key: str, ttl: int = 900) -> dict | None:
        """A short-lived URL a browser can PUT the (already client-encrypted or plain) bytes to, bypassing the API."""
        url = self._c.generate_presigned_url('put_object', Params={'Bucket': self._bucket, 'Key': self._k(key)}, ExpiresIn=ttl)
        return {'method': 'PUT', 'url': url, 'expires_in': ttl}


_storage = None
_lock = threading.Lock()


def get_storage():
    global _storage
    with _lock:
        if _storage is None:
            if os.environ.get('STORAGE_BACKEND', 'local') == 's3':
                _storage = S3Storage(os.environ['S3_BUCKET'], os.environ.get('S3_PREFIX', ''), os.environ.get('S3_ENDPOINT_URL'))
            else:
                _storage = LocalStorage(os.path.join(config.data_dir(), 'blobs'))
        return _storage


def reset() -> None:
    global _storage
    with _lock:
        _storage = None


def model_prefix(ws: str, model_id: str | None = None) -> str:
    import re
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', ws)[:80]
    return f'models/{safe}' + (f'/{model_id}' if model_id else '')


def table_prefix(ws: str, role: str | None = None) -> str:
    import re
    base = f'tables/{re.sub(r"[^A-Za-z0-9_.-]", "_", ws)[:80]}'
    return f'{base}/{role}' if role else base


def upload_prefix(ws: str) -> str:
    import re
    return f'uploads/{re.sub(r"[^A-Za-z0-9_.-]", "_", ws)[:80]}'
