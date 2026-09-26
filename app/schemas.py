"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal[
        "checkin", "mentor_confirm", "leave_correction", "activity_revision"
    ]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    # 旧冻结快照可能缺少以下溯源字段，故提供默认值。
    original_check_in_at_utc: str | None = None
    original_check_out_at_utc: str | None = None
    applied_revision_id: str | None = None
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]
    activity_revisions: list[dict[str, Any]] = []


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


class ActivityRevisionDraftIn(BaseModel):
    revision_id: str = Field(..., min_length=1, max_length=64)
    check_in_at: datetime
    check_out_at: datetime
    exempt_student_ids: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("revision_id")
    @classmethod
    def _revision_id_charset(cls, v: str) -> str:
        if "#" in v or not v.strip():
            raise ValueError("revision_id must be non-blank and must not contain '#'")
        return v

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v

    @model_validator(mode="after")
    def _check_order(self) -> "ActivityRevisionDraftIn":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self


class RevisionActionIn(BaseModel):
    reason: str = ""


class ActivityRevisionOut(BaseModel):
    plan_version: str
    activity_id: str
    revision_id: str
    status: str
    effective: bool
    check_in_at_utc: str
    check_out_at_utc: str
    exempt_student_ids: list[str]
    reason: str
    drafted_event_id: str
    approved_event_id: str | None
    revoked_event_id: str | None


class ActivityRevisionListOut(BaseModel):
    plan_version: str
    activity_id: str
    effective_revision_id: str | None
    revisions: list[ActivityRevisionOut]


class IntervalOut(BaseModel):
    start_utc: str
    end_utc: str
    seconds: int


class RevisionImpactStudentOut(BaseModel):
    student_id: str
    exempt: bool
    affected: bool
    before_intervals: list[IntervalOut]
    after_intervals: list[IntervalOut]
    overlap_intervals: list[IntervalOut]
    before_seconds: int
    after_seconds: int
    delta_seconds: int
    daily_before: list[DailyTotal]
    daily_after: list[DailyTotal]
    total_seconds_before: int
    total_seconds_after: int


class RevisionImpactOut(BaseModel):
    plan_version: str
    activity_id: str
    revision_id: str
    revision_status: str
    currently_effective: bool
    candidate: dict[str, Any]
    affected_students: int
    students: list[RevisionImpactStudentOut]
