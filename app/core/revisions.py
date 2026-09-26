"""活动时间修订领域逻辑。

修订（revision）以“事件之外的有效版本”方式改变一场实训（activity）的起止
时间：重放内核在不修改原始签到事件的前提下，把已批准修订叠加到匹配的签到
记录上。每个修订带单调递增的版本号；student_ids 为 None 表示适用全体学生，
否则只适用名单内学生（部分学生例外）。已撤销的修订不参与叠加，此时回退到
更早的有效版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .clock import (
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
)

if TYPE_CHECKING:
    from .replay import CheckinRecord


class RevisionStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REVOKED = "revoked"


@dataclass(frozen=True)
class Revision:
    """一个活动时间修订版本。"""

    revision_id: str
    plan_version: str
    activity_id: str
    version: int
    new_start_utc: datetime
    new_end_utc: datetime
    student_ids: frozenset[str] | None
    reason: str = ""
    status: RevisionStatus = RevisionStatus.DRAFT
    created_by: str = "system"
    approved_by: str | None = None
    revoked_by: str | None = None
    created_at: datetime | None = None
    approved_at: datetime | None = None
    revoked_at: datetime | None = None
    approved_event_id: str | None = None
    revoked_event_id: str | None = None
    approved_seq: int | None = None
    revoked_seq: int | None = None

    def applies_to(self, student_id: str) -> bool:
        return self.student_ids is None or student_id in self.student_ids

    def to_detail_dict(self) -> dict[str, Any]:
        return {
            "new_start_at": _iso(self.new_start_utc),
            "new_end_at": _iso(self.new_end_utc),
            "student_ids": None if self.student_ids is None else sorted(self.student_ids),
            "reason": self.reason,
        }


@dataclass
class DayAttributionChange:
    academic_day: str
    before_seconds: int
    after_seconds: int

    @property
    def delta_seconds(self) -> int:
        return self.after_seconds - self.before_seconds


@dataclass
class OverlapInterval:
    """修订后时间窗与原始签到窗的重叠区间（UTC）。"""

    start_utc: datetime
    end_utc: datetime

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)


@dataclass
class AffectedCheckin:
    event_id: str
    before_start_utc: datetime
    before_end_utc: datetime
    after_start_utc: datetime
    after_end_utc: datetime
    revision_id: str
    version: int
    overlaps: list[OverlapInterval] = field(default_factory=list)
    day_changes: list[DayAttributionChange] = field(default_factory=list)

    @property
    def before_seconds(self) -> int:
        return elapsed_seconds(self.before_start_utc, self.before_end_utc)

    @property
    def after_seconds(self) -> int:
        return elapsed_seconds(self.after_start_utc, self.after_end_utc)


@dataclass
class StudentImpact:
    student_id: str
    before_confirmed_seconds: int
    after_confirmed_seconds: int
    before_pending_seconds: int
    after_pending_seconds: int
    affected_checkins: list[AffectedCheckin] = field(default_factory=list)
    day_changes: list[DayAttributionChange] = field(default_factory=list)

    @property
    def confirmed_delta_seconds(self) -> int:
        return self.after_confirmed_seconds - self.before_confirmed_seconds

    @property
    def pending_delta_seconds(self) -> int:
        return self.after_pending_seconds - self.before_pending_seconds


@dataclass
class RevisionImpact:
    """修订影响预览：受影响学生、日归属变化、重叠区间。"""

    revision_id: str
    activity_id: str
    version: int
    student_ids: frozenset[str] | None
    affected_student_count: int
    students: list[StudentImpact]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "activity_id": self.activity_id,
            "version": self.version,
            "student_ids": (
                None if self.student_ids is None else sorted(self.student_ids)
            ),
            "affected_student_count": self.affected_student_count,
            "students": [
                {
                    "student_id": s.student_id,
                    "before_confirmed_seconds": s.before_confirmed_seconds,
                    "after_confirmed_seconds": s.after_confirmed_seconds,
                    "confirmed_delta_seconds": s.confirmed_delta_seconds,
                    "before_pending_seconds": s.before_pending_seconds,
                    "after_pending_seconds": s.after_pending_seconds,
                    "pending_delta_seconds": s.pending_delta_seconds,
                    "day_changes": [
                        {
                            "academic_day": d.academic_day,
                            "before_seconds": d.before_seconds,
                            "after_seconds": d.after_seconds,
                            "delta_seconds": d.delta_seconds,
                        }
                        for d in s.day_changes
                    ],
                    "affected_checkins": [
                        {
                            "event_id": c.event_id,
                            "revision_id": c.revision_id,
                            "version": c.version,
                            "before_start_utc": _iso(c.before_start_utc),
                            "before_end_utc": _iso(c.before_end_utc),
                            "after_start_utc": _iso(c.after_start_utc),
                            "after_end_utc": _iso(c.after_end_utc),
                            "before_seconds": c.before_seconds,
                            "after_seconds": c.after_seconds,
                            "overlap_intervals": [
                                {
                                    "start_utc": _iso(o.start_utc),
                                    "end_utc": _iso(o.end_utc),
                                    "seconds": o.seconds,
                                }
                                for o in c.overlaps
                            ],
                            "day_changes": [
                                {
                                    "academic_day": d.academic_day,
                                    "before_seconds": d.before_seconds,
                                    "after_seconds": d.after_seconds,
                                    "delta_seconds": d.delta_seconds,
                                }
                                for d in c.day_changes
                            ],
                        }
                        for c in s.affected_checkins
                    ],
                }
                for s in self.students
            ],
        }


def _iso(moment: datetime) -> str:
    return to_utc(moment).isoformat().replace("+00:00", "Z")


def approved_revisions(
    revisions: list[Revision],
) -> list[Revision]:
    """仅保留已批准且未撤销的修订，按版本升序。"""

    return sorted(
        (r for r in revisions if r.status == RevisionStatus.APPROVED),
        key=lambda r: (r.version, r.revision_id),
    )


def revisions_effective_at(
    revisions: list[Revision], cutoff_seq: int | None
) -> list[Revision]:
    """返回在 cutoff_seq 时刻已经生效的修订。

    批准/撤销都以只追加事件记录，并各自携带其在事件流中的单调序号。冻结
    快照依据 cutoff 序号重放时，只有“批准事件已发生且撤销事件尚未发生”的
    修订参与叠加，因此旧冻结继续使用当时的版本。cutoff_seq 为 None 时按
    当前状态返回所有已批准修订。
    """

    if cutoff_seq is None:
        return approved_revisions(revisions)

    effective: list[Revision] = []
    for rev in revisions:
        approved_at = rev.approved_seq
        if approved_at is None or approved_at > cutoff_seq:
            continue
        revoked_at = rev.revoked_seq
        if revoked_at is not None and revoked_at <= cutoff_seq:
            continue
        effective.append(rev)
    return sorted(effective, key=lambda r: (r.version, r.revision_id))


def resolve_effective_revisions(
    revisions: list[Revision],
    *,
    student_ids: set[str] | None = None,
) -> dict[tuple[str, str], Revision]:
    """计算 (activity_id, student_id) -> 生效修订。

    版本号越大越新；全局修订（student_ids is None）作为默认，学生级例外
    （student_ids 显式列名）在同一活动上始终优先于全局修订，无论版本先后，
    因为它描述的是该学生的特批时间窗。学生级例外之间仍取最高版本。

    student_ids 给定时只解析这些学生；否则从修订覆盖名单与调用方提供的
    学生集合（默认空）的并集解析。
    """

    target_students = set(student_ids or ())
    global_rev: dict[str, Revision] = {}
    scoped: dict[tuple[str, str], Revision] = {}
    for rev in approved_revisions(revisions):
        if rev.student_ids is None:
            current = global_rev.get(rev.activity_id)
            if current is None or rev.version > current.version:
                global_rev[rev.activity_id] = rev
        else:
            target_students.update(rev.student_ids)
            for sid in rev.student_ids:
                key = (rev.activity_id, sid)
                current = scoped.get(key)
                if current is None or rev.version > current.version:
                    scoped[key] = rev

    effective: dict[tuple[str, str], Revision] = {}
    for activity_id, rev in global_rev.items():
        for sid in target_students:
            effective.setdefault((activity_id, sid), rev)
    for key, rev in scoped.items():
        effective[key] = rev
    return effective


def _day_map(
    intervals: list[tuple[datetime, datetime]], tz_name: str
) -> dict[str, int]:
    totals: dict[str, int] = {}
    for start, end in merge_intervals(intervals):
        for day, seg_start, seg_end in split_by_academic_day(start, end, tz_name):
            key = day.isoformat()
            totals[key] = totals.get(key, 0) + elapsed_seconds(seg_start, seg_end)
    return totals


def _day_changes(
    before: list[tuple[datetime, datetime]],
    after: list[tuple[datetime, datetime]],
    tz_name: str,
) -> list[DayAttributionChange]:
    before_map = _day_map(before, tz_name)
    after_map = _day_map(after, tz_name)
    changes = []
    for day in sorted(set(before_map) | set(after_map)):
        b = before_map.get(day, 0)
        a = after_map.get(day, 0)
        if b != a:
            changes.append(DayAttributionChange(academic_day=day, before_seconds=b, after_seconds=a))
    return changes


def _intersect(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
) -> OverlapInterval | None:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end > start:
        return OverlapInterval(start_utc=start, end_utc=end)
    return None


def preview_revision_impact(
    candidate: Revision,
    checkins: list[CheckinRecord],
    *,
    tz_name: str,
    baseline_revisions: list[Revision] | None = None,
) -> RevisionImpact:
    """计算候选修订叠加到当前有效版本之上的影响。

    baseline_revisions 为除候选外已经生效的已批准修订（候选可为草稿）。
    预览以“候选已批准”为假设重放各学生的签到区间，但不修改任何记录。
    """

    baseline = list(baseline_revisions or [])
    hypothetical = Revision(
        revision_id=candidate.revision_id,
        plan_version=candidate.plan_version,
        activity_id=candidate.activity_id,
        version=candidate.version,
        new_start_utc=candidate.new_start_utc,
        new_end_utc=candidate.new_end_utc,
        student_ids=candidate.student_ids,
        reason=candidate.reason,
        status=RevisionStatus.APPROVED,
        created_by=candidate.created_by,
    )

    before_effective = resolve_effective_revisions(
        baseline,
        student_ids={c.student_id for c in checkins},
    )
    after_revisions = baseline + [hypothetical]
    after_effective = resolve_effective_revisions(
        after_revisions,
        student_ids={c.student_id for c in checkins},
    )

    # 按学生分组签到。
    by_student: dict[str, list[CheckinRecord]] = {}
    for record in checkins:
        by_student.setdefault(record.student_id, []).append(record)

    student_impacts: list[StudentImpact] = []
    target_ids = (
        set(by_student)
        if candidate.student_ids is None
        else set(candidate.student_ids) & set(by_student)
    )
    for sid in sorted(target_ids):
        records = by_student.get(sid, [])
        if not records:
            continue

        before_conf: list[tuple[datetime, datetime]] = []
        before_pending: list[tuple[datetime, datetime]] = []
        after_conf: list[tuple[datetime, datetime]] = []
        after_pending: list[tuple[datetime, datetime]] = []
        affected: list[AffectedCheckin] = []

        for record in records:
            b_rev = before_effective.get((record.activity_id, sid))
            a_rev = after_effective.get((record.activity_id, sid))
            b_start = b_rev.new_start_utc if b_rev is not None else record.start_utc
            b_end = b_rev.new_end_utc if b_rev is not None else record.end_utc
            a_start = a_rev.new_start_utc if a_rev is not None else record.start_utc
            a_end = a_rev.new_end_utc if a_rev is not None else record.end_utc

            if record.counts:
                before_conf.append((b_start, b_end))
                after_conf.append((a_start, a_end))
            else:
                before_pending.append((b_start, b_end))
                after_pending.append((a_start, a_end))

            if record.activity_id == candidate.activity_id and (
                a_start != b_start or a_end != b_end
            ):
                overlap = _intersect(
                    record.start_utc, record.end_utc, a_start, a_end
                )
                day_changes = _day_changes(
                    [(b_start, b_end)], [(a_start, a_end)], tz_name
                )
                affected.append(
                    AffectedCheckin(
                        event_id=record.event_id,
                        before_start_utc=b_start,
                        before_end_utc=b_end,
                        after_start_utc=a_start,
                        after_end_utc=a_end,
                        revision_id=a_rev.revision_id if a_rev is not None else candidate.revision_id,
                        version=a_rev.version if a_rev is not None else candidate.version,
                        overlaps=[overlap] if overlap is not None else [],
                        day_changes=day_changes,
                    )
                )

        from .clock import union_seconds

        b_conf = union_seconds(before_conf)
        a_conf = union_seconds(after_conf)
        b_pending = union_seconds(before_pending)
        a_pending = union_seconds(after_pending)
        day_changes = _day_changes(before_conf, after_conf, tz_name)

        if affected:
            student_impacts.append(
                StudentImpact(
                    student_id=sid,
                    before_confirmed_seconds=b_conf,
                    after_confirmed_seconds=a_conf,
                    before_pending_seconds=b_pending,
                    after_pending_seconds=a_pending,
                    affected_checkins=sorted(affected, key=lambda c: c.event_id),
                    day_changes=day_changes,
                )
            )

    return RevisionImpact(
        revision_id=candidate.revision_id,
        activity_id=candidate.activity_id,
        version=candidate.version,
        student_ids=candidate.student_ids,
        affected_student_count=len(student_impacts),
        students=student_impacts,
    )
