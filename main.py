#!/usr/bin/env python3
"""
工学院大学 Moodle 課題締切 LINE通知ツール
=========================================
Playwright で SSO（UNIVERSAL PASSPORT）認証を行い、
Moodle AJAX API で課題の締切を取得して LINE 通知します。

GitHub Actions の cron で毎朝自動実行することを想定しています。
"""

import os
import re
import sys
import json
import requests
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────
# 設定（GitHub Secrets / .env から読み込み）
# ─────────────────────────────────────
MOODLE_URL      = os.environ.get("MOODLE_URL", "").rstrip("/")
MOODLE_USERNAME = os.environ.get("MOODLE_USERNAME", "")
MOODLE_PASSWORD = os.environ.get("MOODLE_PASSWORD", "")

LINE_TOKEN   = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")

NOTIFY_DAYS = [
    int(d.strip())
    for d in os.environ.get("NOTIFY_DAYS_BEFORE", "1").split(",")
    if d.strip().isdigit()
]

JST = timezone(timedelta(hours=9))


# ─────────────────────────────────────
# Playwright ログイン（SSO 対応）
# ─────────────────────────────────────
def login_moodle_session() -> tuple[requests.Session, str, str]:
    """
    Playwright でブラウザを操作して SSO ログインし、
    (requests.Session, sesskey, userid) を返す。
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = ctx.new_page()

        # ── 1. Moodle のダッシュボードへアクセス → SSO にリダイレクト ──
        print(f"[INFO] Moodle にアクセス中...")
        page.goto(f"{MOODLE_URL}/my/", timeout=60_000)
        print(f"[INFO] リダイレクト先: {page.url}")

        # ── 2. パスワード入力欄が現れるまで待つ（最大20秒）──
        try:
            page.wait_for_selector("input[type='password']", timeout=20_000)
            print("[INFO] ログインフォーム検出")
        except PwTimeout:
            print(f"[WARN] パスワード欄が見つかりません。現在URL: {page.url}")
            # すでに Moodle にいる場合はそのまま続行
            if "M.cfg" not in page.content():
                browser.close()
                raise RuntimeError(
                    f"ログインフォームが表示されませんでした (URL: {page.url})"
                )

        # ── 3. ユーザー名を入力（フィールド名をいくつか試す）──
        if page.locator("input[type='password']").count() > 0:
            for sel in [
                "input[name*='loginId']",
                "input[name*='login_id']",
                "input[name='username']",
                "input[name='j_username']",
                "input[type='text']:visible",
            ]:
                try:
                    if page.locator(sel).count() > 0:
                        page.fill(sel, MOODLE_USERNAME, timeout=3_000)
                        print(f"[INFO] ユーザー名入力: {sel}")
                        break
                except PwTimeout:
                    continue

            # ── 4. パスワードを入力 ──
            page.fill("input[type='password']", MOODLE_PASSWORD)
            print("[INFO] パスワード入力完了")

            # ── 5. ログインボタンをクリック ──
            for sel in [
                "input[type='submit']",
                "button[type='submit']",
                "button:has-text('ログイン')",
                "button:has-text('Login')",
                "button:has-text('サインイン')",
            ]:
                try:
                    if page.locator(sel).count() > 0:
                        page.click(sel, timeout=3_000)
                        print(f"[INFO] ログインボタンクリック: {sel}")
                        break
                except PwTimeout:
                    continue

            # ── 6. Moodle ダッシュボードへの遷移を待つ ──
            try:
                page.wait_for_function(
                    "() => typeof M !== 'undefined' && typeof M.cfg !== 'undefined'",
                    timeout=30_000,
                )
                print(f"[INFO] ログイン成功: {page.url}")
            except PwTimeout:
                print(f"[WARN] Moodle への遷移待ちタイムアウト (URL: {page.url})")

        # ── 7. sesskey / userid を取得 ──
        content = page.content()
        sesskey_m = re.search(r'"sesskey"\s*:\s*"([^"]+)"', content)
        userid_m  = re.search(r'"userid"\s*:\s*(\d+)',      content)

        if not sesskey_m:
            browser.close()
            raise RuntimeError(
                f"sesskey が取得できません。ログインに失敗した可能性があります (URL: {page.url})"
            )

        sesskey = sesskey_m.group(1)
        userid  = userid_m.group(1) if userid_m else None

        # ── 8. ブラウザのクッキーを requests.Session に移植 ──
        session = requests.Session()
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
        for ck in ctx.cookies():
            session.cookies.set(ck["name"], ck["value"])

        browser.close()
        print(f"[INFO] 認証完了 (userid={userid})")
        return session, sesskey, userid


# ─────────────────────────────────────
# Moodle AJAX API
# ─────────────────────────────────────
def _ajax_call(session: requests.Session, sesskey: str,
               methodname: str, args: dict) -> dict | list:
    """Moodle の AJAX API エンドポイントを呼び出す"""
    url = f"{MOODLE_URL}/lib/ajax/service.php?sesskey={sesskey}&info={methodname}"
    resp = session.post(
        url,
        data=json.dumps([{"index": 0, "methodname": methodname, "args": args}]),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()

    if not result:
        raise RuntimeError(f"AJAX API 空レスポンス [{methodname}]")
    if result[0].get("error"):
        raise RuntimeError(f"AJAX API エラー [{methodname}]: {result[0]}")

    return result[0]["data"]


def get_assignments(session: requests.Session, sesskey: str, userid: str) -> list[dict]:
    """履修中の全コースから締切付き課題を取得する"""
    courses = _ajax_call(session, sesskey, "core_enrol_get_users_courses",
                         {"userid": int(userid)})
    if not courses:
        return []

    course_ids = [c["id"] for c in courses]
    result = _ajax_call(session, sesskey, "mod_assign_get_assignments",
                        {"courseids": course_ids, "capabilities": []})

    assignments = []
    for course in result.get("courses", []):
        for assign in course.get("assignments", []):
            due_ts = assign.get("duedate", 0)
            if due_ts and due_ts > 0:
                assignments.append({
                    "course":  course["fullname"],
                    "name":    assign["name"],
                    "duedate": datetime.fromtimestamp(due_ts, tz=JST),
                })
    return assignments


# ─────────────────────────────────────
# LINE Messaging API
# ─────────────────────────────────────
def send_line_message(text: str) -> bool:
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_TOKEN}",
        },
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": text}]},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"[ERROR] LINE送信失敗: {resp.status_code} {resp.text}", file=sys.stderr)
        return False
    return True


# ─────────────────────────────────────
# 設定値の検証
# ─────────────────────────────────────
def validate_config() -> None:
    missing = []
    if not MOODLE_URL:      missing.append("MOODLE_URL")
    if not MOODLE_USERNAME: missing.append("MOODLE_USERNAME")
    if not MOODLE_PASSWORD: missing.append("MOODLE_PASSWORD")
    if not LINE_TOKEN:      missing.append("LINE_CHANNEL_ACCESS_TOKEN")
    if not LINE_USER_ID:    missing.append("LINE_USER_ID")
    if missing:
        print(f"[ERROR] 環境変数が未設定: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────
# メイン処理
# ─────────────────────────────────────
def main() -> None:
    validate_config()

    today = datetime.now(tz=JST).date()
    print(f"[{today}] 課題チェック開始 | 通知タイミング: {NOTIFY_DAYS} 日前")

    session, sesskey, userid = login_moodle_session()
    print("✅ Moodle ログイン成功")

    all_assignments = get_assignments(session, sesskey, userid)
    print(f"✅ 取得した課題数: {len(all_assignments)} 件")

    to_notify = []
    for a in all_assignments:
        days_left = (a["duedate"].date() - today).days
        if days_left in NOTIFY_DAYS:
            to_notify.append({**a, "days_left": days_left})

    if not to_notify:
        print("📭 通知対象の課題はありませんでした")
        return

    to_notify.sort(key=lambda x: x["duedate"])
    lines = [f"📢 課題の締切通知 [{today.strftime('%Y/%m/%d')}]"]
    for a in to_notify:
        if a["days_left"] == 0:
            timing = "⚠️ 今日が締切！"
        elif a["days_left"] == 1:
            timing = "🔴 明日締切"
        else:
            timing = f"🟡 あと {a['days_left']} 日"
        lines.append(
            f"\n━━━━━━━━━━\n"
            f"📘 {a['course']}\n"
            f"📝 {a['name']}\n"
            f"⏰ {a['duedate'].strftime('%m/%d(%a) %H:%M')} {timing}"
        )

    if send_line_message("\n".join(lines)):
        print(f"✅ LINE通知送信完了（{len(to_notify)} 件）")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
