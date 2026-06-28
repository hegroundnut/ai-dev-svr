"""
边缘服务器核心管理器 — 管理类脑盒子与无人机设备
"""
import time
import threading
import logging
from typing import Dict, List, Optional, Any, Callable
from models import (
    BrainBoxNode,
    NavigationTask,
    BrainBoxStatus,
    NavigationStatus,
)
from config.settings import settings
from .heartbeat import HeartbeatMonitor
from .registry import BrainBoxRegistry

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .ws_server import BrainBoxWSManager

logger = logging.getLogger(__name__)


class EdgeManager:
    """
    边缘服务器核心管理器（单例）

    功能:
    - 管理类脑盒子实例 (WS 自动发现 / 更新元数据 / 拉黑 / 心跳监控)
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

        self._registry = BrainBoxRegistry()
        self._tasks: Dict[str, NavigationTask] = {}

        self._ws_manager = ws_manager
        self._request_timeout = settings.request_timeout

        self._heartbeat = HeartbeatMonitor(
            self._registry,
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

    def update_brain_box_meta(
        self,
        box_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """更新类脑盒子元数据（盒子通过 WS 自动发现，不存在则创建）"""
        return self._registry.update_box_meta(box_id, metadata)

    def blacklist_brain_box(self, box_id: str) -> Dict[str, Any]:
        """拉黑类脑盒子并断开其 WebSocket 连接"""
        if self._ws_manager and self._ws_manager.is_connected(box_id):
            self._ws_manager.disconnect(box_id)
        return self._registry.blacklist_box(box_id)

    def list_brain_boxes(self) -> Dict[str, Any]:
        """获取所有类脑盒子列表"""
        return self._registry.list_boxes()

    # ==================================================================
    #  心跳接收（brain_box → edge_server）
    # ==================================================================

    def receive_heartbeat(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """接收类脑盒子心跳（通过 WS event 自动上报，HTTP 接口作为备用）。"""
        box_id = params.get("box_id", "")
        return self._registry.update_heartbeat(
            box_id,
            drone_count=params.get("drone_count", 0),
            online_count=params.get("online_count", 0),
        )

    # ==================================================================
    #  无人机状态上报（brain_box → edge_server）
    # ==================================================================

    def receive_drone_report(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        接收无人机状态上报

        brain_box 定期/即时上报其管辖的无人机信息。
        """
        box_id = params.get("box_id", "")
        if self._registry.is_blacklisted(box_id):
            return {"code": -1, "msg": f"类脑盒子 {box_id} 已被拉黑", "data": {}}
        if not self._registry.box_exists(box_id):
            return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}

        devices_data = params.get("devices", [])
        event = params.get("event", "")

        if event == "status_change":
            device_data = params.get("device", {})
            if device_data:
                self._registry.upsert_device(box_id, device_data)
            return {"code": 0, "msg": "status_change received", "data": {}}

        for dev in devices_data:
            self._registry.upsert_device(box_id, dev)

        return {
            "code": 0,
            "msg": "success",
            "data": {"updated_count": len(devices_data)},
        }

    # ==================================================================
    #  轨迹上报（brain_box → edge_server）
    # ==================================================================

    def receive_trajectory_report(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """接收导航轨迹上报"""
        box_id = params.get("box_id", "")
        trajectory = params.get("trajectory", {})

        with self._lock_internal:
            if self._registry.is_blacklisted(box_id):
                return {"code": -1, "msg": f"类脑盒子 {box_id} 已被拉黑", "data": {}}
            if not self._registry.box_exists(box_id):
                return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}

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
        return self._registry.list_devices(box_id=box_id)

    def get_device_info(self, box_id: str, device_id: str) -> Dict[str, Any]:
        """获取设备详情"""
        return self._registry.get_device(box_id, device_id)

    # ==================================================================
    #  指令发送（WebSocket）
    # ==================================================================

    def _send_command_to_box(self, box_id: str, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """通过 WebSocket 向 brainBox 发送指令并等待响应。"""
        if self._registry.is_blacklisted(box_id):
            return {"code": -1, "msg": f"类脑盒子 {box_id} 已被拉黑", "data": {}}
        box = self._registry.get_box(box_id)
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
            if box_id and self._registry.is_blacklisted(box_id):
                logger.warning("Rejected heartbeat from blacklisted BrainBox: %s", box_id)
                return
            if box_id and not self._registry.box_exists(box_id):
                self._registry.update_box_meta(box_id, {})
                self._registry.set_box_ws_connected(box_id, True)
                logger.info("BrainBox auto-discovered via WS: %s", box_id)
            elif box_id:
                self._registry.set_box_ws_connected(box_id, True)
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

        if self._registry.is_blacklisted(box_id):
            return {"code": -1, "msg": f"类脑盒子 {box_id} 已被拉黑", "data": {}}
        box = self._registry.get_box(box_id)
        if not box:
            return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}
        if box.status == BrainBoxStatus.OFFLINE:
            return {"code": -1, "msg": f"类脑盒子 {box_id} 离线", "data": {}}

        with self._lock_internal:
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

    def list_tasks(self, box_id: str = "all", instruction_id: str = "") -> Dict[str, Any]:
        """查询导航任务列表（instruction_id 非空时转发到 brainBox 筛选）"""
        if instruction_id:
            return self._list_tasks_from_boxes(box_id, instruction_id)

        with self._lock_internal:
            if box_id != "all" and not self._registry.box_exists(box_id):
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

    def _list_tasks_from_boxes(self, box_id: str, instruction_id: str) -> Dict[str, Any]:
        """向 brainBox 转发 list_tasks 请求，由 brainBox 按 instruction_id 筛选."""
        all_tasks: List[Dict[str, Any]] = []
        if box_id != "all":
            boxes_to_query = [box_id]
        else:
            boxes_to_query = [b.box_id for b in self._registry.get_all_boxes()]

        for bid in boxes_to_query:
            if self._registry.is_blacklisted(bid):
                continue
            box = self._registry.get_box(bid)
            if not box or box.status == BrainBoxStatus.OFFLINE:
                continue
            result = self._send_command_to_box(bid, "list_tasks", {"instruction_id": instruction_id})
            if result.get("code", -1) == 0:
                tasks = result.get("data", {}).get("tasks", [])
                for t in tasks:
                    t["box_id"] = bid
                all_tasks.extend(tasks)

        return {
            "code": 0,
            "msg": "success",
            "data": {
                "total": len(all_tasks),
                "tasks": all_tasks,
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
        box = self._registry.mark_box_offline(box_id, reason)
        if box and self._on_box_offline:
            self._on_box_offline(box)

    def get_all_brain_boxes(self) -> List[BrainBoxNode]:
        return self._registry.get_all_boxes()

    def get_brain_box_status(self, box_id: str) -> Dict[str, Any]:
        """通过 WS 查询类脑盒子详细状态"""
        return self._send_command_to_box(box_id, "status", {})

    def shutdown(self) -> None:
        self._heartbeat.stop()
        logger.info("EdgeManager shutdown complete")
