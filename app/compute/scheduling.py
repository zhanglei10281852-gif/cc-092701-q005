"""领取调度的纯函数打分逻辑。

有效排序分（effective_score）::

    effective_score = priority * (class_weight / 100) + age_bonus

- 班级权重 weight 以 100 为基准乘到任务优先级上，高权重班级在同一时间整体靠前；
- 老化加分 age_bonus 按任务可领取时刻（available_at）累计等待时长，
  每 aging_step_seconds 增加 aging_bonus_per_step 分，封顶 max_age_bonus，
  使等待足够久的低权任务可以逐步反超，但不会无限放大。

本模块不访问数据库与时钟，便于在固定输入下复现与审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 跳过原因代码
SKIP_QUOTA_FULL = "class_concurrent_quota_full"
SKIP_INACTIVE_CLASS = "class_policy_inactive"
SKIP_CAPABILITY_MISMATCH = "capability_mismatch"

# 决策结果
OUTCOME_CLAIMED = "claimed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_EMPTY = "empty"

DEFAULT_CONFIG: dict[str, int] = {
    "aging_step_seconds": 300,
    "aging_bonus_per_step": 10,
    "max_age_bonus": 400,
    "default_weight": 100,
    "default_max_concurrent": 100_000,
}

# 单次领取最多扫描的候选数量，避免一个不匹配能力的工作者扫描整表。
SCAN_LIMIT = 500


@dataclass(frozen=True, slots=True)
class ClassPolicy:
    project_code: str
    weight: int
    max_concurrent: int
    is_active: bool
    explicit: bool

    def as_audit(self) -> dict[str, Any]:
        return {
            "project_code": self.project_code,
            "weight": self.weight,
            "max_concurrent": self.max_concurrent,
            "is_active": self.is_active,
            "explicit": self.explicit,
        }


def normalize_config(raw: dict[str, Any] | None) -> dict[str, int]:
    """合并数据库配置与默认值，保证所有调度参数都有确定取值。"""
    config = dict(DEFAULT_CONFIG)
    if raw:
        for key in DEFAULT_CONFIG:
            value = raw.get(key)
            if value is not None:
                config[key] = int(value)
    return config


def resolve_policy(
    project_code: str,
    explicit: dict[str, dict[str, Any]] | None,
    config: dict[str, int],
) -> ClassPolicy:
    """未配置班级策略时回退到全局默认权重与并发份额。"""
    row = (explicit or {}).get(project_code)
    if row is None:
        return ClassPolicy(
            project_code=project_code,
            weight=int(config["default_weight"]),
            max_concurrent=int(config["default_max_concurrent"]),
            is_active=True,
            explicit=False,
        )
    return ClassPolicy(
        project_code=project_code,
        weight=int(row["weight"]),
        max_concurrent=int(row["max_concurrent"]),
        is_active=bool(row["is_active"]),
        explicit=True,
    )


def age_bonus(
    available_at: str,
    decided_at: str,
    config: dict[str, int],
) -> tuple[int, int]:
    """计算等待老化加分，返回 (加分, 已等待秒数)。"""
    from app.core.clock import from_storage

    available = from_storage(available_at)
    decided = from_storage(decided_at)
    waited = max(0, int((decided - available).total_seconds()))  # type: ignore[operator]
    step = int(config["aging_step_seconds"])
    steps = waited // step
    bonus = min(int(config["max_age_bonus"]), steps * int(config["aging_bonus_per_step"]))
    return bonus, waited


def score_task(
    task: dict[str, Any],
    policy: ClassPolicy,
    decided_at: str,
    config: dict[str, int],
) -> dict[str, Any]:
    """对单个候选打分，返回可审计的打分明细。"""
    bonus, waited = age_bonus(str(task["available_at"]), decided_at, config)
    priority = int(task["priority"])
    weight_ratio = policy.weight / 100.0
    weighted_priority = round(priority * weight_ratio, 3)
    effective = round(weighted_priority + bonus, 3)
    return {
        "task_id": int(task["id"]),
        "project_code": str(task["project_code"]),
        "template_algorithm": str(task.get("template_algorithm") or ""),
        "priority": priority,
        "class_weight": policy.weight,
        "weighted_priority": weighted_priority,
        "waited_seconds": waited,
        "age_steps": waited // int(config["aging_step_seconds"]),
        "age_bonus": bonus,
        "effective_score": effective,
        "created_at": str(task["created_at"]),
        "available_at": str(task["available_at"]),
    }


def choose_task(
    candidates: list[dict[str, Any]],
    *,
    decided_at: str,
    config: dict[str, int],
    explicit_policies: dict[str, dict[str, Any]],
    running_counts: dict[str, int],
) -> dict[str, Any]:
    """按确定性规则从候选中选出任务。

    候选池已由仓储层按工作者能力过滤（能力不匹配的任务由调用方另行审计），
    本函数只处理可运行候选的打分与班级并发份额跳过。

    返回 ``{"selected": dict|None, "evaluated": [...], "scanned": int}``。

    队首任务可能因班级并发份额已满或班级停用被跳过，选择会继续扫描后续
    班级的候选，避免单个高权班级占满队首时饿死后面的队列。
    """
    evaluated: list[dict[str, Any]] = []
    eligible: list[tuple[dict[str, Any], dict[str, Any], ClassPolicy]] = []
    scanned = 0

    for task in candidates:
        scanned += 1
        project_code = str(task["project_code"])
        policy = resolve_policy(project_code, explicit_policies, config)
        detail = score_task(task, policy, decided_at, config)

        if not policy.is_active:
            detail["decision"] = "skipped"
            detail["skip_reason"] = SKIP_INACTIVE_CLASS
            detail["class_running"] = int(running_counts.get(project_code, 0))
            detail["class_max_concurrent"] = policy.max_concurrent
            evaluated.append(detail)
            continue

        running = int(running_counts.get(project_code, 0))
        detail["class_running"] = running
        detail["class_max_concurrent"] = policy.max_concurrent
        if running >= policy.max_concurrent:
            detail["decision"] = "skipped"
            detail["skip_reason"] = SKIP_QUOTA_FULL
            evaluated.append(detail)
            continue

        detail["decision"] = "eligible"
        evaluated.append(detail)
        eligible.append((task, detail, policy))

    if not eligible:
        return {
            "selected": None,
            "evaluated": evaluated,
            "scanned": scanned,
        }

    # 有效分降序；平手时优先级降序、班级权重降序、提交时间升序、任务 id 升序，
    # 保证在固定时钟和确定任务集下顺序唯一可复现。
    def ordering(item: tuple[dict[str, Any], dict[str, Any], ClassPolicy]) -> tuple:
        task, detail, policy = item
        return (
            -float(detail["effective_score"]),
            -int(task["priority"]),
            -policy.weight,
            str(task["created_at"]),
            str(task["available_at"]),
            int(task["id"]),
        )

    task, detail, _policy = sorted(eligible, key=ordering)[0]
    detail["decision"] = "selected"
    return {
        "selected": task,
        "evaluated": evaluated,
        "scanned": scanned,
    }
