"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.revisions import (
    Revision,
    RevisionStatus,
    preview_revision_impact,
)

from .core.replay import replay
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    get_freeze,
    get_plan,
    get_revision,
    insert_events,
    insert_freeze,
    insert_revision,
    load_events,
    load_events_up_to,
    load_revisions,
    last_event_seq,
    event_id_at_seq,
    next_revision_version,
    transition_revision,
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


class RevisionStateConflictError(Exception):
    pass


class RevisionValidationError(Exception):
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
    revisions = load_revisions(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        revisions=revisions,
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

    cutoff_seq = last_event_seq(db, plan_version)
    cutoff_id = event_id_at_seq(db, cutoff_seq) if cutoff_seq is not None else None
    events = load_events(db, plan_version)
    revisions = load_revisions(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff_id,
        event_cutoff_seq=cutoff_seq,
        revisions=revisions,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff_id,
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
# 活动时间修订
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_window(
    new_start: datetime, new_end: datetime
) -> tuple[datetime, datetime]:
    if new_start.tzinfo is None or new_end.tzinfo is None:
        raise RevisionValidationError("修订时间必须携带时区信息（RFC 3339）")
    start = new_start.astimezone(timezone.utc)
    end = new_end.astimezone(timezone.utc)
    if end <= start:
        raise RevisionValidationError("new_end_at 必须晚于 new_start_at")
    return start, end


def _revision_to_dict(rev: Revision) -> dict[str, Any]:
    return {
        "revision_id": rev.revision_id,
        "plan_version": rev.plan_version,
        "activity_id": rev.activity_id,
        "version": rev.version,
        "status": rev.status.value,
        "new_start_at": rev.new_start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "new_end_at": rev.new_end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "student_ids": (
            None if rev.student_ids is None else sorted(rev.student_ids)
        ),
        "reason": rev.reason,
        "created_by": rev.created_by,
        "approved_by": rev.approved_by,
        "revoked_by": rev.revoked_by,
        "created_at": rev.created_at.isoformat().replace("+00:00", "Z")
        if rev.created_at is not None
        else None,
        "approved_at": rev.approved_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
        if rev.approved_at is not None
        else None,
        "revoked_at": rev.revoked_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
        if rev.revoked_at is not None
        else None,
        "approved_event_id": rev.approved_event_id,
        "revoked_event_id": rev.revoked_event_id,
        "approved_seq": rev.approved_seq,
        "revoked_seq": rev.revoked_seq,
    }


def _load_core_revision(db: Session, plan_version: str, revision_id: str) -> Revision:
    row = get_revision(db, plan_version, revision_id)
    if row is None:
        raise RevisionNotFoundError(
            f"revision '{revision_id}' for plan '{plan_version}' does not exist"
        )
    from .repository import _revision_to_core

    return _revision_to_core(row)


def draft_revision(
    db: Session,
    *,
    plan_version: str,
    revision_id: str,
    activity_id: str,
    new_start_at: datetime,
    new_end_at: datetime,
    student_ids: list[str] | None,
    reason: str,
    created_by: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    start, end = _validate_window(new_start_at, new_end_at)
    if student_ids is not None:
        student_ids = sorted({s for s in student_ids if s})
        if not student_ids:
            raise RevisionValidationError("student_ids 不能为空名单")
    version = next_revision_version(db, plan_version, activity_id)
    detail = {
        "new_start_at": start.isoformat().replace("+00:00", "Z"),
        "new_end_at": end.isoformat().replace("+00:00", "Z"),
        "student_ids": student_ids,
        "reason": reason or "",
    }
    try:
        row = insert_revision(
            db,
            plan_version=plan_version,
            revision_id=revision_id,
            activity_id=activity_id,
            version=version,
            detail=detail,
            created_by=created_by,
        )
    except IntegrityError as exc:  # 并发草拟导致 (activity,version) 冲突
        db.rollback()
        raise RevisionStateConflictError(
            "修订版本号竞争，请重试草拟"
        ) from exc
    if row is None:
        # 相同 revision_id 已存在：返回现有草稿（幂等草拟）。
        return _revision_to_dict(_load_core_revision(db, plan_version, revision_id))
    from .repository import _revision_to_core

    return _revision_to_dict(_revision_to_core(row))


def _checkins_from_replay(db: Session, plan_version: str):
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    state = replay(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )
    return plan, state


def preview_revision(
    db: Session,
    plan_version: str,
    *,
    revision_id: str | None = None,
    activity_id: str | None = None,
    new_start_at: datetime | None = None,
    new_end_at: datetime | None = None,
    student_ids: list[str] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    if revision_id is not None:
        candidate = _load_core_revision(db, plan_version, revision_id)
    else:
        assert activity_id is not None and new_start_at is not None
        assert new_end_at is not None
        start, end = _validate_window(new_start_at, new_end_at)
        version = next_revision_version(db, plan_version, activity_id)
        candidate = Revision(
            revision_id="(preview)",
            plan_version=plan_version,
            activity_id=activity_id,
            version=version,
            new_start_utc=start,
            new_end_utc=end,
            student_ids=None if student_ids is None else frozenset(student_ids),
            reason=reason or "",
            status=RevisionStatus.DRAFT,
        )

    # 基线 = 除候选外的当前已批准修订。
    baseline = [
        r
        for r in load_revisions(db, plan_version)
        if r.revision_id != candidate.revision_id
    ]
    _, state = _checkins_from_replay(db, plan_version)
    all_checkins = [
        record
        for progress in state.students.values()
        for record in progress.checkins
    ]
    impact = preview_revision_impact(
        candidate,
        all_checkins,
        tz_name=plan.iana_timezone,
        baseline_revisions=baseline,
    )
    result = impact.to_dict()
    if revision_id is not None:
        result["revision_id"] = revision_id
    return result


def approve_revision(
    db: Session,
    *,
    plan_version: str,
    revision_id: str,
    approved_by: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    rev = _load_core_revision(db, plan_version, revision_id)
    if rev.status == RevisionStatus.APPROVED:
        return _revision_to_dict(rev)
    if rev.status != RevisionStatus.DRAFT:
        raise RevisionStateConflictError(
            f"修订处于 '{rev.status.value}' 状态，不能批准"
        )

    payload = {
        "revision_id": revision_id,
        "activity_id": rev.activity_id,
        "version": rev.version,
        "new_start_at": rev.new_start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "new_end_at": rev.new_end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "student_ids": None if rev.student_ids is None else sorted(rev.student_ids),
        "reason": rev.reason,
        "approved_by": approved_by,
    }
    ok, _event_id, _seq = transition_revision(
        db,
        plan_version=plan_version,
        revision_id=revision_id,
        from_status=RevisionStatus.DRAFT.value,
        to_status=RevisionStatus.APPROVED.value,
        kind="approve",
        actor=approved_by,
        acted_at=_utcnow(),
        payload=payload,
    )
    if not ok:
        # 并发：唯一赢家已经提交，其余请求返回当前状态。
        current = _load_core_revision(db, plan_version, revision_id)
        return _revision_to_dict(current)
    return _revision_to_dict(_load_core_revision(db, plan_version, revision_id))


def revoke_revision(
    db: Session,
    *,
    plan_version: str,
    revision_id: str,
    revoked_by: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    rev = _load_core_revision(db, plan_version, revision_id)
    if rev.status == RevisionStatus.REVOKED:
        return _revision_to_dict(rev)
    if rev.status != RevisionStatus.APPROVED:
        raise RevisionStateConflictError(
            f"修订处于 '{rev.status.value}' 状态，不能撤销"
        )

    payload = {
        "revision_id": revision_id,
        "activity_id": rev.activity_id,
        "version": rev.version,
        "revoked_by": revoked_by,
    }
    ok, _event_id, _seq = transition_revision(
        db,
        plan_version=plan_version,
        revision_id=revision_id,
        from_status=RevisionStatus.APPROVED.value,
        to_status=RevisionStatus.REVOKED.value,
        kind="revoke",
        actor=revoked_by,
        acted_at=_utcnow(),
        payload=payload,
    )
    if not ok:
        current = _load_core_revision(db, plan_version, revision_id)
        return _revision_to_dict(current)
    return _revision_to_dict(_load_core_revision(db, plan_version, revision_id))


def get_revision_detail(
    db: Session, plan_version: str, revision_id: str
) -> dict[str, Any]:
    return _revision_to_dict(_load_core_revision(db, plan_version, revision_id))


def list_revision_versions(
    db: Session,
    plan_version: str,
    *,
    activity_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    revisions = load_revisions(db, plan_version)
    if activity_id is not None:
        revisions = [r for r in revisions if r.activity_id == activity_id]
    if status is not None:
        revisions = [r for r in revisions if r.status.value == status]
    return {
        "plan_version": plan_version,
        "activity_id": activity_id,
        "count": len(revisions),
        "revisions": [_revision_to_dict(r) for r in revisions],
    }
