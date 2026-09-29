from __future__ import annotations

import json
import sqlite3
from typing import Any


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

    def queued_available_tasks(self, now: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT t.*,tpl.algorithm AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id WHERE t.status='queued' AND t.available_at<=? ORDER BY t.id",
            (now,),
        ).fetchall()

    def running_counts_by_class(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT project_code,COUNT(*) AS amount FROM compute_tasks WHERE status IN ('running','cancel_requested') GROUP BY project_code"
        ).fetchall()
        return {str(row["project_code"]): int(row["amount"]) for row in rows}

    def current_policy_row(self) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_scheduling_policies ORDER BY version DESC LIMIT 1").fetchone()

    def create_policy(self, *, default_weight: float, default_max_concurrent: int | None, aging_rate_per_hour: float, aging_max_bonus: float, classes: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        version = int(self.connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM compute_scheduling_policies").fetchone()[0])
        cursor = self.connection.execute(
            "INSERT INTO compute_scheduling_policies(version,default_weight,default_max_concurrent,aging_rate_per_hour,aging_max_bonus,classes_json,updated_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (version, default_weight, default_max_concurrent, aging_rate_per_hour, aging_max_bonus, json.dumps(classes, ensure_ascii=False, sort_keys=True), actor, now),
        )
        return dict(self.connection.execute("SELECT * FROM compute_scheduling_policies WHERE id=?", (cursor.lastrowid,)).fetchone())

    def policy_history(self, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM compute_scheduling_policies ORDER BY version DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def add_claim_decision(self, *, worker_id: str, capabilities: list[str], policy_snapshot: dict[str, Any], chosen_task_id: int | None, candidates: list[dict[str, Any]], now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO compute_claim_decisions(worker_id,capabilities_json,policy_version,policy_snapshot_json,chosen_task_id,candidates_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                worker_id,
                json.dumps(capabilities, ensure_ascii=False),
                int(policy_snapshot["version"]),
                json.dumps(policy_snapshot, ensure_ascii=False, sort_keys=True),
                chosen_task_id,
                json.dumps(candidates, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        return int(cursor.lastrowid)

    def list_claim_decisions(self, *, worker_id: str | None, task_id: int | None, chosen_only: bool, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if worker_id:
            clauses.append("worker_id=?")
            values.append(worker_id)
        if task_id is not None:
            clauses.append("chosen_task_id=?")
            values.append(task_id)
        if chosen_only:
            clauses.append("chosen_task_id IS NOT NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT id,worker_id,capabilities_json,policy_version,chosen_task_id,created_at,json_array_length(candidates_json) AS candidate_count FROM compute_claim_decisions" + where + " ORDER BY id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def claim_decision_by_id(self, decision_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_claim_decisions WHERE id=?", (decision_id,)).fetchone()

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
