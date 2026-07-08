"""导航算法注册中心与内置算法实现."""

from __future__ import annotations

import logging
import math
import re
import uuid
from typing import Any

import httpx

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


class WebApiLatLngAlgorithm(NavigationAlgorithm):
    """
    网络API经纬度导航算法.

    通过调用外部路径规划API（Fast Planner）完成轨迹规划。
    用户输入和输出均为经纬度+海拔，算法内部通过 point_mapping 参数
    建立经纬度坐标与API内部 x/y/z 坐标系的映射关系。

    parameters 中需要包含:
    - api_base_url: 外部路径规划API地址（必填）
    - point_mapping: 参考点映射列表，每项为 {lat, lng, alt, x, y, z}
    - map_name: 外部API使用的地图名称
    - 其他规划参数 (max_vel, max_acc, safe_distance, sample_dt 等)
    """

    @property
    def algorithm_name(self) -> str:
        return "web_api_latlng"

    async def generate_trajectory(
        self,
        device_id: str,
        current_position: dict[str, float],
        target_position: dict[str, float],
        parameters: dict[str, Any] | None = None,
    ) -> NavigationTrajectory:
        params = parameters or {}
        point_mapping: list[dict[str, float]] = params.get("point_mapping", [])
        if len(point_mapping) < 2:
            raise ValueError("point_mapping 至少需要2个参考点用于坐标转换")

        api_base_url: str = params.get("api_base_url", "")
        if not api_base_url:
            raise ValueError("parameters 中缺少 api_base_url")
        api_base_url = api_base_url.rstrip("/")
        map_name: str = params.get("map_name", "")
        if not map_name:
            raise ValueError("parameters 中缺少 map_name")

        speed = params.get("speed", 5.0)

        start_xyz = self._latlng_to_xyz(current_position, point_mapping)
        end_xyz = self._latlng_to_xyz(target_position, point_mapping)

        api_result = await self._call_benchmark(api_base_url, map_name, start_xyz, end_xyz, params)

        if api_result.get("status") != "success":
            raise ValueError(
                f"外部路径规划失败: {api_result.get('message', '未知错误')}"
            )

        path: list[list[float]] = api_result.get("path", [])
        if not path:
            raise ValueError("外部路径规划返回空路径")

        waypoints: list[Waypoint] = []
        for pt in path:
            latlng = self._xyz_to_latlng(
                {"x": pt[0], "y": pt[1], "z": pt[2]}, point_mapping
            )
            waypoints.append(
                Waypoint(
                    latitude=latlng["latitude"],
                    longitude=latlng["longitude"],
                    altitude=latlng["altitude"],
                    speed=speed,
                )
            )

        total_distance = api_result.get("path_length_m", 0.0)
        estimated_time = api_result.get("trajectory_duration_s", 0.0)

        return NavigationTrajectory(
            trajectory_id=str(uuid.uuid4()),
            device_id=device_id,
            waypoints=waypoints,
            algorithm_name=self.algorithm_name,
            total_distance=total_distance,
            estimated_time=estimated_time,
            metadata={
                "api_method": "receding_horizon_benchmark",
                "num_replans": api_result.get("num_replans", 0),
                "total_planning_time_ms": api_result.get("total_planning_time_ms", 0),
                "feasible": api_result.get("feasible", False),
                "parameters": params,
            },
        )

    # ------------------------------------------------------------------
    #  坐标转换
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_degree(dms_str: str) -> float:
        """解析度分秒或十进制度数字符串, 支持 N/S/E/W 后缀."""
        dms_str = dms_str.strip().upper()
        sign = 1.0
        if dms_str.endswith("S") or dms_str.endswith("W"):
            sign = -1.0
        dms_str = re.sub(r"[NSEW]$", "", dms_str)
        # 度分秒格式: DD°MM'SS" 或 DD°MM.MMM'
        m = re.match(
            r"^(\d+)[°](?:(\d+)[\'′](?:(\d+(?:\.\d+)?)[\"″])?)?$", dms_str
        )
        if m:
            deg = float(m.group(1))
            minutes = float(m.group(2)) if m.group(2) else 0.0
            seconds = float(m.group(3)) if m.group(3) else 0.0
            return sign * (deg + minutes / 60.0 + seconds / 3600.0)
        return sign * float(dms_str)

    @staticmethod
    def _parse_altitude(alt_str: str) -> float:
        """解析高度字符串, 如 '12m', '12', '12.5m'."""
        alt_str = alt_str.strip()
        m = re.match(r"^([\d.]+)\s*m?$", alt_str, re.IGNORECASE)
        if m:
            return float(m.group(1))
        return float(alt_str)

    @classmethod
    def _compute_conversion_params(
        cls, point_mapping: list[dict[str, float]]
    ) -> dict[str, float]:
        """
        从参考点计算坐标转换参数。

        水平面 (x-y ↔ lat-lng): 2D 仿射变换，支持旋转和非均匀缩放，最小二乘拟合。
        垂直轴 (z ↔ alt): 简单线性映射 (scale + offset)。
        """
        n = len(point_mapping)
        if n < 3:
            raise ValueError("point_mapping 至少需要3个参考点用于2D仿射拟合")

        lats = [p["lat"] for p in point_mapping]
        lngs = [p["lng"] for p in point_mapping]
        alts = [p["alt"] for p in point_mapping]
        xs   = [p["x"]   for p in point_mapping]
        ys   = [p["y"]   for p in point_mapping]
        zs   = [p["z"]   for p in point_mapping]

        # --- 水平面 2D 仿射:  x = a*lat + b*lng + c  ---
        a, b, c = cls._solve_affine_2d(lats, lngs, xs)
        # --- 水平面 2D 仿射:  y = d*lat + e*lng + f  ---
        d, e, f = cls._solve_affine_2d(lats, lngs, ys)

        # --- 垂直轴:  z = g*alt + h  ---
        g, h = cls._solve_linear(alts, zs)

        # 仿射矩阵 [[a,b],[d,e]] 的行列式，用于逆变换
        det = a * e - b * d
        if abs(det) < 1e-12:
            raise ValueError(
                "仿射矩阵奇异 (det≈0)，请检查 point_mapping 参考点是否共线或重复"
            )

        return {
            "a": a, "b": b, "c": c,
            "d": d, "e": e, "f": f,
            "g": g, "h": h,
            "inv_a":  e / det, "inv_b": -b / det,
            "inv_d": -d / det, "inv_e":  a / det,
        }

    @classmethod
    def _latlng_to_xyz(
        cls,
        position: dict[str, float],
        point_mapping: list[dict[str, float]],
    ) -> dict[str, float]:
        """经纬度 + 海拔 → API 内部 x/y/z."""
        c = cls._compute_conversion_params(point_mapping)
        lat = position.get("latitude", 0.0)
        lng = position.get("longitude", 0.0)
        alt = position.get("altitude", 0.0)
        x = c["a"] * lat + c["b"] * lng + c["c"]
        y = c["d"] * lat + c["e"] * lng + c["f"]
        z = c["g"] * alt + c["h"]
        return {"x": round(x, 6), "y": round(y, 6), "z": round(z, 6)}

    @classmethod
    def _xyz_to_latlng(
        cls,
        xyz: dict[str, float],
        point_mapping: list[dict[str, float]],
    ) -> dict[str, float]:
        """API 内部 x/y/z → 经纬度 + 海拔."""
        c = cls._compute_conversion_params(point_mapping)
        x_off = xyz["x"] - c["c"]
        y_off = xyz["y"] - c["f"]
        lat = c["inv_a"] * x_off + c["inv_b"] * y_off
        lng = c["inv_d"] * x_off + c["inv_e"] * y_off
        alt = (xyz["z"] - c["h"]) / c["g"] if abs(c["g"]) > 1e-12 else 0.0
        return {
            "latitude": round(lat, 8),
            "longitude": round(lng, 8),
            "altitude": round(alt, 2),
        }

    # ------------------------------------------------------------------
    #  最小二乘工具 (无 numpy 依赖)
    # ------------------------------------------------------------------

    @classmethod
    def _solve_affine_2d(
        cls, u: list[float], v: list[float], w: list[float],
    ) -> tuple[float, float, float]:
        """
        最小二乘解  w = A*u + B*v + C.

        先中心化 u/v 再求解，避免大坐标值导致的数值精度问题。
        A^T A  [[A],[B],[C]] = A^T w,  其中 A 的第 i 行为 [u_i - mu, v_i - mv, 1].
        """
        n = len(u)
        mu = sum(u) / n
        mv = sum(v) / n

        uc = [ui - mu for ui in u]
        vc = [vi - mv for vi in v]

        suu = sum(ui * ui for ui in uc)
        svv = sum(vi * vi for vi in vc)
        suv = sum(ui * vi for ui, vi in zip(uc, vc))
        suw = sum(ui * wi for ui, wi in zip(uc, w))
        svw = sum(vi * wi for vi, wi in zip(vc, w))
        sw  = sum(w)

        # A^T A = [[suu, suv, 0 ], [suv, svv, 0 ], [0,   0,   n ]]
        # 中心化后 su=0, sv=0, 矩阵变为分块对角，解耦为 2x2 + 1x1
        det2 = suu * svv - suv * suv
        if abs(det2) < 1e-12:
            raise ValueError("参考点不足或共线，无法求解仿射变换")

        A = (svv * suw - suv * svw) / det2
        B = (suu * svw - suv * suw) / det2
        C = sw / n - A * mu - B * mv  # 还原到原始坐标系
        return A, B, C

    @classmethod
    def _solve_linear(
        cls, u: list[float], w: list[float],
    ) -> tuple[float, float]:
        """最小二乘解  w = G*u + H."""
        n = len(u)
        su  = sum(u)
        sw  = sum(w)
        suu = sum(ui * ui for ui in u)
        suw = sum(ui * wi for ui, wi in zip(u, w))
        denom = n * suu - su * su
        if abs(denom) < 1e-12:
            raise ValueError("垂直轴参考点不足")
        G = (n * suw - su * sw) / denom
        H = (suu * sw - su * suw) / denom
        return G, H

    @classmethod
    def _det3(
        cls, a11, a12, a13,
        a21, a22, a23,
        a31, a32, a33,
    ) -> float:
        return (
            a11 * (a22 * a33 - a23 * a32)
          - a12 * (a21 * a33 - a23 * a31)
          + a13 * (a21 * a32 - a22 * a31)
        )

    # ------------------------------------------------------------------
    #  Fast Planner API 调用
    # ------------------------------------------------------------------

    @staticmethod
    async def _call_benchmark(
        api_base_url: str,
        map_name: str,
        start_xyz: dict[str, float],
        end_xyz: dict[str, float],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """调用外部 Fast Planner /api/benchmark 接口（receding-horizon 全局规划）."""
        url = f"{api_base_url}/api/benchmark"

        body: dict[str, Any] = {
            "map_name": map_name,
            "start_pt": [start_xyz["x"], start_xyz["y"], start_xyz["z"]],
            "end_pt": [end_xyz["x"], end_xyz["y"], end_xyz["z"]],
        }

        for key in (
            "max_vel", "max_acc", "safe_distance", "sample_dt",
            "search_horizon", "use_local", "corridor_width",
            "fast_esdf_resolution", "window_radius",
        ):
            if key in params:
                body[key] = params[key]

        if "start_vel" in params:
            body["start_vel"] = params["start_vel"]
        if "end_vel" in params:
            body["end_vel"] = params["end_vel"]
        if "z_range" in params:
            body["z_range"] = params["z_range"]

        logger.info("调用 Fast Planner benchmark: %s, map=%s", url, map_name)

        async with httpx.AsyncClient(timeout=120.0, verify=False) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

        logger.info(
            "Fast Planner benchmark 响应: status=%s, path_points=%d, distance=%.1fm",
            data.get("status"),
            len(data.get("path", [])),
            data.get("path_length_m", 0),
        )
        return data
