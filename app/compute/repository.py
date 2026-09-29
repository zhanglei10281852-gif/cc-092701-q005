from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


class ComputeRepository:
    """封装计算任务运营领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def template_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_templates WHERE code=?", (code,)).fetchone()

    def template_by_id(self, template_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_templates WHERE id=?", (template_id,)).fetchone()

    def active_templates(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM compute_templates WHERE active=1 ORDER BY code,version").fetchall()
        return [dict(row) for row in rows]

    def create_template(self, *, code: str, name: str, algorithm: str, parameter_schema: dict[str, Any], defaults: dict[str, Any], max_runtime_seconds: int, max_attempts: int, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_templates(code,name,algorithm,version,parameter_schema_json,default_parameters_json,max_runtime_seconds,max_attempts,active,created_by,created_at,updated_at) VALUES(?,?,?,1,?,?,?,?,1,?,?,?)",
            (code, name, algorithm, json.dumps(parameter_schema, ensure_ascii=False, sort_keys=True), json.dumps(defaults, ensure_ascii=False, sort_keys=True), max_runtime_seconds, max_attempts, created_by, now, now),
        )
        return dict(self.template_by_id(cursor.lastrowid))

    def quota(self, subject_type: str, subject_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_quotas WHERE subject_type=? AND subject_key=?", (subject_type, subject_key)).fetchone()

    def quotas_by_type(self, subject_type: str) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT subject_key,max_queued,max_running,daily_submissions FROM compute_quotas WHERE subject_type=? ORDER BY subject_key",
            (subject_type,),
        ).fetchall()
        return {
            str(row["subject_key"]): {
                "max_queued": int(row["max_queued"]),
                "max_running": int(row["max_running"]),
                "daily_submissions": int(row["daily_submissions"]),
            }
            for row in rows
        }

    def upsert_quota(self, *, subject_type: str, subject_key: str, max_queued: int, max_running: int, daily_submissions: int, actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO compute_quotas(subject_type,subject_key,max_queued,max_running,daily_submissions,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(subject_type,subject_key) DO UPDATE SET max_queued=excluded.max_queued,max_running=excluded.max_running,daily_submissions=excluded.daily_submissions,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (subject_type, subject_key, max_queued, max_running, daily_submissions, actor, now, now),
        )
        return dict(self.quota(subject_type, subject_key))

    def count_user_states(self, requested_by: str) -> dict[str, int]:
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM compute_tasks WHERE requested_by=? GROUP BY status", (requested_by,)).fetchall()
        return {str(row["status"]): int(row["amount"]) for row in rows}

    def count_user_submissions_since(self, requested_by: str, since: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM compute_tasks WHERE requested_by=? AND created_at>=?", (requested_by, since)).fetchone()[0])

    def task_by_id(self, task_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT t.*,tpl.code AS template_code,tpl.algorithm AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id WHERE t.id=?", (task_id,)).fetchone()

    def task_by_idempotency(self, requested_by: str, key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_tasks WHERE requested_by=? AND idempotency_key=?", (requested_by, key)).fetchone()

    def create_task(self, *, template_id: int, project_code: str, requested_by: str, parameters: dict[str, Any], parameter_digest: str, priority: int, idempotency_key: str, max_attempts: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_tasks(template_id,project_code,requested_by,parameters_json,parameter_digest,priority,idempotency_key,status,attempt_count,max_attempts,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',0,?,?,?,?)",
            (template_id, project_code, requested_by, json.dumps(parameters, ensure_ascii=False, sort_keys=True), parameter_digest, priority, idempotency_key, max_attempts, now, now, now),
        )
        return dict(self.task_by_id(cursor.lastrowid))

    def queued_candidates(
        self,
        capabilities: Iterable[str],
        now: str,
        *,
        scan_limit: int,
        per_class_limit: int,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """返回 (可领取候选, 能力匹配候选总数, 全部已到点候选总数)。

        候选按工作者能力过滤；使用窗口函数保证每个班级排名最靠前的
        ``per_class_limit`` 个任务都会进入扫描窗口，避免某个班级大量高优先级
        任务占满窗口、把其他班级挤出扫描范围。最终结果按全局优先级顺序返回，
        打分与选择在服务层完成。
        """
        capability_list = sorted(set(capabilities))
        capability_condition = ""
        params: list[Any] = [now]
        if capability_list:
            placeholders = ",".join("?" for _ in capability_list)
            capability_condition = f" AND tpl.algorithm IN ({placeholders})"
            params.extend(capability_list)

        due_total = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id WHERE t.status='queued' AND t.available_at<=?",
                (now,),
            ).fetchone()[0]
        )
        matched_total = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id WHERE t.status='queued' AND t.available_at<=?" + capability_condition,
                params,
            ).fetchone()[0]
        )

        query = (
            "SELECT * FROM ("
            "SELECT t.*,tpl.algorithm AS template_algorithm,"
            "ROW_NUMBER() OVER (PARTITION BY t.project_code ORDER BY t.priority DESC,t.created_at ASC,t.id ASC) AS class_rank,"
            "ROW_NUMBER() OVER (ORDER BY t.priority DESC,t.created_at ASC,t.id ASC) AS global_rank "
            "FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id "
            "WHERE t.status='queued' AND t.available_at<=?" + capability_condition +
            ") WHERE class_rank<=? ORDER BY global_rank LIMIT ?"
        )
        rows = self.connection.execute(query, (*params, per_class_limit, scan_limit)).fetchall()
        return [dict(row) for row in rows], matched_total, due_total

    def queued_capability_rejections(self, capabilities: Iterable[str], now: str, limit: int) -> list[dict[str, Any]]:
        """已到可领取时刻、但当前工作者能力不匹配的任务，用于领取审计。"""
        capability_list = sorted(set(capabilities))
        if not capability_list:
            return []
        placeholders = ",".join("?" for _ in capability_list)
        rows = self.connection.execute(
            "SELECT t.id,t.project_code,tpl.algorithm AS template_algorithm "
            "FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id "
            f"WHERE t.status='queued' AND t.available_at<=? AND tpl.algorithm NOT IN ({placeholders}) "
            "ORDER BY t.priority DESC,t.created_at ASC,t.id ASC LIMIT ?",
            [now, *capability_list, limit],
        ).fetchall()
        return [dict(row) for row in rows]

    def running_counts_by_project(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT project_code,COUNT(*) AS amount FROM compute_tasks WHERE status='running' GROUP BY project_code"
        ).fetchall()
        return {str(row["project_code"]): int(row["amount"]) for row in rows}

    def class_policy(self, project_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_class_policies WHERE project_code=?", (project_code,)).fetchone()

    def list_class_policies(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_class_policies ORDER BY project_code").fetchall()]

    def upsert_class_policy(self, *, project_code: str, weight: int, max_concurrent: int, is_active: bool, note: str, actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO compute_class_policies(project_code,weight,max_concurrent,is_active,note,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_code) DO UPDATE SET weight=excluded.weight,max_concurrent=excluded.max_concurrent,is_active=excluded.is_active,note=excluded.note,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (project_code, weight, max_concurrent, 1 if is_active else 0, note, actor, now, now),
        )
        return dict(self.class_policy(project_code))

    def schedule_config(self) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_schedule_config WHERE id=1").fetchone()

    def upsert_schedule_config(self, **values: Any) -> dict[str, Any]:
        fields = ("aging_step_seconds", "aging_bonus_per_step", "max_age_bonus", "default_weight", "default_max_concurrent")
        assignments = ",".join(f"{name}=?" for name in fields)
        params: list[Any] = [values[name] for name in fields]
        self.connection.execute(
            f"UPDATE compute_schedule_config SET {assignments},updated_by=?,updated_at=? WHERE id=1",
            (*params, values["actor"], values["now"]),
        )
        return dict(self.schedule_config())

    def ensure_schedule_config(self, defaults: dict[str, int], *, actor: str, now: str) -> dict[str, Any]:
        existing = self.schedule_config()
        if existing is not None:
            return dict(existing)
        self.connection.execute(
            "INSERT INTO compute_schedule_config(id,aging_step_seconds,aging_bonus_per_step,max_age_bonus,default_weight,default_max_concurrent,updated_by,created_at,updated_at) VALUES(1,?,?,?,?,?,?,?,?)",
            (defaults["aging_step_seconds"], defaults["aging_bonus_per_step"], defaults["max_age_bonus"], defaults["default_weight"], defaults["default_max_concurrent"], actor, now, now),
        )
        return dict(self.schedule_config())

    def add_claim_decision(self, record: dict[str, Any]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO compute_claim_decisions(worker_id,capabilities_json,decided_at,outcome,selected_task_id,lease_expires_at,total_available,scanned_count,config_json,policies_json,running_counts_json,project_quotas_json,evaluated_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record["worker_id"],
                json.dumps(record["capabilities"], ensure_ascii=False, sort_keys=True),
                record["decided_at"],
                record["outcome"],
                record.get("selected_task_id"),
                record.get("lease_expires_at", ""),
                int(record["total_available"]),
                int(record["scanned_count"]),
                json.dumps(record["config"], ensure_ascii=False, sort_keys=True),
                json.dumps(record["policies"], ensure_ascii=False, sort_keys=True),
                json.dumps(record["running_counts"], ensure_ascii=False, sort_keys=True),
                json.dumps(record["project_quotas"], ensure_ascii=False, sort_keys=True),
                json.dumps(record["evaluated"], ensure_ascii=False, sort_keys=True),
                record["created_at"],
            ),
        )
        return int(cursor.lastrowid)

    def claim_decisions(self, *, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM compute_claim_decisions ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
        return [self._decision_row(row) for row in rows]

    def claim_decision(self, decision_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM compute_claim_decisions WHERE id=?", (decision_id,)).fetchone()
        return self._decision_row(row) if row else None

    @staticmethod
    def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("capabilities_json", "config_json", "policies_json", "running_counts_json", "project_quotas_json", "evaluated_json"):
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        return result

    def result_versions(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_results WHERE task_id=? ORDER BY version", (task_id,)).fetchall()]

    def interventions(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_interventions WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]

    def add_intervention(self, *, task_id: int, actor: str, action: str, reason: str, before: dict[str, Any], after: dict[str, Any], batch_key: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO compute_interventions(task_id,actor,action,reason,before_json,after_json,batch_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, actor, action, reason, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), batch_key, now),
        )

    def list_tasks(self, *, status: str | None, project_code: str | None, requested_by: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("t.status=?")
            values.append(status)
        if project_code:
            clauses.append("t.project_code=?")
            values.append(project_code)
        if requested_by:
            clauses.append("t.requested_by=?")
            values.append(requested_by)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT t.*,tpl.code AS template_code,tpl.algorithm AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id" + where + " ORDER BY t.priority DESC,t.created_at DESC,t.id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]
