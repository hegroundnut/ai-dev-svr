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
        """处理导航指令：生成轨迹 → 抽稀压缩 → 写入数据库（状态 pending）.

        若 ``parameters.target_positions`` 传入目标点数组，则对后端算法执行
        ``len(target_positions)`` 次推理：每次以上一段的终点（即上一个目标点）
        为起点。所有分段轨迹拼接为一条长轨迹后统一抽稀入库、上报。
        未传 ``target_positions`` 时保持原有单目标点行为。
        """
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

        targets = self._resolve_target_positions(instruction)

        waypoints: list[Waypoint] = []
        total_distance = 0.0
        estimated_time = 0.0
        segments: list[dict[str, Any]] = []
        start_position = current_position
        trajectory: NavigationTrajectory | None = None

        for index, target in enumerate(targets):
            trajectory = await algo.generate_trajectory(
                device_id=instruction.device_id,
                current_position=start_position,
                target_position=target,
                parameters=instruction.parameters,
            )

            segment_waypoints = trajectory.waypoints
            # 分段衔接点去重：后续分段的第一个航点即上一段的终点
            if (
                waypoints
                and segment_waypoints
                and self._same_position(waypoints[-1], segment_waypoints[0])
            ):
                segment_waypoints = segment_waypoints[1:]
            waypoints.extend(segment_waypoints)

            total_distance += trajectory.total_distance
            estimated_time += trajectory.estimated_time
            segments.append(
                {
                    "index": index,
                    "target_position": dict(target),
                    "trajectory_id": trajectory.trajectory_id,
                    "algorithm_name": trajectory.algorithm_name,
                    "waypoint_count": len(trajectory.waypoints),
                    "total_distance": trajectory.total_distance,
                    "estimated_time": trajectory.estimated_time,
                }
            )

            # 下一段以上一段的终点（本段目标点）为起点
            start_position = target

        if trajectory is None:
            raise ValueError("未生成任何轨迹段")

        # 将所有分段整合为一条长轨迹
        trajectory.waypoints = waypoints
        trajectory.total_distance = total_distance
        trajectory.estimated_time = estimated_time
        if len(targets) > 1:
            trajectory.metadata["multi_target"] = {
                "target_count": len(targets),
                "segments": segments,
            }

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

    @staticmethod
    def _resolve_target_positions(
        instruction: NavigationInstruction,
    ) -> list[dict[str, float]]:
        """解析目标点列表.

        ``parameters.target_positions`` 存在时以其为准执行多段推理；
        否则回退到单个 ``target_position``。
        """
        raw = instruction.parameters.get("target_positions")
        if not raw:
            # 未提供多目标点（缺失 / null / 空数组）时回退到单目标点
            return [instruction.target_position]
        if not isinstance(raw, list):
            raise ValueError("parameters.target_positions 必须为目标点数组")

        targets: list[dict[str, float]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(
                    f"parameters.target_positions[{index}] 必须为目标点对象"
                )
            try:
                targets.append(
                    {
                        "latitude": float(item["latitude"]),
                        "longitude": float(item["longitude"]),
                        "altitude": float(item.get("altitude", 0.0)),
                    }
                )
            except (KeyError, TypeError, ValueError):
                raise ValueError(
                    f"parameters.target_positions[{index}] 需包含数值型 "
                    "latitude/longitude/altitude"
                ) from None
        return targets

    @staticmethod
    def _same_position(a: Waypoint, b: Waypoint) -> bool:
        """判断两个航点是否重合（用于去除分段衔接处的重复点）."""
        return (
            abs(a.latitude - b.latitude) <= 1e-6
            and abs(a.longitude - b.longitude) <= 1e-6
            and abs(a.altitude - b.altitude) <= 0.1
        )

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

    async def execute_waypoints(
        self,
        device_id: str,
        waypoints: list[dict[str, Any]],
        auto_arm: bool = True,
        auto_start: bool = True,
    ) -> dict[str, Any]:
        """直接下发完整航点列表，并可选自动解锁、启动任务.

        用于 manageServer 已解析好的整条长轨迹（多目标点分段拼接结果）：
        一次性上传全部航点，使无人机按顺序飞完整条路径。
        """
        if not device_id:
            return {"success": False, "error": "缺少 device_id"}
        if not isinstance(waypoints, list) or not waypoints:
            return {"success": False, "error": "航点列表不能为空"}

        try:
            proto = self._protocol_registry.get("mavlink")
            if isinstance(proto, MAVLinkProtocol):
                upload = await proto.send_waypoints(device_id, waypoints)
            else:
                upload = await self._drone_manager.send_command(
                    device_id, {"type": "mission", "waypoints": waypoints}
                )
        except Exception:
            logger.exception("航点任务下发异常: device=%s", device_id)
            return {"success": False, "error": "航点任务下发异常"}

        if not upload.get("success", False):
            return upload

        steps: dict[str, Any] = {"upload": upload}

        if auto_arm:
            arm_result = await self._send_control(device_id, {"type": "arm"})
            steps["arm"] = arm_result
            if not arm_result.get("success", False):
                return {
                    "success": False,
                    "error": f"自动解锁失败: {arm_result.get('error', '')}",
                    "steps": steps,
                }

        if auto_start:
            start_result = await self._start_mission(device_id)
            steps["start"] = start_result
            if not start_result.get("success", False):
                return {
                    "success": False,
                    "error": f"任务启动失败: {start_result.get('error', '')}",
                    "steps": steps,
                }

        logger.info(
            "航点任务已下发: device=%s waypoints=%d auto_arm=%s auto_start=%s",
            device_id, len(waypoints), auto_arm, auto_start,
        )
        return {
            "success": True,
            "device_id": device_id,
            "waypoint_count": len(waypoints),
            "auto_arm": auto_arm,
            "auto_start": auto_start,
            "steps": steps,
        }

    async def _send_control(
        self, device_id: str, command: dict[str, Any]
    ) -> dict[str, Any]:
        """向设备发送控制指令（解锁等）."""
        try:
            proto = self._protocol_registry.get("mavlink")
            if isinstance(proto, MAVLinkProtocol):
                return await proto.send_command(device_id, command)
            return await self._drone_manager.send_command(device_id, command)
        except Exception:
            logger.exception("控制指令发送异常: device=%s", device_id)
            return {"success": False, "error": "控制指令发送异常"}

    async def _start_mission(self, device_id: str) -> dict[str, Any]:
        """启动航点任务（切换任务模式并发送 MISSION_START）."""
        try:
            proto = self._protocol_registry.get("mavlink")
            if isinstance(proto, MAVLinkProtocol):
                return await proto.start_mission(device_id)
            return await self._drone_manager.send_command(
                device_id, {"type": "mission_start"}
            )
        except Exception:
            logger.exception("任务启动异常: device=%s", device_id)
            return {"success": False, "error": "任务启动异常"}

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
