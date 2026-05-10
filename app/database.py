"""
database.py  ―  PostgreSQL 接続・テーブル管理
================================================
SQLAlchemy を使用してユーザーデータを永続化する。
"""

import os
from sqlalchemy import create_engine, Column, String, DateTime, JSON, Boolean
from sqlalchemy.orm import DeclarativeBase, Session
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Render の postgres:// を postgresql:// に変換（SQLAlchemy 2.x 対応）
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None


class Base(DeclarativeBase):
    pass


class UserRecord(Base):
    __tablename__ = "users"

    line_user_id        = Column(String, primary_key=True)
    state               = Column(String, default="NEW")
    username            = Column(String, default="")
    password_enc        = Column(String, default="")
    notify_days         = Column(JSON, default=[3, 1])
    notify_hours        = Column(JSON, default=[12])
    stripe_customer_id  = Column(String, default="")
    subscription_status = Column(String, default="trial")
    trial_ends_at       = Column(DateTime(timezone=True), nullable=True)
    created_at          = Column(DateTime(timezone=True), default=lambda: datetime.now(tz=JST))


def init_db() -> None:
    """テーブルを作成（存在しない場合のみ）"""
    if engine is None:
        print("[DB] DATABASE_URL が未設定のためスキップ", flush=True)
        return
    Base.metadata.create_all(engine)
    print("[DB] テーブル初期化完了", flush=True)


def get_session() -> Session:
    if engine is None:
        raise RuntimeError("DATABASE_URL が設定されていません")
    return Session(engine)
