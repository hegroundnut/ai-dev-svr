"""导航算法数据模型与抽象接口."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Waypoint:
    """航点."""

    latitude: float
    longitude: float
    altitude: float
    speed: float = 5.0
    hold_time: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude": self.altitude,
            "speed": self.speed,
            "hold_time": self.hold_time,
            "metadata": self.metadata,
        }


@dataclass
class NavigationTrajectory:
    """导航轨迹."""

    trajectory_id: str
    device_id: str
    waypoints: list[Waypoint]
    algorithm_name: str
    total_distance: float = 0.0
    estimated_time: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trajectory_id": self.trajectory_id,
            "device_id": self.device_id,
            "waypoints": [w.to_dict() for w in self.waypoints],
            "algorithm_name": self.algorithm_name,
            "total_distance": self.total_distance,
            "estimated_time": self.estimated_time,
            "metadata": self.metadata,
        }


@dataclass
class NavigationInstruction:
    """边缘服务下发的导航指令."""

    instruction_id: str
    device_id: str
    target_position: dict[str, float]
    algorithm: str = "default"
    parameters: dict[str, Any] = field(default_factory=dict)


class NavigationAlgorithm(ABC):
    """导航算法抽象基类."""

    @property
    @abstractmethod
    def algorithm_name(self) -> str:
        """算法名称标识."""

    @abstractmethod
    async def generate_trajectory(
        self,
        device_id: str,
        current_position: dict[str, float],
        target_position: dict[str, float],
        parameters: dict[str, Any] | None = None,
    ) -> NavigationTrajectory:
        """根据起点和终点生成导航轨迹."""
