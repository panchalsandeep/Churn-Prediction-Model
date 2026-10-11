"""Model artifacts without pickle.

A bundle is stored as:   magic | JSON header length | JSON (schema, baselines, importance, provenance, ...) | skops model

* The sklearn model uses `skops` (a restricted, inspectable format) instead of `joblib`/`pickle`, which execute arbitrary code
  when loaded. On load, every type referenced by the file is checked against an allow-list *before* anything is constructed.
* Everything else is plain JSON.
* The whole blob is then encrypted per workspace (see crypto.py), which also authenticates it, so a tampered or
  swapped artifact fails to load.
* A model trained with a different scikit-learn minor version is refused with an explicit message rather than loaded
  into a library that may interpret it differently.
"""
from __future__ import annotations

import json
import struct

import numpy as np
import sklearn
import skops.io as sio

from . import db

MAGIC = b'CSB1'
ALLOWED_PREFIXES = ('sklearn.', 'numpy.', 'scipy.', 'builtins.', 'collections.')
_MODEL_KEY = 'model'


class ArtifactError(Exception):
    """Base class: an artifact can't be loaded safely."""


class ArtifactUntrusted(ArtifactError):
    pass


class ArtifactIncompatible(ArtifactError):
    pass


def _minor(version: str) -> tuple[str, str]:
    parts = version.split('.')
    return parts[0], parts[1] if len(parts) > 1 else '0'


def dumps_bundle(bundle: dict) -> bytes:
    meta = {k: v for k, v in bundle.items() if k != _MODEL_KEY}
    header = json.dumps(meta, default=db._json_default, separators=(',', ':')).encode()
    return MAGIC + struct.pack('>I', len(header)) + header + sio.dumps(bundle[_MODEL_KEY])


def loads_bundle(data: bytes, *, check_versions: bool = True) -> dict:
    if not data.startswith(MAGIC):
        raise ArtifactError('Unrecognised model artifact (legacy pickles are not loaded). Retrain the model.')
    (n,) = struct.unpack('>I', data[4:8])
    meta = json.loads(data[8:8 + n])
    blob = data[8 + n:]

    if check_versions:
        trained = (meta.get('provenance') or {}).get('scikit_learn')
        if trained and _minor(trained) != _minor(sklearn.__version__):
            raise ArtifactIncompatible(
                f'This model was trained with scikit-learn {trained}, but the server runs {sklearn.__version__}. '
                'Retrain it (or deploy the matching library version).')

    untrusted = sio.get_untrusted_types(data=blob)
    bad = [t for t in untrusted if not t.startswith(ALLOWED_PREFIXES)]
    if bad:
        raise ArtifactUntrusted(f'Refusing to load a model that references untrusted types: {", ".join(sorted(bad)[:5])}')
    meta[_MODEL_KEY] = sio.loads(blob, trusted=untrusted)

    # JSON turns tuples/ints into lists/strs in a few places; restore what downstream code relies on.
    meta['baseline'] = {k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v) for k, v in meta['baseline'].items()}
    return meta


def same_predictions(a, b, X) -> bool:
    return bool(np.allclose(a.predict_proba(X), b.predict_proba(X)))
