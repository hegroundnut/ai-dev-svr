"""导航算法注册中心与内置算法实现."""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from models.algorithm import NavigationAlgorithm, NavigationTrajectory, Waypoint

logger = logging.getLogger("brainBox.core.algorithm_registry")


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
