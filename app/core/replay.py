"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from .clock import (
    academic_day,
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)
from .revisions import Revision, resolve_effective_revisions, revisions_effective_at


class EventType(StrEnum):
    CHECKIN = "checkin"
    MENTOR_CONFIRM = "mentor_confirm"
    LEAVE_CORRECTION = "leave_correction"
    # 修订生命周期事件只追加进事件流，用于冻结 cutoff 与审计；
    # 重放本身通过传入的修订列表应用时间版本。
    REVISION_APPROVE = "revision_approve"
    REVISION_REVOKE = "revision_revoke"


class CheckinStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


INTERNSHIP_TYPE = "internship"


@dataclass(frozen=True)
class Event:
    """封装领域状态与业务约束。"""

    event_id: str
    plan_version: str
    event_type: EventType
    student_id: str
    payload: dict[str, Any]
    created_at: datetime
    # 只追加事件流中的单调序号（数据库行 id）；冻结 cutoff 按序号界定，
    # 避免不同事件 ID 前缀的字典序与真实插入顺序不一致。
    seq: int | None = None


@dataclass
class CheckinRecord:
    event_id: str
    student_id: str
    activity_id: str
    activity_type: str
    start_utc: datetime
    end_utc: datetime
    status: CheckinStatus
    # 修订叠加后的有效时间窗；None 表示沿用原始签到时间。
    effective_start_utc: datetime | None = None
    effective_end_utc: datetime | None = None
    applied_revision_id: str | None = None
    applied_revision_version: int | None = None

    @property
    def window_start_utc(self) -> datetime:
        return self.effective_start_utc or self.start_utc

    @property
    def window_end_utc(self) -> datetime:
        return self.effective_end_utc or self.end_utc

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)

    @property
    def effective_seconds(self) -> int:
        return elapsed_seconds(self.window_start_utc, self.window_end_utc)

    @property
    def counts(self) -> bool:
        return self.status == CheckinStatus.CONFIRMED


@dataclass
class Adjustment:
    event_id: str
    student_id: str
    seconds: int
    reason: str


@dataclass
class DayTotal:
    academic_day: str
    seconds: int


@dataclass
class StudentProgress:
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DayTotal] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]
    applied_revisions: list[Revision] = field(default_factory=list)


def _parse_checkin(
    event: Event, tz_name: str
) -> CheckinRecord:
    start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
    end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
    activity_type = event.payload.get("activity_type", "regular")
    requires_confirmation = activity_type == INTERNSHIP_TYPE
    status = (
        CheckinStatus.PENDING if requires_confirmation else CheckinStatus.CONFIRMED
    )
    return CheckinRecord(
        event_id=event.event_id,
        student_id=event.student_id,
        activity_id=event.payload.get("activity_id", ""),
        activity_type=activity_type,
        start_utc=start,
        end_utc=end,
        status=status,
    )


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_seq: int | None = None,
    revisions: Iterable[Revision] | None = None,
) -> ReplayState:
    """执行确定性的业务处理。

    revisions 中的已批准（且在 cutoff 时仍未撤销）修订会以有效版本方式叠加
    到匹配活动的签到记录上；原始签到事件不被修改。up_to_seq 按事件流的单调
    序号界定重放范围（冻结历史状态时使用）。
    """
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: (e.seq is None, e.seq if e.seq is not None else e.event_id),
    )
    if up_to_seq is not None:
        sorted_events = [
            e for e in sorted_events if e.seq is not None and e.seq <= up_to_seq
        ]

    revision_list = (
        revisions_effective_at(list(revisions), up_to_seq)
        if revisions is not None
        else []
    )

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    adjustments_by_student: dict[str, list[Adjustment]] = {}

    for event in sorted_events:
        if event.event_type == EventType.CHECKIN:
            record = _parse_checkin(event, timezone_name)
            checkins_by_student.setdefault(event.student_id, []).append(record)
            checkin_index[event.event_id] = record
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target = checkin_index.get(target_id)
            if target is not None and target.student_id == event.student_id:
                target.status = CheckinStatus.CONFIRMED
        elif event.event_type == EventType.LEAVE_CORRECTION:
            seconds = int(event.payload.get("adjustment_seconds", 0))
            adjustments_by_student.setdefault(event.student_id, []).append(
                Adjustment(
                    event_id=event.event_id,
                    student_id=event.student_id,
                    seconds=seconds,
                    reason=str(event.payload.get("reason", "")),
                )
            )
        # revision_approve / revision_revoke 事件只承载 cutoff 与审计语义，
        # 修订本身通过 revisions 参数传入。

    effective_revisions = resolve_effective_revisions(
        revision_list,
        student_ids=set(checkins_by_student) | set(adjustments_by_student),
    )

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    applied_revision_ids: dict[str, set[str]] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = adjustments_by_student.get(student_id, [])

        effective_records: list[CheckinRecord] = []
        applied_here: set[str] = set()
        for record in records:
            rev = effective_revisions.get((record.activity_id, student_id))
            if rev is not None:
                record.effective_start_utc = rev.new_start_utc
                record.effective_end_utc = rev.new_end_utc
                record.applied_revision_id = rev.revision_id
                record.applied_revision_version = rev.version
                applied_here.add(rev.revision_id)
            effective_records.append(record)
        if applied_here:
            applied_revision_ids[student_id] = applied_here

        confirmed_intervals = [
            (r.window_start_utc, r.window_end_utc)
            for r in effective_records
            if r.counts
        ]
        pending_intervals = [
            (r.window_start_utc, r.window_end_utc)
            for r in effective_records
            if r.status == CheckinStatus.PENDING
        ]

        confirmed_seconds = union_seconds(confirmed_intervals)
        pending_seconds = union_seconds(pending_intervals)
        adjustment_seconds = sum(a.seconds for a in adjustments)
        total_seconds = confirmed_seconds + adjustment_seconds
        if total_seconds < 0:
            total_seconds = 0

        day_totals: dict[str, int] = {}
        for start, end in merge_intervals(confirmed_intervals):
            for day, seg_start, seg_end in split_by_academic_day(
                start, end, timezone_name
            ):
                key = day.isoformat()
                day_totals[key] = day_totals.get(key, 0) + elapsed_seconds(
                    seg_start, seg_end
                )
        daily = [
            DayTotal(academic_day=day, seconds=secs)
            for day, secs in sorted(day_totals.items())
        ]

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=total_seconds // (45 * 60),
            pending_lesson_units=pending_seconds // (45 * 60),
            meets_requirement=total_seconds >= required_seconds,
            daily=daily,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
        )

    applied_ids: set[str] = set()
    for ids in applied_revision_ids.values():
        applied_ids |= ids
    applied_manifest = {
        rev.revision_id: rev for rev in revision_list if rev.revision_id in applied_ids
    }

    return ReplayState(
        plan_version=plan_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        students=students,
        applied_revisions=[applied_manifest[rid] for rid in sorted(applied_manifest)],
    )


def explain_checkin(record: CheckinRecord, tz_name: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    segments = split_by_academic_day(
        record.window_start_utc, record.window_end_utc, tz_name
    )
    return {
        "event_id": record.event_id,
        "activity_id": record.activity_id,
        "activity_type": record.activity_type,
        "status": record.status.value,
        "counts": record.counts,
        "check_in_at_utc": record.start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "check_out_at_utc": record.end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "raw_seconds": record.seconds,
        "effective_start_utc": record.window_start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "effective_end_utc": record.window_end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "effective_seconds": record.effective_seconds,
        "applied_revision_id": record.applied_revision_id,
        "applied_revision_version": record.applied_revision_version,
        "academic_days": [
            {
                "day": day.isoformat(),
                "start_utc": seg_start.isoformat().replace("+00:00", "Z"),
                "end_utc": seg_end.isoformat().replace("+00:00", "Z"),
                "seconds": elapsed_seconds(seg_start, seg_end),
            }
            for day, seg_start, seg_end in segments
        ],
    }
