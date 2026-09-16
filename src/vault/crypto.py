"""Vault encryption helpers (AES-GCM + scrypt KDF)."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 当前 KDF 参数 OWASP 推荐 scrypt N>=2^17 本机实测单次约 0.3 秒 解锁延迟可接受
_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1
# meta 无 kdf 字段的旧 vault 固定按 N=2^14 派生 迁移完成前必须保留
LEGACY_SCRYPT_N = 2**14
_DK_LEN = 32
_NONCE_LEN = 12
_VERIFIER_MSG = b"coara-vault-v1"
# N=2^17 时 scrypt 内存约 128 MiB 超出 OpenSSL 默认上限 需显式放宽 maxmem
_SCRYPT_MAXMEM = 512 * 1024 * 1024


def generate_salt() -> bytes:
    return secrets.token_bytes(16)


def derive_key(password: str, salt: bytes, *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> bytearray:
    if not password:
        raise ValueError("password is required")
    if len(salt) < 8:
        raise ValueError("salt is too short")
    # bytearray 承载密钥材料：调用点用完可原地清零（与 session.py 同款手法），
    # 避免不可变 bytes 副本驻留至 GC；逐字节内容与 scrypt 输出完全一致。
    return bytearray(
        hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=_SCRYPT_MAXMEM,
            dklen=_DK_LEN,
        )
    )


def wipe_key_material(buf: bytearray | bytes | None) -> None:
    """原地清零可变密钥缓冲（session.lock 同款手法）；bytes 不可变，静默跳过。"""
    if isinstance(buf, bytearray):
        buf[:] = b"\x00" * len(buf)


def build_verifier(dek: bytes) -> bytes:
    return hmac.new(dek, _VERIFIER_MSG, hashlib.sha256).digest()


def verify_dek(dek: bytes, verifier: bytes) -> bool:
    expected = build_verifier(dek)
    return hmac.compare_digest(expected, verifier)


def encrypt_bytes(plaintext: bytes, dek: bytes) -> bytes:
    nonce = secrets.token_bytes(_NONCE_LEN)
    cipher = AESGCM(dek)
    return nonce + cipher.encrypt(nonce, plaintext, None)


def decrypt_bytes(blob: bytes, dek: bytes) -> bytes:
    if len(blob) <= _NONCE_LEN:
        raise ValueError("ciphertext is too short")
    nonce = blob[:_NONCE_LEN]
    payload = blob[_NONCE_LEN:]
    cipher = AESGCM(dek)
    return cipher.decrypt(nonce, payload, None)
