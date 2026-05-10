"""
scheduler.py  ―  定期実行ジョブ（APScheduler）
================================================
毎朝 7:00 JST と 12:00 JST に全登録ユーザーの課題をチェックして通知する。
"""

import sys
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

JST = timezone(timedelta(hours=9))


def _build_message(assignments: list[dict], now: datetime) -> str | None:
    """通知対象の課題をメッセージ文字列に変換。通知なしなら None を返す"""
    today = now.date()
    to_notify = []

    from app.models import User  # 循環インポート回避のため関数内で

    # ダミーユーザーで notify_days / notify_hours を確認するのではなく
    # 呼び出し元から渡されるので、ここでは判定ロジックのみ実装
    return None  # 実際の判定は run_for_user 内で行う


def _format_message(to_notify: list[dict], now: datetime) -> str:
    lines = [f"📢 課題の締切通知 [{now.strftime('%Y/%m/%d %H:%M')} JST]"]
    for a in to_notify:
        kind, val, hl = a["timing"]
        if kind == "hours":
            timing_str = f"⚠️ あと約 {int(hl)} 時間！"
        elif a["days_left"] == 0:
            timing_str = "⚠️ 今日が締切！"
        elif a["days_left"] == 1:
            timing_str = "🔴 明日締切"
        else:
            timing_str = f"🟡 あと {a['days_left']} 日"

        lines.append(
            f"\n━━━━━━━━━━\n"
            f"📘 {a['course']}\n"
            f"📝 {a['name']}\n"
            f"⏰ {a['duedate'].strftime('%m/%d(%a) %H:%M')} {timing_str}"
        )
    return "\n".join(lines)


def run_notifications() -> None:
    """全登録ユーザーに対して課題チェック＆通知を実行"""
    from app.models import get_all_registered
    from app.lms import login_session_for_user, get_assignments
    from app.crypto import decrypt
    from app.line_bot import push

    now   = datetime.now(tz=JST)
    today = now.date()
    users = get_all_registered()
    print(f"[{now.strftime('%Y/%m/%d %H:%M')} JST] スケジュール実行 | 対象: {len(users)} ユーザー",
          flush=True)

    for user in users:
        try:
            password = decrypt(user.password_enc)
            session, sid, lms_base, final_resp = login_session_for_user(
                user.username, password
            )
            assignments = get_assignments(session, sid, lms_base, start_resp=final_resp)

            to_notify = []
            for a in assignments:
                days_left  = (a["duedate"].date() - today).days
                hours_left = (a["duedate"] - now).total_seconds() / 3600

                timing_kind = None

                # 時間ベース（±1時間の窓）
                for h in user.notify_hours:
                    if h - 1 <= hours_left < h + 1:
                        timing_kind = ("hours", h, hours_left)
                        break

                # 日数ベース（時間通知と重複しない場合のみ）
                if timing_kind is None and days_left in user.notify_days:
                    timing_kind = ("days", days_left, hours_left)

                if timing_kind:
                    to_notify.append({
                        **a,
                        "days_left":  days_left,
                        "hours_left": hours_left,
                        "timing":     timing_kind,
                    })

            if not to_notify:
                print(f"  [{user.username}] 通知対象なし", flush=True)
                continue

            to_notify.sort(key=lambda x: x["duedate"])
            msg = _format_message(to_notify, now)
            push(user.line_user_id, msg)
            print(f"  [{user.username}] {len(to_notify)} 件通知", flush=True)

        except Exception as e:
            print(f"  [ERROR] {user.username}: {e}", file=sys.stderr, flush=True)


def start_scheduler() -> BackgroundScheduler:
    """スケジューラを起動して返す"""
    scheduler = BackgroundScheduler(timezone=JST)

    # 毎朝 7:00 JST（日数ベース通知: 3日前・1日前）
    scheduler.add_job(
        run_notifications,
        CronTrigger(hour=7, minute=0, timezone=JST),
        id="notify_morning",
        replace_existing=True,
    )
    # 毎日 12:00 JST（時間ベース通知: 12時間前）
    scheduler.add_job(
        run_notifications,
        CronTrigger(hour=12, minute=0, timezone=JST),
        id="notify_noon",
        replace_existing=True,
    )

    scheduler.start()
    print("✅ スケジューラ起動: 7:00 JST / 12:00 JST", flush=True)
    return scheduler
