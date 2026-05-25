"""
边缘服务器核心管理器 — 管理类脑盒子与无人机设备
"""
import time
import threading
import logging
from typing import Dict, List, Optional, Any, Callable
from models import (
    BrainBoxNode,
    DroneDevice,
    NavigationTask,
    BrainBoxStatus,
    DeviceStatus,
    NavigationStatus,
)
from config.settings import settings
from .heartbeat import HeartbeatMonitor

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .ws_server import BrainBoxWSManager

logger = logging.getLogger(__name__)


class EdgeManager:
    """
    边缘服务器核心管理器（单例）

    功能:
    - 管理类脑盒子实例 (WS 自动注册 / 手动注册 / 移除 / 心跳监控)
    - 管理无人机设备表 (由类脑盒子上报，绑定到对应 brain_box)
    - 通过 WebSocket 转发指令到类脑盒子
    - 接收并存储轨迹上报
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        heartbeat_interval: float = 10.0,
        box_timeout: float = 30.0,
        on_box_offline: Optional[Callable[[BrainBoxNode], None]] = None,
        ws_manager: Optional["BrainBoxWSManager"] = None,
    ):
        if hasattr(self, "_initialized"):
            return

        self._lock_internal = threading.RLock()

        self._brain_boxes: Dict[str, BrainBoxNode] = {}
        self._devices: Dict[str, DroneDevice] = {}
        self._tasks: Dict[str, NavigationTask] = {}

        self._ws_manager = ws_manager
        self._request_timeout = settings.request_timeout

        self._heartbeat = HeartbeatMonitor(
            self,
            check_interval_s=heartbeat_interval,
            box_timeout_s=box_timeout,
        )
        self._heartbeat.start()

        self._on_box_offline = on_box_offline

        self._initialized = True
        logger.info("EdgeManager initialized")

    # ==================================================================
    #  类脑盒子管理
    # ==================================================================

    def add_brain_box(
        self,
        box_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """注册类脑盒子（WS 连接后通过 add_brain_box 手动注册或自动注册）"""
        with self._lock_internal:
            if box_id in self._brain_boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 已存在", "data": {}}

            box = BrainBoxNode(
                box_id=box_id,
                metadata=metadata or {},
            )
            self._brain_boxes[box_id] = box
            logger.info("BrainBox added: %s", box_id)

            return {"code": 0, "msg": "success", "data": box.to_dict()}

    def remove_brain_box(self, box_id: str) -> Dict[str, Any]:
        """移除类脑盒子并清理其关联的设备"""
        with self._lock_internal:
            if box_id not in self._brain_boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}

            box = self._brain_boxes.pop(box_id)

            removed_devices = [
                did for did, d in self._devices.items() if d.box_id == box_id
            ]
            for did in removed_devices:
                self._devices.pop(did)

            logger.info(
                "BrainBox removed: %s (cleaned %d devices)",
                box_id,
                len(removed_devices),
            )
            return {"code": 0, "msg": "success", "data": box.to_dict()}

    def list_brain_boxes(self) -> Dict[str, Any]:
        """获取所有类脑盒子列表"""
        with self._lock_internal:
            boxes = list(self._brain_boxes.values())
            return {
                "code": 0,
                "msg": "success",
                "data": {
                    "total": len(boxes),
                    "brain_boxes": [b.to_dict() for b in boxes],
                },
            }

    # ==================================================================
    #  心跳接收（brain_box → edge_server）
    # ==================================================================

    def receive_heartbeat(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """接收类脑盒子心跳（通过 WS event 自动上报，HTTP 接口作为备用）。"""
        box_id = params.get("box_id", "")
        with self._lock_internal:
            if box_id in self._brain_boxes:
                box = self._brain_boxes[box_id]
                box.last_heartbeat = time.time()
                box.drone_count = params.get("drone_count", box.drone_count)
                box.online_drone_count = params.get("online_count", box.online_drone_count)
                if box.status == BrainBoxStatus.OFFLINE:
                    box.status = BrainBoxStatus.ONLINE
                    logger.info("BrainBox %s back online", box_id)
                return {"code": 0, "msg": "success", "data": box.to_dict()}

            return {
                "code": -1,
                "msg": f"类脑盒子 {box_id} 未注册，请先通过 WS 连接或手动注册",
                "data": {},
            }

    # ==================================================================
    #  无人机状态上报（brain_box → edge_server）
    # ==================================================================

    def receive_drone_report(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        接收无人机状态上报

        brain_box 定期/即时上报其管辖的无人机信息。
        """
        box_id = params.get("box_id", "")
        with self._lock_internal:
            if box_id not in self._brain_boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 未注册", "data": {}}

            devices_data = params.get("devices", [])
            event = params.get("event", "")

            if event == "status_change":
                device_data = params.get("device", {})
                if device_data:
                    self._upsert_device(box_id, device_data)
                return {"code": 0, "msg": "status_change received", "data": {}}

            for dev in devices_data:
                self._upsert_device(box_id, dev)

            return {
                "code": 0,
                "msg": "success",
                "data": {"updated_count": len(devices_data)},
            }

    def _upsert_device(self, box_id: str, device_data: Dict[str, Any]) -> None:
        """插入或更新设备记录"""
        device_id = device_data.get("device_id", "")
        if not device_id:
            return

        if device_id in self._devices:
            dev = self._devices[device_id]
            dev.status = DeviceStatus(device_data.get("status", dev.status.value))
            dev.last_heartbeat = device_data.get("last_heartbeat", time.time())
            dev.position = device_data.get("position", dev.position)
            dev.metadata = device_data.get("metadata", dev.metadata)
        else:
            dev = DroneDevice(
                device_id=device_id,
                box_id=box_id,
                device_type=device_data.get("device_type", "quadcopter"),
                protocol=device_data.get("protocol", "mavlink"),
                status=DeviceStatus(device_data.get("status", "online")),
                last_heartbeat=device_data.get("last_heartbeat", time.time()),
                position=device_data.get("position", {}),
                metadata=device_data.get("metadata", {}),
            )
            self._devices[device_id] = dev
            logger.info("Drone registered: %s (box=%s)", device_id, box_id)

    # ==================================================================
    #  轨迹上报（brain_box → edge_server）
    # ==================================================================

    def receive_trajectory_report(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """接收导航轨迹上报"""
        box_id = params.get("box_id", "")
        trajectory = params.get("trajectory", {})

        with self._lock_internal:
            if box_id not in self._brain_boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 未注册", "data": {}}

            trajectory_id = trajectory.get("trajectory_id", "")
            device_id = trajectory.get("device_id", "")

            matched = False
            for task in self._tasks.values():
                if task.trajectory_id == trajectory_id and task.status == NavigationStatus.EXECUTING:
                    task.result = trajectory
                    matched = True
                    break

            if not matched:
                for task in self._tasks.values():
                    if (
                        task.box_id == box_id
                        and task.device_id == device_id
                        and task.status in (NavigationStatus.PENDING, NavigationStatus.EXECUTING)
                    ):
                        task.trajectory_id = trajectory_id
                        task.status = NavigationStatus.EXECUTING
                        task.result = trajectory
                        matched = True
                        break

            logger.info(
                "Trajectory report received: box=%s trajectory=%s device=%s matched=%s",
                box_id, trajectory_id, device_id, matched,
            )
            return {"code": 0, "msg": "success", "data": {"trajectory_id": trajectory_id}}

    # ==================================================================
    #  设备查询
    # ==================================================================

    def list_devices(self, box_id: str = "all") -> Dict[str, Any]:
        """获取无人机设备列表"""
        with self._lock_internal:
            if box_id != "all" and box_id not in self._brain_boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}
            devices = list(self._devices.values())
            if box_id != "all":
                devices = [d for d in devices if d.box_id == box_id]

            return {
                "code": 0,
                "msg": "success",
                "data": {
                    "total": len(devices),
                    "devices": [d.to_dict() for d in devices],
                },
            }

    def get_device_info(self, box_id: str, device_id: str) -> Dict[str, Any]:
        """获取设备详情"""
        with self._lock_internal:
            if box_id not in self._brain_boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}
            dev = self._devices.get(device_id)
            if not dev or dev.box_id != box_id:
                return {"code": -1, "msg": f"设备 {device_id} 不存在", "data": {}}
            return {"code": 0, "msg": "success", "data": dev.to_dict()}

    # ==================================================================
    #  指令发送（WebSocket）
    # ==================================================================

    def _send_command_to_box(self, box_id: str, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """通过 WebSocket 向 brainBox 发送指令并等待响应。"""
        with self._lock_internal:
            box = self._brain_boxes.get(box_id)
            if not box:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}
            if box.status == BrainBoxStatus.OFFLINE:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 离线", "data": {}}

        if not self._ws_manager or not self._ws_manager.is_connected(box_id):
            return {"code": -1, "msg": f"类脑盒子 {box_id} WebSocket 未连接", "data": {}}

        try:
            return self._ws_manager.send_request(
                box_id, action, payload, timeout=self._request_timeout
            )
        except TimeoutError:
            return {"code": -1, "msg": f"请求 {box_id} 超时", "data": {}}
        except ConnectionError:
            self.mark_brain_box_offline(box_id, reason="ws_disconnected")
            return {"code": -1, "msg": f"类脑盒子 {box_id} WebSocket 连接断开", "data": {}}

    def on_ws_event(self, action: str, payload: Dict[str, Any]) -> None:
        """Handle incoming WS event from a brainBox (called from WS event loop thread)."""
        if action == "heartbeat":
            box_id = payload.get("box_id", "")
            with self._lock_internal:
                if box_id and box_id not in self._brain_boxes:
                    box = BrainBoxNode(
                        box_id=box_id,
                        ws_connected=True,
                    )
                    self._brain_boxes[box_id] = box
                    logger.info("BrainBox auto-registered via WS: %s", box_id)
                elif box_id in self._brain_boxes:
                    self._brain_boxes[box_id].ws_connected = True
            self.receive_heartbeat(payload)
        elif action == "drone_report":
            self.receive_drone_report(payload)
        elif action == "trajectory_report":
            self.receive_trajectory_report(payload)

    # ==================================================================
    #  转发指令（edge_server → brain_box）
    # ==================================================================

    def forward_connect_drone(
        self, box_id: str, ip: str, port: int = 5760, label: str = ""
    ) -> Dict[str, Any]:
        """通过 WS 转发 TCP 连接无人机指令到指定类脑盒子"""
        return self._send_command_to_box(box_id, "connect_drone", {
            "ip": ip, "port": port, "label": label,
        })

    def forward_disconnect_drone(self, box_id: str, device_id: str) -> Dict[str, Any]:
        """通过 WS 转发断开无人机指令到指定类脑盒子"""
        return self._send_command_to_box(box_id, "disconnect_drone", {
            "device_id": device_id,
        })

    def forward_list_connections(self, box_id: str) -> Dict[str, Any]:
        """通过 WS 转发查询 TCP 连接列表指令到指定类脑盒子"""
        return self._send_command_to_box(box_id, "connections", {})

    def forward_scan_drones(self, box_id: str) -> Dict[str, Any]:
        """通过 WS 转发扫描指令到指定类脑盒子"""
        return self._send_command_to_box(box_id, "scan", {})

    def forward_query_drones(self, box_id: str, query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """通过 WS 转发查询指令到指定类脑盒子"""
        return self._send_command_to_box(box_id, "query", query or {})

    def forward_command(self, box_id: str, device_id: str, command: Dict[str, Any]) -> Dict[str, Any]:
        """通过 WS 转发控制指令到指定类脑盒子"""
        return self._send_command_to_box(box_id, "command", {
            "device_id": device_id, "command": command,
        })

    # ==================================================================
    #  导航任务
    # ==================================================================

    def send_navigation_instruction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        下发导航指令（通过 WS 异步转发）

        创建本地任务记录后，在后台线程中将导航指令转发给 brain_box，
        立即返回任务信息（status=PENDING）。
        """
        box_id = params.get("box_id", "")
        device_id = params.get("device_id", "")
        instruction_id = params.get("instruction_id", "")
        target_position = params.get("target_position", {})
        algorithm = params.get("algorithm", "simple_linear")
        parameters = params.get("parameters", {})

        with self._lock_internal:
            box = self._brain_boxes.get(box_id)
            if not box:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}
            if box.status == BrainBoxStatus.OFFLINE:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 离线", "data": {}}

            task = NavigationTask(
                task_id=NavigationTask.generate_task_id(),
                instruction_id=instruction_id,
                box_id=box_id,
                device_id=device_id,
                target_position=target_position,
                algorithm=algorithm,
                parameters=parameters,
                status=NavigationStatus.PENDING,
            )
            self._tasks[task.task_id] = task

        def _forward():
            payload = {
                "instruction_id": instruction_id,
                "device_id": device_id,
                "target_position": target_position,
                "algorithm": algorithm,
                "parameters": parameters,
            }
            result = self._send_command_to_box(box_id, "instruction", payload)
            with self._lock_internal:
                if result.get("code", -1) == 0:
                    data = result.get("data", {})
                    task.trajectory_id = data.get("trajectory_id")
                    task.status = NavigationStatus.EXECUTING
                    task.result = data
                else:
                    task.status = NavigationStatus.FAILED
                    task.result = result
            logger.info(
                "Navigation instruction forwarded: task=%s box=%s device=%s code=%s",
                task.task_id, box_id, device_id, result.get("code", -1),
            )

        thread = threading.Thread(target=_forward, daemon=True)
        thread.start()

        logger.info(
            "Navigation instruction dispatched: task=%s box=%s device=%s",
            task.task_id, box_id, device_id,
        )
        return {"code": 0, "msg": "success", "data": task.to_dict()}

    def execute_trajectory(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """通过 WS 转发轨迹执行指令，成功后更新任务状态"""
        box_id = params.get("box_id", "")
        trajectory_id = params.get("trajectory_id", "")
        result = self._send_command_to_box(box_id, "execute", {
            "trajectory_id": trajectory_id,
        })
        if result.get("code", -1) == 0:
            with self._lock_internal:
                for task in self._tasks.values():
                    if task.trajectory_id == trajectory_id and task.status == NavigationStatus.EXECUTING:
                        task.status = NavigationStatus.COMPLETED
                        task.completed_at = time.time()
                        logger.info("Task %s completed via execute_trajectory", task.task_id)
                        break
        return result

    def list_tasks(self, box_id: str = "all") -> Dict[str, Any]:
        """查询导航任务列表"""
        with self._lock_internal:
            if box_id != "all" and box_id not in self._brain_boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}
            tasks = list(self._tasks.values())
            if box_id != "all":
                tasks = [t for t in tasks if t.box_id == box_id]
            return {
                "code": 0,
                "msg": "success",
                "data": {
                    "total": len(tasks),
                    "tasks": [t.to_dict() for t in tasks],
                },
            }

    def get_task_info(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务详情"""
        with self._lock_internal:
            task = self._tasks.get(task_id)
            if not task:
                return None
            return task.to_dict()

    # ==================================================================
    #  状态管理
    # ==================================================================

    def mark_brain_box_offline(self, box_id: str, reason: str = "unknown") -> None:
        with self._lock_internal:
            box = self._brain_boxes.get(box_id)
            if box:
                box.status = BrainBoxStatus.OFFLINE
                box.ws_connected = False
                logger.warning("BrainBox marked offline: %s (reason=%s)", box_id, reason)

                for dev in self._devices.values():
                    if dev.box_id == box_id and dev.status != DeviceStatus.OFFLINE:
                        dev.status = DeviceStatus.OFFLINE

                if self._on_box_offline:
                    self._on_box_offline(box)

    def get_all_brain_boxes(self) -> List[BrainBoxNode]:
        with self._lock_internal:
            return list(self._brain_boxes.values())

    def get_brain_box_status(self, box_id: str) -> Dict[str, Any]:
        """通过 WS 查询类脑盒子详细状态"""
        return self._send_command_to_box(box_id, "status", {})

    def shutdown(self) -> None:
        self._heartbeat.stop()
        logger.info("EdgeManager shutdown complete")
