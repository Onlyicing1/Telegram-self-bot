"""Deterministic schedule value objects and calculations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SUPPORTED_TYPES = frozenset({"once", "interval", "daily", "weekly"})

class ScheduleError(ValueError):
    pass

class OneShotState(str, Enum):
    FUTURE = "future"
    DUE = "due"
    GRACE = "overdue_grace"
    EXPIRED = "expired"


def _tz(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip():
        raise ScheduleError("timezone is required")
    if name.strip() == "UTC":
        return timezone.utc  # type: ignore[return-value]
    try:
        return ZoneInfo(name.strip())
    except ZoneInfoNotFoundError as exc:
        raise ScheduleError("invalid IANA timezone") from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleError("reference datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _localize(day: date, clock: time, zone: ZoneInfo) -> datetime:
    naive = datetime.combine(day, clock).replace(tzinfo=zone, fold=0)
    roundtrip = naive.astimezone(timezone.utc).astimezone(zone)
    if roundtrip.replace(tzinfo=None) == naive.replace(tzinfo=None):
        return naive
    for minutes in range(1, 181):
        candidate = naive.replace(tzinfo=None) + timedelta(minutes=minutes)
        localized = candidate.replace(tzinfo=zone, fold=0)
        if localized.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == candidate:
            return localized
    raise ScheduleError("could not normalize nonexistent local time")

@dataclass(frozen=True)
class OnceSchedule:
    at: datetime
    timezone: str
    def __post_init__(self):
        _tz(self.timezone)
        if self.at.tzinfo is not None:
            raise ScheduleError("once at must be a naive local datetime")
    def occurrence(self) -> datetime:
        return _localize(self.at.date(), self.at.time(), _tz(self.timezone)).astimezone(timezone.utc)

@dataclass(frozen=True)
class IntervalSchedule:
    interval: timedelta
    def __post_init__(self):
        if self.interval <= timedelta(0): raise ScheduleError("interval must be positive")

@dataclass(frozen=True)
class DailySchedule:
    at: time
    timezone: str
    def __post_init__(self): _tz(self.timezone)

@dataclass(frozen=True)
class WeeklySchedule:
    weekday: int
    at: time
    timezone: str
    def __post_init__(self):
        if not 0 <= self.weekday <= 6: raise ScheduleError("weekday must be between 0 and 6")
        _tz(self.timezone)

Schedule = OnceSchedule | IntervalSchedule | DailySchedule | WeeklySchedule


def parse_schedule(schedule_type: str, payload: dict) -> Schedule:
    if schedule_type not in SUPPORTED_TYPES or not isinstance(payload, dict): raise ScheduleError("invalid schedule payload")
    try:
        if schedule_type == "once": return OnceSchedule(datetime.fromisoformat(payload["at"]), payload["timezone"])
        if schedule_type == "interval": return IntervalSchedule(timedelta(seconds=float(payload["seconds"])))
        clock = time(int(payload["hour"]), int(payload.get("minute", 0)), int(payload.get("second", 0)))
        if schedule_type == "daily": return DailySchedule(clock, payload["timezone"])
        return WeeklySchedule(int(payload["weekday"]), clock, payload["timezone"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ScheduleError("malformed schedule payload") from exc


def next_occurrence(schedule: Schedule, reference: datetime, previous: datetime | None = None) -> datetime:
    ref = _utc(reference)
    if isinstance(schedule, OnceSchedule):
        value = schedule.occurrence(); return value if value >= ref else value
    if isinstance(schedule, IntervalSchedule):
        if previous is None: raise ScheduleError("interval requires previous scheduled occurrence")
        anchor = _utc(previous)
        while anchor <= ref: anchor += schedule.interval
        return anchor
    zone = _tz(schedule.timezone); local_ref = ref.astimezone(zone)
    target_day = local_ref.date()
    if isinstance(schedule, WeeklySchedule):
        target_day += timedelta(days=(schedule.weekday - target_day.weekday()) % 7)
    candidate = _localize(target_day, schedule.at, zone)
    if candidate.astimezone(timezone.utc) <= ref:
        days = 7 if isinstance(schedule, WeeklySchedule) else 1
        candidate = _localize(target_day + timedelta(days=days), schedule.at, zone)
    return candidate.astimezone(timezone.utc)


def advance_interval(previous: datetime, interval: timedelta, reference: datetime, max_steps: int = 1000) -> datetime:
    if interval <= timedelta(0): raise ScheduleError("interval must be positive")
    candidate = _utc(previous); ref = _utc(reference)
    for _ in range(max_steps):
        if candidate > ref: return candidate
        candidate += interval
    raise ScheduleError("interval advancement exceeded bound")


def recurring_due(schedule: Schedule, scheduled_for: datetime, reference: datetime) -> bool:
    return _utc(scheduled_for) <= _utc(reference)


def catch_up_occurrence(schedule: Schedule, scheduled_for: datetime, reference: datetime) -> tuple[datetime, datetime | None]:
    scheduled = _utc(scheduled_for); ref = _utc(reference)
    if scheduled > ref: return scheduled, scheduled
    if isinstance(schedule, IntervalSchedule): return scheduled, advance_interval(scheduled, schedule.interval, ref)
    return scheduled, next_occurrence(schedule, ref)


def one_shot_state(scheduled_for: datetime, reference: datetime, grace: timedelta) -> OneShotState:
    if grace < timedelta(0): raise ScheduleError("grace must not be negative")
    scheduled, ref = _utc(scheduled_for), _utc(reference)
    if ref < scheduled: return OneShotState.FUTURE
    if ref == scheduled: return OneShotState.DUE
    return OneShotState.GRACE if ref <= scheduled + grace else OneShotState.EXPIRED
