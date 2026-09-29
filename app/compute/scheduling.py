from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from app.core.clock import from_storage

# 内置默认调度策略：数据库中还没有任何策略版本时使用。
# 默认所有班级权重相同、不限制并发份额、等待每小时累积 1 个有效分（上限 100），
# 因此默认行为下高优先级仍然领先，但等待足够久的任务会逐步提升有效排序。
DEFAULT_POLICY_VERSION = 0
DEFAULT_WEIGHT = 1.0
DEFAULT_AGING_RATE_PER_HOUR = 1.0
DEFAULT_AGING_MAX_BONUS = 100.0

SCORE_PRECISION = 6

# 跳过原因是可机读的稳定编码，供教务处审计与报表使用。
SKIP_CAPABILITY_MISMATCH = "capability_mismatch"
SKIP_CLASS_CONCURRENCY_FULL = "class_concurrency_full"
SKIP_CLAIM_RACE_LOST = "claim_race_lost"


@dataclass(frozen=True, slots=True)
class ClassRule:
    """单个班级的调度覆盖配置；字段为 None 时回落到全局默认值。"""

    weight: float | None = None
    max_concurrent: int | None = None


@dataclass(frozen=True, slots=True)
class SchedulingPolicy:
    """某一版本的完整调度策略。每次配置变更都生成新的自包含版本，只影响后续决策。"""

    version: int
    default_weight: float
    default_max_concurrent: int | None
    aging_rate_per_hour: float
    aging_max_bonus: float
    classes: dict[str, ClassRule]
    updated_by: str = ""
    created_at: str = ""

    def class_weight(self, class_code: str) -> float:
        rule = self.classes.get(class_code)
        if rule is None or rule.weight is None:
            return self.default_weight
        return rule.weight

    def class_max_concurrent(self, class_code: str) -> int | None:
        rule = self.classes.get(class_code)
        if rule is None or rule.max_concurrent is None:
            return self.default_max_concurrent
        return rule.max_concurrent

    def snapshot(self) -> dict[str, Any]:
        """存入领取决策记录的可审计快照，决策解释不依赖后续配置变更。"""
        return {
            "version": self.version,
            "default_weight": self.default_weight,
            "default_max_concurrent": self.default_max_concurrent,
            "aging_rate_per_hour": self.aging_rate_per_hour,
            "aging_max_bonus": self.aging_max_bonus,
            "classes": {
                code: {"weight": rule.weight, "max_concurrent": rule.max_concurrent}
                for code, rule in sorted(self.classes.items())
            },
        }


def default_policy() -> SchedulingPolicy:
    return SchedulingPolicy(
        version=DEFAULT_POLICY_VERSION,
        default_weight=DEFAULT_WEIGHT,
        default_max_concurrent=None,
        aging_rate_per_hour=DEFAULT_AGING_RATE_PER_HOUR,
        aging_max_bonus=DEFAULT_AGING_MAX_BONUS,
        classes={},
    )


def policy_from_row(row: sqlite3.Row) -> SchedulingPolicy:
    raw_classes = json.loads(row["classes_json"])
    classes = {
        str(code): ClassRule(
            weight=None if value.get("weight") is None else float(value["weight"]),
            max_concurrent=value.get("max_concurrent"),
        )
        for code, value in raw_classes.items()
    }
    return SchedulingPolicy(
        version=int(row["version"]),
        default_weight=float(row["default_weight"]),
        default_max_concurrent=row["default_max_concurrent"],
        aging_rate_per_hour=float(row["aging_rate_per_hour"]),
        aging_max_bonus=float(row["aging_max_bonus"]),
        classes=classes,
        updated_by=str(row["updated_by"]),
        created_at=str(row["created_at"]),
    )


def aging_bonus(policy: SchedulingPolicy, wait_seconds: float) -> float:
    """等待老化：等待越久有效排序加成分越大，但不超过策略上限。"""
    bonus = max(0.0, wait_seconds) * policy.aging_rate_per_hour / 3600.0
    return round(min(policy.aging_max_bonus, bonus), SCORE_PRECISION)


def effective_score(policy: SchedulingPolicy, *, priority: int, weight: float, wait_seconds: float) -> tuple[float, float]:
    """有效分 = 原始优先级 × 班级权重 + 等待老化加成。返回 (有效分, 老化加成)。"""
    bonus = aging_bonus(policy, wait_seconds)
    return round(priority * weight + bonus, SCORE_PRECISION), bonus


def rank_candidates(
    rows: Iterable[sqlite3.Row],
    *,
    policy: SchedulingPolicy,
    now: datetime,
    running_counts: dict[str, int],
    capabilities: list[str],
) -> list[dict[str, Any]]:
    """为可领取候选计算有效分，给出确定性的全序排名与每条记录的跳过原因。

    排序键：有效分降序 → 原始优先级降序 → 创建时间升序 → 任务 id 升序。
    任务 id 唯一，因此排名是全序的：固定时钟与确定任务集下重复执行得到同样的顺序。
    配额已满或能力不匹配的记录保留在排名中并标注跳过原因，不会阻塞后续记录。
    """
    capability_set = set(capabilities)
    entries: list[dict[str, Any]] = []
    for row in rows:
        available_at = from_storage(row["available_at"]) or now
        wait_seconds = max(0.0, (now - available_at).total_seconds())
        class_code = str(row["project_code"])
        weight = policy.class_weight(class_code)
        score, bonus = effective_score(policy, priority=int(row["priority"]), weight=weight, wait_seconds=wait_seconds)
        entries.append(
            {
                "task_id": int(row["id"]),
                "class_code": class_code,
                "algorithm": str(row["template_algorithm"]),
                "priority": int(row["priority"]),
                "weight": weight,
                "available_at": row["available_at"],
                "wait_seconds": round(wait_seconds, 3),
                "aging_bonus": bonus,
                "effective_score": score,
                "running_in_class": int(running_counts.get(class_code, 0)),
                "max_concurrent": policy.class_max_concurrent(class_code),
                "created_at": row["created_at"],
                "eligible": True,
                "skip_reason": None,
            }
        )
    entries.sort(key=lambda item: (-item["effective_score"], -item["priority"], item["created_at"], item["task_id"]))
    for entry in entries:
        if capability_set and entry["algorithm"] not in capability_set:
            entry["eligible"] = False
            entry["skip_reason"] = SKIP_CAPABILITY_MISMATCH
        elif entry["max_concurrent"] is not None and entry["running_in_class"] >= entry["max_concurrent"]:
            entry["eligible"] = False
            entry["skip_reason"] = SKIP_CLASS_CONCURRENCY_FULL
    return entries
