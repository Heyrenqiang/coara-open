"""Master password strength policy for vault setup / change_password.

Applies only when a password is created or changed; unlock never checks
complexity so existing weak passwords keep working, and the env password
path (``COARA_VAULT_PASSWORD``) is exempt.
"""

from __future__ import annotations

_MIN_PASSWORD_LEN = 8

# 常见弱密码小字典 只挡最典型的直线撞库口令 不引入大字典依赖
_COMMON_WEAK_PASSWORDS = frozenset(
    {
        "12345678",
        "123456789",
        "1234567890",
        "01234567",
        "password",
        "password1",
        "passw0rd",
        "qwerty123",
        "abc12345",
        "abcdefgh",
        "11111111",
        "00000000",
        "88888888",
        "iloveyou",
        "admin123",
        "1q2w3e4r",
    }
)


def validate_password_strength(password: str) -> None:
    """Raise ValueError when the password fails the setup/change policy."""
    if len(password) < _MIN_PASSWORD_LEN:
        raise ValueError("vault password must be at least 8 characters")
    if password.lower() in _COMMON_WEAK_PASSWORDS:
        raise ValueError("vault password is too common; pick a less predictable one")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise ValueError("vault password must contain both letters and digits")
