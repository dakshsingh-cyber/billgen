"""Daily free-action limits and subscription helpers (pure logic, no routes)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from models import User, db, utcnow

FREE_PER_DAY = 5
IST = ZoneInfo("Asia/Kolkata")
PLAN_DAYS = 30


def today_ist() -> date:
    return datetime.now(IST).date()


def has_active_plan(user: User) -> bool:
    return user.paid_until is not None and user.paid_until > utcnow()


def remaining_free(user: User) -> int:
    if user.usage_day != today_ist():
        return FREE_PER_DAY
    return max(0, FREE_PER_DAY - (user.usage_count or 0))


def activate_plan(user: User) -> None:
    """Grant/extend the paid plan by PLAN_DAYS (extends from current expiry if still active)."""
    now = utcnow()
    base = user.paid_until if user.paid_until and user.paid_until > now else now
    user.paid_until = base + timedelta(days=PLAN_DAYS)


# NOTE: an action is consumed ONLY after input validation (and bill calculation) has
# succeeded, so bad input never costs the user anything. A single generate request
# (one PDF download or one Excel run) counts as exactly 1 action.
def use_action(user: User) -> bool:
    if has_active_plan(user):
        return True  # paid users: never blocked, counter untouched

    # DAILY RESET LOGIC: the counter belongs to one IST calendar day. If the stored day
    # differs from today's IST date, a new day has started, so start again from zero.
    today = today_ist()
    if user.usage_day != today:
        user.usage_day = today
        user.usage_count = 0

    if (user.usage_count or 0) >= FREE_PER_DAY:
        db.session.rollback()  # discard the in-memory reset; nothing is committed
        return False

    user.usage_count = (user.usage_count or 0) + 1
    db.session.commit()
    return True
