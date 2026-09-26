"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.replay import EventType, ReplayState, replay
from .core.revisions import (
    ActivityRevision,
    RevisionAction,
    RevisionStatus,
    compute_revision_impact,
    revision_event_id,
    revision_to_dict,
)
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    get_freeze,
    get_plan,
    insert_events,
    insert_freeze,
    load_events,
    load_events_up_to,
    max_event_id,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class RevisionNotFoundError(Exception):
    pass


class RevisionConflictError(Exception):
    pass


class RevisionValidationError(ValueError):
    pass


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# ---------------------------------------------------------------------------
# 活动时间修订：草拟、影响预览、审批、撤销与版本查询
# ---------------------------------------------------------------------------

_REVISION_ACTOR_PREFIX = "activity:"


def _replay_state(db: Session, plan_version: str) -> ReplayState:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    return replay(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )


def _find_revision(
    state: ReplayState, activity_id: str, revision_id: str
) -> ActivityRevision:
    for revision in state.revisions.get(activity_id, []):
        if revision.revision_id == revision_id:
            return revision
    raise RevisionNotFoundError(
        f"revision '{revision_id}' for activity '{activity_id}' does not exist"
    )


def _revision_out(plan_version: str, revision: ActivityRevision) -> dict[str, Any]:
    return {"plan_version": plan_version, **revision_to_dict(revision)}


def _append_revision_event(
    db: Session,
    *,
    plan_version: str,
    activity_id: str,
    revision_id: str,
    action: RevisionAction,
    payload: dict[str, Any],
) -> bool:
    accepted, _ = insert_events(
        db,
        plan_version=plan_version,
        events=[
            {
                "event_id": revision_event_id(revision_id, action),
                "event_type": EventType.ACTIVITY_REVISION.value,
                "student_id": f"{_REVISION_ACTOR_PREFIX}{activity_id}",
                "payload": payload,
            }
        ],
    )
    return bool(accepted)


def list_activity_revisions(
    db: Session, plan_version: str, activity_id: str
) -> dict[str, Any]:
    """版本查询：列出某活动的全部修订及当前有效版本。"""
    state = _replay_state(db, plan_version)
    revisions = state.revisions.get(activity_id, [])
    effective_id = next(
        (r.revision_id for r in revisions if r.effective), None
    )
    return {
        "plan_version": plan_version,
        "activity_id": activity_id,
        "effective_revision_id": effective_id,
        "revisions": [_revision_out(plan_version, r) for r in revisions],
    }


def get_activity_revision(
    db: Session, plan_version: str, activity_id: str, revision_id: str
) -> dict[str, Any]:
    state = _replay_state(db, plan_version)
    return _revision_out(
        plan_version, _find_revision(state, activity_id, revision_id)
    )


def draft_activity_revision(
    db: Session,
    plan_version: str,
    activity_id: str,
    *,
    revision_id: str,
    check_in_at: datetime,
    check_out_at: datetime,
    exempt_student_ids: list[str] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """草拟一份活动时间修订（事件溯源，不改动任何签到）。"""
    _require_plan(db, plan_version)
    if check_in_at.tzinfo is None or check_out_at.tzinfo is None:
        raise RevisionValidationError(
            "revision timestamps must be timezone-aware (RFC 3339)"
        )
    if check_out_at <= check_in_at:
        raise RevisionValidationError("check_out_at must be after check_in_at")

    state = _replay_state(db, plan_version)
    for revisions in state.revisions.values():
        if any(r.revision_id == revision_id for r in revisions):
            raise RevisionConflictError(
                f"revision '{revision_id}' already exists"
            )

    accepted = _append_revision_event(
        db,
        plan_version=plan_version,
        activity_id=activity_id,
        revision_id=revision_id,
        action=RevisionAction.DRAFT,
        payload={
            "revision_id": revision_id,
            "activity_id": activity_id,
            "action": RevisionAction.DRAFT.value,
            "check_in_at": check_in_at.isoformat(),
            "check_out_at": check_out_at.isoformat(),
            "exempt_student_ids": list(exempt_student_ids or []),
            "reason": reason,
        },
    )
    if not accepted:
        # 并发草拟同一 revision_id：后提交者视为冲突。
        raise RevisionConflictError(f"revision '{revision_id}' already exists")
    state = _replay_state(db, plan_version)
    return _revision_out(
        plan_version, _find_revision(state, activity_id, revision_id)
    )


def approve_activity_revision(
    db: Session,
    plan_version: str,
    activity_id: str,
    revision_id: str,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """审批修订：最新已批准的修订成为活动有效版本。

    审批是幂等的：并发或重复审批同一草稿只记录一条审批事件；
    同一活动存在多份已批准修订时，按事件顺序最新者生效。
    """
    _require_plan(db, plan_version)
    state = _replay_state(db, plan_version)
    revision = _find_revision(state, activity_id, revision_id)
    if revision.status == RevisionStatus.REVOKED:
        raise RevisionConflictError(
            f"revision '{revision_id}' has been revoked and cannot be approved"
        )
    if revision.status == RevisionStatus.DRAFT:
        _append_revision_event(
            db,
            plan_version=plan_version,
            activity_id=activity_id,
            revision_id=revision_id,
            action=RevisionAction.APPROVE,
            payload={
                "revision_id": revision_id,
                "activity_id": activity_id,
                "action": RevisionAction.APPROVE.value,
                "reason": reason,
            },
        )
    state = _replay_state(db, plan_version)
    return _revision_out(
        plan_version, _find_revision(state, activity_id, revision_id)
    )


def revoke_activity_revision(
    db: Session,
    plan_version: str,
    activity_id: str,
    revision_id: str,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """撤销修订：草稿被撤回；有效修订被撤销后回退到上一份已批准版本。"""
    _require_plan(db, plan_version)
    state = _replay_state(db, plan_version)
    revision = _find_revision(state, activity_id, revision_id)
    if revision.status != RevisionStatus.REVOKED:
        _append_revision_event(
            db,
            plan_version=plan_version,
            activity_id=activity_id,
            revision_id=revision_id,
            action=RevisionAction.REVOKE,
            payload={
                "revision_id": revision_id,
                "activity_id": activity_id,
                "action": RevisionAction.REVOKE.value,
                "reason": reason,
            },
        )
    state = _replay_state(db, plan_version)
    return _revision_out(
        plan_version, _find_revision(state, activity_id, revision_id)
    )


def preview_revision_impact(
    db: Session, plan_version: str, activity_id: str, revision_id: str
) -> dict[str, Any]:
    """影响预览：假设该修订成为有效版本，计算受影响学生、日归属与重叠区间。"""
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    before = replay(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )
    candidate = _find_revision(before, activity_id, revision_id)
    after = replay(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        revision_overrides={activity_id: candidate},
    )
    impact = compute_revision_impact(
        before,
        after,
        activity_id=activity_id,
        candidate=candidate,
        timezone_name=plan.iana_timezone,
    )
    return {
        "plan_version": plan_version,
        "activity_id": activity_id,
        "revision_id": revision_id,
        "revision_status": candidate.status.value,
        "currently_effective": candidate.effective,
        "candidate": {
            "check_in_at_utc": revision_to_dict(candidate)["check_in_at_utc"],
            "check_out_at_utc": revision_to_dict(candidate)["check_out_at_utc"],
            "exempt_student_ids": sorted(candidate.exempt_student_ids),
            "reason": candidate.reason,
        },
        **impact,
    }
