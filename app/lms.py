"""
lms.py  ―  KU-LMS (UNIVERSAL PASSPORT) ログイン・課題取得
===========================================================
GakuNin SAML 統合認証フローを requests のみで実装。
username / password を引数で受け取るので、複数ユーザーに対応。
"""

import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

JST = timezone(timedelta(hours=9))

# 工学院大学 KU-LMS のベース URL（環境変数 MOODLE_URL から読み込み）
KU_LMS_URL = os.environ.get("MOODLE_URL", "https://lms.kogakuin.ac.jp/lms").rstrip("/")


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
    s: requests.Session,
    sid: str,
    lms_base: str,
    username: str,
    password: str,
) -> tuple[str, str, requests.Response]:
    """
    GakuNin SAML フローでログインし、(new_sid, lms_base, final_resp) を返す。
    """
    login_root      = f"{lms_base}/lginLgir/;SID={sid}"
    gakunin_forward = f"{lms_base}/lginLgir/gakuninForward;SID={sid}"

    resp = s.post(
        gakunin_forward,
        data={"lginFlag": "2", "guest": "", "shortUrl": ""},
        headers={"Referer": login_root,
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
        allow_redirects=True,
    )

    for step in range(20):
        soup = BeautifulSoup(resp.text, "html.parser")
        url  = resp.url
        title = soup.find("title")
        title_text = title.string.strip() if title and title.string else "no-title"
        forms = soup.find_all("form")
        meta = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
        # エラーメッセージを抽出
        error_els = soup.find_all(class_=re.compile(r"(error|alert|warn|msg|notice)", re.I))
        errors = [e.get_text(" ", strip=True)[:80] for e in error_els if e.get_text(strip=True)]
        body_text = soup.get_text(" ", strip=True)[:200]
        print(f"[GakuNin Step{step+1}] {url[:80]} | errors={errors} | body={body_text}", flush=True)

        # ─ LMS 本体に戻ってきたら成功 ─
        if (lms_base in url and
                "/lginLgir/" not in url and
                "/error/" not in url):
            new_sid = _extract_sid(url) or sid
            return new_sid, lms_base, resp

        # ─ study-auth / CoursePower ─
        if "study-auth" in url or "eduapi" in url:
            links = [(a.get_text(strip=True), a.get("href", ""))
                     for a in soup.find_all("a", href=True)]
            for _, href in links:
                if lms_base in href and "/lginLgir/" not in href:
                    resp = s.get(href, timeout=30, allow_redirects=True)
                    break
            else:
                form = soup.find("form")
                if form:
                    action = _abs_url(url, form.get("action", url))
                    data = {inp.get("name", ""): inp.get("value", "")
                            for inp in form.find_all("input") if inp.get("name")}
                    resp = s.post(action, data=data,
                                  headers={"Referer": url},
                                  timeout=30, allow_redirects=True)
                else:
                    resp = s.get(f"{lms_base}/", timeout=30, allow_redirects=True)
            continue

        # ─ meta refresh ─
        meta = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
        if meta:
            content = meta.get("content", "")
            m = re.search(r"URL=(.+)", content, re.I)
            if m:
                refresh_url = _abs_url(url, m.group(1).strip().rstrip('"\''))
                resp = s.get(refresh_url, timeout=30, allow_redirects=True)
                continue

        # ─ SAMLResponse フォーム → SP へ転送 ─
        for f in soup.find_all("form"):
            data = {inp.get("name", ""): inp.get("value", "")
                    for inp in f.find_all("input") if inp.get("name")}
            if "SAMLResponse" in data:
                action = _abs_url(url, f.get("action", url))
                resp = s.post(action, data=data,
                              headers={"Referer": url},
                              timeout=30, allow_redirects=True)
                break
        else:
            # ─ ログインフォーム ─
            # 全フォームのフィールド名をデバッグ出力
            all_forms = soup.find_all("form")
            print(f"[GakuNin Step{step+1}] フォーム数: {len(all_forms)}", flush=True)
            for fi, f in enumerate(all_forms):
                all_inputs = [(inp.get("name",""), inp.get("type","text"), inp.get("value","")[:20])
                              for inp in f.find_all("input") if inp.get("name")]
                print(f"  Form[{fi}] action={f.get('action','')} inputs={all_inputs}", flush=True)

            login_form = None
            for f in soup.find_all("form"):
                data = {inp.get("name", ""): inp.get("value", "")
                        for inp in f.find_all("input") if inp.get("name")}
                names_lower = {k.lower() for k in data}
                pwd_like  = {"password", "passwd", "j_password", "pass"}
                user_like = {"username", "j_username", "uid", "userid", "user",
                             "loginid", "login_id", "id", "account", "mail",
                             "email", "login", "name", "userid"}
                # パスワードまたはユーザー名フィールドがあるフォームを探す
                if names_lower & (pwd_like | user_like):
                    login_form = (f, data)
                    break
                # フォールバック: テキスト入力が1つ以上あるフォーム
                text_inputs = [inp for inp in f.find_all("input")
                               if inp.get("type", "text") in ("text", "email")
                               and inp.get("name")]
                if text_inputs:
                    login_form = (f, data)
                    break

            if login_form:
                f, data = login_form
                user_like = {"username", "j_username", "uid", "userid", "user",
                             "loginid", "login_id", "id", "account", "mail",
                             "email", "login", "name"}
                filled_user = False
                for inp in f.find_all("input"):
                    if inp.get("name", "").lower() in user_like:
                        data[inp.get("name")] = username
                        print(f"  → username field: {inp.get('name')} = {username}", flush=True)
                        filled_user = True
                        break
                if not filled_user:
                    for inp in f.find_all("input"):
                        n = inp.get("name", "")
                        t = inp.get("type", "text")
                        if t in ("text", "email") and n and n.lower() != "dummy":
                            data[n] = username
                            print(f"  → fallback username field: {n} (type={t}) = {username}", flush=True)
                            filled_user = True
                            break
                if not filled_user:
                    print(f"  → WARNING: usernameフィールドが見つかりません", flush=True)

                filled_pass = False
                for inp in f.find_all("input", type="password"):
                    data[inp.get("name")] = password
                    print(f"  → password field: {inp.get('name')}", flush=True)
                    filled_pass = True
                    break
                if not filled_pass:
                    print(f"  → passwordフィールドなし（2段階ログインの1段階目の可能性）", flush=True)

                action = _abs_url(url, f.get("action", url))
                method = f.get("method", "post").lower()
                print(f"  → submit to {action} ({method}) data_keys={list(data.keys())}", flush=True)
                if method == "get":
                    resp = s.get(action, params=data, timeout=30, allow_redirects=True)
                else:
                    resp = s.post(action, data=data,
                                  headers={"Referer": url,
                                           "Content-Type": "application/x-www-form-urlencoded"},
                                  timeout=30, allow_redirects=True)
                continue

            break  # フォームもリダイレクトもない

    raise RuntimeError("GakuNin ログイン失敗（フロー完了せず）")


# ─────────────────────────────────────
# セッション作成（外部から呼ぶエントリポイント）
# ─────────────────────────────────────

def login_session_for_user(
    username: str,
    password: str,
    lms_url: str = KU_LMS_URL,
) -> tuple[requests.Session, str, str, requests.Response]:
    """
    指定ユーザーで KU-LMS にログインし、
    (session, sid, lms_base, final_resp) を返す。
    ログイン失敗時は RuntimeError を送出。
    """
    s = _make_session()

    resp = s.get(f"{lms_url}/", timeout=30, allow_redirects=True)
    login_page_url = resp.url.split("#")[0]

    sid = _extract_sid(login_page_url)
    if not sid:
        raise RuntimeError(f"SID 取得失敗 (URL: {login_page_url})")

    lms_m = re.match(r"(https?://[^/]+(?:/[^/]+)*)/lginLgir/", login_page_url)
    lms_base = lms_m.group(1) if lms_m else lms_url

    new_sid, lms_base, final_resp = _login_gakunin(s, sid, lms_base, username, password)
    return s, new_sid, lms_base, final_resp


# ─────────────────────────────────────
# 課題取得
# ─────────────────────────────────────

def get_assignments(
    s: requests.Session,
    sid: str,
    lms_base: str,
    start_resp: requests.Response | None = None,
) -> list[dict]:
    """
    課題一覧を取得して返す。
    各要素: {"course": str, "name": str, "duedate": datetime(JST)}
    """
    assignments: list[dict] = []

    actual_sid = sid
    if start_resp is not None:
        new_sid = _extract_sid(start_resp.url)
        if new_sid:
            actual_sid = new_sid

    kadai_url = f"{lms_base}/klmsKlil/;SID={actual_sid}"
    resp = s.get(kadai_url, timeout=30, allow_redirects=True)

    if "/error/" in resp.url or resp.status_code >= 400:
        return assignments

    soup = BeautifulSoup(resp.text, "html.parser")
    klil_referer = resp.url

    ajax_url = f"{lms_base}/klmsKlal/index;SID={actual_sid}"

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

    ajax_headers = {
        "Referer":           klil_referer,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept":           "text/html, */*; q=0.01",
    }
    ajax_resp = s.post(ajax_url, data=form_data, headers=ajax_headers,
                       timeout=30, allow_redirects=True)
    a_soup = BeautifulSoup(ajax_resp.text, "html.parser")
    _extract_assignments(a_soup, assignments)

    return assignments


def _extract_assignments(soup: BeautifulSoup, assignments: list[dict]) -> None:
    today = datetime.now(tz=JST).date()
    date_pat = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})[^\d]+(\d{1,2}):(\d{2})")

    for row in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        if len(cells) < 5:
            continue

        deadline_str = cells[0].strip()
        if not deadline_str or deadline_str in ("‐", "-", "－"):
            continue

        m = date_pat.search(deadline_str)
        if not m:
            continue

        year, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour, minute   = int(m.group(4)), int(m.group(5))
        if hour >= 24:
            hour, minute = 23, 59

        try:
            duedate = datetime(year, mon, day, hour, minute, tzinfo=JST)
        except ValueError:
            continue

        if duedate.date() < today:
            continue

        assignments.append({
            "course":  cells[4].strip() if len(cells) > 4 else "不明",
            "name":    f"[{cells[1].strip()}] {cells[2].strip()}",
            "duedate": duedate,
        })
