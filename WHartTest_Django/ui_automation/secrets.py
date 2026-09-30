"""Encryption helpers for credentials used by UI automation environments."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _cipher() -> Fernet:
    key_material = getattr(settings, "UI_AUTH_ENCRYPTION_KEY", "") or settings.SECRET_KEY
    if not key_material or (
        not getattr(settings, "DEBUG", False)
        and str(key_material).startswith("django-insecure-")
    ):
        raise ImproperlyConfigured(
            "生产环境 UI 自动登录密码不能使用 Django 开发密钥加密，请设置强随机的 UI_AUTH_ENCRYPTION_KEY。"
        )
    key = base64.urlsafe_b64encode(
        hashlib.sha256(("wharttest-ui-auth-v1:" + key_material).encode("utf-8")).digest()
    )
    return Fernet(key)


def encrypt_ui_secret(value: str) -> str:
    if not value:
        return ""
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_ui_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise ImproperlyConfigured(
            "UI 自动登录密码无法解密，请检查服务端加密密钥是否变更。"
        ) from exc
