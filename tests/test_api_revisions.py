"""活动时间修订 API 测试：草拟、影响预览、审批、撤销、版本查询与冻结交互。"""

from __future__ import annotations

import threading

from sqlalchemy import func, select

from app.models import Event as EventModel
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

PV = SHANGHAI_PLAN["plan_version"]


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _checkin(eid, student, start, end, activity_id="A1", activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _import(client, events):
    resp = client.post(f"/api/plans/{PV}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _draft(client, rid, start, end, activity_id="A1", exempt=None, reason=""):
    return client.post(
        f"/api/plans/{PV}/activities/{activity_id}/revisions",
        json={
            "revision_id": rid,
            "check_in_at": start,
            "check_out_at": end,
            "exempt_student_ids": exempt or [],
            "reason": reason,
        },
    )


def _approve(client, rid, activity_id="A1"):
    return client.post(
        f"/api/plans/{PV}/activities/{activity_id}/revisions/{rid}/approve",
        json={},
    )


def _revoke(client, rid, activity_id="A1"):
    return client.post(
        f"/api/plans/{PV}/activities/{activity_id}/revisions/{rid}/revoke",
        json={},
    )


def _progress(client, student):
    resp = client.get(f"/api/plans/{PV}/students/{student}/progress")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_revision_lifecycle_draft_preview_approve_revoke(client):
    _create_plan(client)
    _import(
        client,
        [
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
            _checkin("E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
        ],
    )

    # 草拟：状态为 draft，不影响学时。
    resp = _draft(
        client,
        "REV-001",
        "2024-03-15T09:00:00+08:00",
        "2024-03-15T12:00:00+08:00",
        reason="活动实际开始时间晚了一小时",
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["effective"] is False
    assert body["check_in_at_utc"] == "2024-03-15T01:00:00Z"
    assert _progress(client, "S1")["total_seconds"] == 4 * 3600

    # 影响预览：两名学生受影响，重叠区间 09:00-12:00，每人少 1 小时。
    impact = client.get(
        f"/api/plans/{PV}/activities/A1/revisions/REV-001/impact"
    ).json()
    assert impact["revision_status"] == "draft"
    assert impact["currently_effective"] is False
    assert impact["affected_students"] == 2
    s1 = next(s for s in impact["students"] if s["student_id"] == "S1")
    assert s1["delta_seconds"] == -3600
    assert s1["before_seconds"] == 4 * 3600
    assert s1["after_seconds"] == 3 * 3600
    assert s1["overlap_intervals"] == [
        {
            "start_utc": "2024-03-15T01:00:00Z",
            "end_utc": "2024-03-15T04:00:00Z",
            "seconds": 3 * 3600,
        }
    ]
    assert s1["daily_before"] == [{"academic_day": "2024-03-15", "seconds": 4 * 3600}]
    assert s1["daily_after"] == [{"academic_day": "2024-03-15", "seconds": 3 * 3600}]
    assert s1["total_seconds_after"] == 3 * 3600

    # 审批：生效后进度与签到溯源同步更新。
    approved = _approve(client, "REV-001")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["effective"] is True

    progress = _progress(client, "S1")
    assert progress["total_seconds"] == 3 * 3600
    checkin = progress["checkins"][0]
    assert checkin["applied_revision_id"] == "REV-001"
    assert checkin["check_in_at_utc"] == "2024-03-15T01:00:00Z"
    assert checkin["original_check_in_at_utc"] == "2024-03-15T00:00:00Z"

    # 已生效修订的预览不再有变化。
    impact = client.get(
        f"/api/plans/{PV}/activities/A1/revisions/REV-001/impact"
    ).json()
    assert impact["currently_effective"] is True
    assert impact["affected_students"] == 0

    # 版本查询：REV-001 为当前有效版本。
    listing = client.get(f"/api/plans/{PV}/activities/A1/revisions").json()
    assert listing["effective_revision_id"] == "REV-001"
    assert [r["revision_id"] for r in listing["revisions"]] == ["REV-001"]

    # 撤销：回退到原始签到时间。
    revoked = _revoke(client, "REV-001")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["effective"] is False
    assert _progress(client, "S1")["total_seconds"] == 4 * 3600
    listing = client.get(f"/api/plans/{PV}/activities/A1/revisions").json()
    assert listing["effective_revision_id"] is None


def test_multiple_revisions_supersede_and_freezes_pin_their_versions(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")],
    )
    # 修订前冻结：4 小时。
    f0 = client.post(f"/api/plans/{PV}/freezes/F-0", json={}).json()
    assert f0["students"][0]["total_seconds"] == 4 * 3600
    assert f0["activity_revisions"] == []

    _draft(client, "REV-001", "2024-03-15T09:00:00+08:00", "2024-03-15T12:00:00+08:00")
    _approve(client, "REV-001")
    f1 = client.post(f"/api/plans/{PV}/freezes/F-1", json={}).json()
    assert f1["students"][0]["total_seconds"] == 3 * 3600
    assert f1["event_cutoff_id"] == "REV-001#1-approve"
    assert f1["activity_revisions"][0]["revision_id"] == "REV-001"
    assert f1["activity_revisions"][0]["effective"] is True

    # 第二次修订取代第一次。
    _draft(client, "REV-002", "2024-03-15T10:00:00+08:00", "2024-03-15T12:00:00+08:00")
    _approve(client, "REV-002")
    listing = client.get(f"/api/plans/{PV}/activities/A1/revisions").json()
    by_id = {r["revision_id"]: r for r in listing["revisions"]}
    assert by_id["REV-001"]["status"] == "superseded"
    assert by_id["REV-002"]["status"] == "approved"
    assert listing["effective_revision_id"] == "REV-002"

    f2 = client.post(f"/api/plans/{PV}/freezes/F-2", json={}).json()
    assert f2["students"][0]["total_seconds"] == 2 * 3600

    # 已冻结快照保持各自时代的版本。
    assert client.get(f"/api/plans/{PV}/freezes/F-0").json()["students"][0][
        "total_seconds"
    ] == 4 * 3600
    f1_again = client.get(f"/api/plans/{PV}/freezes/F-1").json()
    assert f1_again["students"][0]["total_seconds"] == 3 * 3600
    assert f1_again["activity_revisions"][0]["status"] == "approved"

    # 冻结差异能解释修订带来的变化。
    diff = client.get(f"/api/plans/{PV}/freezes/F-1/diff/F-2").json()
    assert diff["students_affected"] == 1
    fields = diff["student_changes"][0]["fields"]
    assert fields["total_seconds"] == {"before": 3 * 3600, "after": 2 * 3600}

    # 撤销 REV-002 后回退到 REV-001，新冻结采用最新已批准版本。
    _revoke(client, "REV-002")
    listing = client.get(f"/api/plans/{PV}/activities/A1/revisions").json()
    assert listing["effective_revision_id"] == "REV-001"
    f3 = client.post(f"/api/plans/{PV}/freezes/F-3", json={}).json()
    assert f3["students"][0]["total_seconds"] == 3 * 3600
    by_id = {r["revision_id"]: r for r in f3["activity_revisions"]}
    assert by_id["REV-001"]["effective"] is True
    assert by_id["REV-002"]["status"] == "revoked"


def test_partial_student_exemption(client):
    _create_plan(client)
    _import(
        client,
        [
            _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
            _checkin("E-02", "S2", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
            _checkin("E-03", "S3", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00"),
        ],
    )
    # S3 迟到，保留其实际签到，其余学生应用更正后的时间。
    resp = _draft(
        client,
        "REV-001",
        "2024-03-15T09:00:00+08:00",
        "2024-03-15T12:00:00+08:00",
        exempt=["S3"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["exempt_student_ids"] == ["S3"]

    impact = client.get(
        f"/api/plans/{PV}/activities/A1/revisions/REV-001/impact"
    ).json()
    assert impact["affected_students"] == 2
    s3 = next(s for s in impact["students"] if s["student_id"] == "S3")
    assert s3["exempt"] is True
    assert s3["affected"] is False
    assert s3["delta_seconds"] == 0

    _approve(client, "REV-001")
    assert _progress(client, "S1")["total_seconds"] == 3 * 3600
    assert _progress(client, "S2")["total_seconds"] == 3 * 3600
    s3_progress = _progress(client, "S3")
    assert s3_progress["total_seconds"] == 4 * 3600
    assert s3_progress["checkins"][0]["applied_revision_id"] is None

    # 豁免是按修订生效的：新修订未豁免 S3，则 S3 也被更正。
    _draft(client, "REV-002", "2024-03-15T10:00:00+08:00", "2024-03-15T12:00:00+08:00")
    _approve(client, "REV-002")
    assert _progress(client, "S3")["total_seconds"] == 2 * 3600
    assert _progress(client, "S3")["checkins"][0]["applied_revision_id"] == "REV-002"


def test_cross_midnight_revision_moves_day_attribution(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00")],
    )
    before = _progress(client, "S1")
    assert before["daily"] == [
        {"academic_day": "2024-03-15", "seconds": 7200},
        {"academic_day": "2024-03-16", "seconds": 7200},
    ]

    _draft(client, "REV-001", "2024-03-15T23:00:00+08:00", "2024-03-16T03:00:00+08:00")
    impact = client.get(
        f"/api/plans/{PV}/activities/A1/revisions/REV-001/impact"
    ).json()
    s1 = impact["students"][0]
    assert s1["daily_before"] == [
        {"academic_day": "2024-03-15", "seconds": 7200},
        {"academic_day": "2024-03-16", "seconds": 7200},
    ]
    assert s1["daily_after"] == [
        {"academic_day": "2024-03-15", "seconds": 3600},
        {"academic_day": "2024-03-16", "seconds": 3 * 3600},
    ]
    assert s1["overlap_intervals"][0]["seconds"] == 3 * 3600

    _approve(client, "REV-001")
    after = _progress(client, "S1")
    assert after["daily"] == [
        {"academic_day": "2024-03-15", "seconds": 3600},
        {"academic_day": "2024-03-16", "seconds": 3 * 3600},
    ]
    assert after["total_seconds"] == 4 * 3600


def test_concurrent_approvals_of_same_revision_record_single_event(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")],
    )
    _draft(client, "REV-001", "2024-03-15T09:00:00+08:00", "2024-03-15T12:00:00+08:00")

    from app import services

    outcomes: list[str] = []
    lock = threading.Lock()

    def _do_approve():
        session = TestSessionLocal()
        try:
            out = services.approve_activity_revision(
                session, PV, "A1", "REV-001"
            )
            with lock:
                outcomes.append(out["status"])
        finally:
            session.close()

    threads = [threading.Thread(target=_do_approve) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 并发审批同一草稿是幂等的：全部成功，只记录一条审批事件。
    assert outcomes == ["approved"] * 4
    session = TestSessionLocal()
    try:
        count = session.execute(
            select(func.count())
            .select_from(EventModel)
            .where(EventModel.event_id == "REV-001#1-approve")
        ).scalar_one()
    finally:
        session.close()
    assert count == 1
    assert _progress(client, "S1")["total_seconds"] == 3 * 3600


def test_concurrent_competing_approvals_resolve_deterministically(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")],
    )
    _draft(client, "REV-A", "2024-03-15T09:00:00+08:00", "2024-03-15T12:00:00+08:00")
    _draft(client, "REV-B", "2024-03-15T10:00:00+08:00", "2024-03-15T12:00:00+08:00")

    from app import services

    errors: list[Exception] = []
    lock = threading.Lock()

    def _do_approve(rid):
        session = TestSessionLocal()
        try:
            services.approve_activity_revision(session, PV, "A1", rid)
        except Exception as exc:  # pragma: no cover - 失败时记录
            with lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=_do_approve, args=("REV-A",)),
        threading.Thread(target=_do_approve, args=("REV-B",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # 两份修订都已批准；事件顺序决定最新者（REV-B）生效。
    listing = client.get(f"/api/plans/{PV}/activities/A1/revisions").json()
    by_id = {r["revision_id"]: r for r in listing["revisions"]}
    assert by_id["REV-A"]["status"] == "superseded"
    assert by_id["REV-B"]["status"] == "approved"
    assert listing["effective_revision_id"] == "REV-B"
    assert _progress(client, "S1")["total_seconds"] == 2 * 3600

    # 竞态之后的新冻结采用最终生效的修订。
    freeze = client.post(f"/api/plans/{PV}/freezes/F-RACE", json={}).json()
    assert freeze["students"][0]["total_seconds"] == 2 * 3600


def test_revision_errors(client):
    _create_plan(client)
    # 未知计划。
    resp = client.post(
        "/api/plans/NOPE/activities/A1/revisions",
        json={
            "revision_id": "REV-1",
            "check_in_at": "2024-03-15T09:00:00+08:00",
            "check_out_at": "2024-03-15T12:00:00+08:00",
        },
    )
    assert resp.status_code == 404

    # 结束早于开始 / 裸时间 / 非法 revision_id：422。
    for payload in (
        {
            "revision_id": "REV-1",
            "check_in_at": "2024-03-15T12:00:00+08:00",
            "check_out_at": "2024-03-15T09:00:00+08:00",
        },
        {
            "revision_id": "REV-1",
            "check_in_at": "2024-03-15T09:00:00",
            "check_out_at": "2024-03-15T12:00:00",
        },
        {
            "revision_id": "REV#1",
            "check_in_at": "2024-03-15T09:00:00+08:00",
            "check_out_at": "2024-03-15T12:00:00+08:00",
        },
    ):
        resp = client.post(f"/api/plans/{PV}/activities/A1/revisions", json=payload)
        assert resp.status_code == 422, payload

    _draft(client, "REV-001", "2024-03-15T09:00:00+08:00", "2024-03-15T12:00:00+08:00")
    # 重复草拟同一 revision_id：409。
    resp = _draft(
        client, "REV-001", "2024-03-15T10:00:00+08:00", "2024-03-15T12:00:00+08:00"
    )
    assert resp.status_code == 409

    # 未知修订：404。
    assert client.get(f"/api/plans/{PV}/activities/A1/revisions/NOPE").status_code == 404
    assert (
        client.get(f"/api/plans/{PV}/activities/A1/revisions/NOPE/impact").status_code
        == 404
    )
    assert _approve(client, "NOPE").status_code == 404
    assert _revoke(client, "NOPE").status_code == 404

    # 撤销后可幂等重复撤销，但已撤销的修订不能再审批。
    assert _revoke(client, "REV-001").status_code == 200
    assert _revoke(client, "REV-001").status_code == 200
    resp = _approve(client, "REV-001")
    assert resp.status_code == 409


def test_raw_revision_events_imported_via_events_endpoint(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")],
    )
    payload = {
        "events": [
            {
                "event_id": "R-01",
                "event_type": "activity_revision",
                "student_id": "activity:A1",
                "payload": {
                    "revision_id": "REV-1",
                    "activity_id": "A1",
                    "action": "draft",
                    "check_in_at": "2024-03-15T09:00:00+08:00",
                    "check_out_at": "2024-03-15T12:00:00+08:00",
                },
            },
            {
                "event_id": "R-02",
                "event_type": "activity_revision",
                "student_id": "activity:A1",
                "payload": {
                    "revision_id": "REV-1",
                    "activity_id": "A1",
                    "action": "approve",
                },
            },
        ]
    }
    first = client.post(f"/api/plans/{PV}/events", json=payload).json()
    assert first["accepted"] == 2
    # 重复导入幂等，状态不变。
    second = client.post(f"/api/plans/{PV}/events", json=payload).json()
    assert second["accepted"] == 0
    assert set(second["duplicates"]) == {"R-01", "R-02"}

    assert _progress(client, "S1")["total_seconds"] == 3 * 3600
    detail = client.get(f"/api/plans/{PV}/activities/A1/revisions/REV-1").json()
    assert detail["status"] == "approved"
    assert detail["effective"] is True
    assert detail["drafted_event_id"] == "R-01"
    assert detail["approved_event_id"] == "R-02"


def test_frozen_snapshot_records_revision_provenance(client):
    _create_plan(client)
    _import(
        client,
        [_checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")],
    )
    _draft(client, "REV-001", "2024-03-15T09:00:00+08:00", "2024-03-15T12:00:00+08:00")
    _approve(client, "REV-001")
    client.post(f"/api/plans/{PV}/freezes/F-1", json={})

    # 冻结后撤销修订：冻结快照仍保留当时的版本与溯源信息。
    _revoke(client, "REV-001")
    frozen = client.get(f"/api/plans/{PV}/freezes/F-1").json()
    assert frozen["students"][0]["total_seconds"] == 3 * 3600
    checkin = frozen["students"][0]["checkins"][0]
    assert checkin["applied_revision_id"] == "REV-001"
    assert checkin["original_check_in_at_utc"] == "2024-03-15T00:00:00Z"
    revision = frozen["activity_revisions"][0]
    assert revision["revision_id"] == "REV-001"
    assert revision["status"] == "approved"
    assert revision["effective"] is True

    # 实时快照已回退，冻结学生解释接口仍返回冻结时的版本。
    assert client.get(f"/api/plans/{PV}/snapshot").json()["students"][0][
        "total_seconds"
    ] == 4 * 3600
    explained = client.get(f"/api/plans/{PV}/freezes/F-1/explain/S1").json()
    assert explained["total_seconds"] == 3 * 3600
    assert explained["checkins"][0]["applied_revision_id"] == "REV-001"
