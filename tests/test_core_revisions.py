"""活动时间修订的内核重放测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.clock import to_utc
from app.core.replay import (
    CheckinStatus,
    Event,
    EventType,
    replay,
)
from app.core.revisions import (
    ActivityRevision,
    RevisionStatus,
    compute_revision_impact,
)


def _event(
    event_id: str,
    event_type: EventType,
    student_id: str,
    payload: dict,
    plan_version: str = "P1",
) -> Event:
    return Event(
        event_id=event_id,
        plan_version=plan_version,
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def _checkin(
    eid: str,
    student: str,
    start: str,
    end: str,
    *,
    activity_type: str = "regular",
    activity_id: str = "A1",
) -> Event:
    return _event(
        eid,
        EventType.CHECKIN,
        student,
        {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    )


def _revision(
    eid: str,
    rid: str,
    action: str,
    *,
    activity_id: str = "A1",
    start: str | None = None,
    end: str | None = None,
    exempt: tuple[str, ...] = (),
) -> Event:
    payload: dict = {
        "revision_id": rid,
        "activity_id": activity_id,
        "action": action,
    }
    if start is not None:
        payload["check_in_at"] = start
    if end is not None:
        payload["check_out_at"] = end
    if exempt:
        payload["exempt_student_ids"] = list(exempt)
    return _event(eid, EventType.ACTIVITY_REVISION, f"activity:{activity_id}", payload)


def _replay(events, **kwargs):
    params = dict(
        plan_version="P1", timezone_name="Asia/Shanghai", required_seconds=0
    )
    params.update(kwargs)
    return replay(events, **params)


def test_approved_revision_retimes_checkins_without_rewriting_source():
    events = [
        _checkin(
            "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"
        ),
        _revision(
            "R-01",
            "REV-1",
            "draft",
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
        ),
        _revision("R-02", "REV-1", "approve"),
    ]
    state = _replay(events)
    progress = state.students["S1"]
    # 有效区间变为 09:00-12:00，原始签到未被改写。
    assert progress.confirmed_seconds == 3 * 3600
    record = progress.checkins[0]
    assert record.applied_revision_id == "REV-1"
    assert record.original_start_utc == to_utc(
        datetime.fromisoformat("2024-03-15T08:00:00+08:00")
    )
    assert record.original_end_utc == to_utc(
        datetime.fromisoformat("2024-03-15T12:00:00+08:00")
    )
    revision = state.revisions["A1"][0]
    assert revision.status == RevisionStatus.APPROVED
    assert revision.effective is True


def test_draft_without_approval_does_not_apply():
    events = [
        _checkin(
            "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"
        ),
        _revision(
            "R-01",
            "REV-1",
            "draft",
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
        ),
    ]
    state = _replay(events)
    assert state.students["S1"].confirmed_seconds == 4 * 3600
    revision = state.revisions["A1"][0]
    assert revision.status == RevisionStatus.DRAFT
    assert revision.effective is False


def test_multiple_revisions_latest_approved_wins_and_revoke_falls_back():
    events = [
        _checkin(
            "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"
        ),
        _revision(
            "R-01",
            "REV-1",
            "draft",
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
        ),
        _revision("R-02", "REV-1", "approve"),
        _revision(
            "R-03",
            "REV-2",
            "draft",
            start="2024-03-15T10:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
        ),
        _revision("R-04", "REV-2", "approve"),
    ]
    state = _replay(events)
    # 最新批准的 REV-2 生效，REV-1 被取代。
    assert state.students["S1"].confirmed_seconds == 2 * 3600
    by_id = {r.revision_id: r for r in state.revisions["A1"]}
    assert by_id["REV-1"].status == RevisionStatus.SUPERSEDED
    assert by_id["REV-1"].effective is False
    assert by_id["REV-2"].status == RevisionStatus.APPROVED
    assert by_id["REV-2"].effective is True

    # 撤销 REV-2 后回退到 REV-1。
    events.append(_revision("R-05", "REV-2", "revoke"))
    state = _replay(events)
    assert state.students["S1"].confirmed_seconds == 3 * 3600
    by_id = {r.revision_id: r for r in state.revisions["A1"]}
    assert by_id["REV-2"].status == RevisionStatus.REVOKED
    assert by_id["REV-1"].status == RevisionStatus.APPROVED
    assert by_id["REV-1"].effective is True

    # 再撤销 REV-1，回退到签到原始时间。
    events.append(_revision("R-06", "REV-1", "revoke"))
    state = _replay(events)
    assert state.students["S1"].confirmed_seconds == 4 * 3600
    assert state.students["S1"].checkins[0].applied_revision_id is None


def test_exempt_student_keeps_original_interval():
    events = [
        _checkin(
            "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"
        ),
        _checkin(
            "E-02", "S3", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"
        ),
        _revision(
            "R-01",
            "REV-1",
            "draft",
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
            exempt=("S3",),
        ),
        _revision("R-02", "REV-1", "approve"),
    ]
    state = _replay(events)
    assert state.students["S1"].confirmed_seconds == 3 * 3600
    assert state.students["S1"].checkins[0].applied_revision_id == "REV-1"
    # 被豁免的学生保留原始签到区间。
    assert state.students["S3"].confirmed_seconds == 4 * 3600
    assert state.students["S3"].checkins[0].applied_revision_id is None


def test_cross_midnight_revision_shifts_day_attribution():
    events = [
        _checkin(
            "E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00"
        ),
        _revision(
            "R-01",
            "REV-1",
            "draft",
            start="2024-03-15T23:00:00+08:00",
            end="2024-03-16T03:00:00+08:00",
        ),
        _revision("R-02", "REV-1", "approve"),
    ]
    state = _replay(events)
    daily = {d.academic_day: d.seconds for d in state.students["S1"].daily}
    # 跨午夜活动整体后移一小时，日归属随之改变。
    assert daily == {"2024-03-15": 3600, "2024-03-16": 3 * 3600}


def test_revision_also_retimes_pending_internship_checkins():
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T12:00:00+08:00",
            activity_type="internship",
        ),
        _revision(
            "R-01",
            "REV-1",
            "draft",
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
        ),
        _revision("R-02", "REV-1", "approve"),
    ]
    state = _replay(events)
    progress = state.students["S1"]
    assert progress.checkins[0].status == CheckinStatus.PENDING
    assert progress.pending_seconds == 3 * 3600
    assert progress.confirmed_seconds == 0


def test_replay_up_to_event_id_uses_revision_version_at_the_time():
    events = [
        _checkin(
            "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"
        ),
        _revision(
            "R-01",
            "REV-1",
            "draft",
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
        ),
        _revision("R-02", "REV-1", "approve"),
    ]
    # 截止于草拟事件的重放（等同于当时冻结）只看到原始时间。
    past = _replay(events, up_to_event_id="R-01")
    assert past.students["S1"].confirmed_seconds == 4 * 3600
    assert past.revisions["A1"][0].status == RevisionStatus.DRAFT
    # 完整重放应用已批准修订。
    full = _replay(events)
    assert full.students["S1"].confirmed_seconds == 3 * 3600


def test_invalid_revision_events_are_ignored():
    events = [
        _checkin(
            "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"
        ),
        # 审批未知修订：忽略。
        _revision("R-01", "REV-X", "approve"),
        # 结束早于开始的草拟：忽略。
        _revision(
            "R-02",
            "REV-2",
            "draft",
            start="2024-03-15T12:00:00+08:00",
            end="2024-03-15T09:00:00+08:00",
        ),
        # 缺少时间的草拟：忽略。
        _revision("R-03", "REV-3", "draft"),
        _revision(
            "R-04",
            "REV-4",
            "draft",
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
        ),
        # 同一 revision_id 的重复草拟：首次生效。
        _revision(
            "R-05",
            "REV-4",
            "draft",
            start="2024-03-15T10:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
        ),
        _revision("R-06", "REV-4", "approve"),
        # 已批准修订不能再次审批（状态机忽略），重复撤销同样忽略。
        _revision("R-07", "REV-4", "revoke"),
        _revision("R-08", "REV-4", "approve"),
        _revision("R-09", "REV-4", "revoke"),
    ]
    state = _replay(events)
    assert "REV-2" not in {r.revision_id for r in state.revisions.get("A1", [])}
    assert "REV-3" not in {r.revision_id for r in state.revisions.get("A1", [])}
    rev4 = {r.revision_id: r for r in state.revisions["A1"]}["REV-4"]
    assert rev4.status == RevisionStatus.REVOKED
    assert rev4.start_utc == to_utc(
        datetime.fromisoformat("2024-03-15T09:00:00+08:00")
    )
    # 所有修订均被撤销，回到原始签到时间。
    assert state.students["S1"].confirmed_seconds == 4 * 3600


def test_revision_fold_is_deterministic_under_shuffle():
    import random

    events = [
        _checkin(
            "E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"
        ),
        _revision(
            "R-01",
            "REV-1",
            "draft",
            start="2024-03-15T09:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
        ),
        _revision("R-02", "REV-1", "approve"),
        _revision(
            "R-03",
            "REV-2",
            "draft",
            start="2024-03-15T10:00:00+08:00",
            end="2024-03-15T12:00:00+08:00",
        ),
        _revision("R-04", "REV-2", "approve"),
    ]
    baseline = _replay(events)
    rng = random.Random(7)
    shuffled = list(events)
    rng.shuffle(shuffled)
    replayed = _replay(shuffled)
    assert (
        baseline.students["S1"].confirmed_seconds
        == replayed.students["S1"].confirmed_seconds
        == 2 * 3600
    )
    for activity_id in baseline.revisions:
        left = [(r.revision_id, r.status, r.effective) for r in baseline.revisions[activity_id]]
        right = [(r.revision_id, r.status, r.effective) for r in replayed.revisions[activity_id]]
        assert left == right


def test_compute_revision_impact_reports_overlap_and_day_attribution():
    checkin = _checkin(
        "E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00"
    )
    before = _replay([checkin])
    candidate = ActivityRevision(
        revision_id="REV-1",
        activity_id="A1",
        start_utc=to_utc(datetime.fromisoformat("2024-03-15T23:00:00+08:00")),
        end_utc=to_utc(datetime.fromisoformat("2024-03-16T03:00:00+08:00")),
        exempt_student_ids=frozenset(),
        reason="",
        drafted_event_id="R-01",
    )
    after = _replay([checkin], revision_overrides={"A1": candidate})
    impact = compute_revision_impact(
        before, after, activity_id="A1", candidate=candidate, timezone_name="Asia/Shanghai"
    )
    assert impact["affected_students"] == 1
    student = impact["students"][0]
    assert student["student_id"] == "S1"
    assert student["exempt"] is False
    # 新旧区间的重叠部分为 23:00-02:00（3 小时）。
    assert student["overlap_intervals"] == [
        {
            "start_utc": "2024-03-15T15:00:00Z",
            "end_utc": "2024-03-15T18:00:00Z",
            "seconds": 3 * 3600,
        }
    ]
    assert student["before_seconds"] == 4 * 3600
    assert student["after_seconds"] == 4 * 3600
    assert student["delta_seconds"] == 0
    # 日归属从 2h+2h 变为 1h+3h。
    assert student["daily_before"] == [
        {"academic_day": "2024-03-15", "seconds": 7200},
        {"academic_day": "2024-03-16", "seconds": 7200},
    ]
    assert student["daily_after"] == [
        {"academic_day": "2024-03-15", "seconds": 3600},
        {"academic_day": "2024-03-16", "seconds": 3 * 3600},
    ]
