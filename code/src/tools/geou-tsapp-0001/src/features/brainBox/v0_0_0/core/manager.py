"""类脑盒子核心管理器 — 整合所有子系统."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.settings import get_settings
from models.algorithm import NavigationInstruction
from storage.database import Database

from core.drone_manager import DroneManager
from core.edge_reporter import EdgeReporter
from core.mavlink_comm import MAVLinkProtocol
from core.navigation_service import AlgorithmRegistry, NavigationService, SimpleNavigationAlgorithm
from core.protocol_registry import ProtocolRegistry
from core.ws_client import EdgeWSClient

logger = logging.getLogger("brainBox.core.manager")


class BrainBoxManager:
    """
    类脑盒子核心管理器。

    集成: MAVLink通信 → 无人机管理 → 导航服务 → 边缘上报
    """

    def __init__(self) -> None:
        self._settings = get_settings()

        # 数据库
        self._database = Database(db_path=self._settings.storage.db_path)
        self._database.open()

        self._protocol_registry = ProtocolRegistry()
        self._mavlink = MAVLinkProtocol(self._settings.mavlink)
        self._protocol_registry.register(self._mavlink)

        self._algorithm_registry = AlgorithmRegistry()
        self._algorithm_registry.register(SimpleNavigationAlgorithm())

        self._drone_manager = DroneManager(
            protocol_registry=self._protocol_registry,
            database=self._database,
            scan_interval=self._settings.mavlink.scan_interval,
            evict_timeout=self._settings.storage.device_evict_timeout,
        )

        self._ws_client = EdgeWSClient(
            ws_url=self._settings.edge.ws_url,
            box_id=self._settings.box_id,
            on_request=self._handle_ws_request,
        )
        self._edge_reporter = EdgeReporter(
            ws_client=self._ws_client,
            drone_manager=self._drone_manager,
            heartbeat_interval=self._settings.edge.heartbeat_interval,
            report_interval=self._settings.edge.report_interval,
            box_id=self._settings.box_id,
        )

        self._navigation_service = NavigationService(
            algorithm_registry=self._algorithm_registry,
            drone_manager=self._drone_manager,
            protocol_registry=self._protocol_registry,
            database=self._database,
        )

        self._started = False

    # ── WS request dispatch ──

    async def _handle_ws_request(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch incoming WS request from manageServer to the appropriate method."""
        # (method_name, is_async, needs_payload)
        _dispatch: dict[str, tuple[str, bool, bool]] = {
            "connect_drone": ("connect_drone", True, True),
            "disconnect_drone": ("disconnect_drone", True, True),
            "connections": ("list_connections", False, False),
            "scan": ("scan_drones", True, False),
            "query": ("query_drones", False, True),
            "command": ("send_command", True, True),
            "summary": ("drones_summary", False, False),
            "instruction": ("navigation_instruction", True, True),
            "execute": ("execute_trajectory", True, True),
            "trajectories": ("list_trajectories", False, False),
            "algorithms": ("list_algorithms", False, False),
            "status": ("system_status", False, False),
            "protocols": ("list_protocols", False, False),
        }
        entry = _dispatch.get(action)
        if entry is None:
            return {"code": -1, "msg": f"Unknown action: {action}", "data": {}}

        method_name, is_async, needs_payload = entry
        method = getattr(self, method_name)
        args = (payload,) if needs_payload else ()
        if is_async:
            return await method(*args)
        else:
            return method(*args)

    async def start(self) -> None:
        """启动所有子系统."""
        await self._protocol_registry.connect_all()
        await self._drone_manager.start()
        await self._ws_client.start()
        await self._edge_reporter.start()
        self._started = True
        logger.info("BrainBoxManager 所有服务已启动")

    async def stop(self) -> None:
        """停止所有子系统."""
        await self._edge_reporter.stop()
        await self._ws_client.stop()
        await self._drone_manager.stop()
        await self._protocol_registry.disconnect_all()
        self._database.close()
        self._started = False
        logger.info("BrainBoxManager 所有服务已停止")

    # ── 无人机管理 ──

    async def connect_drone(self, params: dict[str, Any]) -> dict[str, Any]:
        """主动 TCP 连接到指定无人机."""
        ip = params.get("ip", "")
        port = params.get("port", 5760)
        label = params.get("label", "")
        if not ip:
            return {"code": -1, "msg": "缺少必要参数: ip", "data": {}}
        result = await self._mavlink.connect_drone(ip=ip, port=int(port), label=label)
        if result.get("success", False):
            return {"code": 0, "msg": "TCP 连接已建立", "data": result}
        return {"code": -1, "msg": result.get("error", "连接失败"), "data": result}

    async def disconnect_drone(self, params: dict[str, Any]) -> dict[str, Any]:
        """断开指定无人机的 TCP 连接."""
        device_id = params.get("device_id", "")
        if not device_id:
            return {"code": -1, "msg": "缺少必要参数: device_id", "data": {}}
        result = await self._mavlink.disconnect_drone(device_id=device_id)
        if result.get("success", False):
            return {"code": 0, "msg": "TCP 连接已断开", "data": result}
        return {"code": -1, "msg": result.get("error", "断开失败"), "data": result}

    def list_connections(self) -> dict[str, Any]:
        """列出所有 TCP 主动连接通道."""
        connections = self._mavlink.list_tcp_connections()
        return {
            "code": 0, "msg": "success",
            "data": {"total": len(connections), "connections": connections},
        }

    async def scan_drones(self) -> dict[str, Any]:
        """扫描无人机."""
        devices = await self._drone_manager.scan_now()
        return {
            "code": 0, "msg": "success",
            "data": {"total": len(devices), "devices": [d.to_dict() for d in devices]},
        }

    def query_drones(self, params: dict[str, Any]) -> dict[str, Any]:
        """查询无人机信息."""
        device_id = params.get("device_id")
        if device_id:
            device = self._drone_manager.get_device(device_id)
            if device:
                return {"code": 0, "msg": "success", "data": device.to_dict()}
            return {"code": -1, "msg": f"设备 {device_id} 未找到", "data": {}}
        summary = self._drone_manager.get_all_devices_summary()
        return {"code": 0, "msg": "success", "data": summary}

    async def send_command(self, params: dict[str, Any]) -> dict[str, Any]:
        """向无人机发送控制指令."""
        device_id = params["device_id"]
        command = params["command"]
        result = await self._drone_manager.send_command(device_id, command)
        if result.get("success", False):
            return {"code": 0, "msg": "success", "data": result}
        return {"code": -1, "msg": result.get("error", ""), "data": result}

    def drones_summary(self) -> dict[str, Any]:
        """获取无人机汇总信息."""
        summary = self._drone_manager.get_all_devices_summary()
        return {"code": 0, "msg": "success", "data": summary}

    # ── 导航 ──

    async def navigation_instruction(self, params: dict[str, Any]) -> dict[str, Any]:
        """接收导航指令，生成轨迹."""
        instruction = NavigationInstruction(
            instruction_id=params["instruction_id"],
            device_id=params["device_id"],
            target_position=params["target_position"],
            algorithm=params.get("algorithm", "default"),
            parameters=params.get("parameters", {}),
        )
        try:
            trajectory = await self._navigation_service.process_instruction(instruction)
        except ValueError as e:
            return {"code": -1, "msg": str(e), "data": {}}

        trajectory_data = trajectory.to_dict()
        await self._edge_reporter.report_trajectory(trajectory_data)
        return {"code": 0, "msg": "导航轨迹已生成并上报", "data": trajectory_data}

    async def execute_trajectory(self, params: dict[str, Any]) -> dict[str, Any]:
        """执行导航轨迹."""
        trajectory_id = params["trajectory_id"]
        result = await self._navigation_service.execute_trajectory(trajectory_id)
        if result.get("success", False):
            return {"code": 0, "msg": "轨迹执行中", "data": result}
        return {"code": -1, "msg": result.get("error", ""), "data": result}

    def list_trajectories(self) -> dict[str, Any]:
        """列出所有待执行轨迹."""
        trajectories = self._navigation_service.get_active_trajectories()
        return {"code": 0, "msg": "success", "data": trajectories}

    def list_algorithms(self) -> dict[str, Any]:
        """列出可用导航算法."""
        algorithms = self._navigation_service.list_algorithms()
        return {"code": 0, "msg": "success", "data": algorithms}

    # ── 系统 ──

    def system_status(self) -> dict[str, Any]:
        """获取系统状态."""
        summary = self._drone_manager.get_all_devices_summary()
        algorithms = self._navigation_service.list_algorithms()
        protocols = self._navigation_service.list_protocols()
        return {
            "code": 0, "msg": "success",
            "data": {
                "status": "running" if self._started else "stopped",
                "drones": summary,
                "algorithms": algorithms,
                "protocols": protocols,
            },
        }

    def list_protocols(self) -> dict[str, Any]:
        """列出已注册通信协议."""
        protocols = self._navigation_service.list_protocols()
        return {"code": 0, "msg": "success", "data": protocols}