"""
line_bot.py  ―  LINE Messaging API ウェブフック処理
=====================================================
ユーザーの状態に応じた会話フロー:

  友達追加
    └→ 学籍番号を入力 (WAITING_USERNAME)
         └→ パスワードを入力 (WAITING_PASSWORD)
              └→ KU-LMS にログイン試行
                   ├─ 成功 → 登録完了 (REGISTERED)
                   └─ 失敗 → 学籍番号入力に戻る

登録済みユーザーのコマンド:
  「設定」      → 現在の通知設定を表示
  「日数 3,1」  → 通知する日数前を変更
  「時間 12」   → 通知する時間前を変更
  「解除」      → 登録を削除
"""

import os
import threading

from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    FollowEvent,
    UnfollowEvent,
    TextMessageContent,
)

from app.models import get_or_create_user, save_user, get_user
from app.crypto import encrypt

CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
DEVELOPER_LINE_USER_ID = os.environ.get("DEVELOPER_LINE_USER_ID", "")

handler       = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)


# ─────────────────────────────────────
# メッセージ送信ヘルパー
# ─────────────────────────────────────

def reply(reply_token: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def push(line_user_id: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=line_user_id,
                messages=[TextMessage(text=text)],
            )
        )


# ─────────────────────────────────────
# イベントハンドラ
# ─────────────────────────────────────

@handler.add(FollowEvent)
def handle_follow(event) -> None:
    """友達追加 → ユーザー名の入力を促す"""
    user = get_or_create_user(event.source.user_id)
    user.state = "WAITING_USERNAME"
    save_user(user)

    reply(event.reply_token,
          "👋 KU-LMS 課題締切通知ボットへようこそ！\n\n"
          "工学院大学の統合認証（GakuNin）の\n"
          "📌 ユーザー名を入力してください。\n\n"
          "例: ab123456\n\n"
          "━━━━━━━━━━\n"
          "⚠️ セキュリティについて\n"
          "入力したパスワードはAES-256で暗号化して\n"
          "サーバーに保存されます。\n"
          "KU-LMSへのログイン以外には使用しません。\n"
          "「解除」でいつでも削除できます。\n\n"
          "━━━━━━━━━━\n"
          "📄 各種規約:\n"
          "利用規約:\n"
          "https://towanoji.github.io/moodle-line-notifier/terms.html\n\n"
          "プライバシーポリシー:\n"
          "https://towanoji.github.io/moodle-line-notifier/privacy.html\n\n"
          "特定商取引法に基づく表記:\n"
          "https://towanoji.github.io/moodle-line-notifier/legal.html")


@handler.add(UnfollowEvent)
def handle_unfollow(event) -> None:
    """ブロック → ユーザーデータを削除"""
    user = get_user(event.source.user_id)
    if user:
        user.state = "BLOCKED"
        user.password_enc = ""   # パスワードは即座に削除
        save_user(user)


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event) -> None:
    uid  = event.source.user_id
    text = event.message.text.strip()
    user = get_or_create_user(uid)

    # ── 意見箱（フィードバック待ち）──
    if user.state == "WAITING_FEEDBACK":
        # 開発者に転送
        if DEVELOPER_LINE_USER_ID:
            push(DEVELOPER_LINE_USER_ID,
                 f"📬 【意見箱】\n"
                 f"ユーザー: {uid}\n\n"
                 f"{text}")
        # 状態を戻す
        user.state = "REGISTERED"
        save_user(user)
        reply(event.reply_token,
              "✅ ご意見ありがとうございます！\n\n"
              "開発者に届けました。\n"
              "いただいたご意見はサービス改善に活かします 🙏\n\n"
              "引き続きご利用ください 📚")
        return

    # ── ユーザー名待ち ──
    if user.state == "NEW":
        # NEW状態では何が来てもユーザー名入力を促す
        user.state = "WAITING_USERNAME"
        save_user(user)
        reply(event.reply_token,
              "📌 ユーザー名を入力してください。\n例: ab123456")
        return

    if user.state == "WAITING_USERNAME":
        user.username = text
        user.state = "WAITING_PASSWORD"
        save_user(user)
        reply(event.reply_token,
              f"✅ ユーザー名: {text}\n\n"
              "🔑 統合認証のパスワードを入力してください。\n\n"
              "⚠️ このメッセージは送信後すぐに処理され、\n"
              "ログ等には残りません。")
        return

    # ── パスワード待ち ──
    if user.state == "WAITING_PASSWORD":
        # まずリプライして「確認中」を伝える（リプライは1回しか使えないため）
        reply(event.reply_token, "⏳ KU-LMS にログイン確認中...\nしばらくお待ちください（最大30秒）")

        # ログイン処理は別スレッドで行い、結果を push で送る
        def _try_login(uid: str, username: str, password: str) -> None:
            import sys
            from datetime import datetime, timedelta, timezone
            from app.lms import login_session_for_user
            from app.stripe_payment import create_checkout_url
            JST = timezone(timedelta(hours=9))
            try:
                print(f"[LOGIN] {username} のログイン試行開始", flush=True)
                login_session_for_user(username, password)
                # 成功
                print(f"[LOGIN] {username} ログイン成功", flush=True)
                user.username      = username
                user.password_enc  = encrypt(password)
                user.temp_username = ""
                user.state         = "REGISTERED"
                user.subscription_status = "trial"
                user.trial_ends_at = datetime.now(tz=JST) + timedelta(days=30)
                save_user(user)

                # Stripe 決済リンクを生成
                from app.stripe_payment import create_checkout_url, create_paypay_checkout_url
                try:
                    checkout_url = create_checkout_url(uid)
                except Exception as e:
                    print(f"[STRIPE] Checkout URL 生成失敗: {e}", file=sys.stderr, flush=True)
                    checkout_url = None
                try:
                    paypay_url = create_paypay_checkout_url(uid)
                except Exception as e:
                    print(f"[STRIPE] PayPay URL 生成失敗: {e}", file=sys.stderr, flush=True)
                    paypay_url = None

                msg = (
                    "✅ 登録完了！\n\n"
                    "毎朝 7:00 と 12:00 に課題の締切を確認して\n"
                    "📅 3日前・1日前・12時間前に通知します。\n\n"
                    "━━━━━━━━━━\n"
                    "🎁 30日間無料トライアル中！\n"
                    "以降は月額199円で継続できます。\n\n"
                    "お支払い方法を選んでください:\n\n"
                )
                if checkout_url:
                    msg += (
                        "💳 クレジットカード（自動継続）:\n"
                        f"{checkout_url}\n\n"
                    )
                if paypay_url:
                    msg += (
                        "💰 PayPay（手動月払い）:\n"
                        f"{paypay_url}\n\n"
                    )
                msg += (
                    "━━━━━━━━━━\n"
                    "使えるコマンド:\n"
                    "「設定」→ 通知タイミングの確認・変更\n"
                    "「意見箱」→ ご意見・ご要望を送る\n"
                    "「解除」→ 登録を削除"
                )
                push(uid, msg)
            except Exception as e:
                # 失敗（詳細ログ）
                print(f"[LOGIN ERROR] {username}: {e}", file=sys.stderr, flush=True)
                user.state = "WAITING_USERNAME"
                user.username = ""
                save_user(user)
                push(uid,
                     "❌ ログインに失敗しました。\n\n"
                     "ユーザー名またはパスワードが正しくない可能性があります。\n\n"
                     "再度、ユーザー名を入力してください。\n"
                     "例: ab123456")

        t = threading.Thread(
            target=_try_login,
            args=(uid, user.username, text),
            daemon=True,
        )
        t.start()
        return

    # ── 登録済みユーザーのコマンド ──
    if user.state == "REGISTERED":

        if text == "設定":
            reply(event.reply_token,
                  "⚙️ 現在の通知設定\n\n"
                  f"📅 日数: {user.notify_days} 日前\n"
                  f"⏰ 時間: {user.notify_hours} 時間前\n\n"
                  "━━━━━━━━━━\n"
                  "変更コマンド:\n"
                  "「日数 3,1」→ 3日前と1日前に通知\n"
                  "「時間 12」  → 12時間前に通知\n"
                  "「時間 なし」→ 時間通知をオフ")
            return

        if text.startswith("日数 "):
            days_str = text.removeprefix("日数 ").strip()
            days = [int(d) for d in days_str.split(",") if d.strip().isdigit()]
            if days:
                user.notify_days = sorted(days, reverse=True)
                save_user(user)
                reply(event.reply_token,
                      f"✅ 日数通知を変更しました。\n📅 {user.notify_days} 日前に通知します。")
            else:
                reply(event.reply_token, "❌ 形式エラー。例: 「日数 3,1」")
            return

        if text.startswith("時間 "):
            hours_str = text.removeprefix("時間 ").strip()
            if hours_str == "なし":
                user.notify_hours = []
                save_user(user)
                reply(event.reply_token, "✅ 時間通知をオフにしました。")
            else:
                hours = [int(h) for h in hours_str.split(",") if h.strip().isdigit()]
                if hours:
                    user.notify_hours = sorted(hours)
                    save_user(user)
                    reply(event.reply_token,
                          f"✅ 時間通知を変更しました。\n⏰ {user.notify_hours} 時間前に通知します。")
                else:
                    reply(event.reply_token, "❌ 形式エラー。例: 「時間 12」")
            return

        if text == "解除":
            user.state = "NEW"
            user.username = ""
            user.password_enc = ""
            save_user(user)
            reply(event.reply_token,
                  "✅ 登録を解除しました。\n\n"
                  "再度登録するには\n"
                  "ユーザー名を入力してください。\n"
                  "例: ab123456")
            return

        if text == "意見箱":
            user.state = "WAITING_FEEDBACK"
            save_user(user)
            reply(event.reply_token,
                  "📬 意見・要望を受け付けます！\n\n"
                  "サービスへのご意見・ご要望・\n"
                  "不具合報告などをそのまま\n"
                  "メッセージで送ってください。\n\n"
                  "開発者に直接届きます 📩\n"
                  "（返信できない場合があります）")
            return

        # その他
        reply(event.reply_token,
              "📌 使えるコマンド:\n"
              "「設定」→ 通知タイミングの確認・変更\n"
              "「意見箱」→ ご意見・ご要望を送る\n"
              "「解除」→ 登録を削除")
        return

    # NEW / 不明な状態
    user.state = "WAITING_USERNAME"
    save_user(user)
    reply(event.reply_token, "📌 ユーザー名を入力してください。\n例: ab123456")
