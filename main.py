#!/usr/bin/env python3
"""
工学院大学 KU-LMS 課題締切 LINE通知ツール
=========================================
KU-LMS（UNIVERSAL PASSPORT）に GakuNin/統合認証（SAML）でログインし、
課題の締切を取得して LINE 通知します。

GitHub Actions の cron で毎朝自動実行することを想定しています。
"""

import os
import re
import sys
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

# ─────────────────────────────────────
# 設定（GitHub Secrets から読み込み）
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


def _abs_url(base: str, path: str) -> str:
    if path.startswith("http"):
        return path
    return urljoin(base, path)


# ─────────────────────────────────────
# GakuNin SAML ログイン
# ─────────────────────────────────────
def _login_gakunin(
    s: requests.Session, sid: str, lms_base: str
) -> tuple[str, str, requests.Response]:
    """
    GakuNin SAML フローでログインし、(new_sid, lms_base, final_resp) を返す。
    final_resp はログイン後の最終ページのレスポンス。
    """
    login_root      = f"{lms_base}/lginLgir/;SID={sid}"
    gakunin_forward = f"{lms_base}/lginLgir/gakuninForward;SID={sid}"

    print(f"[INFO] GakuNin フォワード: {gakunin_forward}")
    resp = s.post(
        gakunin_forward,
        data={"lginFlag": "2", "guest": "", "shortUrl": ""},
        headers={"Referer": login_root,
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
        allow_redirects=True,
    )
    print(f"[INFO] → {resp.url[:100]}  ({resp.status_code})")

    for step in range(12):
        soup = BeautifulSoup(resp.text, "html.parser")
        url  = resp.url
        print(f"[INFO] Step{step+1}: {url[:100]}")

        # ─ LMS 本体に戻ってきたら成功 ─
        if (lms_base in url and
                "/lginLgir/" not in url and
                "/error/" not in url):
            new_sid = _extract_sid(url) or sid
            print(f"[INFO] ✅ LMS ログイン成功: {url}")
            return new_sid, lms_base, resp

        # ─ study-auth の CoursePower ページ ─
        # ログイン完了後の SSO 中継ページ。LMS への遷移先を探す。
        if "study-auth" in url or "eduapi" in url:
            body_text = soup.get_text(" ", strip=True)
            print(f"[DEBUG] CoursePower テキスト: {body_text[:300]}")

            # ページ内の全リンクを出力
            links_on_page = [(a.get_text(strip=True), a.get("href", ""))
                             for a in soup.find_all("a", href=True)]
            print(f"[DEBUG] リンク数: {len(links_on_page)}")
            for t, h in links_on_page[:20]:
                print(f"  [{t[:40]}] → {h[:80]}")

            # LMS へ戻るリンクを探す
            for _, href in links_on_page:
                if lms_base in href and "/lginLgir/" not in href:
                    print(f"[INFO] LMS リンク発見: {href}")
                    resp = s.get(href, timeout=30, allow_redirects=True)
                    break
            else:
                # フォームがあれば送信
                form = soup.find("form")
                if form:
                    action = _abs_url(url, form.get("action", url))
                    data = {inp.get("name", ""): inp.get("value", "")
                            for inp in form.find_all("input") if inp.get("name")}
                    print(f"[INFO] CoursePower フォーム送信: {action}")
                    resp = s.post(action, data=data,
                                  headers={"Referer": url},
                                  timeout=30, allow_redirects=True)
                else:
                    # LMS ルートへ直接アクセスして新しいセッションを取得
                    print(f"[INFO] LMS ルートへ直接アクセス: {lms_base}/")
                    resp = s.get(f"{lms_base}/", timeout=30, allow_redirects=True)
            continue

        # ─ meta refresh ─
        meta = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
        if meta:
            content = meta.get("content", "")
            m = re.search(r"URL=(.+)", content, re.I)
            if m:
                refresh_url = _abs_url(url, m.group(1).strip().rstrip('"\''))
                print(f"[INFO] meta refresh → {refresh_url[:100]}")
                resp = s.get(refresh_url, timeout=30, allow_redirects=True)
                continue

        # ─ SAMLResponse フォーム → SP へ転送 ─
        for f in soup.find_all("form"):
            data = {inp.get("name", ""): inp.get("value", "")
                    for inp in f.find_all("input") if inp.get("name")}
            if "SAMLResponse" in data:
                action = _abs_url(url, f.get("action", url))
                print(f"[INFO] SAMLResponse → {action}")
                resp = s.post(action, data=data,
                              headers={"Referer": url},
                              timeout=30, allow_redirects=True)
                break
        else:
            # ─ ログインフォーム ─
            login_form = None
            for f in soup.find_all("form"):
                data = {inp.get("name", ""): inp.get("value", "")
                        for inp in f.find_all("input") if inp.get("name")}
                names_lower = {k.lower() for k in data}
                pwd_like  = {"password", "passwd", "j_password", "pass"}
                user_like = {"username", "j_username", "uid", "userid", "user"}
                if names_lower & (pwd_like | user_like):
                    login_form = (f, data)
                    break

            if login_form:
                f, data = login_form
                # ユーザー名フィールド（名前優先）
                user_like = {"username", "j_username", "uid", "userid", "user"}
                for inp in f.find_all("input"):
                    if inp.get("name", "").lower() in user_like:
                        data[inp.get("name")] = MOODLE_USERNAME
                        print(f"[INFO] ユーザーIDフィールド: {inp.get('name')}")
                        break
                else:
                    for inp in f.find_all("input"):
                        n = inp.get("name", "")
                        if inp.get("type", "text") == "text" and n and n.lower() != "dummy":
                            data[n] = MOODLE_USERNAME
                            break
                # パスワードフィールド
                for inp in f.find_all("input", type="password"):
                    data[inp.get("name")] = MOODLE_PASSWORD
                    print(f"[INFO] パスワードフィールド: {inp.get('name')}")
                    break

                action = _abs_url(url, f.get("action", url))
                method = f.get("method", "post").lower()
                print(f"[INFO] ログインフォーム送信: {method.upper()} {action}")
                print(f"[DEBUG]   fields={list(data.keys())}")
                if method == "get":
                    resp = s.get(action, params=data, timeout=30, allow_redirects=True)
                else:
                    resp = s.post(action, data=data,
                                  headers={"Referer": url,
                                           "Content-Type": "application/x-www-form-urlencoded"},
                                  timeout=30, allow_redirects=True)
                continue

            # ─ フォームもリダイレクトもない ─
            body_text = soup.get_text(" ", strip=True)
            print(f"[DEBUG] ページテキスト: {body_text[:300]}")
            for el in soup.find_all(class_=re.compile(r"(error|alert|warn|msg)", re.I)):
                t = el.get_text(strip=True)
                if t:
                    print(f"[WARN] {t[:150]}")
            break

    raise RuntimeError("GakuNin ログイン失敗（フロー完了せず）")


# ─────────────────────────────────────
# KU-LMS ログイン（メインエントリ）
# ─────────────────────────────────────
def login_session() -> tuple[requests.Session, str, str, requests.Response]:
    """(session, sid, lms_base, final_resp) を返す"""
    s = _make_session()

    resp = s.get(f"{MOODLE_URL}/", timeout=30, allow_redirects=True)
    login_page_url = resp.url.split("#")[0]
    print(f"[INFO] ログインページ: {login_page_url}")

    sid = _extract_sid(login_page_url)
    if not sid:
        raise RuntimeError(f"SID 取得失敗 (URL: {login_page_url})")
    print(f"[INFO] SID: {sid[:12]}...")

    lms_m = re.match(r"(https?://[^/]+(?:/[^/]+)*)/lginLgir/", login_page_url)
    lms_base = lms_m.group(1) if lms_m else MOODLE_URL
    print(f"[INFO] LMS ベース: {lms_base}")

    new_sid, lms_base, final_resp = _login_gakunin(s, sid, lms_base)
    return s, new_sid, lms_base, final_resp


# ─────────────────────────────────────
# KU-LMS 課題取得
# ─────────────────────────────────────
def get_assignments(
    s: requests.Session,
    sid: str,
    lms_base: str,
    start_resp: requests.Response | None = None,
) -> list[dict]:
    """
    課題一覧ページ (/lms/klmsKlil/) から締切課題を取得する。
    """
    assignments: list[dict] = []

    # ログイン後の最終ページから新しい SID を取得
    actual_sid = sid
    if start_resp is not None:
        new_sid = _extract_sid(start_resp.url)
        if new_sid:
            actual_sid = new_sid
            print(f"[INFO] 新SID取得: {actual_sid[:12]}...")

    # ── 課題一覧ページ (/lms/klmsKlil/) を取得 ──
    kadai_url = f"{lms_base}/klmsKlil/;SID={actual_sid}"
    print(f"[INFO] 課題一覧URL: {kadai_url}")
    resp = s.get(kadai_url, timeout=30, allow_redirects=True)
    print(f"[INFO] → {resp.url}")

    if "/error/" in resp.url or resp.status_code >= 400:
        print(f"[WARN] 課題一覧取得失敗: {resp.url}")
        return assignments

    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.find("title")
    print(f"[DEBUG] ページタイトル: {title.string if title else '不明'}")
    print(f"[DEBUG] サイズ: {len(resp.text)} bytes")

    # ── JavaScript の全内容から dispKlaf 関連を抽出 ──
    js_urls: list[str] = []
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        # dispKlaf に関連する行を表示
        for line in text.splitlines():
            if "dispKlaf" in line or "klmsKlaf" in line or "klmsKlal" in line:
                print(f"[DEBUG] JS: {line.strip()[:200]}")
        # URL パターンを収集（SID あり）
        for m in re.finditer(
            r"""['"]([^'"]*(?:klms|klaf|Klil|Klaf|Klal|tpic|kadai)[^'"]*;SID=[^'"]+)['"]""",
            text, re.I
        ):
            url_candidate = _abs_url(resp.url, m.group(1))
            if url_candidate not in js_urls:
                js_urls.append(url_candidate)
                print(f"[INFO] JS内URL(SID付): {url_candidate[:100]}")
        # URL パターンを収集（SID なし）
        for m in re.finditer(
            r"""['"](/lms/[^'"]*(?:klaf|Klaf|Klal|klal)[^'"]*?)['"]""",
            text, re.I
        ):
            url_candidate = _abs_url(resp.url, m.group(1))
            if url_candidate not in js_urls:
                js_urls.append(url_candidate)
                print(f"[INFO] JS内URL(SIDなし): {url_candidate[:100]}")

    # Referer を klmsKlil に設定（iframe として読み込まれる想定）
    klil_referer = resp.url  # klmsKlil/doIndex の URL

    def _fetch_with_referer(url: str) -> requests.Response | None:
        """Referer を klmsKlil に設定して GET する"""
        try:
            r = s.get(url, timeout=20, allow_redirects=True,
                      headers={"Referer": klil_referer})
            if "/error/" in r.url or "/lginLgir/" in r.url or len(r.text) < 300:
                return None
            return r
        except Exception as e:
            print(f"  [WARN] {url[:60]}: {e}")
            return None

    def _print_rows(r_soup: BeautifulSoup) -> None:
        rows = r_soup.find_all("tr")
        print(f"[DEBUG] テーブル行数: {len(rows)}")
        for row in rows:
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
            if any(c for c in cells):
                print(f"  行: {cells}")
        for el in r_soup.find_all(string=re.compile(r"\d{4}[/\-年]\d{1,2}")):
            ctx = (el.parent.get_text(" ", strip=True) if el.parent else str(el))
            print(f"  日付: {ctx[:150]}")

    # ── klmsKlil の #list_form を取得して klmsKlal/index に AJAX POST ──
    # loadBlock('#list_block', '#list_form', '/lms/klmsKlal/index;SID=...', true)
    ajax_url = f"{lms_base}/klmsKlal/index;SID={actual_sid}"

    # #list_form のフィールドを収集
    list_form = soup.find("form", id="list_form") or soup.find("form", id="listForm")
    form_data: dict = {}
    if list_form:
        for inp in list_form.find_all("input"):
            name  = inp.get("name", "")
            value = inp.get("value", "")
            type_ = inp.get("type", "text")
            if not name or type_ == "submit":
                continue
            if type_ == "checkbox":
                form_data.setdefault(name, [])
                form_data[name].append(value)
            else:
                form_data[name] = value
        for sel in list_form.find_all("select"):
            name = sel.get("name", "")
            opt  = sel.find("option", selected=True) or sel.find("option")
            if name and opt:
                form_data[name] = opt.get("value", "")
        print(f"[DEBUG] #list_form フィールド: {form_data}")
    else:
        print("[DEBUG] #list_form が見つかりません。空データで POST します。")

    # XMLHttpRequest として AJAX POST
    ajax_headers = {
        "Referer":           klil_referer,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept":           "text/html, */*; q=0.01",
    }
    print(f"[INFO] AJAX POST: {ajax_url}")
    try:
        ajax_resp = s.post(ajax_url, data=form_data, headers=ajax_headers,
                           timeout=30, allow_redirects=True)
        print(f"[INFO] AJAX応答: {ajax_resp.url} ({len(ajax_resp.text)} bytes)")
        a_soup = BeautifulSoup(ajax_resp.text, "html.parser")
        _extract_assignments(a_soup, assignments, lms_base, actual_sid, s)
    except Exception as e:
        print(f"[WARN] AJAX POSTエラー: {e}")

    return assignments


def _extract_assignments(
    soup: BeautifulSoup,
    assignments: list[dict],
    lms_base: str,
    sid: str,
    s: requests.Session,
) -> None:
    """
    klmsKlal/index のHTMLから課題を抽出する。

    テーブル列構造:
      [0] 期限  [1] 教材種別  [2] 教材名  [3] 状況  [4] 講義名  [5] 期  [6] 曜日時限  [7] 教員
    """
    today = datetime.now(tz=JST).date()
    date_pat = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})[^\d]+(\d{1,2}):(\d{2})")

    for row in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        if len(cells) < 5:
            continue

        deadline_str = cells[0].strip()
        # 期限が「‐」または空 → 通知対象外
        if not deadline_str or deadline_str in ("‐", "-", "－"):
            continue

        m = date_pat.search(deadline_str)
        if not m:
            continue

        year, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour, minute   = int(m.group(4)), int(m.group(5))
        # 24:00 は当日の 23:59 として扱う
        if hour >= 24:
            hour, minute = 23, 59

        try:
            duedate = datetime(year, mon, day, hour, minute, tzinfo=JST)
        except ValueError:
            continue

        # 期限切れは除外
        if duedate.date() < today:
            continue

        mat_type = cells[1].strip()          # テスト / レポート / アンケート
        name     = cells[2].strip()          # 教材名
        course   = cells[4].strip() if len(cells) > 4 else "不明"  # 講義名

        assignments.append({
            "course":  course,
            "name":    f"[{mat_type}] {name}",
            "duedate": duedate,
        })
        print(f"[INFO] 課題発見: {course} / {name} → {duedate.strftime('%Y/%m/%d %H:%M')}")


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

    session, sid, lms_base, final_resp = login_session()
    print(f"✅ KU-LMS ログイン成功 → {final_resp.url[:80]}")

    all_assignments = get_assignments(session, sid, lms_base, start_resp=final_resp)
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
