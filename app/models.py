"""
models.py  ―  ユーザーデータ管理（インメモリ）
================================================
現時点ではメモリ上で管理。
サーバー再起動でデータが消えるため、後で PostgreSQL に移行することを推奨。

ユーザーの状態遷移:
  NEW → WAITING_USERNAME → WAITING_PASSWORD → REGISTERED
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

JST = timezone(timedelta(hours=9))


@dataclass
class User:
    line_user_id: str

    # 登録フローの状態
    state: str = "NEW"
    # NEW              : 未登録
    # WAITING_USERNAME : 学籍番号入力待ち
    # WAITING_PASSWORD : パスワード入力待ち
    # REGISTERED       : 登録済み（トライアル or サブスク有効）

    # 認証情報（パスワードは暗号化して保存）
    username:     str = ""
    password_enc: str = ""

    # 通知タイミング設定
    notify_days:  list = field(default_factory=lambda: [3, 1])   # 日数前
    notify_hours: list = field(default_factory=lambda: [12])      # 時間前

    # Stripe サブスクリプション管理
    stripe_customer_id:  str = ""
    subscription_status: str = "trial"
    # trial     : 無料トライアル中
    # active    : サブスク有効（課金済み）
    # cancelled : サブスク停止
    trial_ends_at: Optional[datetime] = None

    # 登録フロー中の一時保存
    temp_username: str = ""

    created_at: datetime = field(default_factory=datetime.now)

    def is_active(self) -> bool:
        """通知を送るべきユーザーか判定"""
        if self.state != "REGISTERED":
            return False
        if self.subscription_status == "active":
            return True
        if self.subscription_status == "trial":
            if self.trial_ends_at is None:
                return True
            return datetime.now(tz=JST) < self.trial_ends_at
        return False

    def days_left_in_trial(self) -> int:
        """トライアル残り日数（0以下は期限切れ）"""
        if self.trial_ends_at is None:
            return 14
        delta = self.trial_ends_at - datetime.now(tz=JST)
        return max(0, delta.days)


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
    """通知対象（アクティブ）なユーザー一覧"""
    return [u for u in _store.values() if u.is_active()]


def get_all_trial_expiring() -> list[User]:
    """トライアル残り3日以内のユーザー（課金催促用）"""
    return [
        u for u in _store.values()
        if u.state == "REGISTERED"
        and u.subscription_status == "trial"
        and 0 <= u.days_left_in_trial() <= 3
    ]


def total_users() -> int:
    return len(_store)
