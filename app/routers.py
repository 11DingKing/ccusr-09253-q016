"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    ActivityRevisionDraftIn,
    ActivityRevisionListOut,
    ActivityRevisionOut,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    RevisionActionIn,
    RevisionImpactOut,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 活动时间修订
# ---------------------------------------------------------------------------


def _revision_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, services.RevisionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, services.RevisionValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=404, detail=str(exc))


@router.post(
    "/plans/{plan_version}/activities/{activity_id}/revisions",
    response_model=ActivityRevisionOut,
    status_code=status.HTTP_201_CREATED,
)
def draft_revision(
    plan_version: str,
    activity_id: str,
    body: ActivityRevisionDraftIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.draft_activity_revision(
            db,
            plan_version,
            activity_id,
            revision_id=body.revision_id,
            check_in_at=body.check_in_at,
            check_out_at=body.check_out_at,
            exempt_student_ids=body.exempt_student_ids,
            reason=body.reason,
        )
    except (
        services.PlanNotFoundError,
        services.RevisionConflictError,
        services.RevisionValidationError,
    ) as exc:
        raise _revision_errors(exc) from exc


@router.get(
    "/plans/{plan_version}/activities/{activity_id}/revisions",
    response_model=ActivityRevisionListOut,
)
def list_revisions(
    plan_version: str, activity_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.list_activity_revisions(db, plan_version, activity_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/activities/{activity_id}/revisions/{revision_id}",
    response_model=ActivityRevisionOut,
)
def get_revision(
    plan_version: str,
    activity_id: str,
    revision_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.get_activity_revision(
            db, plan_version, activity_id, revision_id
        )
    except (services.PlanNotFoundError, services.RevisionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/activities/{activity_id}/revisions/{revision_id}/impact",
    response_model=RevisionImpactOut,
)
def preview_revision(
    plan_version: str,
    activity_id: str,
    revision_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.preview_revision_impact(
            db, plan_version, activity_id, revision_id
        )
    except (services.PlanNotFoundError, services.RevisionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/activities/{activity_id}/revisions/{revision_id}/approve",
    response_model=ActivityRevisionOut,
)
def approve_revision(
    plan_version: str,
    activity_id: str,
    revision_id: str,
    body: RevisionActionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.approve_activity_revision(
            db, plan_version, activity_id, revision_id, reason=body.reason
        )
    except (
        services.PlanNotFoundError,
        services.RevisionNotFoundError,
        services.RevisionConflictError,
    ) as exc:
        raise _revision_errors(exc) from exc


@router.post(
    "/plans/{plan_version}/activities/{activity_id}/revisions/{revision_id}/revoke",
    response_model=ActivityRevisionOut,
)
def revoke_revision(
    plan_version: str,
    activity_id: str,
    revision_id: str,
    body: RevisionActionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.revoke_activity_revision(
            db, plan_version, activity_id, revision_id, reason=body.reason
        )
    except (services.PlanNotFoundError, services.RevisionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
