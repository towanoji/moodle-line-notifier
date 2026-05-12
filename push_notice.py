#!/usr/bin/env python3
"""
push_notice.py  ―  既存登録済みユーザーへの一括通知
=====================================================
登録済み全ユーザーにパスワード取り扱いの説明文を送信する。
一回限りのスクリプト。

必要な環境変数:
  DATABASE_URL              : PostgreSQL の接続URL
  ENCRYPTION_KEY            : 暗号化キー
  LINE_CHANNEL_ACCESS_TOKEN : LINE Messaging API トークン
"""

import os
import sys
import time
import requests

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")


def send_line_push(user_id: str, text: str) -> bool:
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {LINE_TOKEN}",
        },
        json={"to": user_id, "messages": [{"type": "text", "text": text}]},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  [LINE ERROR] {resp.status_code} {resp.text}", file=sys.stderr)
        return False
    return True


NOTICE_TEXT = (
    "🎁 【お知らせ】無料化のご案内\n\n"
    "いつもご利用ありがとうございます！\n\n"
    "サービスの普及を目指し、\n"
    "当面の間、完全無料でご利用いただけることになりました 🙌\n\n"
    "引き続き課題の締切通知をお届けします📅\n\n"
    "ご意見・ご要望は「意見箱」から\n"
    "お気軽にお送りください 📩"
)


def main() -> None:
    missing = [k for k, v in [
        ("DATABASE_URL",              os.environ.get("DATABASE_URL", "")),
        ("ENCRYPTION_KEY",            os.environ.get("ENCRYPTION_KEY", "")),
        ("LINE_CHANNEL_ACCESS_TOKEN", LINE_TOKEN),
    ] if not v]
    if missing:
        print(f"[ERROR] 環境変数が未設定: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    from app.database import init_db, get_session, UserRecord
    from app.models import _record_to_user

    init_db()

    with get_session() as s:
        records = s.query(UserRecord).filter(UserRecord.state == "REGISTERED").all()
        users = [_record_to_user(r) for r in records]

    print(f"対象ユーザー数: {len(users)} 人", flush=True)

    success = 0
    fail = 0
    for user in users:
        ok = send_line_push(user.line_user_id, NOTICE_TEXT)
        if ok:
            print(f"  ✅ {user.username} ({user.line_user_id[:10]}...) 送信完了", flush=True)
            success += 1
        else:
            print(f"  ❌ {user.username} ({user.line_user_id[:10]}...) 送信失敗", file=sys.stderr, flush=True)
            fail += 1
        # LINE APIのレート制限を避けるため少し待機
        time.sleep(0.5)

    print(f"\n✅ 完了: 成功 {success} 件 / 失敗 {fail} 件", flush=True)


if __name__ == "__main__":
    main()
