from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

import app.main as _root
from app import (
    behavior_events,
)

logger = logging.getLogger("personal_ai_os.api")

router = APIRouter()

PROJECT_STATUSES = {"planning", "active", "paused", "completed", "archived"}
PROJECT_EVENT_TYPES = {"progress", "decision", "issue", "learn", "note"}
GOAL_KINDS = {"long_term", "milestone"}
GOAL_STATUSES = {"active", "done", "dropped"}
TASK_STATUSES = {"todo", "doing", "done", "cancelled"}
TASK_PRIORITIES = {"low", "normal", "high"}

# ---------------------------------------------------------------------------
# Phase 3 iteration 3.4: projects, goals, tasks (structured personal data)
# ---------------------------------------------------------------------------

def _non_blank(value: str) -> str:
    """Reject whitespace-only required text (Pydantic min_length allows spaces)."""
    if not value or not value.strip():
        raise ValueError("内容不能为空")
    return value


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=8000)
    tech_stack: str = Field(default="", max_length=1000)
    repo_url: str = Field(default="", max_length=500)
    status: str = Field(default="active")

    @field_validator("name")
    @classmethod
    def _v(cls, v: str) -> str:
        return _non_blank(v)


class ProjectEventIn(BaseModel):
    event_type: str
    content: str = Field(min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def _v(cls, v: str) -> str:
        return _non_blank(v)


class GoalIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8000)
    kind: str = Field(default="long_term")
    status: str = Field(default="active")
    measure: str = Field(default="", max_length=1000)
    target_date: str | None = None
    project_id: int | None = None

    @field_validator("title")
    @classmethod
    def _v(cls, v: str) -> str:
        return _non_blank(v)


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    detail: str = Field(default="", max_length=8000)
    status: str = Field(default="todo")
    priority: str = Field(default="normal")
    due_date: str | None = None
    project_id: int | None = None
    goal_id: int | None = None

    @field_validator("title")
    @classmethod
    def _v(cls, v: str) -> str:
        return _non_blank(v)


def _validate_iso_date(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} 必须是 YYYY-MM-DD 格式") from exc
    return value


def _check_enum(value: str, allowed: set[str], field: str) -> str:
    value = value.strip().lower()
    if value not in allowed:
        raise HTTPException(status_code=400, detail=f"非法 {field}，允许：{sorted(allowed)}")
    return value


def _require_project(conn: sqlite3.Connection, project_id: int | None) -> None:
    if project_id is None:
        return
    if conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="关联项目不存在")


def _require_goal(conn: sqlite3.Connection, goal_id: int | None) -> None:
    if goal_id is None:
        return
    if conn.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="关联目标不存在")


@router.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    with _root.db() as conn:
        projects = [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()]
        for project in projects:
            pid = project["id"]
            project["event_count"] = conn.execute(
                "SELECT COUNT(*) FROM project_events WHERE project_id=?", (pid,)
            ).fetchone()[0]
            project["open_task_count"] = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE project_id=? AND status IN ('todo','doing')", (pid,)
            ).fetchone()[0]
            project["goal_count"] = conn.execute(
                "SELECT COUNT(*) FROM goals WHERE project_id=? AND status='active'", (pid,)
            ).fetchone()[0]
        return projects


@router.post("/api/projects")
def create_project(body: ProjectIn) -> dict[str, Any]:
    status = _check_enum(body.status, PROJECT_STATUSES, "项目状态")
    timestamp = _root.now_iso()
    with _root.db() as conn:
        cur = conn.execute(
            "INSERT INTO projects(name,description,tech_stack,repo_url,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (body.name.strip(), body.description.strip(), body.tech_stack.strip(),
             body.repo_url.strip(), status, timestamp, timestamp),
        )
        project_id = cur.lastrowid
        _root.record_behavior_event(
            conn, behavior_events.EVENT_PROJECT_CREATED, subject_type="project", subject_id=project_id,
            occurred_at=timestamp, project_id=project_id, metadata={"status": status},
        )
        return dict(conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())


@router.get("/api/projects/{project_id}")
def get_project(project_id: int) -> dict[str, Any]:
    with _root.db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        result = dict(row)
        result["events"] = [dict(r) for r in conn.execute(
            "SELECT id,event_type,content,created_at FROM project_events WHERE project_id=? ORDER BY id DESC",
            (project_id,),
        ).fetchall()]
        result["goals"] = [dict(r) for r in conn.execute(
            "SELECT id,title,kind,status,target_date FROM goals WHERE project_id=? ORDER BY id DESC",
            (project_id,),
        ).fetchall()]
        result["tasks"] = [dict(r) for r in conn.execute(
            "SELECT id,title,status,priority,due_date FROM tasks WHERE project_id=? ORDER BY id DESC",
            (project_id,),
        ).fetchall()]
        return result


@router.patch("/api/projects/{project_id}")
def update_project(project_id: int, body: ProjectIn) -> dict[str, Any]:
    status = _check_enum(body.status, PROJECT_STATUSES, "项目状态")
    with _root.db() as conn:
        existing = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        timestamp = _root.now_iso()
        conn.execute(
            "UPDATE projects SET name=?,description=?,tech_stack=?,repo_url=?,status=?,updated_at=? WHERE id=?",
            (body.name.strip(), body.description.strip(), body.tech_stack.strip(), body.repo_url.strip(),
             status, timestamp, project_id),
        )
        fields_changed = any(
            existing[key] != value
            for key, value in (
                ("name", body.name.strip()), ("description", body.description.strip()),
                ("tech_stack", body.tech_stack.strip()), ("repo_url", body.repo_url.strip()),
            )
        )
        if existing["status"] != status:
            _root.record_behavior_event(
                conn, behavior_events.EVENT_PROJECT_STATUS_CHANGED, subject_type="project",
                subject_id=project_id, occurred_at=timestamp, project_id=project_id,
                metadata={"old_status": existing["status"], "new_status": status},
            )
        elif fields_changed:
            _root.record_behavior_event(
                conn, behavior_events.EVENT_PROJECT_UPDATED, subject_type="project",
                subject_id=project_id, occurred_at=timestamp, project_id=project_id,
            )
        return dict(conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())


@router.delete("/api/projects/{project_id}")
def delete_project(project_id: int) -> dict[str, bool]:
    with _root.db() as conn:
        if conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        # project_events cascade via FK; tasks/goals keep existing rows with
        # project_id set to NULL (ON DELETE SET NULL), guarded explicitly too.
        conn.execute("UPDATE tasks SET project_id=NULL, updated_at=? WHERE project_id=?", (_root.now_iso(), project_id))
        conn.execute("UPDATE goals SET project_id=NULL, updated_at=? WHERE project_id=?", (_root.now_iso(), project_id))
        conn.execute("DELETE FROM project_events WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return {"deleted": True}


@router.post("/api/projects/{project_id}/events")
def add_project_event(project_id: int, body: ProjectEventIn) -> dict[str, Any]:
    event_type = _check_enum(body.event_type, PROJECT_EVENT_TYPES, "事件类型")
    with _root.db() as conn:
        if conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        timestamp = _root.now_iso()
        cur = conn.execute(
            "INSERT INTO project_events(project_id,event_type,content,created_at) VALUES(?,?,?,?)",
            (project_id, event_type, body.content.strip(), timestamp),
        )
        event_id = cur.lastrowid
        _root.record_behavior_event(
            conn, behavior_events.EVENT_PROJECT_EVENT_RECORDED, subject_type="project_event",
            subject_id=event_id, occurred_at=timestamp, project_id=project_id,
            metadata={"project_event_type": event_type},
        )
        conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (timestamp, project_id))
        return dict(conn.execute("SELECT * FROM project_events WHERE id=?", (event_id,)).fetchone())


@router.get("/api/goals")
def list_goals(status: str | None = None, project_id: int | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        status = _check_enum(status, GOAL_STATUSES, "目标状态")
        clauses.append("status=?")
        params.append(status)
    if project_id is not None:
        clauses.append("project_id=?")
        params.append(project_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _root.db() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM goals{where} ORDER BY (target_date IS NULL), target_date, id DESC", params
        ).fetchall()]


@router.post("/api/goals")
def create_goal(body: GoalIn) -> dict[str, Any]:
    kind = _check_enum(body.kind, GOAL_KINDS, "目标类型")
    status = _check_enum(body.status, GOAL_STATUSES, "目标状态")
    target_date = _validate_iso_date(body.target_date, "target_date")
    timestamp = _root.now_iso()
    with _root.db() as conn:
        _require_project(conn, body.project_id)
        cur = conn.execute(
            "INSERT INTO goals(title,description,kind,status,measure,target_date,project_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (body.title.strip(), body.description.strip(), kind, status, body.measure.strip(),
             target_date, body.project_id, timestamp, timestamp),
        )
        goal_id = cur.lastrowid
        _root.record_behavior_event(
            conn, behavior_events.EVENT_GOAL_CREATED, subject_type="goal", subject_id=goal_id,
            occurred_at=timestamp, project_id=body.project_id, goal_id=goal_id,
            metadata={"kind": kind, "status": status},
        )
        return dict(conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone())


@router.patch("/api/goals/{goal_id}")
def update_goal(goal_id: int, body: GoalIn) -> dict[str, Any]:
    kind = _check_enum(body.kind, GOAL_KINDS, "目标类型")
    status = _check_enum(body.status, GOAL_STATUSES, "目标状态")
    target_date = _validate_iso_date(body.target_date, "target_date")
    with _root.db() as conn:
        existing = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="目标不存在")
        _require_project(conn, body.project_id)
        timestamp = _root.now_iso()
        conn.execute(
            "UPDATE goals SET title=?,description=?,kind=?,status=?,measure=?,target_date=?,project_id=?,updated_at=? "
            "WHERE id=?",
            (body.title.strip(), body.description.strip(), kind, status, body.measure.strip(),
             target_date, body.project_id, timestamp, goal_id),
        )
        fields_changed = any(
            existing[key] != value
            for key, value in (
                ("title", body.title.strip()), ("description", body.description.strip()),
                ("kind", kind), ("measure", body.measure.strip()),
                ("target_date", target_date), ("project_id", body.project_id),
            )
        )
        if existing["status"] != status:
            _root.record_behavior_event(
                conn, behavior_events.EVENT_GOAL_STATUS_CHANGED, subject_type="goal",
                subject_id=goal_id, occurred_at=timestamp, project_id=body.project_id, goal_id=goal_id,
                metadata={"old_status": existing["status"], "new_status": status},
            )
        elif fields_changed:
            _root.record_behavior_event(
                conn, behavior_events.EVENT_GOAL_UPDATED, subject_type="goal",
                subject_id=goal_id, occurred_at=timestamp, project_id=body.project_id, goal_id=goal_id,
            )
        return dict(conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone())


@router.delete("/api/goals/{goal_id}")
def delete_goal(goal_id: int) -> dict[str, bool]:
    with _root.db() as conn:
        if conn.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="目标不存在")
        conn.execute("UPDATE tasks SET goal_id=NULL, updated_at=? WHERE goal_id=?", (_root.now_iso(), goal_id))
        conn.execute("DELETE FROM goals WHERE id=?", (goal_id,))
    return {"deleted": True}


@router.get("/api/tasks")
def list_tasks(status: str | None = None, project_id: int | None = None) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        status = _check_enum(status, PROJECT_STATUSES if False else TASK_STATUSES, "任务状态")
        clauses.append("t.status=?")
        params.append(status)
    if project_id is not None:
        clauses.append("t.project_id=?")
        params.append(project_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _root.db() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT t.*, p.name AS project_name FROM tasks t LEFT JOIN projects p ON p.id=t.project_id"
            f"{where} ORDER BY (t.due_date IS NULL), t.due_date, t.id DESC",
            params,
        ).fetchall()]


def _apply_task_write(conn: sqlite3.Connection, body: TaskIn, existing: sqlite3.Row | None, task_id: int | None) -> int:
    status = _check_enum(body.status, TASK_STATUSES, "任务状态")
    priority = _check_enum(body.priority, TASK_PRIORITIES, "优先级")
    due_date = _validate_iso_date(body.due_date, "due_date")
    _require_project(conn, body.project_id)
    _require_goal(conn, body.goal_id)
    timestamp = _root.now_iso()
    completed_at = timestamp if status == "done" else None
    common_meta = {"priority": priority}
    if task_id is None:
        cur = conn.execute(
            "INSERT INTO tasks(title,detail,status,priority,due_date,project_id,goal_id,completed_at,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (body.title.strip(), body.detail.strip(), status, priority, due_date, body.project_id,
             body.goal_id, completed_at, timestamp, timestamp),
        )
        new_id = cur.lastrowid
        _root.record_behavior_event(
            conn, behavior_events.EVENT_TASK_CREATED, subject_type="task", subject_id=new_id,
            occurred_at=timestamp, project_id=body.project_id, goal_id=body.goal_id, task_id=new_id,
            metadata={**common_meta, "status": status},
        )
        if status == "done":
            _root.record_behavior_event(
                conn, behavior_events.EVENT_TASK_COMPLETED, subject_type="task", subject_id=new_id,
                occurred_at=timestamp, project_id=body.project_id, goal_id=body.goal_id, task_id=new_id,
            )
        elif status == "cancelled":
            _root.record_behavior_event(
                conn, behavior_events.EVENT_TASK_CANCELLED, subject_type="task", subject_id=new_id,
                occurred_at=timestamp, project_id=body.project_id, goal_id=body.goal_id, task_id=new_id,
            )
        return new_id

    old_status = existing["status"] if existing is not None else None
    conn.execute(
        "UPDATE tasks SET title=?,detail=?,status=?,priority=?,due_date=?,project_id=?,goal_id=?,"
        "completed_at=?,updated_at=? WHERE id=?",
        (body.title.strip(), body.detail.strip(), status, priority, due_date, body.project_id,
         body.goal_id, completed_at, timestamp, task_id),
    )
    fields_changed = any(
        existing is not None and existing[key] != value
        for key, value in (
            ("title", body.title.strip()), ("detail", body.detail.strip()),
            ("priority", priority), ("due_date", due_date),
            ("project_id", body.project_id), ("goal_id", body.goal_id),
        )
    )
    lifecycle_event: str | None = None
    if old_status in {"todo", "doing"} and status == "done":
        lifecycle_event = behavior_events.EVENT_TASK_COMPLETED
    elif old_status == "done" and status in {"todo", "doing"}:
        lifecycle_event = behavior_events.EVENT_TASK_REOPENED
    elif old_status != "cancelled" and status == "cancelled":
        lifecycle_event = behavior_events.EVENT_TASK_CANCELLED

    if lifecycle_event is not None:
        _root.record_behavior_event(
            conn, lifecycle_event, subject_type="task", subject_id=task_id,
            occurred_at=timestamp, project_id=body.project_id, goal_id=body.goal_id, task_id=task_id,
            metadata={"old_status": old_status, "new_status": status},
        )
    elif old_status != status or fields_changed:
        _root.record_behavior_event(
            conn, behavior_events.EVENT_TASK_UPDATED, subject_type="task", subject_id=task_id,
            occurred_at=timestamp, project_id=body.project_id, goal_id=body.goal_id, task_id=task_id,
            metadata={"old_status": old_status, "new_status": status} if old_status != status else None,
        )
    return task_id


@router.post("/api/tasks")
def create_task(body: TaskIn) -> dict[str, Any]:
    with _root.db() as conn:
        new_id = _apply_task_write(conn, body, None, None)
        return dict(conn.execute(
            "SELECT t.*, p.name AS project_name FROM tasks t LEFT JOIN projects p ON p.id=t.project_id "
            "WHERE t.id=?", (new_id,)).fetchone())


@router.patch("/api/tasks/{task_id}")
def update_task(task_id: int, body: TaskIn) -> dict[str, Any]:
    with _root.db() as conn:
        existing = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        _apply_task_write(conn, body, existing, task_id)
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


@router.delete("/api/tasks/{task_id}")
def delete_task(task_id: int) -> dict[str, bool]:
    with _root.db() as conn:
        if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"deleted": True}



