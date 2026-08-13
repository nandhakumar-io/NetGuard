"""Materializes RecurringMaintenanceSchedule rows ("patch Tuesdays",
"first Saturday firmware window", ...) into concrete MaintenanceWindow
rows -- the same table alert_service already checks for suppression, so
generated windows behave identically to one-off windows.

Generation is idempotent: re-running the sweep never creates a duplicate
for a (schedule, occurrence date) pair that's already been materialized,
so it's safe to run on every beat tick.
"""
import datetime
import uuid

from sqlalchemy.orm import Session

from app.models.maintenance_window import MaintenanceScope, MaintenanceWindow
from app.models.recurring_maintenance_schedule import (
    RecurrenceFrequency,
    RecurringMaintenanceSchedule,
)

# How far ahead to materialize concrete windows. Long enough that a
# schedule change window is always visible/plannable well in advance,
# short enough that editing a schedule doesn't leave a huge tail of
# already-materialized (and now stale) future windows to clean up.
DEFAULT_HORIZON_DAYS = 60


def _nth_weekday_of_month(year: int, month: int, day_of_week: int, week_of_month: int) -> datetime.date:
    """day_of_week: 0=Monday..6=Sunday. week_of_month: 1=first, 2=second,
    ... -1=last occurrence of that weekday in the month."""
    first_of_month = datetime.date(year, month, 1)
    if month == 12:
        next_month = datetime.date(year + 1, 1, 1)
    else:
        next_month = datetime.date(year, month + 1, 1)
    last_of_month = next_month - datetime.timedelta(days=1)

    if week_of_month == -1:
        d = last_of_month
        while d.weekday() != day_of_week:
            d -= datetime.timedelta(days=1)
        return d

    d = first_of_month
    while d.weekday() != day_of_week:
        d += datetime.timedelta(days=1)
    d += datetime.timedelta(weeks=week_of_month - 1)
    if d.month != month:
        raise ValueError(f"No {week_of_month}th weekday {day_of_week} in {year}-{month:02d}")
    return d


def _occurrence_dates(schedule: RecurringMaintenanceSchedule, horizon_end: datetime.date) -> list[datetime.date]:
    """All occurrence dates from schedule.active_from through the earlier
    of schedule.active_until and horizon_end (inclusive)."""
    start = schedule.active_from.date()
    end = horizon_end
    if schedule.active_until:
        end = min(end, schedule.active_until.date())
    if end < start:
        return []

    dates: list[datetime.date] = []

    if schedule.frequency == RecurrenceFrequency.WEEKLY:
        # First occurrence on/after `start` matching day_of_week.
        d = start
        while d.weekday() != schedule.day_of_week:
            d += datetime.timedelta(days=1)
        step = datetime.timedelta(weeks=max(schedule.interval, 1))
        while d <= end:
            dates.append(d)
            d += step

    elif schedule.frequency == RecurrenceFrequency.MONTHLY:
        year, month = start.year, start.month
        interval = max(schedule.interval, 1)
        months_checked = 0
        # Cap iterations generously (horizon_days can't realistically
        # need more than a couple years of monthly steps).
        while months_checked < 36:
            try:
                occ = _nth_weekday_of_month(year, month, schedule.day_of_week, schedule.week_of_month or 1)
            except ValueError:
                occ = None
            if occ and start <= occ <= end:
                dates.append(occ)
            if occ and occ > end:
                break
            month += interval
            while month > 12:
                month -= 12
                year += 1
            months_checked += 1

    return dates


def generate_windows(db: Session, schedule: RecurringMaintenanceSchedule, *, horizon_days: int = DEFAULT_HORIZON_DAYS) -> list[MaintenanceWindow]:
    """Materializes any not-yet-created occurrences for `schedule` within
    the horizon. Returns the newly created MaintenanceWindow rows (does
    not return already-existing ones). Caller commits.
    """
    if not schedule.enabled:
        return []

    horizon_end = datetime.date.today() + datetime.timedelta(days=horizon_days)
    occurrence_dates = _occurrence_dates(schedule, horizon_end)
    if not occurrence_dates:
        return []

    existing = (
        db.query(MaintenanceWindow.starts_at)
        .filter(MaintenanceWindow.recurrence_id == schedule.id)
        .all()
    )
    existing_dates = {row.starts_at.date() for row in existing}

    created: list[MaintenanceWindow] = []
    for occ_date in occurrence_dates:
        if occ_date in existing_dates:
            continue
        starts_at = datetime.datetime.combine(occ_date, schedule.start_time, tzinfo=datetime.timezone.utc)
        ends_at = starts_at + datetime.timedelta(minutes=schedule.duration_minutes)

        window = MaintenanceWindow(
            id=uuid.uuid4(),
            name=schedule.name,
            reason=schedule.reason,
            scope=MaintenanceScope(schedule.scope),
            device_id=schedule.device_id,
            site=schedule.site,
            starts_at=starts_at,
            ends_at=ends_at,
            created_by=schedule.created_by,
            recurrence_id=schedule.id,
        )
        db.add(window)
        created.append(window)

    return created


def generate_all(db: Session, *, horizon_days: int = DEFAULT_HORIZON_DAYS) -> int:
    """Materializes upcoming windows for every enabled schedule. Intended
    to run on a daily Celery beat tick (see
    app.tasks.run_recurring_window_generation_task) as well as
    immediately whenever a schedule is created/updated, so newly-defined
    recurrences don't wait a full day for their first window to appear.
    Returns the number of windows created.
    """
    schedules = db.query(RecurringMaintenanceSchedule).filter(RecurringMaintenanceSchedule.enabled == True).all()
    total = 0
    for schedule in schedules:
        total += len(generate_windows(db, schedule, horizon_days=horizon_days))
    if total:
        db.commit()
    return total


def preview_occurrences(schedule: RecurringMaintenanceSchedule, *, horizon_days: int = DEFAULT_HORIZON_DAYS) -> list[datetime.datetime]:
    """Read-only: the upcoming start datetimes this schedule would
    produce, without writing anything -- used by the "preview" endpoint
    so an operator can sanity-check a recurrence rule before saving it.
    """
    horizon_end = datetime.date.today() + datetime.timedelta(days=horizon_days)
    return [
        datetime.datetime.combine(d, schedule.start_time, tzinfo=datetime.timezone.utc)
        for d in _occurrence_dates(schedule, horizon_end)
    ]
