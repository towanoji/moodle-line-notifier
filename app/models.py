"""
models.py  ―  ユーザーデータ管理（インメモリ）
================================================
現時点ではメモリ上で管理。
サーバー再起動でデータが消えるため、後で PostgreSQL に移行することを推奨。

ユーザーの状態遷移:
  NEW → WAITING_USERNAME → WAITING_PASSWORD → REGISTERED
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class User:
    line_user_id: str

    # 登録フローの状態
    state: str = "NEW"
    # NEW              : 未登録
    # WAITING_USERNAME : 学籍番号入力待ち
    # WAITING_PASSWORD : パスワード入力待ち
    # REGISTERED       : 登録済み

    # 認証情報（パスワードは暗号化して保存）
    username:     str = ""
    password_enc: str = ""

    # 通知タイミング設定
    notify_days:  list = field(default_factory=lambda: [3, 1])   # 日数前
    notify_hours: list = field(default_factory=lambda: [12])      # 時間前

    # 登録フロー中の一時保存
    temp_username: str = ""

    created_at: datetime = field(default_factory=datetime.now)


# ─────────────────────────────────────
# インメモリストア
# ─────────────────────────────────────
_store: dict[str, User] = {}


def get_user(line_user_id: str) -> Optional[User]:
    return _store.get(line_user_id)


def get_or_create_user(line_user_id: str) -> User:
    if line_user_id not in _store:
        _store[line_user_id] = User(line_user_id=line_user_id)
    return _store[line_user_id]


def save_user(user: User) -> None:
    _store[user.line_user_id] = user


def get_all_registered() -> list[User]:
    return [u for u in _store.values() if u.state == "REGISTERED"]


def total_users() -> int:
    return len(_store)
