"""
Agent Task Queue (ATQ) — lightweight HTTP API for orchestrating homelab agents.

Runs on AI2 (10.10.10.226:8700). Used by:
  - Claude Code: creates tasks, reads results, handles escalations
  - Hermes: claims tasks, writes results, escalates when stuck
  - Any host on 10.10.10.0/24 can push/pull tasks

Task lifecycle: pending → active → done | escalated | failed
"""

import sqlite3
import uuid
import json
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

WHATSAPP_BRIDGE = "http://127.0.0.1:3000"
WHATSAPP_SELF = "15712158218@s.whatsapp.net"

DB_PATH = Path.home() / "atq" / "tasks.db"

app = FastAPI(title="Agent Task Queue", version="1.0.0")


# ── DB ────────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          TEXT PRIMARY KEY,
                type        TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                priority    INTEGER NOT NULL DEFAULT 5,
                assigned_to TEXT NOT NULL DEFAULT 'hermes',
                created_by  TEXT NOT NULL DEFAULT 'claude',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                context     TEXT NOT NULL DEFAULT '{}',
                instructions TEXT NOT NULL DEFAULT '',
                result      TEXT,
                error       TEXT
            )
        """)
        con.commit()


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def row_to_dict(row) -> dict:
    d = dict(row)
    for field in ("context", "result"):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except Exception:
                pass
    return d


# ── Models ────────────────────────────────────────────────────────────────────

VALID_STATUSES = {"pending", "active", "done", "escalated", "failed"}
VALID_AGENTS   = {"hermes", "hermes-ai1", "hermes-ai2", "claude", "human"}


class TaskCreate(BaseModel):
    type: str
    instructions: str
    context: dict = Field(default_factory=dict)
    priority: int = 5
    assigned_to: str = "hermes"
    created_by: str = "claude"


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    assigned_to: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()


@app.post("/tasks", status_code=201)
def create_task(body: TaskCreate):
    if body.assigned_to not in VALID_AGENTS:
        raise HTTPException(400, f"assigned_to must be one of {VALID_AGENTS}")
    now = datetime.now().isoformat()
    task_id = str(uuid.uuid4())[:8]
    with db() as con:
        con.execute(
            """INSERT INTO tasks
               (id, type, status, priority, assigned_to, created_by,
                created_at, updated_at, context, instructions)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (task_id, body.type, "pending", body.priority,
             body.assigned_to, body.created_by, now, now,
             json.dumps(body.context), body.instructions)
        )
    return {"id": task_id, "status": "pending"}


@app.get("/tasks")
def list_tasks(
    status:      Optional[str] = Query(None),
    assigned_to: Optional[str] = Query(None),
    type:        Optional[str] = Query(None),
    limit:       int           = Query(50),
):
    clauses, params = [], []
    if status:
        clauses.append("status = ?"); params.append(status)
    if assigned_to:
        clauses.append("assigned_to = ?"); params.append(assigned_to)
    if type:
        clauses.append("type = ?"); params.append(type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM tasks {where} ORDER BY priority DESC, created_at ASC LIMIT ?",
            params + [limit]
        ).fetchall()
    return {"tasks": [row_to_dict(r) for r in rows], "count": len(rows)}


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Task not found")
    return row_to_dict(row)


@app.patch("/tasks/{task_id}")
def update_task(task_id: str, body: TaskUpdate):
    if body.status and body.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {VALID_STATUSES}")
    if body.assigned_to and body.assigned_to not in VALID_AGENTS:
        raise HTTPException(400, f"assigned_to must be one of {VALID_AGENTS}")

    updates, params = [], []
    if body.status      is not None: updates.append("status = ?");      params.append(body.status)
    if body.result      is not None: updates.append("result = ?");      params.append(json.dumps(body.result))
    if body.error       is not None: updates.append("error = ?");       params.append(body.error)
    if body.assigned_to is not None: updates.append("assigned_to = ?"); params.append(body.assigned_to)
    if not updates:
        raise HTTPException(400, "Nothing to update")

    updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
    params.append(task_id)

    with db() as con:
        cur = con.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Task not found")
    return get_task(task_id)


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: str):
    with db() as con:
        cur = con.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Task not found")


@app.get("/stats")
def stats():
    with db() as con:
        rows = con.execute(
            "SELECT status, COUNT(*) as n FROM tasks GROUP BY status"
        ).fetchall()
        total = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    by_status = {r["status"]: r["n"] for r in rows}
    return {"total": total, "by_status": by_status}


@app.get("/health")
def health():
    return {"ok": True}


# ── WhatsApp proxy ────────────────────────────────────────────────────────────

class WAMessage(BaseModel):
    message: str
    chatId: Optional[str] = None  # defaults to self-chat

@app.post("/whatsapp/send")
def whatsapp_send(body: WAMessage):
    """Proxy to the WhatsApp bridge (localhost:3000) — reachable from any homelab host."""
    chat_id = body.chatId or WHATSAPP_SELF
    try:
        r = httpx.post(f"{WHATSAPP_BRIDGE}/send",
                       json={"chatId": chat_id, "message": body.message},
                       timeout=15)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"bridge error: {e.response.text}")
    except Exception as e:
        raise HTTPException(502, f"bridge unreachable: {e}")
