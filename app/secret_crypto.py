"""At-rest encryption for connector credentials (Personal AI OS 1.2, 3.5.2).

Connector tokens (e.g. a read-only GitHub fine-grained PAT) must never live
in ``data_sources.config_json`` (which is returned by the API) or in logs.
They are encrypted with Fernet (AES-128-CBC + HMAC) using a key stored in a
separate file from the SQLite database, so copying the database alone does
not expose credentials.

Key resolution order:
1. ``PAOS_SECRET_KEY`` environment variable (urlsafe base64 32-byte key).
2. File at ``PAOS_SECRET_KEY_PATH`` (default: ``data/secret.key``).

The file is created with owner-only permissions on first use. This is a
local single-user application; the threat model is database
exfiltration/accidental sharing, not a hostile local OS user.
"""

from __future__ import annotations

import base64
import os
import stat
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_KEY_PATH = _ROOT / "data" / "secret.key"

ENV_KEY = "PAOS_SECRET_KEY"
ENV_KEY_PATH = "PAOS_SECRET_KEY_PATH"


class SecretCryptoError(Exception):
    """Raised when credential encryption/decryption is impossible."""


def _normalise_key(raw: bytes) -> bytes:
    key = raw.strip()
    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise SecretCryptoError("密钥不是合法的 Fernet 密钥") from exc
    return key


def _write_key_file(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows: chmod best effort; the data directory stays inside the app.
        pass
    return key


def load_key() -> bytes:
    env_key = os.getenv(ENV_KEY)
    if env_key:
        try:
            return _normalise_key(env_key.encode("utf-8"))
        except SecretCryptoError as exc:
            raise SecretCryptoError(f"环境变量 {ENV_KEY} 中的密钥无效") from exc

    path = Path(os.getenv(ENV_KEY_PATH) or str(_DEFAULT_KEY_PATH))
    if path.exists():
        try:
            return _normalise_key(path.read_bytes())
        except SecretCryptoError as exc:
            raise SecretCryptoError(f"密钥文件 {path} 内容无效") from exc
    return _write_key_file(path)


@lru_cache(maxsize=4)
def _fernet_for(key_path: str) -> Fernet:
    # Cache by resolved key location so tests that point the env var at a
    # temporary file still get an independent cipher.
    os.environ[ENV_KEY_PATH] = key_path
    return Fernet(load_key())


def _fernet() -> Fernet:
    key_path = os.getenv(ENV_KEY_PATH) or str(_DEFAULT_KEY_PATH)
    return _fernet_for(str(Path(key_path).resolve()))


def reset_cipher_cache() -> None:
    """Drop cached ciphers (used after changing the key env in tests)."""
    _fernet_for.cache_clear()


def encrypt_secret(plaintext: str) -> str:
    value = (plaintext or "").strip()
    if not value:
        raise SecretCryptoError("凭据内容不能为空")
    if len(value) > 4000:
        raise SecretCryptoError("凭据过长（上限 4000 字符）")
    token = _fernet().encrypt(value.encode("utf-8"))
    return base64.urlsafe_b64encode(token).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    try:
        token = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
        return _fernet().decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretCryptoError("凭据解密失败（密钥可能已更换）") from exc
