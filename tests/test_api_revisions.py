"""活动时间修订 API 测试。"""

from __future__ import annotations

import threading

from tests.conftest import SHANGHAI_PLAN


def _create_plan(client, required_seconds=10800):
    plan = dict(SHANGHAI_PLAN)
    plan["required_seconds"] = required_seconds
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text
    return plan["plan_version"]


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


def _draft(revision_id, activity_id, start, end, *, student_ids=None, reason="fix"):
    body = {
        "revision_id": revision_id,
        "activity_id": activity_id,
        "new_start_at": start,
        "new_end_at": end,
        "reason": reason,
    }
    if student_ids is not None:
        body["student_ids"] = student_ids
    return body


def test_draft_preview_approve_applies_effective_window_without_touching_raw(client):
    pv = _create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                ),
                _checkin(
                    "E-02",
                    "S2",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                ),
            ]
        },
    )

    # 草拟时分配版本号 1，状态为 draft。
    draft = client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-01",
            "A1",
            "2024-03-15T09:00:00+08:00",
            "2024-03-15T09:30:00+08:00",
            reason="organizer correction",
        ),
    )
    assert draft.status_code == 201, draft.text
    assert draft.json()["version"] == 1
    assert draft.json()["status"] == "draft"

    # 已存草稿预览：两名学生各缩短到 30 分钟。
    preview = client.post(f"/api/plans/{pv}/revisions/R-01/preview").json()
    assert preview["affected_student_count"] == 2
    deltas = {s["student_id"]: s["confirmed_delta_seconds"] for s in preview["students"]}
    assert deltas == {"S1": -5400, "S2": -5400}
    overlap = preview["students"][0]["affected_checkins"][0]["overlap_intervals"][0]
    # 原始 08-10 与修订 09-09:30 的重叠为 09-09:30。
    assert overlap["seconds"] == 1800

    # 内联（ad-hoc）预览同样可用，且不落库。
    adhoc = client.post(
        f"/api/plans/{pv}/revisions/preview",
        json={
            "activity_id": "A1",
            "new_start_at": "2024-03-15T08:00:00+08:00",
            "new_end_at": "2024-03-15T11:00:00+08:00",
        },
    ).json()
    assert adhoc["affected_student_count"] == 2

    # 草稿未批准：实时快照不变。
    before = client.get(f"/api/plans/{pv}/snapshot").json()
    assert before["applied_revisions"] == []

    # 批准后实时快照采用有效版本；原始签到时间仍然保留。
    approved = client.post(
        f"/api/plans/{pv}/revisions/R-01/approve", json={"actor_id": "org-a"}
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["approved_by"] == "org-a"

    after = client.get(f"/api/plans/{pv}/snapshot").json()
    assert {r["revision_id"] for r in after["applied_revisions"]} == {"R-01"}
    by_student = {s["student_id"]: s for s in after["students"]}
    assert by_student["S1"]["total_seconds"] == 1800
    checkin = by_student["S1"]["checkins"][0]
    assert checkin["raw_seconds"] == 7200
    assert checkin["effective_seconds"] == 1800
    assert checkin["check_in_at_utc"] != checkin["effective_start_utc"]
    assert checkin["applied_revision_id"] == "R-01"
    assert checkin["applied_revision_version"] == 1


def test_multiple_revisions_highest_version_wins_and_revoke_falls_back(client):
    pv = _create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                )
            ]
        },
    )

    def total():
        snap = client.get(f"/api/plans/{pv}/snapshot").json()
        return snap["students"][0]["total_seconds"]

    client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-01",
            "A1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        ),
    )
    client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-02",
            "A1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T08:30:00+08:00",
        ),
    )
    versions = client.get(f"/api/plans/{pv}/revisions").json()
    assert [r["version"] for r in versions["revisions"]] == [1, 2]
    assert versions["count"] == 2

    # 批准 v1：1 小时。
    client.post(f"/api/plans/{pv}/revisions/R-01/approve", json={"actor_id": "a"})
    assert total() == 3600

    # 再批准 v2：30 分钟（最高版本生效）。
    client.post(f"/api/plans/{pv}/revisions/R-02/approve", json={"actor_id": "a"})
    assert total() == 1800
    live = client.get(f"/api/plans/{pv}/snapshot").json()
    assert {r["revision_id"] for r in live["applied_revisions"]} == {"R-02"}

    # 撤销 v2：回退到 v1。
    client.post(f"/api/plans/{pv}/revisions/R-02/revoke", json={"actor_id": "a"})
    assert total() == 3600
    live = client.get(f"/api/plans/{pv}/snapshot").json()
    assert {r["revision_id"] for r in live["applied_revisions"]} == {"R-01"}

    # 撤销 v1：回到原始签到 2 小时。
    client.post(f"/api/plans/{pv}/revisions/R-01/revoke", json={"actor_id": "a"})
    assert total() == 7200
    live = client.get(f"/api/plans/{pv}/snapshot").json()
    assert live["applied_revisions"] == []

    # 版本查询支持状态过滤。
    revoked = client.get(
        f"/api/plans/{pv}/revisions", params={"status_filter": "revoked"}
    ).json()
    assert {r["revision_id"] for r in revoked["revisions"]} == {"R-01", "R-02"}


def test_partial_student_exception_overrides_global_revision(client):
    pv = _create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                ),
                _checkin(
                    "E-02",
                    "S2",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                ),
            ]
        },
    )

    # 全体修订 v1：缩短到 1 小时。
    client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-01",
            "A1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        ),
    )
    client.post(f"/api/plans/{pv}/revisions/R-01/approve", json={"actor_id": "a"})

    # 学生 S2 的特批修订 v2：90 分钟。
    client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-02",
            "A1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:30:00+08:00",
            student_ids=["S2"],
        ),
    )
    client.post(f"/api/plans/{pv}/revisions/R-02/approve", json={"actor_id": "a"})

    by_student = {
        s["student_id"]: s
        for s in client.get(f"/api/plans/{pv}/snapshot").json()["students"]
    }
    assert by_student["S1"]["total_seconds"] == 3600
    assert by_student["S2"]["total_seconds"] == 5400
    assert (
        by_student["S1"]["checkins"][0]["applied_revision_id"] == "R-01"
    )
    assert (
        by_student["S2"]["checkins"][0]["applied_revision_id"] == "R-02"
    )

    # 撤销 S2 特批后，S2 回落到全体修订 v1。
    client.post(f"/api/plans/{pv}/revisions/R-02/revoke", json={"actor_id": "a"})
    by_student = {
        s["student_id"]: s
        for s in client.get(f"/api/plans/{pv}/snapshot").json()["students"]
    }
    assert by_student["S2"]["total_seconds"] == 3600
    assert by_student["S2"]["checkins"][0]["applied_revision_id"] == "R-01"


def test_cross_midnight_revision_reattributes_academic_days(client):
    pv = _create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T22:00:00+08:00",
                    "2024-03-16T02:00:00+08:00",
                )
            ]
        },
    )
    client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-01",
            "A1",
            "2024-03-15T23:00:00+08:00",
            "2024-03-16T01:00:00+08:00",
        ),
    )
    client.post(f"/api/plans/{pv}/revisions/R-01/approve", json={"actor_id": "a"})

    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    # 23:00-01:00 = 2 小时，跨两天各 1 小时。
    assert progress["total_seconds"] == 7200
    days = {d["academic_day"]: d["seconds"] for d in progress["daily"]}
    assert days == {"2024-03-15": 3600, "2024-03-16": 3600}
    checkin = progress["checkins"][0]
    assert len(checkin["academic_days"]) == 2
    assert checkin["raw_seconds"] == 4 * 3600
    assert checkin["effective_seconds"] == 7200


def test_frozen_snapshots_keep_then_current_versions_new_freeze_uses_latest(client):
    pv = _create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                )
            ]
        },
    )

    # F1 在任何修订之前冻结：原始 2 小时。
    f1 = client.post(f"/api/plans/{pv}/freezes/F1", json={})
    assert f1.status_code == 201, f1.text
    assert f1.json()["applied_revisions"] == []

    client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-01",
            "A1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        ),
    )
    client.post(f"/api/plans/{pv}/revisions/R-01/approve", json={"actor_id": "a"})

    # F2 在 v1 生效时冻结：1 小时，记录 v1。
    f2 = client.post(f"/api/plans/{pv}/freezes/F2", json={}).json()
    assert f2["students"][0]["total_seconds"] == 3600
    assert [r["revision_id"] for r in f2["applied_revisions"]] == ["R-01"]

    client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-02",
            "A1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T08:30:00+08:00",
        ),
    )
    client.post(f"/api/plans/{pv}/revisions/R-02/approve", json={"actor_id": "a"})

    # F3 使用最新已批准修订 v2：30 分钟。
    f3 = client.post(f"/api/plans/{pv}/freezes/F3", json={}).json()
    assert f3["students"][0]["total_seconds"] == 1800
    assert [r["revision_id"] for r in f3["applied_revisions"]] == ["R-02"]

    # 撤销 v2、再冻结 F4：回落到 v1（v1 仍批准）。
    client.post(f"/api/plans/{pv}/revisions/R-02/revoke", json={"actor_id": "a"})
    f4 = client.post(f"/api/plans/{pv}/freezes/F4", json={}).json()
    assert f4["students"][0]["total_seconds"] == 3600
    assert [r["revision_id"] for r in f4["applied_revisions"]] == ["R-01"]

    # 历史冻结全部保持各自当时的版本，不随后续变化。
    assert (
        client.get(f"/api/plans/{pv}/freezes/F1").json()["students"][0][
            "total_seconds"
        ]
        == 7200
    )
    f2_again = client.get(f"/api/plans/{pv}/freezes/F2").json()
    assert f2_again["students"][0]["total_seconds"] == 3600
    assert [r["revision_id"] for r in f2_again["applied_revisions"]] == ["R-01"]
    f3_again = client.get(f"/api/plans/{pv}/freezes/F3").json()
    assert f3_again["students"][0]["total_seconds"] == 1800
    assert [r["revision_id"] for r in f3_again["applied_revisions"]] == ["R-02"]


def test_freeze_cutoff_uses_insertion_order_not_event_id_lex_order(client):
    # event_id 字典序与插入顺序不一致时，冻结 cutoff 仍须按事件流单调序号界定。
    pv = _create_plan(client)
    # "ZZ-01" 字典序在 "REV-..." 之后，但它先入库。
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "ZZ-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                )
            ]
        },
    )
    client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-01",
            "A1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        ),
    )
    approve = client.post(
        f"/api/plans/{pv}/revisions/R-01/approve", json={"actor_id": "a"}
    ).json()
    approve_event_id = approve["approved_event_id"]
    # 防御性前提：业务事件 ID 字典序确实在生命周期事件之后。
    assert "ZZ-01" > approve_event_id

    # 在批准之后冻结：必须采用 R1（1 小时），cutoff 指向真实末条事件。
    frozen = client.post(f"/api/plans/{pv}/freezes/F1", json={}).json()
    assert frozen["event_cutoff_id"] == approve_event_id
    assert frozen["event_cutoff_seq"] == approve["approved_seq"]
    assert frozen["students"][0]["total_seconds"] == 3600
    assert [r["revision_id"] for r in frozen["applied_revisions"]] == ["R-01"]

    # 冻结后又到一条业务事件（seq 更大、event_id 更小）：历史冻结不变。
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": "AA-02",
                    "event_type": "leave_correction",
                    "student_id": "S1",
                    "payload": {
                        "adjustment_seconds": 3600,
                        "reason": "approved make-up",
                    },
                }
            ]
        },
    )
    frozen_again = client.get(f"/api/plans/{pv}/freezes/F1").json()
    assert frozen_again["students"][0]["total_seconds"] == 3600
    # 实时快照包含后到修正：3600（修订后签到）+ 3600（补修）。
    live = client.get(f"/api/plans/{pv}/snapshot").json()
    assert live["students"][0]["total_seconds"] == 3600 + 3600


def test_concurrent_approval_only_one_lifecycle_event(client):
    from app import services
    from app.models import Event as EventModel
    from tests.conftest import TestSessionLocal

    pv = _create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                )
            ]
        },
    )
    client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-01",
            "A1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        ),
    )

    statuses: list[str] = []
    lock = threading.Lock()

    def _approve():
        session = TestSessionLocal()
        try:
            result = services.approve_revision(
                session,
                plan_version=pv,
                revision_id="R-01",
                approved_by="org",
            )
            with lock:
                statuses.append(result["status"])
        finally:
            session.close()

    threads = [threading.Thread(target=_approve) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses == ["approved"] * 4

    # 只追加了一条批准生命周期事件。
    session = TestSessionLocal()
    try:
        approve_events = (
            session.query(EventModel)
            .filter(EventModel.plan_version == pv)
            .filter(EventModel.event_type == "revision_approve")
            .all()
        )
        assert len(approve_events) == 1
        revoke_events = (
            session.query(EventModel)
            .filter(EventModel.plan_version == pv)
            .filter(EventModel.event_type == "revision_revoke")
            .all()
        )
        assert revoke_events == []
    finally:
        session.close()

    # 重复批准幂等，不产生第二条事件。
    again = client.post(
        f"/api/plans/{pv}/revisions/R-01/approve", json={"actor_id": "org"}
    )
    assert again.status_code == 200
    assert again.json()["status"] == "approved"
    session = TestSessionLocal()
    try:
        assert (
            session.query(EventModel)
            .filter(EventModel.event_type == "revision_approve")
            .count()
            == 1
        )
    finally:
        session.close()


def test_revision_lifecycle_validation_errors(client):
    pv = _create_plan(client)
    client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                _checkin(
                    "E-01",
                    "S1",
                    "2024-03-15T08:00:00+08:00",
                    "2024-03-15T10:00:00+08:00",
                )
            ]
        },
    )

    # 未知培养方案 -> 404。
    assert (
        client.post(
            "/api/plans/NOPE/revisions",
            json=_draft(
                "R1",
                "A1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T09:00:00+08:00",
            ),
        ).status_code
        == 404
    )

    # 结束早于开始 -> 422。
    bad_window = client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-BAD",
            "A1",
            "2024-03-15T10:00:00+08:00",
            "2024-03-15T08:00:00+08:00",
        ),
    )
    assert bad_window.status_code == 422

    # 草稿成功。
    client.post(
        f"/api/plans/{pv}/revisions",
        json=_draft(
            "R-01",
            "A1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        ),
    )

    # 撤销草稿 -> 409。
    assert (
        client.post(
            f"/api/plans/{pv}/revisions/R-01/revoke", json={"actor_id": "a"}
        ).status_code
        == 409
    )

    # 操作未知修订 -> 404。
    assert (
        client.post(
            f"/api/plans/{pv}/revisions/NOPE/approve", json={"actor_id": "a"}
        ).status_code
        == 404
    )

    # 预览缺少时间窗 -> 422。
    assert (
        client.post(
            f"/api/plans/{pv}/revisions/preview",
            json={"activity_id": "A1"},
        ).status_code
        == 422
    )

    # 批准后再次尝试非法迁移：批准已撤销修订 -> 409。
    client.post(f"/api/plans/{pv}/revisions/R-01/approve", json={"actor_id": "a"})
    client.post(f"/api/plans/{pv}/revisions/R-01/revoke", json={"actor_id": "a"})
    assert (
        client.post(
            f"/api/plans/{pv}/revisions/R-01/approve", json={"actor_id": "a"}
        ).status_code
        == 409
    )
