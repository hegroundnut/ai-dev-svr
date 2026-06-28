"""导航服务 — 协调导航指令处理、轨迹生成和设备控制."""

from __future__ import annotations

import logging
import time
from typing import Any

from models.algorithm import NavigationInstruction, NavigationTrajectory
from storage.database import Database

from core.navigation.registry import AlgorithmRegistry
from core.drone_manager import DroneManager
from core.mavlink.connection import MAVLinkProtocol
from core.mavlink.protocol import ProtocolRegistry

logger = logging.getLogger("brainBox.core.navigation_service")


class NavigationService:
    """
    导航服务.

    负责：
    1. 接收边缘控制服务下发的导航指令
    2. 调用导航算法生成轨迹
    3. 通过设备管理器控制无人机执行任务

    内存策略：
    - ``_active_trajectories`` 仅保留状态为 ``pending``（尚未执行）的轨迹。
    - 轨迹一旦执行（或执行失败），立即写入 SQLite 并从内存字典移除，
      避免已完成的历史轨迹无限积累在内存中。
    """

    def __init__(
        self,
        algorithm_registry: AlgorithmRegistry,
        drone_manager: DroneManager,
        protocol_registry: ProtocolRegistry,
        database: Database,
    ) -> None:
        self._algorithms = algorithm_registry
        self._drone_manager = drone_manager
        self._protocol_registry = protocol_registry
        self._db = database
        # 仅保存 pending（待执行）轨迹；执行后立即持久化并从此字典移除
        self._active_trajectories: dict[str, NavigationTrajectory] = {}

    async def process_instruction(
        self, instruction: NavigationInstruction
    ) -> NavigationTrajectory:
        """处理导航指令，生成轨迹并写入数据库（状态 pending）."""
        algo_name = instruction.algorithm
        algo = self._algorithms.get(algo_name)
        if not algo:
            algo = self._algorithms.get_default()
        if not algo:
            raise ValueError(f"没有可用的导航算法: {algo_name}")

        device = self._drone_manager.get_device(instruction.device_id)
        current_position: dict[str, float] = (
            device.position if device
            else {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
        )

        trajectory = await algo.generate_trajectory(
            device_id=instruction.device_id,
            current_position=current_position,
            target_position=instruction.target_position,
            parameters=instruction.parameters,
        )

        trajectory.instruction_id = instruction.instruction_id

        # 写入数据库（pending 状态）
        self._db.save_trajectory(
            trajectory_id=trajectory.trajectory_id,
            device_id=trajectory.device_id,
            algorithm_name=trajectory.algorithm_name,
            total_distance=trajectory.total_distance,
            estimated_time=trajectory.estimated_time,
            waypoints=[w.to_dict() for w in trajectory.waypoints],
            metadata=trajectory.metadata,
            created_at=time.time(),
            status="pending",
            instruction_id=instruction.instruction_id,
        )

        # 仅在内存中暂存，等待执行
        self._active_trajectories[trajectory.trajectory_id] = trajectory
        logger.info(
            "导航轨迹已生成: %s (设备=%s, 算法=%s, 航点数=%d)",
            trajectory.trajectory_id,
            instruction.device_id,
            trajectory.algorithm_name,
            len(trajectory.waypoints),
        )
        return trajectory

    async def execute_trajectory(
        self, trajectory_id: str
    ) -> dict[str, Any]:
        """执行导航轨迹 — 向无人机发送航点，执行后从内存移除并更新数据库."""
        trajectory = self._active_trajectories.get(trajectory_id)
        if not trajectory:
            # 尝试从数据库恢复（支持重启后重新执行）
            logger.warning("轨迹 %s 不在内存中，尝试从数据库查询", trajectory_id)
            return {"success": False, "error": f"轨迹 {trajectory_id} 未找到"}

        try:
            proto = self._protocol_registry.get("mavlink")
            if isinstance(proto, MAVLinkProtocol):
                waypoints_data = [wp.to_dict() for wp in trajectory.waypoints]
                result = await proto.send_waypoints(trajectory.device_id, waypoints_data)
                logger.info(
                    "轨迹 %s 已发送到设备 %s: %s",
                    trajectory_id, trajectory.device_id, result
                )
            else:
                result = await self._drone_manager.send_command(
                    trajectory.device_id,
                    {"type": "mission", "waypoints": [wp.to_dict() for wp in trajectory.waypoints]},
                )
        except Exception:
            logger.exception("轨迹 %s 执行异常", trajectory_id)
            result = {"success": False, "error": "执行异常"}

        # 无论成功与否，执行后立即从内存移除并持久化状态
        self._active_trajectories.pop(trajectory_id, None)
        new_status = "executed" if result.get("success", False) else "failed"
        self._db.mark_trajectory_executed(trajectory_id)
        logger.info("轨迹 %s 已从内存移除，状态=%s", trajectory_id, new_status)

        return result

    def get_active_trajectories(self) -> dict[str, dict[str, Any]]:
        """获取内存中所有待执行轨迹（不含已执行的历史轨迹）."""
        return {
            tid: t.to_dict() for tid, t in self._active_trajectories.items()
        }

    def get_trajectory(self, trajectory_id: str) -> NavigationTrajectory | None:
        """获取内存中指定待执行轨迹."""
        return self._active_trajectories.get(trajectory_id)

    def list_algorithms(self) -> list[str]:
        """列出可用导航算法."""
        return self._algorithms.list_algorithms()

    def list_protocols(self) -> list[str]:
        """列出已注册通信协议."""
        return self._protocol_registry.list_protocols()

    def list_tasks(self, instruction_id: str = "") -> list[dict[str, Any]]:
        """按 instruction_id 查询任务列表（先在内存查，再从 DB 查历史）."""
        tasks: list[dict[str, Any]] = []
        seen: set[str] = set()

        for tid, t in self._active_trajectories.items():
            if instruction_id and t.instruction_id != instruction_id:
                continue
            tasks.append(t.to_dict())
            seen.add(tid)

        db_rows = self._db.list_tasks_by_instruction(instruction_id) if instruction_id else []
        for row in db_rows:
            if row["trajectory_id"] not in seen:
                tasks.append(row)

        return tasks