"""SQLite 数据库管理 — 轨迹历史与离线设备历史的持久化存储."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("brainBox.storage.database")

_DDL = """
CREATE TABLE IF NOT EXISTS trajectories (
    trajectory_id   TEXT PRIMARY KEY,
    device_id       TEXT NOT NULL,
    algorithm_name  TEXT NOT NULL,
    total_distance  REAL DEFAULT 0.0,
    estimated_time  REAL DEFAULT 0.0,
    instruction_id  TEXT DEFAULT '',
    waypoints_json  TEXT NOT NULL,
    metadata_json   TEXT DEFAULT '{}',
    created_at      REAL NOT NULL,
    executed_at     REAL,
    status          TEXT DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_trajectories_device
    ON trajectories (device_id);

CREATE INDEX IF NOT EXISTS idx_trajectories_created
    ON trajectories (created_at);

CREATE TABLE IF NOT EXISTS device_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           TEXT NOT NULL,
    device_type         TEXT NOT NULL,
    protocol            TEXT NOT NULL,
    ip_address          TEXT DEFAULT '',
    port                INTEGER DEFAULT 0,
    last_heartbeat      REAL NOT NULL,
    last_position_json  TEXT DEFAULT '{}',
    metadata_json       TEXT DEFAULT '{}',
    evicted_at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_history_device
    ON device_history (device_id);

CREATE INDEX IF NOT EXISTS idx_device_history_evicted
    ON device_history (evicted_at);
"""


class Database:
    """
    SQLite 数据库封装.

    提供轨迹历史和离线设备历史的读写接口。
    使用同步 sqlite3（在 asyncio 环境中通过 run_in_executor 调用，
    或直接在非热路径中同步调用均可）。
    """

    def __init__(self, db_path: str | Path = "data/brain_box.db") -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    # ── 生命周期 ──────────────────────────────────────────────

    def open(self) -> None:
        """打开数据库连接并初始化表结构."""
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(_DDL)
        self._conn.commit()
        # migrate existing DBs that lack the instruction_id column
        try:
            self._conn.execute("ALTER TABLE trajectories ADD COLUMN instruction_id TEXT DEFAULT ''")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_trajectories_instruction ON trajectories (instruction_id)")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        logger.info("数据库已打开: %s", self._db_path)

    def close(self) -> None:
        """关闭数据库连接."""
        if self._conn:
            self._conn.close()
            self._conn = None
        logger.info("数据库已关闭")

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("数据库未打开，请先调用 open()")
        return self._conn

    # ── 轨迹操作 ─────────────────────────────────────────────

    def save_trajectory(
        self,
        trajectory_id: str,
        device_id: str,
        algorithm_name: str,
        total_distance: float,
        estimated_time: float,
        waypoints: list[dict[str, Any]],
        metadata: dict[str, Any],
        created_at: float | None = None,
        status: str = "pending",
        instruction_id: str = "",
    ) -> None:
        """将轨迹写入数据库."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO trajectories
                (trajectory_id, device_id, algorithm_name, total_distance,
                 estimated_time, instruction_id, waypoints_json, metadata_json, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trajectory_id,
                device_id,
                algorithm_name,
                total_distance,
                estimated_time,
                instruction_id,
                json.dumps(waypoints, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                created_at if created_at is not None else time.time(),
                status,
            ),
        )
        self.conn.commit()

    def mark_trajectory_executed(self, trajectory_id: str) -> None:
        """将轨迹标记为已执行."""
        self.conn.execute(
            "UPDATE trajectories SET status='executed', executed_at=? WHERE trajectory_id=?",
            (time.time(), trajectory_id),
        )
        self.conn.commit()

    def get_trajectory(self, trajectory_id: str) -> dict[str, Any] | None:
        """从数据库查询单条轨迹."""
        row = self.conn.execute(
            "SELECT * FROM trajectories WHERE trajectory_id=?",
            (trajectory_id,),
        ).fetchone()
        return _row_to_trajectory(row) if row else None

    def list_trajectories(
        self,
        device_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询轨迹列表，支持按设备和状态过滤."""
        conditions: list[str] = []
        params: list[Any] = []
        if device_id:
            conditions.append("device_id=?")
            params.append(device_id)
        if status:
            conditions.append("status=?")
            params.append(status)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM trajectories {where} ORDER BY created_at DESC LIMIT ?",  # noqa: S608
            params,
        ).fetchall()
        return [_row_to_trajectory(r) for r in rows]

    def list_tasks_by_instruction(
        self,
        instruction_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按 instruction_id 查询轨迹列表."""
        rows = self.conn.execute(
            "SELECT * FROM trajectories WHERE instruction_id=? ORDER BY created_at DESC LIMIT ?",
            (instruction_id, limit),
        ).fetchall()
        return [_row_to_trajectory(r) for r in rows]

    # ── 离线设备历史操作 ──────────────────────────────────────

    def save_device_history(
        self,
        device_id: str,
        device_type: str,
        protocol: str,
        ip_address: str,
        port: int,
        last_heartbeat: float,
        last_position: dict[str, float],
        metadata: dict[str, Any],
        evicted_at: float | None = None,
    ) -> None:
        """将被驱逐的离线设备写入历史表."""
        self.conn.execute(
            """
            INSERT INTO device_history
                (device_id, device_type, protocol, ip_address, port,
                 last_heartbeat, last_position_json, metadata_json, evicted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                device_type,
                protocol,
                ip_address,
                port,
                last_heartbeat,
                json.dumps(last_position, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                evicted_at if evicted_at is not None else time.time(),
            ),
        )
        self.conn.commit()

    def list_device_history(
        self,
        device_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """查询设备历史记录."""
        if device_id:
            rows = self.conn.execute(
                "SELECT * FROM device_history WHERE device_id=? ORDER BY evicted_at DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM device_history ORDER BY evicted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_device_history(r) for r in rows]


# ── 辅助函数 ──────────────────────────────────────────────────


def _row_to_trajectory(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["waypoints"] = json.loads(d.pop("waypoints_json", "[]"))
    d["metadata"] = json.loads(d.pop("metadata_json", "{}"))
    return d


def _row_to_device_history(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["last_position"] = json.loads(d.pop("last_position_json", "{}"))
    d["metadata"] = json.loads(d.pop("metadata_json", "{}"))
    return d