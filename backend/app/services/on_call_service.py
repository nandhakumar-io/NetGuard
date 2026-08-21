"""Resolves who's currently on call for an OnCallSchedule.

Used by app.api.on_call_schedules (the "currently on call" hint shown
next to the schedule picker) and by app.services.escalation_service._send
(so an EscalationPolicy with on_call_schedule_id set pages whoever's
actually on rotation right now instead of always the same static
address).

Deliberately simple, deterministic rotation math -- no persisted
"whose turn is it" state to drift out of sync or need manual
correction. Given a schedule's rotation_type, the current contact is
computed purely from wall-clock time (in the schedule's timezone) and
an epoch anchor, the same way a cron schedule is stateless: anyone
calling current_contact() at the same moment gets the same answer,
and nothing has to run on a timer to "advance" the rotation.
"""
from __future__ import annotations

import datetime as dt

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 fallback, not expected here
    ZoneInfo = None  # type: ignore

from app.models.on_call_schedule import OnCallRotationType, OnCallSchedule

# Arbitrary fixed Monday used as the start of "week 0" for WEEKLY
# rotation, so which week is primary vs. secondary is consistent across
# restarts/deployments rather than anchored to whenever the schedule
# row happened to be created.
_ROTATION_EPOCH = dt.date(2024, 1, 1)  # a Monday


def _now_local(schedule: OnCallSchedule) -> dt.datetime:
    tz = None
    if schedule.timezone and ZoneInfo is not None:
        try:
            tz = ZoneInfo(schedule.timezone)
        except Exception:
            tz = None
    now = dt.datetime.now(dt.timezone.utc)
    return now.astimezone(tz) if tz else now


def _handover_passed_today(now_local: dt.datetime, handover: str | None) -> bool:
    """Whether today's handover time has already passed, for deciding
    which side of a day/week boundary "now" falls on when a handover
    time other than midnight is configured."""
    if not handover:
        return True  # no configured time = flip at midnight, always "passed"
    try:
        hh, mm = (int(p) for p in handover.split(":", 1))
    except (ValueError, AttributeError):
        return True
    return (now_local.hour, now_local.minute) >= (hh, mm)


def current_contact(schedule: OnCallSchedule) -> tuple[str | None, bool]:
    """Returns (email, is_secondary). Falls back to primary_user_email
    whenever there's nothing to rotate against (no secondary configured,
    rotation disabled, or schedule disabled)."""
    if not schedule.enabled or not schedule.secondary_user_email:
        return schedule.primary_user_email, False

    rotation = schedule.rotation_type
    if isinstance(rotation, str):
        rotation = OnCallRotationType(rotation)

    if rotation == OnCallRotationType.NONE:
        return schedule.primary_user_email, False

    now_local = _now_local(schedule)
    handover_passed = _handover_passed_today(now_local, schedule.shift_handover_time)
    anchor_date = now_local.date() if handover_passed else now_local.date() - dt.timedelta(days=1)

    if rotation == OnCallRotationType.DAILY:
        days_since_epoch = (anchor_date - _ROTATION_EPOCH).days
        on_secondary = days_since_epoch % 2 == 1
    else:  # WEEKLY
        days_since_epoch = (anchor_date - _ROTATION_EPOCH).days
        weeks_since_epoch = days_since_epoch // 7
        on_secondary = weeks_since_epoch % 2 == 1

    if on_secondary:
        return schedule.secondary_user_email, True
    return schedule.primary_user_email, False
