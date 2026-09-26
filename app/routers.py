"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    RevisionActionIn,
    RevisionDraftIn,
    RevisionImpactOut,
    RevisionListOut,
    RevisionOut,
    RevisionPreviewIn,
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


@router.post(
    "/plans/{plan_version}/revisions",
    response_model=RevisionOut,
    status_code=status.HTTP_201_CREATED,
)
def draft_revision(
    plan_version: str, body: RevisionDraftIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.draft_revision(
            db,
            plan_version=plan_version,
            revision_id=body.revision_id,
            activity_id=body.activity_id,
            new_start_at=body.new_start_at,
            new_end_at=body.new_end_at,
            student_ids=body.student_ids,
            reason=body.reason,
            created_by=body.created_by,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RevisionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except services.RevisionStateConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/revisions/preview",
    response_model=RevisionImpactOut,
)
def preview_revision(
    plan_version: str, body: RevisionPreviewIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.preview_revision(
            db,
            plan_version,
            revision_id=body.revision_id,
            activity_id=body.activity_id,
            new_start_at=body.new_start_at,
            new_end_at=body.new_end_at,
            student_ids=body.student_ids,
            reason=body.reason,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RevisionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/revisions",
    response_model=RevisionListOut,
)
def list_revisions(
    plan_version: str,
    activity_id: str | None = None,
    status_filter: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.list_revision_versions(
            db, plan_version, activity_id=activity_id, status=status_filter
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/revisions/{revision_id}",
    response_model=RevisionOut,
)
def get_revision(
    plan_version: str, revision_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.get_revision_detail(db, plan_version, revision_id)
    except (services.PlanNotFoundError, services.RevisionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/revisions/{revision_id}/preview",
    response_model=RevisionImpactOut,
)
def preview_stored_revision(
    plan_version: str, revision_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.preview_revision(db, plan_version, revision_id=revision_id)
    except (services.PlanNotFoundError, services.RevisionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/revisions/{revision_id}/approve",
    response_model=RevisionOut,
)
def approve_revision(
    plan_version: str,
    revision_id: str,
    body: RevisionActionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.approve_revision(
            db,
            plan_version=plan_version,
            revision_id=revision_id,
            approved_by=body.actor_id,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RevisionStateConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/revisions/{revision_id}/revoke",
    response_model=RevisionOut,
)
def revoke_revision(
    plan_version: str,
    revision_id: str,
    body: RevisionActionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.revoke_revision(
            db,
            plan_version=plan_version,
            revision_id=revision_id,
            revoked_by=body.actor_id,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RevisionStateConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
