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
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
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
    raw_seconds: int
    effective_start_utc: str
    effective_end_utc: str
    effective_seconds: int
    applied_revision_id: str | None
    applied_revision_version: int | None
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
    event_cutoff_seq: int | None = None
    students: list[dict[str, Any]]
    applied_revisions: list[dict[str, Any]] = []


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


# ---------------------------------------------------------------------------
# 活动时间修订
# ---------------------------------------------------------------------------

RevisionStatusLiteral = Literal["draft", "approved", "revoked"]


class RevisionDraftIn(BaseModel):
    revision_id: str = Field(..., min_length=1, max_length=128)
    activity_id: str = Field(..., min_length=1, max_length=128)
    new_start_at: datetime
    new_end_at: datetime
    # None/缺省 = 全体学生；名单 = 部分学生例外
    student_ids: list[str] | None = None
    reason: str = ""
    created_by: str = Field("system", min_length=1, max_length=128)

    @model_validator(mode="after")
    def _check_window(self) -> "RevisionDraftIn":
        if self.new_end_at <= self.new_start_at:
            raise ValueError("new_end_at must be after new_start_at")
        return self

    @field_validator("new_start_at", "new_end_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v

    @field_validator("student_ids")
    @classmethod
    def _clean_student_ids(
        cls, v: list[str] | None
    ) -> list[str] | None:
        if v is not None:
            cleaned = sorted({s for s in v if s})
            if not cleaned:
                raise ValueError("student_ids must not be empty")
            return cleaned
        return v


class RevisionPreviewIn(BaseModel):
    # 二选一：给 revision_id 预览已存草稿，或内联时间窗做临时预览。
    revision_id: str | None = Field(None, min_length=1, max_length=128)
    activity_id: str | None = Field(None, min_length=1, max_length=128)
    new_start_at: datetime | None = None
    new_end_at: datetime | None = None
    student_ids: list[str] | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _check_shape(self) -> "RevisionPreviewIn":
        if self.revision_id is not None:
            return self
        if not (
            self.activity_id is not None
            and self.new_start_at is not None
            and self.new_end_at is not None
        ):
            raise ValueError(
                "revision_id 或完整的 activity_id/new_start_at/new_end_at 必填"
            )
        if self.new_end_at <= self.new_start_at:
            raise ValueError("new_end_at must be after new_start_at")
        return self

    @field_validator("new_start_at", "new_end_at")
    @classmethod
    def _ensure_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v

    @field_validator("student_ids")
    @classmethod
    def _clean_student_ids(
        cls, v: list[str] | None
    ) -> list[str] | None:
        if v is not None:
            cleaned = sorted({s for s in v if s})
            if not cleaned:
                raise ValueError("student_ids must not be empty")
            return cleaned
        return v


class RevisionActionIn(BaseModel):
    actor_id: str = Field(..., min_length=1, max_length=128)


class RevisionOut(BaseModel):
    revision_id: str
    plan_version: str
    activity_id: str
    version: int
    status: RevisionStatusLiteral
    new_start_at: str
    new_end_at: str
    student_ids: list[str] | None
    reason: str
    created_by: str
    approved_by: str | None
    revoked_by: str | None
    created_at: str | None
    approved_at: str | None
    revoked_at: str | None
    approved_event_id: str | None
    revoked_event_id: str | None
    approved_seq: int | None = None
    revoked_seq: int | None = None


class RevisionListOut(BaseModel):
    plan_version: str
    activity_id: str | None
    count: int
    revisions: list[RevisionOut]


class RevisionImpactOut(BaseModel):
    revision_id: str
    activity_id: str
    version: int
    student_ids: list[str] | None
    affected_student_count: int
    students: list[dict[str, Any]]
