"""Envelope encryption with a per-workspace data key.

    master key (KEK)  ──wraps──▶  workspace data key (DEK, random 256-bit)  ──encrypts──▶  artifacts, snapshots, fields

* **Per-tenant keys:** each workspace has its own DEK, so one tenant's data cannot be read with another's key.
* **Crypto-shredding:** deleting a workspace's wrapped DEK makes every copy of its ciphertext (backups included)
  permanently unreadable. This is how "delete my data" is made real.
* **Authenticated:** AES-256-GCM. Every ciphertext is bound to its workspace and a context string (model id, customer id,
  column), so swapping blobs between tenants or rows fails to decrypt instead of leaking.
* **Pluggable KEK:** `KEY_PROVIDER=local` reads CHURN_MASTER_KEY (base64, 32 bytes) or, in development only, a generated
  key file. `KEY_PROVIDER=aws-kms` wraps DEKs with a KMS key (needs boto3 and AWS credentials; not verified in this repo's CI).
"""
from __future__ import annotations

import base64
import os
import threading
import time

import sqlalchemy as sa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import config, db

MAGIC = b'CSE1'
FIELD_PREFIX = 'enc1:'
_NONCE = 12
_DEK_TTL = 300.0


class KeyShredded(Exception):
    """The workspace's key was destroyed (or never existed): its data is unrecoverable by design."""


class CryptoConfigError(RuntimeError):
    pass


# ── Key providers (the KEK) ──────────────────────────────────────────────────────────────────────

class LocalKeyProvider:
    name = 'local'

    def __init__(self, master: bytes):
        if len(master) != 32:
            raise CryptoConfigError('The master key must be exactly 32 bytes (base64-encoded).')
        self._aes = AESGCM(master)

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE)
        return nonce + self._aes.encrypt(nonce, dek, aad)

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        return self._aes.decrypt(wrapped[:_NONCE], wrapped[_NONCE:], aad)


class AwsKmsKeyProvider:
    """Wrap DEKs with AWS KMS. Requires boto3; encryption context binds the wrap to the workspace."""
    name = 'aws-kms'

    def __init__(self, key_id: str):
        import boto3  # noqa: PLC0415 (optional dependency)
        self._kms = boto3.client('kms')
        self._key_id = key_id

    def wrap(self, dek: bytes, aad: bytes) -> bytes:
        return self._kms.encrypt(KeyId=self._key_id, Plaintext=dek, EncryptionContext={'aad': aad.decode()})['CiphertextBlob']

    def unwrap(self, wrapped: bytes, aad: bytes) -> bytes:
        return self._kms.decrypt(CiphertextBlob=wrapped, EncryptionContext={'aad': aad.decode()})['Plaintext']


_provider = None
_lock = threading.Lock()
_deks: dict[str, tuple[bytes, float]] = {}


def _master_from_env_or_file() -> bytes:
    raw = os.environ.get('CHURN_MASTER_KEY')
    if raw:
        return base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
    if config.PRODUCTION:
        raise CryptoConfigError('Production needs CHURN_MASTER_KEY (or KEY_PROVIDER=aws-kms); refusing to invent a key.')
    path = os.path.join(config.data_dir(), 'master.key')       # development convenience only
    if not os.path.exists(path):
        with open(path, 'wb') as f:
            f.write(os.urandom(32))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return open(path, 'rb').read()


def provider():
    global _provider
    with _lock:
        if _provider is None:
            kind = os.environ.get('KEY_PROVIDER', 'local')
            if kind == 'aws-kms':
                _provider = AwsKmsKeyProvider(os.environ['KMS_KEY_ID'])
            else:
                _provider = LocalKeyProvider(_master_from_env_or_file())
        return _provider


def reset() -> None:
    """Forget cached keys and provider (tests and key rotation)."""
    global _provider
    with _lock:
        _provider = None
        _deks.clear()


def new_master_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


# ── Data keys (the DEK) ──────────────────────────────────────────────────────────────────────────

def _aad(ws: str) -> bytes:
    return f'dek|{ws}'.encode()


def dek_for(ws: str, create: bool = True) -> bytes | None:
    now = time.time()
    hit = _deks.get(ws)
    if hit and hit[1] > now:
        return hit[0]
    keys = db.keys_t
    with db.get_engine().begin() as c:
        row = c.execute(sa.select(keys).where(keys.c.workspace_id == ws)).first()
        if row is None:
            if not create:
                return None
            dek = os.urandom(32)
            c.execute(keys.insert().values(workspace_id=ws, provider=provider().name, wrapped_dek=provider().wrap(dek, _aad(ws)),
                                           status='active', created_at=now))
        elif row.status != 'active' or row.wrapped_dek is None:
            raise KeyShredded(f'The encryption key for workspace {ws} was destroyed; its data cannot be recovered.')
        else:
            dek = provider().unwrap(row.wrapped_dek, _aad(ws))
    _deks[ws] = (dek, now + _DEK_TTL)
    return dek


def shred(ws: str) -> bool:
    """Destroy the workspace key. Every ciphertext made with it becomes permanently unreadable."""
    keys = db.keys_t
    with db.get_engine().begin() as c:
        res = c.execute(sa.update(keys).where(keys.c.workspace_id == ws).values(status='shredded', wrapped_dek=None, shredded_at=time.time()))
    _deks.pop(ws, None)
    return bool(res.rowcount)


def rewrap_all(old, new) -> int:
    """Rotate the master key: unwrap every DEK with `old` and wrap it with `new`. Data is not re-encrypted."""
    keys = db.keys_t
    n = 0
    with db.system_context(), db.get_engine().begin() as c:
        for row in c.execute(sa.select(keys).where(keys.c.status == 'active')).all():
            dek = old.unwrap(row.wrapped_dek, _aad(row.workspace_id))
            c.execute(sa.update(keys).where(keys.c.workspace_id == row.workspace_id).values(wrapped_dek=new.wrap(dek, _aad(row.workspace_id))))
            n += 1
    reset()
    return n


# ── Data encryption ──────────────────────────────────────────────────────────────────────────────

def _ctx(ws: str, context: str) -> bytes:
    return f'{ws}|{context}'.encode()


def encrypt_bytes(ws: str, data: bytes, context: str = '') -> bytes:
    nonce = os.urandom(_NONCE)
    return MAGIC + nonce + AESGCM(dek_for(ws)).encrypt(nonce, data, _ctx(ws, context))


def decrypt_bytes(ws: str, blob: bytes, context: str = '') -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError('Not an encrypted ChurnSight blob.')
    dek = dek_for(ws, create=False)
    if dek is None:
        raise KeyShredded(f'No encryption key for workspace {ws}.')
    return AESGCM(dek).decrypt(blob[4:4 + _NONCE], blob[4 + _NONCE:], _ctx(ws, context))


def pseudonymize_id(ws: str, value) -> str:
    """Stable, workspace-specific, irreversible stand-in for a customer id (HMAC-SHA256 under a key derived from the workspace key)."""
    import hashlib
    import hmac
    key = hmac.new(dek_for(ws), b'pseudonymize-v1', hashlib.sha256).digest()
    return 'h_' + hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest()[:20]


def encrypt_text(ws: str, text: str | None, context: str) -> str | None:
    """Field-level encryption for stored attributes. A no-op unless ENCRYPT_FIELDS is on."""
    if not isinstance(text, str) or not config.ENCRYPT_FIELDS:
        return text if isinstance(text, str) else None
    return FIELD_PREFIX + base64.b64encode(encrypt_bytes(ws, text.encode(), context)).decode()


def decrypt_text(ws: str, value: str | None, context: str) -> str | None:
    if value is None or not value.startswith(FIELD_PREFIX):
        return value
    return decrypt_bytes(ws, base64.b64decode(value[len(FIELD_PREFIX):]), context).decode()
