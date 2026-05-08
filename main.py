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

        # ── 1. Moodle ルートへアクセス → SSO にリダイレクト ──
        # /my/ は notFound になるためルート(/)からアクセスする
        print(f"[INFO] Moodle にアクセス中...")
        page.goto(f"{MOODLE_URL}/", timeout=60_000, wait_until="networkidle")
        print(f"[INFO] リダイレクト先: {page.url}")

        # ── 2. JSでDOM要素を調査 ──
        page.wait_for_timeout(5_000)  # JS描画を十分待つ
        print(f"[DEBUG] ページタイトル: {page.title()}")

        # JS経由でinput要素を全列挙
        inputs_info = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input')).map(el => ({
                type: el.type, name: el.name, id: el.id,
                className: el.className.substring(0,50),
                placeholder: el.placeholder,
                visible: el.offsetParent !== null
            }));
        }""")
        print(f"[DEBUG] input要素: {json.dumps(inputs_info, ensure_ascii=False)}")

        # JS経由でform要素を全列挙
        forms_info = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('form')).map(f => ({
                name: f.name, id: f.id, action: f.action,
                html: f.innerHTML.substring(0, 300)
            }));
        }""")
        print(f"[DEBUG] form要素: {json.dumps(forms_info, ensure_ascii=False)}")

        # すでにMoodleにいる場合はスキップ
        if "M.cfg" in page.content():
            print("[INFO] すでにMoodleにログイン済み")
        else:
            # ── 3. GakuNinモードなら通常ログインに切り替え ──
            # ページが「統合認証（GakuNin）」モードで表示されている場合、
            # 「通常のログイン方法」リンクをクリックしてID/PWフォームを表示させる
            try:
                back_link = page.locator("a:has-text('通常のログイン方法')")
                if back_link.count() > 0:
                    print("[INFO] GakuNinモード検出 → 通常ログインに切り替え")
                    back_link.click(timeout=5_000)
                    page.wait_for_timeout(3_000)  # JS描画を待つ

                    # 切り替え後のinput要素を再確認
                    inputs_info2 = page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('input:not([type="hidden"])')).map(el => ({
                            type: el.type, name: el.name, id: el.id,
                            visible: el.offsetParent !== null
                        }));
                    }""")
                    print(f"[DEBUG] 切り替え後input: {json.dumps(inputs_info2, ensure_ascii=False)}")
            except PwTimeout:
                print("[WARN] 通常ログインリンクのクリックに失敗")

            # ── 4. ユーザー名フィールドを探して入力 ──
            user_selectors = [
                "input[name*='loginId']", "input[name*='login_id']",
                "input[name*='userId']",  "input[name*='user_id']",
                "input[name='username']", "input[name='j_username']",
                "input[type='text']:visible",
            ]
            filled_user = False
            for sel in user_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        page.fill(sel, MOODLE_USERNAME, timeout=3_000)
                        print(f"[INFO] ユーザー名入力: {sel}")
                        filled_user = True
                        break
                except PwTimeout:
                    continue
            if not filled_user:
                print("[WARN] ユーザー名フィールドが見つかりませんでした")

            # ── 5. パスワードを入力 ──
            pw_selectors = [
                "input[type='password']",
                "input[name*='password']", "input[name*='passwd']",
            ]
            for sel in pw_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        page.fill(sel, MOODLE_PASSWORD, timeout=3_000)
                        print(f"[INFO] パスワード入力: {sel}")
                        break
                except PwTimeout:
                    continue

            # ── 6. ログインボタンをクリック ──
            # GakuNinフォームのボタンではなくlginLgirActionFormのボタンを使う
            login_clicked = False
            for sel in [
                "#loginForm button[type='submit']",
                "#loginForm input[type='submit']",
                "form[name='lginLgirActionForm']:not(#gakuninLoginForm) button",
                "input[type='submit']", "button[type='submit']",
                "button:has-text('ログイン')",
            ]:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0:
                        loc.first.click(timeout=3_000)
                        print(f"[INFO] ログインボタンクリック: {sel}")
                        login_clicked = True
                        break
                except PwTimeout:
                    continue
            if not login_clicked:
                print("[WARN] ログインボタンが見つかりませんでした")

            # ── 7. ページ遷移を待つ ──
            try:
                page.wait_for_load_state("networkidle", timeout=30_000)
                print(f"[INFO] ログイン後URL: {page.url}")
            except PwTimeout:
                print(f"[WARN] ページ遷移待ちタイムアウト (URL: {page.url})")

            # ── 7b. ログイン後ページを調査（SID抽出・リンク列挙）──
            if "M.cfg" not in page.content():
                # SIDをURLから抽出
                sid_match = re.search(r';SID=([^#?/]+)', page.url)
                sid = sid_match.group(1) if sid_match else ""
                print(f"[DEBUG] SID: {sid[:10]}...")

                # ページ内の全リンクを列挙
                links = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                        text: a.textContent.trim().substring(0, 60),
                        href: a.href
                    })).filter(l => l.href && !l.href.startsWith('javascript'));
                }""")
                print(f"[DEBUG] ページ内リンク: {json.dumps(links[:40], ensure_ascii=False)}")

                # ネットワークリクエストをキャプチャしながら/lginTpic/へ移動
                captured = []
                page.on("request", lambda r: captured.append(r.url)
                        if any(k in r.url for k in ['json','ajax','api','assign','course','task','kadai'])
                        else None)

                if sid:
                    topics_url = f"{MOODLE_URL}/lginTpic/;SID={sid}"
                    try:
                        page.goto(topics_url, timeout=15_000, wait_until="networkidle")
                        print(f"[DEBUG] トピックページURL: {page.url}")
                        print(f"[DEBUG] トピックHTML(先頭3000字): {page.content()[:3000]}")
                    except Exception as e:
                        print(f"[DEBUG] lginTpic移動エラー: {e}")

                page.wait_for_timeout(3_000)
                print(f"[DEBUG] キャプチャAPIリクエスト: {captured[:30]}")

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
