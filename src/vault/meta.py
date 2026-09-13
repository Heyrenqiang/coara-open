"""Vault metadata file (salt + verifier, no plaintext password)."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.vault.crypto import (
    _SCRYPT_N,
    _SCRYPT_P,
    _SCRYPT_R,
    LEGACY_SCRYPT_N,
    build_verifier,
    derive_key,
    generate_salt,
    verify_dek,
)

# meta 可被手工篡改 派生参数加边界防御超大内存 DoS
_KDF_MIN_N = 2**10
_KDF_MAX_N = 2**18
_KDF_MAX_R = 32
_KDF_MAX_P = 16


@dataclass(slots=True, frozen=True)
class KdfParams:
    algo: str
    n: int
    r: int
    p: int


@dataclass(slots=True)
class VaultMeta:
    version: int
    salt: bytes
    verifier: bytes
    # 缺失即为 legacy vault（scrypt N=2^14） 见 resolve_kdf
    kdf: KdfParams | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": self.version,
            "salt": base64.b64encode(self.salt).decode("ascii"),
            "verifier": base64.b64encode(self.verifier).decode("ascii"),
        }
        if self.kdf is not None:
            payload["kdf"] = {
                "algo": self.kdf.algo,
                "n": self.kdf.n,
                "r": self.kdf.r,
                "p": self.kdf.p,
            }
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, object]) -> VaultMeta:
        version = int(payload.get("version", 1))
        salt_raw = str(payload.get("salt") or "")
        verifier_raw = str(payload.get("verifier") or "")
        kdf: KdfParams | None = None
        kdf_raw = payload.get("kdf")
        if isinstance(kdf_raw, dict):
            kdf = KdfParams(
                algo=str(kdf_raw.get("algo") or ""),
                n=int(kdf_raw.get("n") or 0),
                r=int(kdf_raw.get("r") or 0),
                p=int(kdf_raw.get("p") or 0),
            )
        return cls(
            version=version,
            salt=base64.b64decode(salt_raw.encode("ascii")),
            verifier=base64.b64decode(verifier_raw.encode("ascii")),
            kdf=kdf,
        )


def current_kdf_params() -> KdfParams:
    return KdfParams(algo="scrypt", n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)


def resolve_kdf(meta: VaultMeta) -> tuple[int, int, int]:
    """Return the (n, r, p) to derive with; legacy metas (no kdf field) pin 2^14."""
    kdf = meta.kdf
    if kdf is None:
        return LEGACY_SCRYPT_N, _SCRYPT_R, _SCRYPT_P
    if kdf.algo != "scrypt":
        raise ValueError(f"unsupported vault kdf algorithm: {kdf.algo}")
    if not (_KDF_MIN_N <= kdf.n <= _KDF_MAX_N) or kdf.n & (kdf.n - 1):
        raise ValueError("invalid vault kdf parameters")
    if not (1 <= kdf.r <= _KDF_MAX_R and 1 <= kdf.p <= _KDF_MAX_P):
        raise ValueError("invalid vault kdf parameters")
    return kdf.n, kdf.r, kdf.p


def kdf_is_current(meta: VaultMeta) -> bool:
    kdf = meta.kdf
    return kdf is not None and kdf.algo == "scrypt" and (kdf.n, kdf.r, kdf.p) == (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)


def load_meta(path: Path) -> VaultMeta:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid vault metadata")
    return VaultMeta.from_json(payload)


def save_meta(path: Path, meta: VaultMeta) -> None:
    """Persist meta atomically.

    Writes to a temporary file in the same directory, then ``os.replace`` it
    over the target. This ensures the meta file is never left half-written
    — critical for ``change_password``, where a crash between ``store.rekey``
    and ``save_meta`` would otherwise leave the DB re-encrypted with the new
    DEK while the meta still describes the old salt/verifier (data loss).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(meta.to_json(), ensure_ascii=False, indent=2) + "\n"
    # NamedTemporaryFile + os.replace for atomic publish on both POSIX and Windows.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".vault_meta_",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            # fsync may fail on some networked filesystems; the atomic
            # rename still protects against partial writes.
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Cleanup the temp file on any failure before propagating.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def create_meta(password: str) -> VaultMeta:
    salt = generate_salt()
    dek = derive_key(password, salt)
    return VaultMeta(version=1, salt=salt, verifier=build_verifier(dek), kdf=current_kdf_params())


def unlock_dek(password: str, meta: VaultMeta) -> bytes:
    n, r, p = resolve_kdf(meta)
    dek = derive_key(password, meta.salt, n=n, r=r, p=p)
    if not verify_dek(dek, meta.verifier):
        raise ValueError("incorrect vault password")
    return dek
