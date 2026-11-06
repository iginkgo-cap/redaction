"""实体值加密与查重（ADR-007 §4）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"


class KeyLoadError(Exception):
    """密钥读不到或格式不对，启动即失败。"""


@dataclass(frozen=True)
class Keys:
    encryption_key: bytes
    hmac_key: bytes


def _decode(name: str, raw: str | None) -> bytes:
    if not raw:
        raise KeyLoadError(f"missing {name}")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise KeyLoadError(f"{name} must be 32 bytes (base64)")
    return key


def _from_json(text: str) -> Keys:
    d = json.loads(text)
    return Keys(_decode("encryption_key", d.get("encryption_key")), _decode("hmac_key", d.get("hmac_key")))


def load_keys(settings) -> Keys:  # noqa: ANN001
    if settings.key_source == "secretsmanager":
        if not settings.key_secret_id:
            raise KeyLoadError("REDACTION_KEY_SECRET_ID is required when REDACTION_KEY_SOURCE=secretsmanager")
        import boto3

        secret = boto3.client("secretsmanager").get_secret_value(SecretId=settings.key_secret_id)
        return _from_json(secret["SecretString"])
    if settings.key_file:
        return _from_json(settings.key_file.read_text())
    return Keys(
        _decode("REDACTION_ENCRYPTION_KEY", settings.encryption_key),
        _decode("REDACTION_HMAC_KEY", settings.hmac_key),
    )


class Cipher:
    def __init__(self, keys: Keys):
        self._aes = AESGCM(keys.encryption_key)
        self._hmac_key = keys.hmac_key

    def encrypt(self, value: str) -> bytes:
        nonce = os.urandom(12)
        return _VERSION + nonce + self._aes.encrypt(nonce, value.encode(), None)

    def decrypt(self, blob: bytes) -> str:
        if blob[:1] != _VERSION:
            raise ValueError(f"unknown key version {blob[:1]!r}")
        return self._aes.decrypt(blob[1:13], blob[13:], None).decode()

    def digest(self, namespace: str, kind: str, norm_value: str) -> bytes:
        msg = f"{namespace}|{kind}|{norm_value}".encode()
        return hmac.new(self._hmac_key, msg, hashlib.sha256).digest()
