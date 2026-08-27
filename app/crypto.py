"""Encryption at rest for hosted-provider API keys (`provider_settings.api_key`).

Scope, stated honestly: this protects against reading the key straight out
of a DB dump/backup/`SELECT *` on a local single-user install — it is not a
multi-tenant secret-management story. A real hosted deployment needs
KMS/Vault-backed secrets, tenant-scoped authorization, and rotation; this
module does not attempt to be that.

The key is a Fernet key, read from WORK_CUBE_SECRET_KEY if set, otherwise
generated once and persisted to a local, gitignored file so it survives
restarts. Losing that file makes previously-saved provider keys
undecryptable — same tradeoff any local secret store makes without a
password/passphrase layer, and out of scope to solve here.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_KEY_FILE = Path(__file__).parent.parent / ".secret_key"


def _load_or_create_key() -> bytes:
    env_key = os.environ.get("WORK_CUBE_SECRET_KEY")
    if env_key:
        return env_key.encode()

    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()

    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    _KEY_FILE.chmod(0o600)
    return key


_fernet = Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(
            "stored value could not be decrypted — WORK_CUBE_SECRET_KEY or "
            ".secret_key may have changed since it was saved"
        ) from exc
