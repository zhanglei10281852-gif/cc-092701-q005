from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from app.compute.scheduling import (
    SKIP_CAPABILITY_MISMATCH,
    SKIP_QUOTA_FULL,
    choose_task,
    normalize_config,
    resolve_policy,
)
from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock
from app.database import close_connection, init_db
from app.database import get_connection

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

START = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)


def _service(db_path: str, clock: FrozenClock) -> ComputeOperationsService:
    os.environ["TOWNSHIP_DATABASE_PATH"] = db_path
    close_connection()
    init_db()
    service = ComputeOperationsService(get_connection(), clock)
    service.create_template(TEMPLATE, "administrator")
    return service


def _submit(service: ComputeOperationsService, key: str, project: str, priority: int) -> dict:
    return service.submit(
        {
            "template_code": "solver-a",
            "project_code": project,
            "requested_by": f"user-{key}",
            "parameters": {"iterations": 10, "mode": "fast"},
            "priority": priority,
            "idempotency_key": key,
        }
    )


@pytest.fixture()
def service_env(tmp_path):
    clock = FrozenClock(START)
    service = _service(str(tmp_path / "scheduling.db"), clock)
    yield service, clock, tmp_path
    close_connection()


def test_class_weight_multiplies_priority(service_env):
    service, clock, _ = service_env
    # 认证班权重 200：原始优先级 50 → 加权 100；普通班权重 100：优先级 80 → 80。
    service.set_class_policy(
        {"project_code": "cert", "weight": 200, "max_concurrent": 100, "is_active": True, "note": "认证班"},
        "administrator",
    )
    basic = _submit(service, "weight-basic", "basic", 80)
    cert = _submit(service, "weight-cert", "cert", 50)
    result = service.claim("w1", ["solver-a"], 60)
    assert result["task"]["id"] == cert["id"]
    decision = service.get_claim_decision(result["decision_id"])
    assert decision["outcome"] == "claimed"
    scanned = {item["task_id"]: item for item in decision["evaluated"]["scanned"]}
    assert scanned[cert["id"]]["weighted_priority"] == 100.0
    assert scanned[basic["id"]]["weighted_priority"] == 80.0


def test_aging_gradually_lifts_long_waiting_task_but_high_priority_leads_when_fresh(service_env):
    service, clock, _ = service_env
    service.set_schedule_config(
        {"aging_step_seconds": 60, "aging_bonus_per_step": 20, "max_age_bonus": 400}, "administrator"
    )
    # 同一时刻提交：普通班低优先级与高优先级任务，初始高优先级领先。
    basic = _submit(service, "age-basic", "basic", 50)
    cert = _submit(service, "age-cert", "cert", 90)
    first = service.claim("w1", ["solver-a"], 60)
    assert first["task"]["id"] == cert["id"]

    # 普通班任务等待 25 分钟：25 步 × 20 = 500，封顶 400，有效分 450，
    # 此时新到达的高优先级任务（90 分）也必须排在它后面。
    clock.advance(minutes=25)
    cert2 = _submit(service, "age-cert-2", "cert", 90)
    second = service.claim("w2", ["solver-a"], 60)
    assert second["task"]["id"] == basic["id"]
    detail = next(
        item for item in service.get_claim_decision(second["decision_id"])["evaluated"]["scanned"]
        if item["task_id"] == basic["id"]
    )
    assert detail["waited_seconds"] == 1500
    assert detail["age_bonus"] == 400
    assert detail["effective_score"] == 450.0
    assert cert2["id"] != basic["id"]


def test_full_class_concurrency_is_skipped_without_starving_queue(service_env):
    service, clock, _ = service_env
    service.set_class_policy(
        {"project_code": "cert", "weight": 100, "max_concurrent": 1, "is_active": True, "note": ""},
        "administrator",
    )
    cert1 = _submit(service, "cert-1", "cert", 90)
    cert2 = _submit(service, "cert-2", "cert", 90)
    cert3 = _submit(service, "cert-3", "cert", 90)
    basic = _submit(service, "basic-1", "basic", 10)

    first = service.claim("w1", ["solver-a"], 60)
    assert first["task"]["id"] == cert1["id"]

    # 认证班并发份额已满，队首两条被跳过，普通班任务必须能被领到。
    second = service.claim("w2", ["solver-a"], 60)
    assert second["task"]["id"] == basic["id"]
    assert second["outcome"] == "claimed"
    decision = service.get_claim_decision(second["decision_id"])
    skipped = [item for item in decision["evaluated"]["scanned"] if item["decision"] == "skipped"]
    assert {item["task_id"] for item in skipped} == {cert2["id"], cert3["id"]}
    assert all(item["skip_reason"] == SKIP_QUOTA_FULL for item in skipped)
    assert all(item["class_running"] == 1 and item["class_max_concurrent"] == 1 for item in skipped)
    assert decision["running_counts"] == {"cert": 1}


def test_inactive_class_tasks_are_skipped(service_env):
    service, clock, _ = service_env
    service.set_class_policy(
        {"project_code": "paused", "weight": 200, "max_concurrent": 100, "is_active": False, "note": "停课"},
        "administrator",
    )
    paused = _submit(service, "paused-1", "paused", 90)
    ready = _submit(service, "ready-1", "basic", 10)
    result = service.claim("w1", ["solver-a"], 60)
    assert result["task"]["id"] == ready["id"]
    decision = service.get_claim_decision(result["decision_id"])
    paused_detail = next(item for item in decision["evaluated"]["scanned"] if item["task_id"] == paused["id"])
    assert paused_detail["decision"] == "skipped"
    assert paused_detail["skip_reason"] == "class_policy_inactive"


def test_capability_mismatch_recorded_and_empty_outcome(service_env):
    service, clock, _ = service_env
    task = _submit(service, "cap-1", "basic", 50)

    mismatch = service.claim("w1", ["other-algorithm"], 60)
    assert mismatch["task"] is None
    assert mismatch["outcome"] == "skipped"
    decision = service.get_claim_decision(mismatch["decision_id"])
    rejected = decision["evaluated"]["capability_rejections"]
    assert [item["task_id"] for item in rejected] == [task["id"]]
    assert rejected[0]["skip_reason"] == SKIP_CAPABILITY_MISMATCH

    empty = service.claim("w2", ["solver-a"], 60)
    assert empty["task"]["id"] == task["id"]

    clock.advance(seconds=5)
    idle = service.claim("w3", ["solver-a"], 60)
    assert idle["task"] is None and idle["outcome"] == "empty"
    idle_decision = service.get_claim_decision(idle["decision_id"])
    assert idle_decision["evaluated"]["due_total"] == 0


def test_config_change_only_affects_later_decisions(service_env):
    service, clock, _ = service_env
    service.set_schedule_config({"aging_step_seconds": 300, "aging_bonus_per_step": 10}, "administrator")
    task = _submit(service, "cfg-1", "basic", 50)
    waiting = _submit(service, "cfg-keep", "basic", 40)
    clock.advance(minutes=30)
    first = service.claim("w1", ["solver-a"], 60)
    assert first["task"]["id"] == task["id"]
    first_decision = service.get_claim_decision(first["decision_id"])
    assert first_decision["config"]["aging_step_seconds"] == 300
    assert first_decision["config"]["aging_bonus_per_step"] == 10

    service.set_schedule_config(
        {"aging_step_seconds": 60, "aging_bonus_per_step": 20, "max_age_bonus": 10000}, "administrator"
    )
    second = service.claim("w2", ["solver-a"], 60)
    assert second["task"]["id"] == waiting["id"]
    second_decision = service.get_claim_decision(second["decision_id"])
    assert second_decision["config"]["aging_step_seconds"] == 60
    # 历史决策保留当时的配置快照，不被后续变更改写。
    assert service.get_claim_decision(first["decision_id"])["config"]["aging_step_seconds"] == 300
    detail = next(
        item for item in second_decision["evaluated"]["scanned"] if item["task_id"] == waiting["id"]
    )
    assert detail["age_bonus"] == 20 * (detail["waited_seconds"] // 60)


def test_replay_under_fixed_clock_and_task_set_is_deterministic(tmp_path):
    def replay(path: str) -> tuple[list[int], list[str], str]:
        close_connection()
        clock = FrozenClock(START)
        service = _service(path, clock)
        service.set_class_policy(
            {"project_code": "cert", "weight": 150, "max_concurrent": 1, "is_active": True, "note": ""},
            "administrator",
        )
        service.set_schedule_config(
            {"aging_step_seconds": 60, "aging_bonus_per_step": 15, "max_age_bonus": 300}, "administrator"
        )
        tasks = {
            "cert1": _submit(service, "rep-1", "cert", 90)["id"],
            "basic2": _submit(service, "rep-2", "basic", 40)["id"],
            "cert3": _submit(service, "rep-3", "cert", 80)["id"],
            "basic4": _submit(service, "rep-4", "basic", 60)["id"],
        }
        clock.advance(minutes=25)
        tasks["basic5"] = _submit(service, "rep-5", "basic", 30)["id"]

        selected: list[int] = []
        outcomes: list[str] = []
        digests: list[str] = []
        for worker in ("w1", "w2", "w3", "w4", "w5"):
            result = service.claim(worker, ["solver-a"], 60)
            outcomes.append(result["outcome"])
            if result["task"] is not None:
                selected.append(result["task"]["id"])
            decision = service.get_claim_decision(result["decision_id"])
            digests.append(
                json.dumps(decision["evaluated"], sort_keys=True, ensure_ascii=False)
                + json.dumps(decision["config"], sort_keys=True)
            )
            clock.advance(seconds=90)
        return selected, outcomes, "|".join(digests)

    first = replay(str(tmp_path / "rep-a.db"))
    second = replay(str(tmp_path / "rep-b.db"))
    assert first == second

    selected, outcomes, _ = first
    # 认证班只有 1 个并发槽：cert1 被领取后 cert3 始终被跳过，
    # 普通班 basic4/basic2/basic5 依次穿插领取，不被队首饿死。
    assert selected == [1, 4, 2, 5]
    assert outcomes == ["claimed", "claimed", "claimed", "claimed", "skipped"]


def test_pure_scoring_is_a_stable_pure_function():
    config = normalize_config(
        {"aging_step_seconds": 60, "aging_bonus_per_step": 10, "max_age_bonus": 100,
         "default_weight": 100, "default_max_concurrent": 10}
    )
    tasks = [
        {"id": 1, "project_code": "a", "template_algorithm": "x", "priority": 50,
         "created_at": "2026-09-27T08:00:00+00:00", "available_at": "2026-09-27T08:00:00+00:00"},
        {"id": 2, "project_code": "b", "template_algorithm": "x", "priority": 50,
         "created_at": "2026-09-27T08:00:00+00:00", "available_at": "2026-09-27T08:00:00+00:00"},
    ]
    policies = {"a": {"weight": 100, "max_concurrent": 10, "is_active": 1}}
    kwargs = dict(
        decided_at="2026-09-27T08:30:00+00:00",
        config=config,
        explicit_policies=policies,
        running_counts={},
    )
    first = choose_task(tasks, **kwargs)
    second = choose_task(tasks, **kwargs)
    assert first["selected"]["id"] == second["selected"]["id"] == 1
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    # 默认策略解析对未配置班级生效。
    assert resolve_policy("unknown", policies, config).weight == 100
