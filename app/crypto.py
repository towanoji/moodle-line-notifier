"""
crypto.py  ―  認証情報の暗号化・復号
==========================================
Fernet（AES-128-CBC + HMAC-SHA256）を使用。
ENCRYPTION_KEY 環境変数に 32バイトのBase64URLエンコード済みキーを設定する。

キー生成方法:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os
from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.environ.get("ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("ENCRYPTION_KEY 環境変数が設定されていません")
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """平文を暗号化してトークン文字列を返す"""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """暗号化トークンを復号して平文を返す"""
    return _get_fernet().decrypt(token.encode()).decode()


def generate_key() -> str:
    """新しい暗号化キーを生成して返す（初期設定時に使用）"""
    return Fernet.generate_key().decode()
