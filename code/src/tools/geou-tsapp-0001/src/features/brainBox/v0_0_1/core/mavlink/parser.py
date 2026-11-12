"""MAVLink 消息解析 — 纯数据转换函数，无副作用.

设备 ID 分配规则
----------------
- 连接通道带有显式 label（connect_drone 传入或配置文件指定）时，
  设备 ID 直接使用 **label 原文**；
- 无 label 时，设备 ID 为 ``{box_id}_{sysid}``（在原有 sysid 逻辑上
  额外加入盒子 ID，避免多盒子场景下全局冲突）；
- 同一 label 通道下出现多台不同 sysid 的无人机时，后续设备追加
  ``_{sysid}`` 后缀以避免 ID 冲突；
- MAVLink sysid 始终保存在 ``metadata["sysid"]`` 中，发送指令时从
  metadata 读取，不再依赖设备 ID 字符串反解。
"""

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


def make_device_id(
    sys_id: int,
    box_id: str,
    explicit_label: str,
    devices: dict[str, DeviceInfo],
) -> str:
    """按分配规则生成设备 ID（label 优先，无 label 用 box_id + sysid）."""
    if explicit_label:
        base_id = explicit_label
    elif box_id:
        base_id = f"{box_id}_{sys_id}"
    else:
        base_id = f"drone_{sys_id}"

    # 同一 label 通道下多台无人机时，追加 sysid 后缀避免冲突
    candidate = base_id
    existing = devices.get(candidate)
    if existing is not None and existing.metadata.get("sysid") not in (None, sys_id):
        candidate = f"{base_id}_{sys_id}"
    return candidate


def resolve_device_id(devices: dict[str, DeviceInfo], sys_id: int) -> str | None:
    """根据 MAVLink sysid 查找已注册设备的 ID（读取 metadata['sysid']）."""
    for device_id, device in devices.items():
        if device.metadata.get("sysid") == sys_id:
            return device_id
    return None


def process_heartbeat(
    msg: Any,
    devices: dict[str, DeviceInfo],
    channel_label: str,
    box_id: str = "",
    explicit_label: str = "",
) -> None:
    """处理心跳消息 (HEARTBEAT)，按 label/box_id 规则分配设备 ID."""
    sys_id = msg.get_srcSystem()
    device_id = make_device_id(sys_id, box_id, explicit_label, devices)

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
                "sysid": sys_id,
                "autopilot": msg.autopilot,
                "mav_type": msg.type,
                "system_status": msg.system_status,
                "channel": channel_label,
                "flight_mode": mode,
                "arm_state": arm_state,
            },
        )
        logger.info(
            "发现新无人机: %s (sysid=%s, type=%s, 通道=%s)",
            device_id,
            sys_id,
            msg.type,
            channel_label,
        )

    device = devices[device_id]
    device.last_heartbeat = time.time()
    device.status = DeviceStatus.ONLINE
    device.metadata["sysid"] = sys_id
    # autopilot/mav_type 每帧刷新：设备对象可能在别处先建立或首帧异常，
    # 只在创建时写入会导致后续一直缺失（ArduPilot 兼容逻辑依赖这两个字段）
    device.metadata["autopilot"] = msg.autopilot
    device.metadata["mav_type"] = msg.type
    device.metadata["system_status"] = msg.system_status
    device.metadata["flight_mode"] = mode
    device.metadata["arm_state"] = arm_state


def process_position(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理全球位置消息 (GLOBAL_POSITION_INT)."""
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
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
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
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
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
    if device:
        device.metadata["velocity"] = {
            "airspeed": round(msg.airspeed, 2),
            "groundspeed": round(msg.groundspeed, 2),
            "throttle": msg.throttle,
            "climb": round(msg.climb, 2),
        }


def process_battery(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理电池状态消息 (BATTERY_STATUS) — 电压/电流/电量."""
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
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
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
    if device:
        device.metadata["sys_status"] = {
            "cpu_load": round(msg.load / 10.0, 1),
            "sensors_present": msg.onboard_control_sensors_present,
            "sensors_enabled": msg.onboard_control_sensors_enabled,
            "sensors_health": msg.onboard_control_sensors_health,
        }


def process_raw_imu(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理 IMU 原始数据 (RAW_IMU) — 加速度/陀螺仪/罗盘."""
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
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
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
    if device:
        device.metadata["barometer"] = {
            "press_abs": round(msg.press_abs, 3),
            "press_diff": round(msg.press_diff, 3),
            "temperature": round(msg.temperature / 100.0, 1),
        }


def process_gps_raw(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理 GPS 原始数据 (GPS_RAW_INT) — 经纬度/卫星数."""
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
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


def process_mission_current(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理任务进度消息 (MISSION_CURRENT) — 更新当前执行的航点序号.

    current_seq 为用户航点序号（减去 home/自动起飞等合成任务项偏移），
    raw_seq 为飞控任务项原始序号。
    """
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
    if device:
        progress = device.metadata.get("mission_progress") or {}
        offset = int(progress.get("user_offset", 0) or 0)
        progress["raw_seq"] = msg.seq
        progress["current_seq"] = max(0, msg.seq - offset)
        progress["last_update"] = time.time()
        device.metadata["mission_progress"] = progress
        logger.debug(
            "无人机 %s 任务进度: 当前航点 seq=%s (飞控序号 %s)",
            device_id, progress["current_seq"], msg.seq,
        )


def process_mission_item_reached(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理航点到达消息 (MISSION_ITEM_REACHED) — 记录最近到达的航点序号（用户航点序号）."""
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
    if device:
        progress = device.metadata.get("mission_progress") or {}
        offset = int(progress.get("user_offset", 0) or 0)
        progress["raw_reached_seq"] = msg.seq
        progress["last_reached_seq"] = max(0, msg.seq - offset)
        progress["last_update"] = time.time()
        device.metadata["mission_progress"] = progress
        logger.info(
            "无人机 %s 到达航点 seq=%s (飞控序号 %s)",
            device_id, progress["last_reached_seq"], msg.seq,
        )


# MAVLink 严重等级 / 指令结果码 → 可读名称（用于解锁、指令失败诊断）
_MAV_SEVERITY_NAMES = {
    0: "EMERGENCY", 1: "ALERT", 2: "CRITICAL", 3: "ERROR",
    4: "WARNING", 5: "NOTICE", 6: "INFO", 7: "DEBUG",
}
_MAV_RESULT_NAMES = {
    0: "ACCEPTED", 1: "TEMPORARILY_REJECTED", 2: "DENIED", 3: "UNSUPPORTED",
    4: "FAILED", 5: "IN_PROGRESS", 6: "CANCELLED",
}

# 飞控主动上报的文本消息（含 PreArm 检查失败原因）
def process_statustext(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理 STATUSTEXT — 记录并输出飞控文本消息（如 PreArm 拒绝解锁原因）."""
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
    if not device:
        return
    text = msg.text
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="ignore")
    text = str(text).strip("\x00 ").strip()
    if not text:
        return

    severity = getattr(msg, "severity", 6)
    sev_name = _MAV_SEVERITY_NAMES.get(severity, str(severity))
    device.metadata["last_status_text"] = {
        "text": text,
        "severity": severity,
        "severity_name": sev_name,
        "time": time.time(),
    }
    if severity <= 3:
        logger.warning("无人机 %s 飞控消息[%s]: %s", device_id, sev_name, text)
    else:
        logger.info("无人机 %s 飞控消息[%s]: %s", device_id, sev_name, text)


def process_command_ack(msg: Any, devices: dict[str, DeviceInfo]) -> None:
    """处理 COMMAND_ACK — 记录指令执行结果（解锁/模式切换等失败诊断依据）."""
    device_id = resolve_device_id(devices, msg.get_srcSystem())
    device = devices.get(device_id) if device_id else None
    result = getattr(msg, "result", 0)
    result_name = _MAV_RESULT_NAMES.get(result, str(result))
    if device:
        device.metadata["last_command_ack"] = {
            "command": msg.command,
            "result": result,
            "result_name": result_name,
            "time": time.time(),
        }
    if result == 0:
        logger.debug("收到指令确认: command=%s result=%s", msg.command, result_name)
    else:
        logger.warning(
            "指令被飞控拒绝: command=%s result=%s (%s)",
            msg.command, result, result_name,
        )


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
