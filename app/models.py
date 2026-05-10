"""
models.py  ―  ユーザーデータ管理（PostgreSQL永続化）
=====================================================
SQLAlchemy + PostgreSQL でユーザーデータを保存する。
サーバー再起動後もデータが保持される。

ユーザーの状態遷移:
  NEW → WAITING_USERNAME → WAITING_PASSWORD → REGISTERED
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import get_session, UserRecord

JST = timezone(timedelta(hours=9))


# ─────────────────────────────────────
# ユーザーデータクラス（アプリ内で使う軽量オブジェクト）
# ─────────────────────────────────────

@dataclass
class User:
    line_user_id: str
    state:               str = "NEW"
    username:            str = ""
    password_enc:        str = ""
    notify_days:         list = field(default_factory=lambda: [3, 1])
    notify_hours:        list = field(default_factory=lambda: [12])
    stripe_customer_id:  str = ""
    subscription_status: str = "trial"
    trial_ends_at:       Optional[datetime] = None
    temp_username:       str = ""
    created_at:          datetime = field(default_factory=lambda: datetime.now(tz=JST))

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
        """トライアル残り日数"""
        if self.trial_ends_at is None:
            return 14
        delta = self.trial_ends_at - datetime.now(tz=JST)
        return max(0, delta.days)


# ─────────────────────────────────────
# DB ↔ User 変換
# ─────────────────────────────────────

def _record_to_user(r: UserRecord) -> User:
    return User(
        line_user_id        = r.line_user_id,
        state               = r.state or "NEW",
        username            = r.username or "",
        password_enc        = r.password_enc or "",
        notify_days         = r.notify_days or [3, 1],
        notify_hours        = r.notify_hours or [12],
        stripe_customer_id  = r.stripe_customer_id or "",
        subscription_status = r.subscription_status or "trial",
        trial_ends_at       = r.trial_ends_at,
        created_at          = r.created_at or datetime.now(tz=JST),
    )


def _apply_user_to_record(user: User, r: UserRecord) -> None:
    r.state               = user.state
    r.username            = user.username
    r.password_enc        = user.password_enc
    r.notify_days         = user.notify_days
    r.notify_hours        = user.notify_hours
    r.stripe_customer_id  = user.stripe_customer_id
    r.subscription_status = user.subscription_status
    r.trial_ends_at       = user.trial_ends_at


# ─────────────────────────────────────
# CRUD
# ─────────────────────────────────────

def get_user(line_user_id: str) -> Optional[User]:
    with get_session() as s:
        r = s.get(UserRecord, line_user_id)
        return _record_to_user(r) if r else None


def get_or_create_user(line_user_id: str) -> User:
    with get_session() as s:
        r = s.get(UserRecord, line_user_id)
        if r is None:
            r = UserRecord(line_user_id=line_user_id)
            s.add(r)
            s.commit()
        return _record_to_user(r)


def save_user(user: User) -> None:
    with get_session() as s:
        r = s.get(UserRecord, user.line_user_id)
        if r is None:
            r = UserRecord(line_user_id=user.line_user_id)
            s.add(r)
        _apply_user_to_record(user, r)
        s.commit()


def get_all_registered() -> list[User]:
    """通知対象（アクティブ）なユーザー一覧"""
    with get_session() as s:
        records = s.query(UserRecord).filter(
            UserRecord.state == "REGISTERED"
        ).all()
        users = [_record_to_user(r) for r in records]
    return [u for u in users if u.is_active()]


def get_all_trial_expiring() -> list[User]:
    """トライアル残り3日以内のユーザー（課金催促用）"""
    users = get_all_registered()
    return [
        u for u in users
        if u.subscription_status == "trial"
        and 0 <= u.days_left_in_trial() <= 3
    ]


def get_user_by_stripe_customer(customer_id: str) -> Optional[User]:
    """Stripe Customer ID からユーザーを検索"""
    with get_session() as s:
        r = s.query(UserRecord).filter(
            UserRecord.stripe_customer_id == customer_id
        ).first()
        return _record_to_user(r) if r else None


def total_users() -> int:
    with get_session() as s:
        return s.query(UserRecord).count()
