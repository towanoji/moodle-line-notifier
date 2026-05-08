#!/usr/bin/env python3
"""
工学院大学 Moodle 課題締切 LINE通知ツール
=========================================
Moodle へのウェブログイン（SSO対応）後、AJAX API で課題の締切を取得し、
LINE Messaging API でプッシュ通知を送ります。

GitHub Actions の cron で毎朝自動実行することを想定しています。
"""

import os
import re
import sys
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────
# 設定（GitHub Secrets / .env から読み込み）
# ─────────────────────────────────────
MOODLE_URL      = os.environ.get("MOODLE_URL", "").rstrip("/")
MOODLE_USERNAME = os.environ.get("MOODLE_USERNAME", "")
MOODLE_PASSWORD = os.environ.get("MOODLE_PASSWORD", "")

LINE_TOKEN      = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID    = os.environ.get("LINE_USER_ID", "")

# 何日前に通知するか（カンマ区切りで複数指定可: "1,3,7"）
NOTIFY_DAYS = [
    int(d.strip())
    for d in os.environ.get("NOTIFY_DAYS_BEFORE", "1").split(",")
    if d.strip().isdigit()
]

JST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ─────────────────────────────────────
# Moodle ウェブログイン（SSO対応）
# ─────────────────────────────────────
def _get_form_fields(soup: BeautifulSoup) -> tuple[str, dict]:
    """フォームのaction URLと入力フィールド辞書を返す"""
    form = soup.find("form")
    if not form:
        return "", {}

    action = form.get("action", "")
    fields = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name and inp.get("type", "").lower() != "submit":
            fields[name] = inp.get("value", "")

    return action, fields


def _fill_credentials(fields: dict) -> dict:
    """フォームのどのフィールドがID/パスワードか推測して埋める"""
    filled = dict(fields)

    # ユーザー名フィールドを探す（優先度順）
    user_keys = ["username", "loginid", "j_username", "login", "userid", "id", "user"]
    for key in list(filled.keys()):
        if any(uk in key.lower() for uk in user_keys):
            filled[key] = MOODLE_USERNAME
            break

    # パスワードフィールドを探す
    pass_keys = ["password", "j_password", "pass", "passwd"]
    for key in list(filled.keys()):
        if any(pk in key.lower() for pk in pass_keys):
            filled[key] = MOODLE_PASSWORD
            break

    return filled


def login_moodle_session() -> tuple[requests.Session, str, str]:
    """
    Moodle にウェブログインし (session, sesskey, userid) を返す。

    ① Moodle ネイティブログイン（/login/index.php）
    ② SSO リダイレクト（UNIVERSAL PASSPORT 等）に自動対応
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    moodle_host = urlparse(MOODLE_URL).netloc

    DEBUG = os.environ.get("MOODLE_DEBUG", "0") == "1"

    # ── STEP 1: ログインページを取得（SSO リダイレクトに追従）──
    resp = session.get(f"{MOODLE_URL}/login/index.php", allow_redirects=True, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    if DEBUG:
        print(f"[DEBUG] Step1 URL: {resp.url}")
        action0, fields0 = _get_form_fields(soup)
        print(f"[DEBUG] Step1 form action: {action0}")
        print(f"[DEBUG] Step1 form fields: {list(fields0.keys())}")

    # ── STEP 2: ログインフォームに認証情報を送信（最大5回リダイレクト対応）──
    for step in range(5):
        action, fields = _get_form_fields(soup)
        if not action:
            if DEBUG:
                print(f"[DEBUG] Step{step+2}: フォームなし → 終了")
            break

        # action が相対URLなら絶対URLに変換
        if not action.startswith("http"):
            action = urljoin(resp.url, action)

        fields = _fill_credentials(fields)
        if DEBUG:
            print(f"[DEBUG] Step{step+2} POST → {action}")
            print(f"[DEBUG] Step{step+2} fields: {list(fields.keys())}")

        resp = session.post(action, data=fields, allow_redirects=True, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")

        if DEBUG:
            print(f"[DEBUG] Step{step+2} response URL: {resp.url}")
            print(f"[DEBUG] Step{step+2} M.cfg present: {'M.cfg' in resp.text}")

        # Moodle のダッシュボードに到達したか確認
        if urlparse(resp.url).netloc == moodle_host and "M.cfg" in resp.text:
            break

    # ── STEP 3: ログイン成功確認 ──
    if "M.cfg" not in resp.text:
        if DEBUG:
            title = soup.find("title")
            print(f"[DEBUG] 最終ページタイトル: {title.text if title else 'N/A'}")
            print(f"[DEBUG] 最終URL: {resp.url}")
        raise RuntimeError(
            "Moodle ログイン失敗。MOODLE_USERNAME / MOODLE_PASSWORD を確認してください。"
        )

    # ── STEP 4: sesskey と userid を抽出 ──
    sesskey_m = re.search(r'"sesskey"\s*:\s*"([^"]+)"', resp.text)
    userid_m  = re.search(r'"userid"\s*:\s*(\d+)',      resp.text)

    if not sesskey_m:
        raise RuntimeError("sesskey の取得に失敗しました。")

    sesskey = sesskey_m.group(1)
    userid  = userid_m.group(1) if userid_m else None

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

    # ① 履修コース一覧
    courses = _ajax_call(session, sesskey, "core_enrol_get_users_courses",
                         {"userid": int(userid)})
    if not courses:
        return []

    # ② 全コースの課題を一括取得
    course_ids = [c["id"] for c in courses]
    result = _ajax_call(session, sesskey, "mod_assign_get_assignments",
                        {"courseids": course_ids, "capabilities": []})

    assignments = []
    for course in result.get("courses", []):
        for assign in course.get("assignments", []):
            due_ts = assign.get("duedate", 0)
            if due_ts and due_ts > 0:
                assignments.append({
                    "course":   course["fullname"],
                    "name":     assign["name"],
                    "duedate":  datetime.fromtimestamp(due_ts, tz=JST),
                })

    return assignments


# ─────────────────────────────────────
# LINE Messaging API
# ─────────────────────────────────────
def send_line_message(text: str) -> bool:
    """LINE Messaging API でプッシュメッセージを送信する"""
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_TOKEN}",
        },
        json={
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": text}],
        },
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
        print(f"[ERROR] 以下の環境変数が設定されていません: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────
# メイン処理
# ─────────────────────────────────────
def main() -> None:
    validate_config()

    today = datetime.now(tz=JST).date()
    print(f"[{today}] 課題チェック開始 | 通知タイミング: {NOTIFY_DAYS} 日前")

    # 1. Moodle にウェブログイン
    session, sesskey, userid = login_moodle_session()
    print("✅ Moodle ログイン成功")

    # 2. 全課題を取得
    all_assignments = get_assignments(session, sesskey, userid)
    print(f"✅ 取得した課題数: {len(all_assignments)} 件")

    # 3. 通知対象（締切まで指定日数の課題）を抽出
    to_notify = []
    for a in all_assignments:
        days_left = (a["duedate"].date() - today).days
        if days_left in NOTIFY_DAYS:
            to_notify.append({**a, "days_left": days_left})

    if not to_notify:
        print("📭 通知対象の課題はありませんでした")
        return

    # 4. メッセージを組み立てて送信
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

    message = "\n".join(lines)

    if send_line_message(message):
        print(f"✅ LINE通知送信完了（{len(to_notify)} 件）")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
