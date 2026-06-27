"""MAVLink 消息解析 — 纯数据转换函数，无副作用."""

from __future__ import annotations

import logging
import time
from typing import Any

from models.device import DeviceInfo, DeviceStatus

logger = logging.getLogger("brainBox.core.mavlink_parser")

_DEFAULT_PORT = 14550


def parse_port(connection_string: str) -> int:
    """从连接字符串中提取端口号."""
    if ":" in connection_string:
        try:
            return int(connection_string.rsplit(":", 1)[-1])
        except ValueError:
            return _DEFAULT_PORT
    return _DEFAULT_PORT


def parse_ip(connection_string: str) -> str:
    """从 TCP 连接字符串中提取 IP 地址，如 'tcp:192.168.43.1:5760' → '192.168.43.1'."""
    parts = connection_string.split(":")
    if len(parts) >= 3:
        return parts[1]
    return ""


def process_heartbeat(
    msg: Any, devices: dict[str, DeviceInfo], channel_label: str
) -> None:
    sys_id = msg.get_srcSystem()
    device_id = f"drone_{sys_id}"

    try:
        from pymavlink import mavutil  # noqa: PLC0415
        mode = mavutil.mode_string_v10(msg)
    except Exception:
        mode = str(msg.custom_mode)

    arm_state = "已解锁" if (msg.base_mode & 128) else "已锁定"

    if device_id not in devices:
        devices[device_id] = DeviceInfo(
            device_id=device_id,
            device_type=get_mav_type_name(msg.type),
            protocol="mavlink",
            status=DeviceStatus.ONLINE,
            metadata={
                "autopilot": msg.autopilot,
                "mav_type": msg.type,
                "system_status": msg.system_status,
                "channel": channel_label,
                "flight_mode": mode,
                "arm_state": arm_state,
            },
        )
        logger.info(
            "发现新无人机: %s (type=%s, 通道=%s)",
            device_id,
            msg.type,
            channel_label,
        )

    device = devices[device_id]
    device.last_heartbeat = time.time()
    device.status = DeviceStatus.ONLINE
    device.metadata["system_status"] = msg.system_status
    device.metadata["flight_mode"] = mode
    device.metadata["arm_state"] = arm_state


def process_position(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理全球位置消息 (GLOBAL_POSITION_INT)."""
    sys_id = msg.get_srcSystem()
    device_id = f"drone_{sys_id}"
    device = devices.get(device_id)
    if device:
        device.position = {
            "latitude": msg.lat / 1e7,
            "longitude": msg.lon / 1e7,
            "altitude": msg.alt / 1000.0,
            "relative_alt": msg.relative_alt / 1000.0,
            "heading": msg.hdg / 100.0,
        }


def process_attitude(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理姿态消息 (ATTITUDE) — 滚转/俯仰/偏航."""
    sys_id = msg.get_srcSystem()
    device_id = f"drone_{sys_id}"
    device = devices.get(device_id)
    if device:
        device.metadata["attitude"] = {
            "roll": round(msg.roll, 4),
            "pitch": round(msg.pitch, 4),
            "yaw": round(msg.yaw, 4),
            "rollspeed": round(msg.rollspeed, 4),
            "pitchspeed": round(msg.pitchspeed, 4),
            "yawspeed": round(msg.yawspeed, 4),
        }


def process_vfr_hud(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理速度/油门消息 (VFR_HUD) — 空速/地速/油门."""
    sys_id = msg.get_srcSystem()
    device_id = f"drone_{sys_id}"
    device = devices.get(device_id)
    if device:
        device.metadata["velocity"] = {
            "airspeed": round(msg.airspeed, 2),
            "groundspeed": round(msg.groundspeed, 2),
            "throttle": msg.throttle,
            "climb": round(msg.climb, 2),
        }


def process_battery(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理电池状态消息 (BATTERY_STATUS) — 电压/电流/电量."""
    sys_id = msg.get_srcSystem()
    device_id = f"drone_{sys_id}"
    device = devices.get(device_id)
    if device:
        voltage = msg.voltages[0] / 1000.0 if msg.voltages[0] != 0xFFFF else 0.0
        current = msg.current_battery / 100.0 if msg.current_battery != -1 else 0.0
        device.metadata["battery"] = {
            "voltage": round(voltage, 3),
            "current": round(current, 2),
            "remaining": msg.battery_remaining,
        }


def process_sys_status(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理系统状态消息 (SYS_STATUS) — CPU 占用/传感器健康."""
    sys_id = msg.get_srcSystem()
    device_id = f"drone_{sys_id}"
    device = devices.get(device_id)
    if device:
        device.metadata["sys_status"] = {
            "cpu_load": round(msg.load / 10.0, 1),
            "sensors_present": msg.onboard_control_sensors_present,
            "sensors_enabled": msg.onboard_control_sensors_enabled,
            "sensors_health": msg.onboard_control_sensors_health,
        }


def process_raw_imu(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理 IMU 原始数据 (RAW_IMU) — 加速度/陀螺仪/罗盘."""
    sys_id = msg.get_srcSystem()
    device_id = f"drone_{sys_id}"
    device = devices.get(device_id)
    if device:
        device.metadata["imu"] = {
            "xacc": msg.xacc,
            "yacc": msg.yacc,
            "zacc": msg.zacc,
            "xgyro": msg.xgyro,
            "ygyro": msg.ygyro,
            "zgyro": msg.zgyro,
            "xmag": msg.xmag,
            "ymag": msg.ymag,
            "zmag": msg.zmag,
        }


def process_scaled_pressure(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理气压计消息 (SCALED_PRESSURE) — 气压/温度."""
    sys_id = msg.get_srcSystem()
    device_id = f"drone_{sys_id}"
    device = devices.get(device_id)
    if device:
        device.metadata["barometer"] = {
            "press_abs": round(msg.press_abs, 3),
            "press_diff": round(msg.press_diff, 3),
            "temperature": round(msg.temperature / 100.0, 1),
        }


def process_gps_raw(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理 GPS 原始数据 (GPS_RAW_INT) — 经纬度/卫星数."""
    sys_id = msg.get_srcSystem()
    device_id = f"drone_{sys_id}"
    device = devices.get(device_id)
    if device:
        device.metadata["gps"] = {
            "latitude": msg.lat / 1e7,
            "longitude": msg.lon / 1e7,
            "altitude": msg.alt / 1000.0,
            "satellites_visible": msg.satellites_visible,
            "fix_type": msg.fix_type,
            "eph": msg.eph,
            "epv": msg.epv,
        }


def get_mav_type_name(mav_type: int) -> str:
    type_map = {
        0: "generic",
        1: "fixed_wing",
        2: "quadrotor",
        3: "coaxial",
        4: "helicopter",
        13: "hexarotor",
        14: "octorotor",
    }
    return type_map.get(mav_type, f"unknown_{mav_type}")
