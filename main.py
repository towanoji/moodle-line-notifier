#!/usr/bin/env python3
"""
工学院大学 KU-LMS 課題締切 LINE通知ツール
=========================================
KU-LMS（UNIVERSAL PASSPORT）に requests で直接ログインし、
課題の締切を取得して LINE 通知します。
Playwright 不要・ブラウザ不要で動作します。

GitHub Actions の cron で毎朝自動実行することを想定しています。
"""

import os
import re
import sys
import json
import requests
from bs4 import BeautifulSoup
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
# ユーティリティ
# ─────────────────────────────────────
def _extract_sid(url: str) -> str:
    """URL パスから ;SID=... を抽出する"""
    m = re.search(r";SID=([^#?/]+)", url)
    return m.group(1) if m else ""


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    })
    return s


# ─────────────────────────────────────
# KU-LMS ログイン補助
# ─────────────────────────────────────
def _get_normal_login_flag(session: requests.Session) -> int | None:
    """
    JS ファイルから backClick() が設定する lginFlag 値を抽出する。
    見つからない場合は None を返す。
    """
    for path in [
        "/lms/js/framework/common.js",
        "/lms/js/framework/parent.js",
        "/lms/js/framework/pager.js",
    ]:
        try:
            resp = session.get(f"{MOODLE_URL}{path}", timeout=10)
            if resp.status_code != 200 or "backClick" not in resp.text:
                continue
            js = resp.text
            print(f"[INFO] JS 取得成功: {path} ({len(js)} bytes)")
            # backClick 関数内の lginFlag 代入を探す
            for pat in [
                r'lginFlagValue["\']?\s*\)\.value\s*=\s*["\']?(\d+)',
                r'getElementById\(["\']lginFlagValue["\'].*?=\s*["\']?(\d+)',
                r'\.lginFlag["\']?\s*=\s*["\']?(\d+)',
                r'lginFlag\b[^=]*=\s*(\d+)',
            ]:
                m = re.search(pat, js)
                if m:
                    val = int(m.group(1))
                    print(f"[INFO] JS から lginFlag={val} を検出")
                    return val
            # backClick 関数の前後50文字にある数字を探す
            bc_match = re.search(r'backClick[\s\S]{0,300}?(\d+)', js)
            if bc_match:
                val = int(bc_match.group(1))
                print(f"[INFO] backClick 近辺から lginFlag={val} を推定")
                return val
        except Exception as e:
            print(f"[DEBUG] JS 取得失敗 ({path}): {e}")
    return None


def _is_login_success(resp_url: str) -> bool:
    """
    レスポンスの最終URLを見てログイン成功かどうか判定する。
    成功 → /lginLgir/login でも /error/ でもない URL に遷移する
    失敗 → /lginLgir/login に戻る or /error/ に飛ぶ
    """
    url = resp_url.split("#")[0]  # フラグメント除去
    if "/lginLgir/login" in url:
        return False
    if "/lginLgir/;" in url:   # ログインページのルート
        return False
    if "/error/" in url:
        return False
    return True


# ─────────────────────────────────────
# KU-LMS ログイン（HTTP requests 方式）
# ─────────────────────────────────────
def login_session() -> tuple[requests.Session, str]:
    """
    KU-LMS に requests で直接 HTTP ログインし、
    (requests.Session, sid) を返す。
    """
    s = _make_session()

    # 1. GET / → ログインページへリダイレクト（SID 付き URL を取得）
    resp = s.get(f"{MOODLE_URL}/", timeout=30, allow_redirects=True)
    print(f"[INFO] 初期URL: {resp.url}")

    sid = _extract_sid(resp.url)
    if not sid:
        raise RuntimeError(f"SID 取得失敗 (URL: {resp.url})")
    print(f"[INFO] SID 取得: {sid[:12]}...")

    # 2. JS から正しい lginFlag 値を取得
    detected_flag = _get_normal_login_flag(s)
    flags_to_try = ([detected_flag] if detected_flag is not None else []) + [0, 1, 3]
    flags_to_try = list(dict.fromkeys(flags_to_try))  # 重複除去

    login_url = f"{MOODLE_URL}/lginLgir/login;SID={sid}"
    referer   = f"{MOODLE_URL}/lginLgir/;SID={sid}"

    # 3. lginFlag を変えながらログイン POST を試みる
    last_resp = None
    for flag in flags_to_try:
        last_resp = s.post(
            login_url,
            data={
                "lginFlag": str(flag),
                "userId":   MOODLE_USERNAME,
                "password": MOODLE_PASSWORD,
            },
            headers={"Referer": referer,
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
            allow_redirects=True,
        )
        final_url = last_resp.url
        print(f"[INFO] lginFlag={flag}: {final_url}")

        if _is_login_success(final_url):
            new_sid = _extract_sid(final_url) or sid
            print(f"[INFO] ✅ ログイン成功 (lginFlag={flag})")
            return s, new_sid

        print(f"[DEBUG] flag={flag}: ログインページに戻った（失敗）")

    # すべて失敗
    print("[ERROR] 全フラグでログイン失敗")
    # エラーメッセージを探す
    if last_resp is not None:
        soup = BeautifulSoup(last_resp.text, "html.parser")
        for el in soup.find_all(class_=re.compile(r"(error|alert|warn|message)", re.I)):
            t = el.get_text(strip=True)
            if t:
                print(f"[DEBUG] ページエラー: {t[:120]}")

    raise RuntimeError(
        "KU-LMS ログイン失敗。\n"
        "【考えられる原因】\n"
        "  1. GitHub Actions の IP が大学側でブロックされている\n"
        "  2. MOODLE_USERNAME / MOODLE_PASSWORD が間違っている\n"
        "  3. 連続失敗によりアカウントが一時ロックされた\n"
        "【解決策】\n"
        "  - セルフホストランナー（キャンパス内 PC）での実行\n"
        "  - 手動でログインして Cookie を Secret に保存する方式への切り替え"
    )


# ─────────────────────────────────────
# KU-LMS 課題取得
# ─────────────────────────────────────
def get_assignments(s: requests.Session, sid: str) -> list[dict]:
    """
    ログイン済みセッションを使ってコース一覧と課題を取得する。
    KU-LMS の API / ページ構造を探索しながら取得する。
    """
    assignments: list[dict] = []

    # ── トピック（コース一覧）ページを取得 ──
    topics_url = f"{MOODLE_URL}/lginTpic/;SID={sid}"
    resp = s.get(topics_url, timeout=30, allow_redirects=True)
    print(f"[INFO] トピックURL: {resp.url}")

    if "error" in resp.url or resp.status_code >= 400:
        print(f"[WARN] トピックページ取得失敗: {resp.url}")
        return assignments

    soup = BeautifulSoup(resp.text, "html.parser")

    # デバッグ: ページタイトルとリンク一覧を出力
    title = soup.find("title")
    print(f"[DEBUG] トピックページタイトル: {title.string if title else '不明'}")

    links = [(a.get_text(strip=True), a.get("href", ""))
             for a in soup.find_all("a", href=True)
             if not a["href"].startswith("javascript")]
    print(f"[DEBUG] リンク数: {len(links)}")
    for text, href in links[:30]:
        print(f"  [{text[:40]}] → {href[:80]}")

    # ── 課題・締切情報を探す（よくある class 名で試す）──
    deadline_candidates = soup.find_all(
        class_=re.compile(r"(assign|deadline|kadai|due|task|report)", re.I)
    )
    print(f"[DEBUG] 締切候補要素数: {len(deadline_candidates)}")
    for el in deadline_candidates[:10]:
        print(f"  {el.get_text(strip=True)[:120]}")

    # ── 日付っぽいテキストを含む要素を探す ──
    date_pattern = re.compile(r"\d{4}[/\-年]\d{1,2}[/\-月]\d{1,2}")
    date_elements = [el for el in soup.find_all(string=date_pattern)]
    print(f"[DEBUG] 日付含む要素数: {len(date_elements)}")
    for el in date_elements[:20]:
        print(f"  {str(el).strip()[:100]}")

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
    missing = [k for k, v in [
        ("MOODLE_URL",               MOODLE_URL),
        ("MOODLE_USERNAME",          MOODLE_USERNAME),
        ("MOODLE_PASSWORD",          MOODLE_PASSWORD),
        ("LINE_CHANNEL_ACCESS_TOKEN", LINE_TOKEN),
        ("LINE_USER_ID",             LINE_USER_ID),
    ] if not v]
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

    session, sid = login_session()
    print("✅ KU-LMS ログイン成功")

    all_assignments = get_assignments(session, sid)
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
