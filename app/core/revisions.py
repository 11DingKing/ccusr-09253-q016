"""活动时间修订：草拟、审批、撤销的折叠与影响计算。

修订是事件流中的一等事件（``activity_revision``），重放内核先在第一遍
折叠出每个活动的修订状态机，再在第二遍解释签到时应用有效版本，原始签到
事件保持不变。同一活动的修订生命周期为：

    draft -> approved -> (被更新的审批取代为 superseded) / revoked

撤销当前有效修订后，最近一个仍处 approved 状态的修订重新生效；若不存在，
则回退到签到原始时间。审批/撤销事件按 event_id 排序确定先后，因此并发
审批的结果是确定性的。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .clock import (
    elapsed_seconds,
    format_utc,
    intersect_intervals,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from .replay import Event, ReplayState


REVISION_EVENT_TYPE = "activity_revision"


class RevisionAction(StrEnum):
    DRAFT = "draft"
    APPROVE = "approve"
    REVOKE = "revoke"


class RevisionStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


@dataclass(frozen=True)
class ActivityRevision:
    """一条活动起止时间修订及其推导状态。"""

    revision_id: str
    activity_id: str
    start_utc: datetime
    end_utc: datetime
    exempt_student_ids: frozenset[str]
    reason: str
    drafted_event_id: str
    approved_event_id: str | None = None
    revoked_event_id: str | None = None
    status: RevisionStatus = RevisionStatus.DRAFT
    effective: bool = False

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)


def revision_event_id(revision_id: str, action: RevisionAction) -> str:
    """生成可排序且幂等的事件标识：draft < approve < revoke。"""
    sequence = {
        RevisionAction.DRAFT: "0",
        RevisionAction.APPROVE: "1",
        RevisionAction.REVOKE: "2",
    }[action]
    return f"{revision_id}#{sequence}-{action.value}"


def fold_activity_revisions(
    sorted_events: Iterable["Event"],
) -> dict[str, list[ActivityRevision]]:
    """按 event_id 顺序折叠修订事件，推导每个活动的修订状态。

    调用方需先按计划过滤并按 event_id 排序。无效迁移（如重复草拟、
    审批未知或已终结的修订）被忽略，保证重放确定性。
    """
    drafts: dict[str, ActivityRevision] = {}
    order: list[str] = []

    for event in sorted_events:
        if event.event_type != REVISION_EVENT_TYPE:
            continue
        payload = event.payload
        revision_id = str(payload.get("revision_id", "") or "")
        if not revision_id:
            continue
        action = payload.get("action")

        if action == RevisionAction.DRAFT:
            if revision_id in drafts:
                continue  # 首次草拟生效，重复草拟忽略
            try:
                start = to_utc(datetime.fromisoformat(payload["check_in_at"]))
                end = to_utc(datetime.fromisoformat(payload["check_out_at"]))
            except (KeyError, TypeError, ValueError):
                continue  # 时间缺失或不合法的草拟无效
            if end <= start:
                continue
            exempt = frozenset(
                str(s) for s in (payload.get("exempt_student_ids") or [])
            )
            drafts[revision_id] = ActivityRevision(
                revision_id=revision_id,
                activity_id=str(payload.get("activity_id", "") or ""),
                start_utc=start,
                end_utc=end,
                exempt_student_ids=exempt,
                reason=str(payload.get("reason", "") or ""),
                drafted_event_id=event.event_id,
            )
            order.append(revision_id)
        elif action == RevisionAction.APPROVE:
            revision = drafts.get(revision_id)
            if revision is None or revision.status != RevisionStatus.DRAFT:
                continue
            drafts[revision_id] = replace(
                revision,
                status=RevisionStatus.APPROVED,
                approved_event_id=event.event_id,
            )
        elif action == RevisionAction.REVOKE:
            revision = drafts.get(revision_id)
            if revision is None or revision.status == RevisionStatus.REVOKED:
                continue
            drafts[revision_id] = replace(
                revision,
                status=RevisionStatus.REVOKED,
                revoked_event_id=event.event_id,
            )

    by_activity: dict[str, list[ActivityRevision]] = {}
    for revision_id in order:
        revision = drafts[revision_id]
        by_activity.setdefault(revision.activity_id, []).append(revision)

    # 每个活动内，最近一个已批准且未撤销的修订为有效版本；
    # 其余已批准的修订标记为 superseded。
    for activity_id, revisions in by_activity.items():
        approved = [r for r in revisions if r.status == RevisionStatus.APPROVED]
        if not approved:
            continue
        winner = max(approved, key=lambda r: str(r.approved_event_id))
        derived: list[ActivityRevision] = []
        for revision in revisions:
            if revision.revision_id == winner.revision_id:
                derived.append(replace(revision, effective=True))
            elif revision.status == RevisionStatus.APPROVED:
                derived.append(
                    replace(revision, status=RevisionStatus.SUPERSEDED)
                )
            else:
                derived.append(revision)
        by_activity[activity_id] = derived

    return by_activity


def effective_revision_map(
    revisions_by_activity: Mapping[str, list[ActivityRevision]],
) -> dict[str, ActivityRevision]:
    """提取每个活动当前有效的修订。"""
    effective: dict[str, ActivityRevision] = {}
    for activity_id, revisions in revisions_by_activity.items():
        for revision in revisions:
            if revision.effective:
                effective[activity_id] = revision
                break
    return effective


def revision_to_dict(revision: ActivityRevision) -> dict[str, Any]:
    """序列化修订视图（用于快照与 API 输出）。"""
    return {
        "revision_id": revision.revision_id,
        "activity_id": revision.activity_id,
        "status": revision.status.value,
        "effective": revision.effective,
        "check_in_at_utc": format_utc(revision.start_utc),
        "check_out_at_utc": format_utc(revision.end_utc),
        "exempt_student_ids": sorted(revision.exempt_student_ids),
        "reason": revision.reason,
        "drafted_event_id": revision.drafted_event_id,
        "approved_event_id": revision.approved_event_id,
        "revoked_event_id": revision.revoked_event_id,
    }


def _daily_breakdown(
    intervals: list[tuple[datetime, datetime]], tz_name: str
) -> list[dict[str, Any]]:
    """把区间按教学日归属拆分汇总。"""
    totals: dict[str, int] = {}
    for start, end in merge_intervals(intervals):
        for day, seg_start, seg_end in split_by_academic_day(start, end, tz_name):
            key = day.isoformat()
            totals[key] = totals.get(key, 0) + elapsed_seconds(seg_start, seg_end)
    return [
        {"academic_day": day, "seconds": seconds}
        for day, seconds in sorted(totals.items())
    ]


def _intervals_out(
    intervals: list[tuple[datetime, datetime]],
) -> list[dict[str, Any]]:
    return [
        {
            "start_utc": format_utc(start),
            "end_utc": format_utc(end),
            "seconds": elapsed_seconds(start, end),
        }
        for start, end in merge_intervals(intervals)
    ]


def compute_revision_impact(
    before: "ReplayState",
    after: "ReplayState",
    *,
    activity_id: str,
    candidate: ActivityRevision,
    timezone_name: str,
) -> dict[str, Any]:
    """对比候选修订生效前后的重放状态，计算影响预览。

    输出每个相关学生的有效区间变化、新旧重叠区间、按教学日的归属
    变化以及学生总学时变化；被豁免的学生以 ``exempt`` 标记。
    """
    students: list[dict[str, Any]] = []
    affected = 0
    for student_id in sorted(before.students):
        progress_before = before.students[student_id]
        checkins_before = [
            c for c in progress_before.checkins if c.activity_id == activity_id
        ]
        if not checkins_before:
            continue
        progress_after = after.students.get(student_id)
        checkins_after = (
            [c for c in progress_after.checkins if c.activity_id == activity_id]
            if progress_after is not None
            else []
        )

        before_intervals = [(c.start_utc, c.end_utc) for c in checkins_before]
        after_intervals = [(c.start_utc, c.end_utc) for c in checkins_after]
        merged_before = merge_intervals(before_intervals)
        merged_after = merge_intervals(after_intervals)
        overlaps = intersect_intervals(merged_before, merged_after)

        before_seconds = union_seconds(merged_before)
        after_seconds = union_seconds(merged_after)
        is_affected = merged_before != merged_after
        if is_affected:
            affected += 1

        students.append(
            {
                "student_id": student_id,
                "exempt": student_id in candidate.exempt_student_ids,
                "affected": is_affected,
                "before_intervals": _intervals_out(merged_before),
                "after_intervals": _intervals_out(merged_after),
                "overlap_intervals": _intervals_out(overlaps),
                "before_seconds": before_seconds,
                "after_seconds": after_seconds,
                "delta_seconds": after_seconds - before_seconds,
                "daily_before": _daily_breakdown(merged_before, timezone_name),
                "daily_after": _daily_breakdown(merged_after, timezone_name),
                "total_seconds_before": progress_before.total_seconds,
                "total_seconds_after": (
                    progress_after.total_seconds
                    if progress_after is not None
                    else 0
                ),
            }
        )

    return {
        "affected_students": affected,
        "students": students,
    }
