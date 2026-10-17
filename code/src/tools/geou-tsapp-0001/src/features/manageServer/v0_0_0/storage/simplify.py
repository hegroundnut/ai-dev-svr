"""轨迹抽稀工具 — 道格拉斯-普克 (Douglas-Peucker) 抽稀 + 坐标精度截断.

轨迹在入库前统一经过压缩，保留形状的同时减少航点数量，
防止长航线（如外部规划 API 返回的高密度路径）导致存储无限膨胀。

压缩策略：
1. **抽稀**：3D 道格拉斯-普克算法（经纬度投影为局部平面米坐标 + 高度），
   垂距偏差小于 ``epsilon_m`` 的中间点被丢弃，首尾点始终保留。
2. **精度截断**：经纬度保留 7 位小数（约 1cm 精度），高度/速度保留 2 位。
"""

from __future__ import annotations

import math
from typing import Any

# 默认抽稀容差（米）：偏离原轨迹小于该值的中间点将被丢弃
DEFAULT_EPSILON_M = 0.5

# 坐标精度截断位数
LATLON_DECIMALS = 7
ALT_DECIMALS = 2

_EARTH_R = 6371000.0
_M_PER_DEG_LAT = 111320.0  # 每纬度对应的米数（赤道附近近似）


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine 公式计算两点水平距离（米）."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return _EARTH_R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _project(
    lat: float, lon: float, alt: float, ref_lat: float
) -> tuple[float, float, float]:
    """经纬度+高度 → 局部平面米坐标（等距圆柱投影近似，适用小范围航线）."""
    x = lon * _M_PER_DEG_LAT * math.cos(math.radians(ref_lat))
    y = lat * _M_PER_DEG_LAT
    return x, y, alt


def _segment_distance(
    p: tuple[float, float, float],
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> float:
    """点 p 到线段 ab 的垂直距离（米，3D）."""
    ax, ay, az = a
    bx, by, bz = b
    px, py, pz = p
    dx, dy, dz = bx - ax, by - ay, bz - az
    seg_len_sq = dx * dx + dy * dy + dz * dz
    if seg_len_sq <= 1e-12:
        return math.sqrt((px - ax) ** 2 + (py - ay) ** 2 + (pz - az) ** 2)
    t = ((px - ax) * dx + (py - ay) * dy + (pz - az) * dz) / seg_len_sq
    t = max(0.0, min(1.0, t))
    return math.sqrt(
        (px - (ax + t * dx)) ** 2
        + (py - (ay + t * dy)) ** 2
        + (pz - (az + t * dz)) ** 2
    )


def rdp_indices(
    points: list[tuple[float, float, float]], epsilon: float
) -> list[int]:
    """道格拉斯-普克抽稀，返回保留点的索引列表（迭代实现，避免深递归）."""
    n = len(points)
    if n <= 2 or epsilon <= 0:
        return list(range(n))

    keep = [False] * n
    keep[0] = keep[-1] = True
    stack: list[tuple[int, int]] = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        max_dist = -1.0
        max_idx = -1
        for i in range(start + 1, end):
            d = _segment_distance(points[i], points[start], points[end])
            if d > max_dist:
                max_dist = d
                max_idx = i
        if max_dist > epsilon:
            keep[max_idx] = True
            stack.append((start, max_idx))
            stack.append((max_idx, end))
    return [i for i, k in enumerate(keep) if k]


def round_waypoint(waypoint: dict[str, Any]) -> dict[str, Any]:
    """截断单个航点的坐标精度（不修改原字典）."""
    rounded = dict(waypoint)
    for key, decimals in (
        ("latitude", LATLON_DECIMALS),
        ("longitude", LATLON_DECIMALS),
        ("altitude", ALT_DECIMALS),
        ("speed", ALT_DECIMALS),
        ("hold_time", ALT_DECIMALS),
    ):
        if isinstance(rounded.get(key), (int, float)):
            rounded[key] = round(float(rounded[key]), decimals)
    return rounded


def path_distance_m(waypoints: list[dict[str, Any]]) -> float:
    """计算航点序列的总距离（米，水平 haversine + 高度差合成）."""
    total = 0.0
    for prev, cur in zip(waypoints, waypoints[1:]):
        horizontal = haversine_m(
            prev.get("latitude", 0.0), prev.get("longitude", 0.0),
            cur.get("latitude", 0.0), cur.get("longitude", 0.0),
        )
        vertical = cur.get("altitude", 0.0) - prev.get("altitude", 0.0)
        total += math.sqrt(horizontal * horizontal + vertical * vertical)
    return round(total, 2)


def simplify_waypoints(
    waypoints: list[dict[str, Any]],
    epsilon_m: float = DEFAULT_EPSILON_M,
) -> tuple[list[dict[str, Any]], float]:
    """抽稀航点列表并截断坐标精度.

    Args:
        waypoints: 航点字典列表（含 latitude/longitude/altitude 等字段）
        epsilon_m: 抽稀容差（米），越小保留的点越多；<=0 时不抽稀仅截断精度

    Returns:
        ``(new_waypoints, total_distance_m)`` — 抽稀后的航点列表
        （首尾点始终保留）与重新计算的总距离（米）。
    """
    if not waypoints:
        return [], 0.0

    if len(waypoints) > 2:
        ref_lat = waypoints[0].get("latitude", 0.0)
        projected = [
            _project(
                w.get("latitude", 0.0),
                w.get("longitude", 0.0),
                w.get("altitude", 0.0),
                ref_lat,
            )
            for w in waypoints
        ]
        indices = rdp_indices(projected, epsilon_m)
    else:
        indices = list(range(len(waypoints)))

    simplified = [round_waypoint(waypoints[i]) for i in indices]
    return simplified, path_distance_m(simplified)


def simplify_trajectory_dict(
    trajectory: dict[str, Any],
    epsilon_m: float = DEFAULT_EPSILON_M,
) -> dict[str, Any]:
    """对轨迹字典（含 waypoints 字段）就地抽稀压缩，返回新字典.

    同时按抽稀结果更新 total_distance，并在 metadata 中记录压缩信息。
    无 waypoints 或格式异常时原样返回（浅拷贝）。
    """
    result = dict(trajectory)
    waypoints = result.get("waypoints")
    if not isinstance(waypoints, list) or not waypoints:
        return result

    simplified, new_distance = simplify_waypoints(waypoints, epsilon_m=epsilon_m)
    raw_count = len(waypoints)
    result["waypoints"] = simplified
    old_distance = result.get("total_distance") or 0.0
    if old_distance > 0 and new_distance > 0 and result.get("estimated_time"):
        result["estimated_time"] = round(
            result["estimated_time"] * new_distance / old_distance, 2
        )
    result["total_distance"] = new_distance

    metadata = dict(result.get("metadata") or {})
    metadata["simplify"] = {
        "epsilon_m": epsilon_m,
        "raw_waypoint_count": raw_count,
        "simplified_waypoint_count": len(simplified),
    }
    result["metadata"] = metadata
    return result
