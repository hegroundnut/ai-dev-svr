"""SQLite 任务数据库 — 导航任务持久化存储（替代内存 dict）.

设计要点：
- 任务/轨迹全部落库，内存不常驻任何任务数据，不随任务量增长；
- 轨迹（result 中的 waypoints）入库前必须经过抽稀压缩（调用方负责，
  见 storage.simplify.simplify_trajectory_dict）；
- 单连接 + RLock 保证多线程安全（HTTP 线程 / WS 事件线程 / 转发后台线程）；
- WAL 模式，与 brainBox 侧数据库风格保持一致。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from models.base import NavigationStatus
from models.task import NavigationTask

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id              TEXT PRIMARY KEY,
    instruction_id       TEXT DEFAULT '',
    box_id               TEXT NOT NULL,
    device_id            TEXT DEFAULT '',
    target_position_json TEXT DEFAULT '{}',
    algorithm            TEXT DEFAULT '',
    parameters_json      TEXT DEFAULT '{}',
    status               TEXT DEFAULT 'pending',
    trajectory_id        TEXT DEFAULT '',
    created_at           REAL NOT NULL,
    completed_at         REAL,
    result_json          TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tasks_box
    ON tasks (box_id);

CREATE INDEX IF NOT EXISTS idx_tasks_instruction
    ON tasks (instruction_id);

CREATE INDEX IF NOT EXISTS idx_tasks_trajectory
    ON tasks (trajectory_id);

CREATE INDEX IF NOT EXISTS idx_tasks_created
    ON tasks (created_at);
"""


def _row_to_task(row: sqlite3.Row) -> NavigationTask:
    d = dict(row)
    return NavigationTask(
        task_id=d["task_id"],
        instruction_id=d.get("instruction_id") or "",
        box_id=d.get("box_id") or "",
        device_id=d.get("device_id") or "",
        target_position=json.loads(d.get("target_position_json") or "{}"),
        algorithm=d.get("algorithm") or "",
        parameters=json.loads(d.get("parameters_json") or "{}"),
        status=NavigationStatus(d.get("status") or "pending"),
        trajectory_id=d.get("trajectory_id") or None,
        created_at=d.get("created_at") or time.time(),
        completed_at=d.get("completed_at"),
        result=json.loads(d.get("result_json") or "{}"),
    )


class TaskDatabase:
    """导航任务 SQLite 存储（线程安全）."""

    def __init__(self, db_path: str | Path = "data/manage_server.db") -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    # ── 生命周期 ──────────────────────────────────────────────

    def open(self) -> None:
        """打开数据库连接并初始化表结构."""
        with self._lock:
            self._conn = sqlite3.connect(
                str(self._db_path), check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.executescript(_DDL)
            self._conn.commit()
            logger.info("任务数据库已打开: %s", self._db_path)

    def close(self) -> None:
        """关闭数据库连接."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
            logger.info("任务数据库已关闭")

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("任务数据库未打开，请先调用 open()")
        return self._conn

    # ── 任务写入 ──────────────────────────────────────────────

    def save_task(self, task: NavigationTask) -> None:
        """插入或整体替换一条任务记录."""
        with self._lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO tasks
                    (task_id, instruction_id, box_id, device_id,
                     target_position_json, algorithm, parameters_json,
                     status, trajectory_id, created_at, completed_at, result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.instruction_id,
                    task.box_id,
                    task.device_id,
                    json.dumps(dict(task.target_position), ensure_ascii=False),
                    task.algorithm,
                    json.dumps(dict(task.parameters), ensure_ascii=False),
                    task.status.value,
                    task.trajectory_id or "",
                    task.created_at,
                    task.completed_at,
                    json.dumps(dict(task.result), ensure_ascii=False, default=str),
                ),
            )
            self.conn.commit()

    def update_task(
        self,
        task_id: str,
        status: Optional[NavigationStatus] = None,
        trajectory_id: Optional[str] = None,
        result: Optional[dict] = None,
        completed_at: Optional[float] = None,
    ) -> bool:
        """按字段增量更新任务，返回是否命中记录."""
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status=?")
            params.append(status.value)
        if trajectory_id is not None:
            sets.append("trajectory_id=?")
            params.append(trajectory_id)
        if result is not None:
            sets.append("result_json=?")
            params.append(json.dumps(result, ensure_ascii=False, default=str))
        if completed_at is not None:
            sets.append("completed_at=?")
            params.append(completed_at)
        if not sets:
            return False
        params.append(task_id)
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?",  # noqa: S608
                params,
            )
            self.conn.commit()
            return cur.rowcount > 0

    # ── 任务查询 ──────────────────────────────────────────────

    def get_task(self, task_id: str) -> Optional[NavigationTask]:
        """按 task_id 查询单条任务."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(
        self,
        box_id: Optional[str] = None,
        instruction_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[NavigationTask]:
        """查询任务列表（按创建时间倒序），支持按盒子/指令过滤."""
        conditions: list[str] = []
        params: list[Any] = []
        if box_id:
            conditions.append("box_id=?")
            params.append(box_id)
        if instruction_id:
            conditions.append("instruction_id=?")
            params.append(instruction_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
                params,
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def latest_task(self, box_id: Optional[str] = None) -> Optional[NavigationTask]:
        """获取最新创建的一条任务（可选按盒子过滤）."""
        tasks = self.list_tasks(box_id=box_id, limit=1)
        return tasks[0] if tasks else None

    def find_by_trajectory(
        self,
        trajectory_id: str,
        statuses: Optional[list[NavigationStatus]] = None,
    ) -> Optional[NavigationTask]:
        """按 trajectory_id 查找任务（可限定状态，取最新一条）."""
        if not trajectory_id:
            return None
        conditions = ["trajectory_id=?"]
        params: list[Any] = [trajectory_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(s.value for s in statuses)
        params.append(1)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE {} "
                "ORDER BY created_at DESC LIMIT ?".format(" AND ".join(conditions)),
                params,
            ).fetchone()
        return _row_to_task(row) if row else None

    def find_by_box_device(
        self,
        box_id: str,
        device_id: str,
        statuses: Optional[list[NavigationStatus]] = None,
    ) -> Optional[NavigationTask]:
        """按 (box_id, device_id, 状态集合) 查找最早创建的一条任务（FIFO 匹配）."""
        conditions = ["box_id=?", "device_id=?"]
        params: list[Any] = [box_id, device_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(s.value for s in statuses)
        params.append(1)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE {} "
                "ORDER BY created_at ASC LIMIT ?".format(" AND ".join(conditions)),
                params,
            ).fetchone()
        return _row_to_task(row) if row else None
