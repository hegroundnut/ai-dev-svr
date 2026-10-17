"""导航服务 — 协调导航指令处理、轨迹生成和设备控制."""

from __future__ import annotations

import logging
import time
from typing import Any

from models.algorithm import NavigationInstruction, NavigationTrajectory, Waypoint
from storage.database import Database

from core.navigation.registry import AlgorithmRegistry
from core.navigation.simplify import DEFAULT_EPSILON_M, simplify_waypoints
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

    存储策略（纯数据库，内存零常驻）：
    - 轨迹生成后先做道格拉斯-普克抽稀 + 坐标精度截断，再写入 SQLite，
      **不在内存中保留任何轨迹**，内存不随轨迹数量增长。
    - 执行/查询均直接读数据库，服务重启后 pending 轨迹仍可继续执行。
    """

    def __init__(
        self,
        algorithm_registry: AlgorithmRegistry,
        drone_manager: DroneManager,
        protocol_registry: ProtocolRegistry,
        database: Database,
        simplify_epsilon: float = DEFAULT_EPSILON_M,
    ) -> None:
        self._algorithms = algorithm_registry
        self._drone_manager = drone_manager
        self._protocol_registry = protocol_registry
        self._db = database
        self._simplify_epsilon = simplify_epsilon

    async def process_instruction(
        self, instruction: NavigationInstruction
    ) -> NavigationTrajectory:
        """处理导航指令：生成轨迹 → 抽稀压缩 → 写入数据库（状态 pending）."""
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

        # 抽稀压缩：减少航点数量并截断坐标精度，防止存储过长
        raw_count = len(trajectory.waypoints)
        simplified, new_distance = simplify_waypoints(
            [w.to_dict() for w in trajectory.waypoints],
            epsilon_m=self._simplify_epsilon,
        )
        trajectory.waypoints = [
            Waypoint(
                latitude=w.get("latitude", 0.0),
                longitude=w.get("longitude", 0.0),
                altitude=w.get("altitude", 0.0),
                speed=w.get("speed", 5.0),
                hold_time=w.get("hold_time", 0.0),
                metadata=w.get("metadata", {}) or {},
            )
            for w in simplified
        ]
        if trajectory.total_distance > 0 and new_distance > 0:
            # 按抽稀前后距离比例缩放预计耗时
            trajectory.estimated_time = round(
                trajectory.estimated_time * new_distance / trajectory.total_distance, 2
            )
        trajectory.total_distance = new_distance
        trajectory.metadata["simplify"] = {
            "epsilon_m": self._simplify_epsilon,
            "raw_waypoint_count": raw_count,
            "simplified_waypoint_count": len(simplified),
        }

        # 写入数据库（pending 状态），不驻留内存
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

        logger.info(
            "导航轨迹已生成并入库: %s (设备=%s, 算法=%s, 航点数=%d→%d)",
            trajectory.trajectory_id,
            instruction.device_id,
            trajectory.algorithm_name,
            raw_count,
            len(trajectory.waypoints),
        )
        return trajectory

    async def execute_trajectory(
        self, trajectory_id: str
    ) -> dict[str, Any]:
        """执行导航轨迹 — 从数据库读取轨迹并发送航点，执行后更新数据库状态.

        轨迹不在内存中常驻，重启后 pending 轨迹仍可执行。
        """
        row = self._db.get_trajectory(trajectory_id)
        if not row:
            return {"success": False, "error": f"轨迹 {trajectory_id} 未找到"}

        status = row.get("status", "")
        if status != "pending":
            return {
                "success": False,
                "error": f"轨迹 {trajectory_id} 状态为 {status}，仅 pending 轨迹可执行",
            }

        waypoints_data = row.get("waypoints", [])
        device_id = row.get("device_id", "")

        try:
            proto = self._protocol_registry.get("mavlink")
            if isinstance(proto, MAVLinkProtocol):
                result = await proto.send_waypoints(device_id, waypoints_data)
                logger.info(
                    "轨迹 %s 已发送到设备 %s: %s",
                    trajectory_id, device_id, result
                )
            else:
                result = await self._drone_manager.send_command(
                    device_id,
                    {"type": "mission", "waypoints": waypoints_data},
                )
        except Exception:
            logger.exception("轨迹 %s 执行异常", trajectory_id)
            result = {"success": False, "error": "执行异常"}

        # 按实际结果更新数据库状态（executed / failed）
        new_status = "executed" if result.get("success", False) else "failed"
        self._db.mark_trajectory_executed(trajectory_id, status=new_status)
        logger.info("轨迹 %s 执行完毕，状态=%s", trajectory_id, new_status)

        return result

    def get_active_trajectories(self) -> dict[str, dict[str, Any]]:
        """获取所有待执行（pending）轨迹 — 直接查询数据库，不占内存."""
        rows = self._db.list_trajectories(status="pending")
        return {row["trajectory_id"]: row for row in rows}

    def get_trajectory(self, trajectory_id: str) -> dict[str, Any] | None:
        """从数据库获取指定轨迹."""
        return self._db.get_trajectory(trajectory_id)

    def list_algorithms(self) -> list[str]:
        """列出可用导航算法."""
        return self._algorithms.list_algorithms()

    def list_protocols(self) -> list[str]:
        """列出已注册通信协议."""
        return self._protocol_registry.list_protocols()

    def list_tasks(self, instruction_id: str = "") -> list[dict[str, Any]]:
        """查询任务列表 — 直接查询数据库.

        instruction_id 非空时按其过滤；为空时返回最近的轨迹任务（上限 100 条）。
        """
        if instruction_id:
            return self._db.list_tasks_by_instruction(instruction_id)
        return self._db.list_trajectories(limit=100)
