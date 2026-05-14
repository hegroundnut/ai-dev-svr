"""设备数据模型."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DeviceStatus(StrEnum):
    """设备状态枚举."""

    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class DeviceInfo:
    """设备基本信息."""

    device_id: str
    device_type: str
    protocol: str
    status: DeviceStatus = DeviceStatus.UNKNOWN
    ip_address: str = ""
    port: int = 0
    last_heartbeat: float = 0.0
    position: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_alive(self, timeout: float = 10.0) -> bool:
        """检查设备是否在线."""
        if self.last_heartbeat == 0.0:
            return False
        return (time.time() - self.last_heartbeat) < timeout

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "device_type": self.device_type,
            "protocol": self.protocol,
            "status": self.status.value,
            "ip_address": self.ip_address,
            "port": self.port,
            "last_heartbeat": self.last_heartbeat,
            "position": self.position,
            "metadata": self.metadata,
        }
