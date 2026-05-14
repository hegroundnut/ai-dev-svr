"""导航服务 — 协调导航指令处理、轨迹生成和设备控制."""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any

from models.algorithm import (
    NavigationAlgorithm,
    NavigationInstruction,
    NavigationTrajectory,
    Waypoint,
)
from storage.database import Database

from core.drone_manager import DroneManager
from core.mavlink_comm import MAVLinkProtocol
from core.protocol_registry import ProtocolRegistry

logger = logging.getLogger("brainBox.core.navigation_service")


class AlgorithmRegistry:
    """
    导航算法注册中心.

    使用注册模式管理所有导航算法，支持运行时动态注册新算法。
    """

    def __init__(self) -> None:
        self._algorithms: dict[str, NavigationAlgorithm] = {}

    def register(self, algorithm: NavigationAlgorithm) -> None:
        """注册导航算法."""
        name = algorithm.algorithm_name
        if name in self._algorithms:
            logger.warning("算法 '%s' 已存在，将被覆盖", name)
        self._algorithms[name] = algorithm
        logger.info("已注册导航算法: %s", name)

    def unregister(self, name: str) -> None:
        """注销导航算法."""
        if name in self._algorithms:
            del self._algorithms[name]
            logger.info("已注销导航算法: %s", name)

    def get(self, name: str) -> NavigationAlgorithm | None:
        """获取指定算法."""
        return self._algorithms.get(name)

    def get_default(self) -> NavigationAlgorithm | None:
        """获取默认算法（第一个注册的）."""
        if self._algorithms:
            return next(iter(self._algorithms.values()))
        return None

    def list_algorithms(self) -> list[str]:
        """列出所有已注册算法."""
        return list(self._algorithms.keys())


class SimpleNavigationAlgorithm(NavigationAlgorithm):
    """
    简单直线导航算法.

    在起点和终点之间按步长生成等距航点，用于演示和基本任务。
    可作为模板实现更复杂算法 (A*, RRT, Dubins 等)。
    """

    @property
    def algorithm_name(self) -> str:
        return "simple_linear"

    async def generate_trajectory(
        self,
        device_id: str,
        current_position: dict[str, float],
        target_position: dict[str, float],
        parameters: dict[str, Any] | None = None,
    ) -> NavigationTrajectory:
        params = parameters or {}
        step_count = params.get("step_count", 10)
        speed = params.get("speed", 5.0)
        altitude = params.get("altitude", target_position.get("altitude", 50.0))

        start_lat = current_position.get("latitude", 0)
        start_lon = current_position.get("longitude", 0)
        end_lat = target_position.get("latitude", 0)
        end_lon = target_position.get("longitude", 0)

        waypoints: list[Waypoint] = []
        for i in range(step_count + 1):
            ratio = i / step_count
            lat = start_lat + (end_lat - start_lat) * ratio
            lon = start_lon + (end_lon - start_lon) * ratio
            waypoints.append(
                Waypoint(latitude=lat, longitude=lon, altitude=altitude, speed=speed)
            )

        total_distance = self._haversine(start_lat, start_lon, end_lat, end_lon)
        estimated_time = total_distance / speed if speed > 0 else 0

        return NavigationTrajectory(
            trajectory_id=str(uuid.uuid4()),
            device_id=device_id,
            waypoints=waypoints,
            algorithm_name=self.algorithm_name,
            total_distance=total_distance,
            estimated_time=estimated_time,
            metadata={"step_count": step_count, "parameters": params},
        )

    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Haversine 公式计算两点距离 (米)."""
        r = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = (
            math.sin(dphi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        )
        return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


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