"""
stripe_payment.py  ―  Stripe 決済連携
========================================
フロー（クレジットカード）:
  1. ユーザー登録完了 → Stripe Checkout URL を LINE で送信
  2. ユーザーがカードを登録（30日間の無料トライアル付き）
  3. 30日後に自動で月額199円が課金される

フロー（PayPay・手動月払い）:
  1. トライアル終了3日前 → PayPay支払いリンクを LINE で送信
  2. ユーザーが199円を手動で支払い
  3. Stripe Webhook で trial_ends_at を30日延長
"""

import os
import stripe
from datetime import datetime, timedelta, timezone

stripe.api_key      = os.environ.get("STRIPE_SECRET_KEY", "")
PRICE_ID            = os.environ.get("STRIPE_PRICE_ID", "")
PAYPAY_PRICE_ID     = os.environ.get("PAYPAY_PRICE_ID", "")
WEBHOOK_SECRET      = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
APP_URL             = os.environ.get("APP_URL", "https://ku-lms-notifier.onrender.com")
JST                 = timezone(timedelta(hours=9))

TRIAL_DAYS = 30


def create_checkout_url(line_user_id: str, customer_id: str = "") -> str:
    """クレジットカード用: サブスクリプション Checkout URL を返す"""
    params: dict = {
        "mode": "subscription",
        "line_items": [{"price": PRICE_ID, "quantity": 1}],
        "subscription_data": {"trial_period_days": TRIAL_DAYS},
        "success_url": f"{APP_URL}/stripe/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url":  f"{APP_URL}/stripe/cancel",
        "metadata":    {"line_user_id": line_user_id},
        "locale":      "ja",
    }
    if customer_id:
        params["customer"] = customer_id

    session = stripe.checkout.Session.create(**params)
    return session.url


def create_paypay_checkout_url(line_user_id: str) -> str:
    """PayPay用: 一回払い199円の Checkout URL を返す"""
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": PAYPAY_PRICE_ID, "quantity": 1}],
        payment_method_types=["paypay"],
        success_url=f"{APP_URL}/stripe/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_URL}/stripe/cancel",
        metadata={"line_user_id": line_user_id, "payment_type": "paypay_monthly"},
        locale="ja",
    )
    return session.url


def handle_webhook_event(payload: bytes, sig_header: str) -> stripe.Event:
    """
    Stripe Webhook のシグネチャを検証してイベントを返す。
    検証失敗時は ValueError を送出。
    """
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError as e:
        raise ValueError(f"Webhook シグネチャ検証失敗: {e}")
    return event
