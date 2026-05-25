"""
数据注册中心 — 管理类脑盒子与无人机设备的内存数据层.

线程安全，所有公开方法均受 RLock 保护。
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from models import BrainBoxNode, BrainBoxStatus, DeviceStatus, DroneDevice

logger = logging.getLogger(__name__)


class BrainBoxRegistry:
    """管理 brain box 节点和关联无人机设备的注册信息."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._boxes: Dict[str, BrainBoxNode] = {}
        self._devices: Dict[str, DroneDevice] = {}

    # ── Box CRUD ─────────────────────────────────────────────

    def add_box(
        self, box_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """注册类脑盒子."""
        with self._lock:
            if box_id in self._boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 已存在", "data": {}}
            box = BrainBoxNode(box_id=box_id, metadata=metadata or {})
            self._boxes[box_id] = box
            logger.info("BrainBox added: %s", box_id)
            return {"code": 0, "msg": "success", "data": box.to_dict()}

    def remove_box(self, box_id: str) -> Dict[str, Any]:
        """移除类脑盒子并清理其关联设备."""
        with self._lock:
            if box_id not in self._boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}
            box = self._boxes.pop(box_id)
            removed_devices = [
                did for did, d in self._devices.items() if d.box_id == box_id
            ]
            for did in removed_devices:
                self._devices.pop(did)
            logger.info(
                "BrainBox removed: %s (cleaned %d devices)",
                box_id, len(removed_devices),
            )
            return {"code": 0, "msg": "success", "data": box.to_dict()}

    def get_box(self, box_id: str) -> Optional[BrainBoxNode]:
        """获取单个类脑盒子."""
        with self._lock:
            return self._boxes.get(box_id)

    def get_all_boxes(self) -> List[BrainBoxNode]:
        """获取所有类脑盒子列表（引用，非拷贝）."""
        with self._lock:
            return list(self._boxes.values())

    def list_boxes(self) -> Dict[str, Any]:
        """获取所有类脑盒子摘要."""
        with self._lock:
            boxes = list(self._boxes.values())
            return {
                "code": 0, "msg": "success",
                "data": {
                    "total": len(boxes),
                    "brain_boxes": [b.to_dict() for b in boxes],
                },
            }

    def box_exists(self, box_id: str) -> bool:
        """检查类脑盒子是否已注册."""
        with self._lock:
            return box_id in self._boxes

    def set_box_ws_connected(self, box_id: str, connected: bool) -> None:
        """设置类脑盒子 WS 连接状态."""
        with self._lock:
            box = self._boxes.get(box_id)
            if box:
                box.ws_connected = connected

    # ── Heartbeat ────────────────────────────────────────────

    def update_heartbeat(
        self, box_id: str, drone_count: int = 0, online_count: int = 0
    ) -> Dict[str, Any]:
        """更新类脑盒子心跳."""
        with self._lock:
            box = self._boxes.get(box_id)
            if not box:
                return {
                    "code": -1,
                    "msg": f"类脑盒子 {box_id} 未注册，请先通过 WS 连接或手动注册",
                    "data": {},
                }
            box.last_heartbeat = time.time()
            box.drone_count = drone_count
            box.online_drone_count = online_count
            if box.status == BrainBoxStatus.OFFLINE:
                box.status = BrainBoxStatus.ONLINE
                logger.info("BrainBox %s back online", box_id)
            return {"code": 0, "msg": "success", "data": box.to_dict()}

    # ── Device ───────────────────────────────────────────────

    def upsert_device(self, box_id: str, device_data: Dict[str, Any]) -> None:
        """插入或更新设备记录."""
        device_id = device_data.get("device_id", "")
        if not device_id:
            return
        with self._lock:
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

    def list_devices(self, box_id: str = "all") -> Dict[str, Any]:
        """获取无人机设备列表."""
        with self._lock:
            if box_id != "all" and box_id not in self._boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}
            devices = list(self._devices.values())
            if box_id != "all":
                devices = [d for d in devices if d.box_id == box_id]
            return {
                "code": 0, "msg": "success",
                "data": {
                    "total": len(devices),
                    "devices": [d.to_dict() for d in devices],
                },
            }

    def get_device(self, box_id: str, device_id: str) -> Dict[str, Any]:
        """获取设备详情."""
        with self._lock:
            if box_id not in self._boxes:
                return {"code": -1, "msg": f"类脑盒子 {box_id} 不存在", "data": {}}
            dev = self._devices.get(device_id)
            if not dev or dev.box_id != box_id:
                return {"code": -1, "msg": f"设备 {device_id} 不存在", "data": {}}
            return {"code": 0, "msg": "success", "data": dev.to_dict()}

    # ── Status ───────────────────────────────────────────────

    def mark_box_offline(self, box_id: str, reason: str = "unknown") -> Optional[BrainBoxNode]:
        """标记类脑盒子离线，同时将其管辖的所有设备设为离线."""
        with self._lock:
            box = self._boxes.get(box_id)
            if not box:
                return None
            box.status = BrainBoxStatus.OFFLINE
            box.ws_connected = False
            logger.warning("BrainBox marked offline: %s (reason=%s)", box_id, reason)
            for dev in self._devices.values():
                if dev.box_id == box_id and dev.status != DeviceStatus.OFFLINE:
                    dev.status = DeviceStatus.OFFLINE
            return box
