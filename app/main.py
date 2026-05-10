"""
main.py  ―  FastAPI エントリポイント
=========================================
エンドポイント:
  POST /webhook        … LINE Messaging API のウェブフック受信
  POST /stripe/webhook … Stripe 決済イベント受信
  GET  /stripe/success … 決済成功後のリダイレクト先
  GET  /stripe/cancel  … 決済キャンセル後のリダイレクト先
  GET  /health         … ヘルスチェック
"""

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse

from app.line_bot import handler, push
from app.scheduler import start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield


app = FastAPI(
    title="KU-LMS 課題締切通知サービス",
    lifespan=lifespan,
)


# ─────────────────────────────────────
# LINE Webhook
# ─────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    """LINE Messaging API からのウェブフックを受け取る"""
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok"}


# ─────────────────────────────────────
# Stripe Webhook
# ─────────────────────────────────────

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe からの決済イベントを受け取る"""
    from app.stripe_payment import handle_webhook_event
    from app.models import get_user, save_user

    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = handle_webhook_event(payload, sig_header)
    except ValueError as e:
        print(f"[STRIPE WEBHOOK ERROR] {e}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=400, detail=str(e))

    print(f"[STRIPE] イベント受信: {event['type']}", flush=True)

    # ── 決済完了（サブスク開始）──
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        line_user_id = session.get("metadata", {}).get("line_user_id", "")
        customer_id  = session.get("customer", "")

        user = get_user(line_user_id)
        if user:
            user.stripe_customer_id  = customer_id
            user.subscription_status = "active"
            save_user(user)
            push(line_user_id,
                 "✅ カードの登録が完了しました！\n\n"
                 "14日間の無料トライアル終了後、\n"
                 "月額200円で自動継続されます。\n"
                 "引き続きお使いください 📚")
            print(f"[STRIPE] {line_user_id} → active", flush=True)

    # ── サブスクリプション停止 ──
    elif event["type"] == "customer.subscription.deleted":
        customer_id = event["data"]["object"].get("customer", "")
        # customer_id からユーザーを検索
        from app.models import _store
        for user in _store.values():
            if user.stripe_customer_id == customer_id:
                user.subscription_status = "cancelled"
                save_user(user)
                push(user.line_user_id,
                     "⚠️ サブスクリプションが停止されました。\n\n"
                     "「サブスク」と送ると再登録できます。")
                print(f"[STRIPE] {user.line_user_id} → cancelled", flush=True)
                break

    # ── 支払い失敗 ──
    elif event["type"] == "invoice.payment_failed":
        customer_id = event["data"]["object"].get("customer", "")
        from app.models import _store
        for user in _store.values():
            if user.stripe_customer_id == customer_id:
                push(user.line_user_id,
                     "❌ 支払いに失敗しました。\n\n"
                     "カード情報をご確認ください。\n"
                     "「サブスク」と送ると再登録できます。")
                break

    return {"status": "ok"}


# ─────────────────────────────────────
# Stripe リダイレクト先
# ─────────────────────────────────────

@app.get("/stripe/success", response_class=HTMLResponse)
async def stripe_success():
    return """
    <html><body style="font-family:sans-serif;text-align:center;padding:60px">
    <h1>✅ カード登録完了！</h1>
    <p>LINEに確認メッセージを送りました。<br>このページは閉じてください。</p>
    </body></html>
    """


@app.get("/stripe/cancel", response_class=HTMLResponse)
async def stripe_cancel():
    return """
    <html><body style="font-family:sans-serif;text-align:center;padding:60px">
    <h1>キャンセルしました</h1>
    <p>後からLINEで「サブスク」と送ると再度登録できます。<br>このページは閉じてください。</p>
    </body></html>
    """


# ─────────────────────────────────────
# ヘルスチェック
# ─────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}
