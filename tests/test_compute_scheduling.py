from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock
from app.database import close_connection, get_connection, init_db


TEMPLATE = {
    "code": "solver-a",
    "name": "方程求解模板",
    "algorithm": "solver-a",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 10000},
        "mode": {"type": "string", "required": True, "choices": ["fast", "accurate"]},
    },
    "default_parameters": {},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}

START = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


def submit_payload(key: str, *, project: str = "basic-2026", user: str = "student-1", priority: int = 50) -> dict:
    return {
        "template_code": "solver-a",
        "project_code": project,
        "requested_by": user,
        "parameters": {"iterations": 100, "mode": "fast"},
        "priority": priority,
        "idempotency_key": key,
    }


def make_service(db_path: Path, clock: FrozenClock) -> ComputeOperationsService:
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(db_path)
    close_connection()
    init_db()
    service = ComputeOperationsService(get_connection(), clock)
    service.create_template(TEMPLATE, "administrator")
    return service


def candidate_of(decision: dict, task_id: int) -> dict:
    return next(item for item in decision["candidates"] if item["task_id"] == task_id)


def test_class_weight_changes_claim_order(tmp_path):
    clock = FrozenClock(START)
    service = make_service(tmp_path / "weight.db", clock)
    service.set_scheduling_policy(
        {
            "default_weight": 1.0,
            "default_max_concurrent": None,
            "aging_rate_per_hour": 0.0,
            "aging_max_bonus": 100.0,
            "classes": {"basic-2026": {"weight": 2.0, "max_concurrent": None}},
        },
        "administrator",
    )
    senior = service.submit(submit_payload("weight-senior", project="senior-cert", priority=80))
    basic = service.submit(submit_payload("weight-basic", project="basic-2026", priority=50))
    claimed = service.claim("teacher-1", ["solver-a"], 60)
    assert claimed["id"] == basic["id"]  # 50 × 2.0 = 100 领先 80 × 1.0
    decision = service.get_claim_decision(claimed["claim_decision_id"])
    assert decision["chosen_task_id"] == basic["id"]
    assert decision["candidates"][0]["task_id"] == basic["id"]
    assert decision["candidates"][0]["effective_score"] == 100.0
    assert candidate_of(decision, senior["id"])["effective_score"] == 80.0


def test_wait_aging_gradually_overtakes_priority(tmp_path):
    clock = FrozenClock(START)
    service = make_service(tmp_path / "aging.db", clock)
    # 默认策略：权重相等、并发不限、老化 1 分/小时、上限 100 分
    aged = service.submit(submit_payload("aging-aged", priority=50))
    rival = service.submit(submit_payload("aging-rival", priority=90))
    first = service.claim("teacher-1", ["solver-a"], 60)
    assert first["id"] == rival["id"]  # 等待时间相近时高优先级仍然领先
    service.complete(rival["id"], "teacher-1", {"value": 1}, {})
    clock.advance(hours=48)
    urgent = service.submit(submit_payload("aging-urgent", priority=90))
    second = service.claim("teacher-2", ["solver-a"], 60)
    assert second["id"] == aged["id"]  # 50 + 48 小时老化 = 98 超过 90
    decision = service.get_claim_decision(second["claim_decision_id"])
    top = candidate_of(decision, aged["id"])
    assert top["wait_seconds"] == 172800.0
    assert top["aging_bonus"] == 48.0
    assert top["effective_score"] == 98.0
    assert candidate_of(decision, urgent["id"])["effective_score"] == 90.0


def test_concurrency_share_skips_full_class_without_starving_queue(tmp_path):
    clock = FrozenClock(START)
    service = make_service(tmp_path / "share.db", clock)
    service.set_scheduling_policy(
        {
            "default_weight": 1.0,
            "default_max_concurrent": None,
            "aging_rate_per_hour": 0.0,
            "aging_max_bonus": 100.0,
            "classes": {"senior-cert": {"weight": None, "max_concurrent": 1}},
        },
        "administrator",
    )
    urgent1 = service.submit(submit_payload("share-urgent-1", project="senior-cert", priority=95))
    urgent2 = service.submit(submit_payload("share-urgent-2", project="senior-cert", priority=90))
    regular = service.submit(submit_payload("share-regular", project="basic-2026", priority=40))
    first = service.claim("teacher-1", ["solver-a"], 60)
    assert first["id"] == urgent1["id"]
    second = service.claim("teacher-2", ["solver-a"], 60)
    assert second["id"] == regular["id"]  # senior-cert 份额已满被跳过，基础班不被队首饿死
    decision = service.get_claim_decision(second["claim_decision_id"])
    skipped = candidate_of(decision, urgent2["id"])
    assert skipped["eligible"] is False
    assert skipped["skip_reason"] == "class_concurrency_full"
    assert skipped["running_in_class"] == 1
    assert skipped["max_concurrent"] == 1
    assert skipped["effective_score"] > candidate_of(decision, regular["id"])["effective_score"]
    # 释放份额后队首班级恢复可领取
    service.complete(urgent1["id"], "teacher-1", {"value": 1}, {})
    third = service.claim("teacher-3", ["solver-a"], 60)
    assert third["id"] == urgent2["id"]


def test_capability_mismatch_skipped_and_recorded(tmp_path):
    clock = FrozenClock(START)
    service = make_service(tmp_path / "capability.db", clock)
    task = service.submit(submit_payload("capability-one"))
    assert service.claim("teacher-9", ["other-algorithm"], 60) is None
    decisions = service.list_claim_decisions(worker_id="teacher-9")
    assert len(decisions) == 1
    assert decisions[0]["chosen_task_id"] is None
    assert decisions[0]["candidate_count"] == 1
    detail = service.get_claim_decision(decisions[0]["id"])
    entry = candidate_of(detail, task["id"])
    assert entry["eligible"] is False
    assert entry["skip_reason"] == "capability_mismatch"
    # 能力匹配的工作者随后仍可领取同一任务
    claimed = service.claim("teacher-1", ["solver-a"], 60)
    assert claimed["id"] == task["id"]


def test_policy_change_applies_only_to_later_decisions(tmp_path):
    clock = FrozenClock(START)
    service = make_service(tmp_path / "policy.db", clock)
    base_policy = {
        "default_weight": 1.0,
        "default_max_concurrent": None,
        "aging_rate_per_hour": 0.0,
        "aging_max_bonus": 100.0,
    }
    first_version = service.set_scheduling_policy({**base_policy, "classes": {"basic-2026": {"weight": 1.0}}}, "administrator")
    assert first_version["version"] == 1
    low = service.submit(submit_payload("policy-low", priority=50))
    high = service.submit(submit_payload("policy-high", project="senior-cert", priority=80))
    first = service.claim("teacher-1", ["solver-a"], 60)
    assert first["id"] == high["id"]  # 版本 1：80 领先 50 × 1.0
    service.complete(high["id"], "teacher-1", {"value": 1}, {})
    second_version = service.set_scheduling_policy({**base_policy, "classes": {"basic-2026": {"weight": 3.0}}}, "administrator")
    assert second_version["version"] == 2
    second = service.claim("teacher-2", ["solver-a"], 60)
    assert second["id"] == low["id"]  # 版本 2：50 × 3.0 = 150，无其他竞争者
    first_decision = service.get_claim_decision(first["claim_decision_id"])
    second_decision = service.get_claim_decision(second["claim_decision_id"])
    assert first_decision["policy_version"] == 1
    assert first_decision["policy_snapshot"]["classes"]["basic-2026"]["weight"] == 1.0
    assert second_decision["policy_version"] == 2
    assert second_decision["policy_snapshot"]["classes"]["basic-2026"]["weight"] == 3.0
    # 历史决策保持当时的快照，不受后续配置变更影响
    again = service.get_claim_decision(first["claim_decision_id"])
    assert again == first_decision
    history = service.scheduling_policy_history()
    assert [item["version"] for item in history] == [2, 1]


def run_deterministic_scenario(db_path: Path) -> list[dict]:
    clock = FrozenClock(START)
    service = make_service(db_path, clock)
    service.set_scheduling_policy(
        {
            "default_weight": 1.0,
            "default_max_concurrent": None,
            "aging_rate_per_hour": 1.0,
            "aging_max_bonus": 100.0,
            "classes": {"senior-cert": {"weight": 1.0, "max_concurrent": 1}},
        },
        "administrator",
    )
    service.submit(submit_payload("det-urgent-1", project="senior-cert", priority=95))
    service.submit(submit_payload("det-urgent-2", project="senior-cert", priority=90))
    service.submit(submit_payload("det-basic-1", project="basic-2026", priority=50))
    clock.advance(hours=30)
    service.submit(submit_payload("det-basic-2", project="basic-2026", priority=60))
    trace: list[dict] = []
    urgent_task_id: int | None = None
    for worker in ("worker-1", "worker-2", "worker-3", "worker-4", "worker-5"):
        if worker == "worker-4":
            # 前三名工作者领取后，urgent-1 完成并释放 senior-cert 的并发份额
            service.complete(urgent_task_id, "worker-1", {"value": 1}, {})
        claimed = service.claim(worker, ["solver-a"], 60)
        if claimed is None:
            trace.append({"worker": worker, "claimed": None})
            continue
        if claimed["idempotency_key"] == "det-urgent-1":
            urgent_task_id = claimed["id"]
        trace.append(
            {
                "worker": worker,
                "claimed": claimed["idempotency_key"],
                "decision": service.get_claim_decision(claimed["claim_decision_id"]),
            }
        )
    return trace


def test_repeated_runs_with_frozen_clock_are_identical(tmp_path):
    first = run_deterministic_scenario(tmp_path / "run-a.db")
    second = run_deterministic_scenario(tmp_path / "run-b.db")
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(second, ensure_ascii=False, sort_keys=True)
    order = [item["claimed"] for item in first]
    # urgent-1 领先；urgent-2 因班级份额被跳过，老化 30 小时的 basic-1 与 basic-2 依次补上；
    # urgent-1 完成释放份额后 urgent-2 被领取；队列清空后领取返回空。
    assert order == ["det-urgent-1", "det-basic-1", "det-basic-2", "det-urgent-2", None]
    second_decision = first[1]["decision"]
    assert second_decision["candidates"][0]["skip_reason"] == "class_concurrency_full"
    assert second_decision["candidates"][0]["eligible"] is False


def test_scheduling_policy_and_claim_decision_endpoints(client):
    template = client.post("/api/compute/templates?actor=administrator", json=TEMPLATE)
    assert template.status_code == 201, template.text
    default_policy = client.get("/api/compute/scheduling-policy")
    assert default_policy.status_code == 200
    assert default_policy.json()["version"] == 0
    invalid = client.put("/api/compute/scheduling-policy?actor=administrator", json={"default_weight": 0})
    assert invalid.status_code == 422
    saved = client.put(
        "/api/compute/scheduling-policy?actor=administrator",
        json={
            "default_weight": 1.0,
            "aging_rate_per_hour": 1.0,
            "aging_max_bonus": 100.0,
            "classes": {"basic-2026": {"weight": 2.0, "max_concurrent": 3}},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1
    current = client.get("/api/compute/scheduling-policy").json()
    assert current["version"] == 1
    assert current["classes"]["basic-2026"] == {"weight": 2.0, "max_concurrent": 3}
    history = client.get("/api/compute/scheduling-policy/history").json()["items"]
    assert [item["version"] for item in history] == [1]
    task = client.post("/api/compute/tasks", json=submit_payload("endpoint-task")).json()
    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "teacher-1", "capabilities": ["solver-a"], "lease_seconds": 60})
    assert claimed.status_code == 200
    assert claimed.json()["task"]["id"] == task["id"]
    decision_id = claimed.json()["task"]["claim_decision_id"]
    decisions = client.get("/api/compute/claim-decisions", params={"worker_id": "teacher-1"}).json()["items"]
    assert len(decisions) == 1
    assert decisions[0]["id"] == decision_id
    assert decisions[0]["chosen_task_id"] == task["id"]
    by_task = client.get("/api/compute/claim-decisions", params={"task_id": task["id"]}).json()["items"]
    assert [item["id"] for item in by_task] == [decision_id]
    detail = client.get(f"/api/compute/claim-decisions/{decision_id}").json()
    assert detail["policy_version"] == 1
    assert detail["policy_snapshot"]["classes"]["basic-2026"]["weight"] == 2.0
    assert detail["candidates"][0]["task_id"] == task["id"]
    assert detail["candidates"][0]["eligible"] is True
    missing = client.get("/api/compute/claim-decisions/999999")
    assert missing.status_code == 404
