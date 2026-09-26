"""活动时间修订内核测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.replay import (
    CheckinStatus,
    Event,
    EventType,
    replay,
)
from app.core.revisions import (
    Revision,
    RevisionStatus,
    preview_revision_impact,
    resolve_effective_revisions,
    revisions_effective_at,
)

SH = "Asia/Shanghai"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _rev(
    revision_id: str,
    *,
    version: int,
    start: str,
    end: str,
    students: list[str] | None = None,
    status: RevisionStatus = RevisionStatus.APPROVED,
    activity_id: str = "A1",
    approved_seq: int | None = -1,
    revoked_seq: int | None = None,
    approved_event_id: str | None = None,
    revoked_event_id: str | None = None,
) -> Revision:
    if approved_seq == -1:
        approved_seq = version + 1
    return Revision(
        revision_id=revision_id,
        plan_version="P1",
        activity_id=activity_id,
        version=version,
        new_start_utc=_dt(start),
        new_end_utc=_dt(end),
        student_ids=None if students is None else frozenset(students),
        status=status,
        approved_event_id=approved_event_id,
        revoked_event_id=revoked_event_id,
        approved_seq=approved_seq,
        revoked_seq=revoked_seq,
    )


def _checkin_event(
    eid: str,
    student: str,
    start: str,
    end: str,
    *,
    activity_id: str = "A1",
    activity_type: str = "regular",
    seq: int | None = None,
) -> Event:
    return Event(
        event_id=eid,
        plan_version="P1",
        event_type=EventType.CHECKIN,
        student_id=student,
        payload={
            "activity_id": activity_id,
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
        created_at=datetime.now(timezone.utc),
        seq=seq,
    )


def _checkin_record(
    eid: str,
    student: str = "S1",
    start: str = "2024-03-15T08:00:00+08:00",
    end: str = "2024-03-15T10:00:00+08:00",
    *,
    activity_id: str = "A1",
    status: CheckinStatus = CheckinStatus.CONFIRMED,
):
    from app.core.replay import CheckinRecord

    return CheckinRecord(
        event_id=eid,
        student_id=student,
        activity_id=activity_id,
        activity_type="regular",
        start_utc=_dt(start),
        end_utc=_dt(end),
        status=status,
    )


# ---------------------------------------------------------------------------
# 有效版本解析
# ---------------------------------------------------------------------------


def test_higher_version_wins_for_global_revision():
    revs = [
        _rev("R1", version=1, start="2024-03-15T09:00:00+08:00", end="2024-03-15T11:00:00+08:00"),
        _rev("R2", version=2, start="2024-03-15T09:30:00+08:00", end="2024-03-15T11:30:00+08:00"),
    ]
    effective = resolve_effective_revisions(revs, student_ids={"S1", "S2"})
    assert effective[("A1", "S1")].revision_id == "R2"
    assert effective[("A1", "S2")].revision_id == "R2"


def test_revoked_revision_falls_back_to_earlier_version():
    revs = [
        _rev("R1", version=1, start="2024-03-15T09:00:00+08:00", end="2024-03-15T11:00:00+08:00"),
        _rev(
            "R2",
            version=2,
            start="2024-03-15T07:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
            status=RevisionStatus.REVOKED,
            revoked_event_id="REV-REVOKE-R2",
        ),
    ]
    effective = resolve_effective_revisions(revs, student_ids={"S1"})
    assert effective[("A1", "S1")].revision_id == "R1"


def test_scoped_student_exception_overrides_global_even_at_lower_version():
    revs = [
        _rev("R1", version=1, start="2024-03-15T09:00:00+08:00", end="2024-03-15T11:00:00+08:00"),
        _rev(
            "R9",
            version=9,
            start="2024-03-15T10:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
            students=["S3"],
        ),
    ]
    effective = resolve_effective_revisions(revs, student_ids={"S1", "S2", "S3"})
    assert effective[("A1", "S1")].revision_id == "R1"
    assert effective[("A1", "S2")].revision_id == "R1"
    assert effective[("A1", "S3")].revision_id == "R9"


def test_drafts_never_resolve_as_effective():
    revs = [
        _rev(
            "R1",
            version=1,
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T11:00:00+08:00",
            status=RevisionStatus.DRAFT,
            approved_seq=None,
        ),
    ]
    assert resolve_effective_revisions(revs, student_ids={"S1"}) == {}


def test_effective_at_cutoff_includes_approved_but_not_future_or_revoked():
    # 事件流按单调序号排列：
    #   seq1 E-01；seq2 批准 R1；seq3 批准 R2；seq4 撤销 R2；seq5 批准 R3
    revs = [
        _rev(
            "R1",
            version=1,
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T11:00:00+08:00",
            approved_seq=2,
        ),
        _rev(
            "R2",
            version=2,
            start="2024-03-15T07:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
            status=RevisionStatus.REVOKED,
            approved_seq=3,
            revoked_seq=4,
        ),
        _rev(
            "R3",
            version=3,
            start="2024-03-15T06:00:00+08:00",
            end="2024-03-15T13:00:00+08:00",
            approved_seq=5,
        ),
    ]
    # Cutoff at the check-in: nothing approved yet.
    assert {r.revision_id for r in revisions_effective_at(revs, 1)} == set()

    # Cutoff right after R1 approval: only R1.
    assert {r.revision_id for r in revisions_effective_at(revs, 2)} == {"R1"}

    # Cutoff between R2 approval and its revocation: R1 + R2.
    assert {r.revision_id for r in revisions_effective_at(revs, 3)} == {"R1", "R2"}

    # After R2 revocation but before R3 approval: R1 only (R2 revoked, R3 future).
    assert {r.revision_id for r in revisions_effective_at(revs, 4)} == {"R1"}

    # Current state (no cutoff): R1 + R3.
    current = revisions_effective_at(revs, None)
    assert {r.revision_id for r in current} == {"R1", "R3"}


# ---------------------------------------------------------------------------
# 影响预览
# ---------------------------------------------------------------------------


def test_preview_lists_affected_students_day_attribution_and_overlap():
    records = [
        _checkin_record("E-01", "S1"),
        _checkin_record("E-02", "S2"),
    ]
    candidate = _rev(
        "R1",
        version=1,
        start="2024-03-15T09:00:00+08:00",
        end="2024-03-15T11:00:00+08:00",
        status=RevisionStatus.DRAFT,
        approved_seq=None,
    )
    impact = preview_revision_impact(candidate, records, tz_name=SH)
    assert impact.affected_student_count == 2
    s1 = impact.students[0]
    assert s1.student_id == "S1"
    # 08-10 (7200s) -> 09-11 (7200s): total unchanged, but the overlap with
    # the raw window is only 09-10 = 3600s.
    assert s1.confirmed_delta_seconds == 0
    checkin = s1.affected_checkins[0]
    assert len(checkin.overlaps) == 1
    assert checkin.overlaps[0].seconds == 3600
    # Day attribution unchanged because both windows sit fully on 03-15.
    assert checkin.day_changes == []


def test_preview_detects_day_attribution_change_when_shifted_across_midnight():
    records = [
        _checkin_record(
            "E-01",
            "S1",
            "2024-03-15T22:00:00+08:00",
            "2024-03-16T02:00:00+08:00",
        ),
    ]
    candidate = _rev(
        "R1",
        version=1,
        start="2024-03-15T23:00:00+08:00",
        end="2024-03-16T01:00:00+08:00",
        status=RevisionStatus.DRAFT,
        approved_seq=None,
    )
    impact = preview_revision_impact(candidate, records, tz_name=SH)
    s1 = impact.students[0]
    days = {d.academic_day: (d.before_seconds, d.after_seconds) for d in s1.day_changes}
    assert days == {"2024-03-15": (7200, 3600), "2024-03-16": (7200, 3600)}
    checkin = s1.affected_checkins[0]
    # 原始 22-02 与修订 23-01 的重叠为 23-01，共 2 小时，跨两天。
    assert checkin.overlaps[0].seconds == 7200
    assert {d.academic_day for d in checkin.day_changes} == {
        "2024-03-15",
        "2024-03-16",
    }


def test_preview_scoped_revision_only_affects_listed_students():
    records = [
        _checkin_record("E-01", "S1"),
        _checkin_record("E-02", "S2"),
        _checkin_record("E-03", "S3"),
    ]
    candidate = _rev(
        "R2",
        version=2,
        start="2024-03-15T09:00:00+08:00",
        end="2024-03-15T09:30:00+08:00",
        students=["S3"],
        status=RevisionStatus.DRAFT,
        approved_seq=None,
    )
    impact = preview_revision_impact(candidate, records, tz_name=SH)
    assert impact.affected_student_count == 1
    assert impact.students[0].student_id == "S3"
    assert impact.students[0].confirmed_delta_seconds == 1800 - 7200


def test_preview_against_existing_baseline_shows_incremental_effect():
    records = [
        _checkin_record("E-01", "S1"),
        _checkin_record("E-02", "S2"),
    ]
    baseline = [
        _rev("R1", version=1, start="2024-03-15T09:00:00+08:00", end="2024-03-15T11:00:00+08:00"),
    ]
    candidate = _rev(
        "R2",
        version=2,
        start="2024-03-15T09:00:00+08:00",
        end="2024-03-15T10:00:00+08:00",
        status=RevisionStatus.DRAFT,
        approved_seq=None,
    )
    impact = preview_revision_impact(
        candidate, records, tz_name=SH, baseline_revisions=baseline
    )
    s1 = {s.student_id: s for s in impact.students}["S1"]
    # 基线已经是 09-11（7200s），R2 再缩短为 09-10（3600s）-> -3600。
    assert s1.before_confirmed_seconds == 7200
    assert s1.after_confirmed_seconds == 3600
    assert s1.confirmed_delta_seconds == -3600


# ---------------------------------------------------------------------------
# 重放叠加（不改原始签到）
# ---------------------------------------------------------------------------


def test_replay_overlays_approved_revision_without_mutating_raw_times():
    events = [
        _checkin_event(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
        ),
    ]
    revs = [
        _rev(
            "R1",
            version=1,
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T11:00:00+08:00",
        ),
    ]
    state = replay(
        events,
        plan_version="P1",
        timezone_name=SH,
        required_seconds=0,
        revisions=revs,
    )
    progress = state.students["S1"]
    assert progress.confirmed_seconds == 7200
    record = progress.checkins[0]
    # 原始签到时间保持不变；有效窗口来自修订。
    assert record.start_utc == _dt("2024-03-15T08:00:00+08:00")
    assert record.end_utc == _dt("2024-03-15T10:00:00+08:00")
    assert record.window_start_utc == _dt("2024-03-15T09:00:00+08:00")
    assert record.window_end_utc == _dt("2024-03-15T11:00:00+08:00")
    assert record.applied_revision_id == "R1"
    assert {r.revision_id for r in state.applied_revisions} == {"R1"}


def test_replay_with_cutoff_pins_historical_revision_versions():
    events = [
        _checkin_event(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            seq=1,
        ),
        Event(
            event_id="REV-000000000002-APPROVE-R1",
            plan_version="P1",
            event_type=EventType.REVISION_APPROVE,
            student_id="",
            payload={"revision_id": "R1"},
            created_at=datetime.now(timezone.utc),
            seq=2,
        ),
        Event(
            event_id="REV-000000000003-APPROVE-R2",
            plan_version="P1",
            event_type=EventType.REVISION_APPROVE,
            student_id="",
            payload={"revision_id": "R2"},
            created_at=datetime.now(timezone.utc),
            seq=3,
        ),
    ]
    revs = [
        _rev(
            "R1",
            version=1,
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T11:00:00+08:00",
            approved_seq=2,
        ),
        _rev(
            "R2",
            version=2,
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T09:30:00+08:00",
            approved_seq=3,
        ),
    ]
    past = replay(
        events,
        plan_version="P1",
        timezone_name=SH,
        required_seconds=0,
        up_to_seq=2,
        revisions=revs,
    )
    # 旧 cutoff：只有 R1（2 小时）。
    assert past.students["S1"].confirmed_seconds == 7200
    assert {r.revision_id for r in past.applied_revisions} == {"R1"}

    current = replay(
        events,
        plan_version="P1",
        timezone_name=SH,
        required_seconds=0,
        revisions=revs,
    )
    # 当前：R2（30 分钟）。
    assert current.students["S1"].confirmed_seconds == 1800
    assert {r.revision_id for r in current.applied_revisions} == {"R2"}


def test_replay_cross_midnight_revision_reattributes_both_days():
    events = [
        _checkin_event(
            "E-01",
            "S1",
            "2024-03-15T20:00:00+08:00",
            "2024-03-15T23:00:00+08:00",
        ),
    ]
    revs = [
        _rev(
            "R1",
            version=1,
            start="2024-03-15T23:00:00+08:00",
            end="2024-03-16T02:00:00+08:00",
        ),
    ]
    state = replay(
        events,
        plan_version="P1",
        timezone_name=SH,
        required_seconds=0,
        revisions=revs,
    )
    daily = {d.academic_day: d.seconds for d in state.students["S1"].daily}
    assert daily == {"2024-03-15": 3600, "2024-03-16": 7200}
