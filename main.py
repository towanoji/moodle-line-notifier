#!/usr/bin/env python3
"""
工学院大学 Moodle 課題締切 LINE通知ツール
=========================================
Moodle Web Service API で課題の締切を取得し、
LINE Messaging API でプッシュ通知を送ります。

GitHub Actions の cron で毎朝自動実行することを想定しています。
"""

import os
import sys
import requests
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


# ─────────────────────────────────────
# Moodle API
# ─────────────────────────────────────
def _moodle_post(token: str, function: str, extra: dict | None = None) -> dict | list:
    """Moodle Web Service REST API を呼び出す共通関数"""
    payload = {
        "wstoken": token,
        "wsfunction": function,
        "moodlewsrestformat": "json",
    }
    if extra:
        payload.update(extra)

    resp = requests.post(
        f"{MOODLE_URL}/webservice/rest/server.php",
        data=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    # エラーレスポンスの検出
    if isinstance(data, dict) and "exception" in data:
        raise RuntimeError(
            f"Moodle API エラー [{function}]: {data.get('message', data)}"
        )
    return data


def get_moodle_token() -> str:
    """ユーザー名・パスワードでログインし、APIトークンを取得する"""
    resp = requests.post(
        f"{MOODLE_URL}/login/token.php",
        data={
            "username": MOODLE_USERNAME,
            "password": MOODLE_PASSWORD,
            "service": "moodle_mobile_app",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if "token" not in data:
        error_msg = data.get("error", str(data))
        raise RuntimeError(f"Moodle ログイン失敗: {error_msg}")

    return data["token"]


def get_assignments(token: str) -> list[dict]:
    """履修中の全コースから締切付き課題を取得する"""
    # ① 自分のユーザーIDを取得
    site_info = _moodle_post(token, "core_webservice_get_site_info")
    user_id = site_info["userid"]

    # ② 履修コース一覧を取得
    courses = _moodle_post(
        token,
        "core_enrol_get_users_courses",
        {"userid": user_id},
    )
    if not courses:
        return []

    # ③ 全コースの課題を一括取得
    extra = {f"courseids[{i}]": c["id"] for i, c in enumerate(courses)}
    result = _moodle_post(token, "mod_assign_get_assignments", extra)

    assignments = []
    for course in result.get("courses", []):
        for assign in course.get("assignments", []):
            due_ts = assign.get("duedate", 0)
            if due_ts and due_ts > 0:
                assignments.append({
                    "course": course["fullname"],
                    "name":   assign["name"],
                    "duedate": datetime.fromtimestamp(due_ts, tz=JST),
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

    # 1. Moodle にログインしてトークン取得
    token = get_moodle_token()
    print("✅ Moodle ログイン成功")

    # 2. 全課題を取得
    all_assignments = get_assignments(token)
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
