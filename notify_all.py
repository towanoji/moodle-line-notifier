#!/usr/bin/env python3
"""
notify_all.py  ―  全登録ユーザーへの課題締切通知
=================================================
GitHub Actions から実行される。
PostgreSQL から全登録ユーザーを読み込み、
各自の KU-LMS にログインして課題を取得し、LINE で通知する。

必要な環境変数（GitHub Secrets）:
  DATABASE_URL              : Render PostgreSQL の External URL
  ENCRYPTION_KEY            : パスワード復号キー
  LINE_CHANNEL_ACCESS_TOKEN : LINE Messaging API トークン
"""

import os
import sys
import requests
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────
# 設定
# ─────────────────────────────────────
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
JST        = timezone(timedelta(hours=9))


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


def build_message(to_notify: list[dict], now: datetime) -> str:
    lines = [f"📢 課題の締切通知 [{now.strftime('%Y/%m/%d %H:%M')} JST]"]
    for a in to_notify:
        kind, val, hl = a["timing"]
        if kind == "hours":
            timing_str = f"⚠️ あと約 {int(hl)} 時間！"
        elif a["days_left"] == 0:
            timing_str = "⚠️ 今日が締切！"
        elif a["days_left"] == 1:
            timing_str = "🔴 明日締切"
        else:
            timing_str = f"🟡 あと {a['days_left']} 日"

        lines.append(
            f"\n━━━━━━━━━━\n"
            f"📘 {a['course']}\n"
            f"📝 {a['name']}\n"
            f"⏰ {a['duedate'].strftime('%m/%d(%a) %H:%M')} {timing_str}"
        )
    return "\n".join(lines)


def process_user(user, now: datetime, today) -> None:
    from app.crypto import decrypt
    from app.lms import login_session_for_user, get_assignments, get_assignments_by_token

    print(f"  [{user.username}] 処理開始", flush=True)

    try:
        decrypted = decrypt(user.password_enc)

        if decrypted.startswith("TOKEN:"):
            # トークン認証方式（新方式）
            token = decrypted[len("TOKEN:"):]
            assignments = get_assignments_by_token(token)
        else:
            # パスワード認証方式（旧ユーザー互換）
            session, sid, lms_base, final_resp = login_session_for_user(
                user.username, decrypted
            )
            assignments = get_assignments(session, sid, lms_base, start_resp=final_resp)
    except Exception as e:
        print(f"  [{user.username}] 取得エラー: {e}", file=sys.stderr, flush=True)
        return

    to_notify = []
    for a in assignments:
        days_left  = (a["duedate"].date() - today).days
        hours_left = (a["duedate"] - now).total_seconds() / 3600

        timing_kind = None
        for h in user.notify_hours:
            if h - 1 <= hours_left < h + 1:
                timing_kind = ("hours", h, hours_left)
                break
        if timing_kind is None and days_left in user.notify_days:
            timing_kind = ("days", days_left, hours_left)

        if timing_kind:
            to_notify.append({
                **a,
                "days_left":  days_left,
                "hours_left": hours_left,
                "timing":     timing_kind,
            })

    if not to_notify:
        print(f"  [{user.username}] 通知対象なし", flush=True)
        return

    to_notify.sort(key=lambda x: x["duedate"])
    msg = build_message(to_notify, now)

    if send_line_push(user.line_user_id, msg):
        print(f"  [{user.username}] {len(to_notify)} 件通知送信完了", flush=True)


def send_paypay_reminder(user, now: datetime) -> None:
    """トライアル/PayPay期限が3日以内のユーザーにPayPay支払いリンクを送信"""
    if user.subscription_status == "active":
        return  # クレジットカード自動更新ユーザーはスキップ
    if user.trial_ends_at is None:
        return

    days_left = (user.trial_ends_at.date() - now.date()).days
    if days_left not in (3, 1):
        return

    try:
        from app.stripe_payment import create_paypay_checkout_url
        url = create_paypay_checkout_url(user.line_user_id)
        msg = (
            f"⏰ ご利用期限まであと {days_left} 日です！\n\n"
            "引き続きご利用いただくには\n"
            "月額199円をお支払いください。\n\n"
            "💰 PayPayでお支払いはこちら:\n"
            f"{url}\n\n"
            "💳 クレジットカードで自動継続する場合は\n"
            "「サブスク」と送ってください。"
        )
        send_line_push(user.line_user_id, msg)
        print(f"  [{user.username}] PayPayリマインダー送信（残{days_left}日）", flush=True)
    except Exception as e:
        print(f"  [{user.username}] PayPayリマインダー失敗: {e}", file=sys.stderr, flush=True)


def main() -> None:
    # 必須環境変数チェック
    missing = [k for k, v in [
        ("DATABASE_URL",              os.environ.get("DATABASE_URL", "")),
        ("ENCRYPTION_KEY",            os.environ.get("ENCRYPTION_KEY", "")),
        ("LINE_CHANNEL_ACCESS_TOKEN", LINE_TOKEN),
    ] if not v]
    if missing:
        print(f"[ERROR] 環境変数が未設定: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    from app.database import init_db
    from app.models import get_all_registered

    init_db()

    now   = datetime.now(tz=JST)
    today = now.date()
    print(f"[{now.strftime('%Y/%m/%d %H:%M')} JST] 全ユーザー通知開始", flush=True)

    users = get_all_registered()
    print(f"対象ユーザー数: {len(users)} 人", flush=True)

    if not users:
        print("登録ユーザーがいません。終了します。", flush=True)
        return

    for user in users:
        process_user(user, now, today)

    print("✅ 全ユーザーの処理完了", flush=True)


if __name__ == "__main__":
    main()
