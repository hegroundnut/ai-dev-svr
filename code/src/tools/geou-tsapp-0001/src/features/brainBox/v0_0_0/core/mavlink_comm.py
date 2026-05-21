"""MAVLink 通信协议实现 — 支持多连接通道与无人机通信.

连接模式说明
------------
- **被动模式（UDP 监听）**: 无人机主动连接 brainBox，brainBox 监听指定端口。
  连接串格式: ``udpin:0.0.0.0:14550``
- **主动模式（TCP 连接）**: brainBox 主动 TCP 连接无人机，适用于无人机开放 TCP 端口的场景。
  连接串格式: ``tcp:192.168.43.1:5760``
  通过 :meth:`MAVLinkProtocol.connect_drone` 动态添加 TCP 连接。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from config.settings import MAVLinkConfig, MAVLinkConnectionEntry, ReconnectConfig
from models.device import DeviceInfo, DeviceStatus

from core.protocol_registry import DeviceProtocol

logger = logging.getLogger("brainBox.core.mavlink")

_MAX_SIMULATED_DRONES = 3


class _MAVLinkChannel:
    """单个 MAVLink 连接通道（内部使用）.

    维护一个 system_id → 远端地址 的映射表，
    发送指令时自动路由到正确的无人机地址（解决 udpin 多无人机问题）。
    """

    def __init__(
        self,
        entry: MAVLinkConnectionEntry,
        system_id: int,
        component_id: int,
        reconnect: ReconnectConfig | None = None,
    ) -> None:
        self.entry = entry
        self.system_id = system_id
        self.component_id = component_id
        self.connection: Any = None
        self.simulated = False
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._addr_map: dict[int, tuple[str, int]] = {}
        self._send_lock = asyncio.Lock()
        self._reconnect_cfg = reconnect or ReconnectConfig()
        self._reconnect_attempts = 0
        self._devices_ref: dict[str, DeviceInfo] | None = None
        self._is_tcp = entry.connection_string.startswith("tcp:")

    @property
    def label(self) -> str:
        return self.entry.label or self.entry.connection_string

    async def open(self, devices: dict[str, DeviceInfo]) -> None:
        """打开连接并启动消息接收循环."""
        self._devices_ref = devices
        try:
            from pymavlink import mavutil  # noqa: PLC0415

            self.connection = mavutil.mavlink_connection(
                self.entry.connection_string,
                source_system=self.system_id,
                source_component=self.component_id,
                baud=self.entry.baud_rate,
            )
            self.simulated = False
            self._running = True
            self._task = asyncio.create_task(self._recv_loop(devices))
            logger.info(
                "MAVLink 通道已连接: %s (%s)",
                self.label,
                self.entry.connection_string,
            )
        except ImportError:
            logger.warning(
                "pymavlink 未安装，通道 '%s' 使用模拟模式", self.label
            )
            self.simulated = True
            self._running = True
            self._task = asyncio.create_task(
                self._simulated_loop(devices)
            )
        except Exception:
            logger.exception("MAVLink 通道 '%s' 连接失败", self.label)
            raise

    async def close(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.connection:
            self.connection.close()
            self.connection = None
        logger.info("MAVLink 通道已断开: %s", self.label)

    async def send_to_device(self, target_system: int, send_fn: Any) -> None:
        """向指定 system_id 的设备发送消息（自动路由到正确地址）."""
        async with self._send_lock:
            addr = self._addr_map.get(target_system)
            if addr and hasattr(self.connection, "last_address"):
                saved = getattr(self.connection, "last_address", None)
                self.connection.last_address = addr
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, send_fn
                    )
                finally:
                    self.connection.last_address = saved
            else:
                await asyncio.get_event_loop().run_in_executor(None, send_fn)

    # ── 统一消息接收循环 ──────────────────────────────────────

    async def _recv_loop(self, devices: dict[str, DeviceInfo]) -> None:
        """统一消息接收 — 不按类型过滤，收到什么就分发处理.

        TCP 通道在连续无消息或 socket 异常时自动触发重连。
        """
        _consecutive_idle = 0
        _idle_threshold = max(1, int(self._reconnect_cfg.idle_timeout))

        while self._running:
            try:
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.connection.recv_match(
                        blocking=True, timeout=1
                    ),
                )
                if msg is None:
                    if self._is_tcp and self._reconnect_cfg.enabled:
                        _consecutive_idle += 1
                        if _consecutive_idle >= _idle_threshold:
                            logger.warning(
                                "TCP 通道 %s 连续 %.0fs 无消息，尝试重连",
                                self.label,
                                _consecutive_idle,
                            )
                            if not await self._try_reconnect():
                                return  # 重连耗尽或通道已关闭
                            _consecutive_idle = 0
                    continue

                _consecutive_idle = 0
                msg_type = msg.get_type()
                if msg_type == "BAD_DATA":
                    continue

                sys_id = msg.get_srcSystem()
                if hasattr(self.connection, "last_address") and self.connection.last_address:
                    self._addr_map[sys_id] = self.connection.last_address

                if msg_type == "HEARTBEAT":
                    _process_heartbeat(msg, devices, self.label)
                elif msg_type == "GLOBAL_POSITION_INT":
                    _process_position(msg, devices)
                elif msg_type == "ATTITUDE":
                    _process_attitude(msg, devices)
                elif msg_type == "VFR_HUD":
                    _process_vfr_hud(msg, devices)
                elif msg_type == "BATTERY_STATUS":
                    _process_battery(msg, devices)
                elif msg_type == "SYS_STATUS":
                    _process_sys_status(msg, devices)
                elif msg_type == "RAW_IMU":
                    _process_raw_imu(msg, devices)
                elif msg_type == "SCALED_PRESSURE":
                    _process_scaled_pressure(msg, devices)
                elif msg_type == "GPS_RAW_INT":
                    _process_gps_raw(msg, devices)
                elif msg_type == "COMMAND_ACK":
                    logger.debug(
                        "收到指令确认: command=%s, result=%s",
                        msg.command,
                        msg.result,
                    )

            except OSError:
                logger.warning(
                    "MAVLink 通道 '%s' socket 异常 (EOF/连接重置)", self.label,
                )
                if self._is_tcp and self._reconnect_cfg.enabled:
                    if not await self._try_reconnect():
                        return
                else:
                    break
            except Exception:
                logger.exception("MAVLink 消息接收异常 (通道 %s)", self.label)
            await asyncio.sleep(0.01)

    # ── TCP 自动重连 ──────────────────────────────────────────

    async def _try_reconnect(self) -> bool:
        """尝试重连 TCP 通道. 返回 True 表示重连成功或应继续等待, False 表示放弃."""
        if not self._running:
            return False

        max_attempts = self._reconnect_cfg.max_attempts
        if max_attempts > 0 and self._reconnect_attempts >= max_attempts:
            logger.error(
                "TCP 通道 %s 已达最大重连次数 (%d/%d)，放弃重连",
                self.label,
                self._reconnect_attempts,
                max_attempts,
            )
            self._running = False
            return False

        self._reconnect_attempts += 1
        delay = min(
            self._reconnect_cfg.base_delay * (2 ** (self._reconnect_attempts - 1)),
            self._reconnect_cfg.max_delay,
        )

        logger.warning(
            "TCP 通道 %s 将在 %.1fs 后重连 (第 %d 次)",
            self.label,
            delay,
            self._reconnect_attempts,
        )

        # 标记通道下所有设备为离线
        self._mark_devices_offline()

        # 关闭旧连接
        self._close_connection()

        # 退避等待
        await asyncio.sleep(delay)

        if not self._running:
            return False

        # 尝试建立新连接
        try:
            from pymavlink import mavutil  # noqa: PLC0415

            self.connection = mavutil.mavlink_connection(
                self.entry.connection_string,
                source_system=self.system_id,
                source_component=self.component_id,
                baud=self.entry.baud_rate,
            )
            attempts = self._reconnect_attempts
            self._reconnect_attempts = 0
            logger.info(
                "TCP 通道 %s 重连成功 (第 %d 次尝试后)",
                self.label,
                attempts,
            )
            return True
        except ImportError:
            logger.error("pymavlink 未安装，无法重连 TCP 通道 %s", self.label)
            self._running = False
            return False
        except Exception:
            logger.exception(
                "TCP 通道 %s 重连失败 (第 %d 次)",
                self.label,
                self._reconnect_attempts,
            )
            return True  # 继续循环，下次空闲检测时再次尝试

    def _mark_devices_offline(self) -> None:
        """将当前通道下的所有设备标记为离线."""
        if not self._devices_ref:
            return
        for device in self._devices_ref.values():
            if device.metadata.get("channel") == self.label:
                device.status = DeviceStatus.OFFLINE
                logger.info("设备 %s 因通道 '%s' 断开标记为离线", device.device_id, self.label)

    def _close_connection(self) -> None:
        """关闭底层连接（不取消任务）。"""
        if self.connection:
            try:
                self.connection.close()
            except Exception:
                logger.debug("关闭连接时异常 (通道 %s)", self.label, exc_info=True)
            self.connection = None

    async def _simulated_loop(
        self, devices: dict[str, DeviceInfo]
    ) -> None:
        """模拟心跳循环（pymavlink 不可用时）."""
        sim_id = 0
        prefix = self.label.replace(" ", "_")
        while self._running:
            sim_id_str = f"drone_sim_{prefix}_{sim_id}"
            if sim_id_str not in devices and sim_id < _MAX_SIMULATED_DRONES:
                devices[sim_id_str] = DeviceInfo(
                    device_id=sim_id_str,
                    device_type="quadcopter",
                    protocol="mavlink",
                    status=DeviceStatus.ONLINE,
                    ip_address="127.0.0.1",
                    port=_parse_port(self.entry.connection_string) + sim_id,
                    last_heartbeat=time.time(),
                    position={
                        "latitude": 39.9042 + sim_id * 0.001,
                        "longitude": 116.4074 + sim_id * 0.001,
                        "altitude": 100.0 + sim_id * 10,
                    },
                    metadata={
                        "autopilot": "simulated",
                        "mav_type": "quadrotor",
                        "system_status": "active",
                        "channel": self.label,
                    },
                )
                logger.info("模拟发现无人机: %s (通道 %s)", sim_id_str, self.label)
                sim_id += 1

            for device in devices.values():
                if device.metadata.get("channel") == self.label:
                    device.last_heartbeat = time.time()
                    device.status = DeviceStatus.ONLINE

            await asyncio.sleep(3.0)


# ── 公共辅助函数 ──────────────────────────────────────────────

_DEFAULT_PORT = 14550


def _parse_port(connection_string: str) -> int:
    """从连接字符串中提取端口号."""
    if ":" in connection_string:
        try:
            return int(connection_string.rsplit(":", 1)[-1])
        except ValueError:
            return _DEFAULT_PORT
    return _DEFAULT_PORT


def _parse_ip(connection_string: str) -> str:
    """从 TCP 连接字符串中提取 IP 地址，如 'tcp:192.168.43.1:5760' → '192.168.43.1'."""
    parts = connection_string.split(":")
    if len(parts) >= 3:
        return parts[1]
    return ""


def _process_heartbeat(
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
            device_type=_get_mav_type_name(msg.type),
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


def _process_position(msg: Any, devices: dict[str, DeviceInfo]) -> None:
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


def _process_attitude(msg: Any, devices: dict[str, DeviceInfo]) -> None:
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


def _process_vfr_hud(msg: Any, devices: dict[str, DeviceInfo]) -> None:
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


def _process_battery(msg: Any, devices: dict[str, DeviceInfo]) -> None:
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


def _process_sys_status(msg: Any, devices: dict[str, DeviceInfo]) -> None:
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


def _process_raw_imu(msg: Any, devices: dict[str, DeviceInfo]) -> None:
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


def _process_scaled_pressure(msg: Any, devices: dict[str, DeviceInfo]) -> None:
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


def _process_gps_raw(msg: Any, devices: dict[str, DeviceInfo]) -> None:
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


def _get_mav_type_name(mav_type: int) -> str:
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


# ── 主协议类 ──────────────────────────────────────────────────


class MAVLinkProtocol(DeviceProtocol):
    """
    MAVLink 通信协议实现（多连接通道）.

    支持两种连接模式：

    1. **被动模式（UDP 监听）**: 无人机主动连接 brainBox。
       连接串示例: ``udpin:0.0.0.0:14550``

    2. **主动模式（TCP 连接）**: brainBox 主动 TCP 连接无人机。
       连接串示例: ``tcp:192.168.43.1:5760``
       通过 :meth:`connect_drone` 动态添加，通过 :meth:`disconnect_drone` 断开。

    UDP 通信架构:
    - 类脑盒子: udpin:0.0.0.0:14550  → 监听端口
    - 无人机:   udpout:brain_box_ip:14550 → 主动连接
    - udpin 会记住每台无人机的地址，发送指令时自动路由到正确目标

    TCP 通信架构:
    - 类脑盒子: tcp:drone_ip:5760  → 主动连接
    - 无人机:   开放 TCP 5760 端口 → 等待连接
    """

    def __init__(self, config: MAVLinkConfig) -> None:
        self._config = config
        self._devices: dict[str, DeviceInfo] = {}
        self._channels: list[_MAVLinkChannel] = []
        # device_id → channel label 映射，用于 TCP 主动连接的断开管理
        self._tcp_device_channel: dict[str, str] = {}

    @property
    def protocol_name(self) -> str:
        return "mavlink"

    @property
    def devices(self) -> dict[str, DeviceInfo]:
        return dict(self._devices)

    async def connect(self) -> None:
        """建立所有 MAVLink 通道连接."""
        entries = self._config.get_connections()
        logger.info("MAVLink 初始化 %d 个连接通道", len(entries))
        for entry in entries:
            channel = _MAVLinkChannel(
                entry=entry,
                system_id=self._config.system_id,
                component_id=self._config.component_id,
                reconnect=self._config.reconnect,
            )
            try:
                await channel.open(self._devices)
                self._channels.append(channel)
            except Exception:
                logger.exception(
                    "MAVLink 通道 '%s' 启动失败，跳过",
                    entry.label or entry.connection_string,
                )

    async def disconnect(self) -> None:
        """断开所有 MAVLink 通道."""
        for ch in self._channels:
            try:
                await ch.close()
            except Exception:
                logger.exception("MAVLink 通道 '%s' 关闭异常", ch.label)
        self._channels.clear()
        self._tcp_device_channel.clear()
        logger.info("MAVLink 所有通道已断开")

    async def scan_devices(self) -> list[DeviceInfo]:
        """返回当前所有通道已发现的无人机列表."""
        self._update_device_status()
        return list(self._devices.values())

    async def send_command(
        self, device_id: str, command: dict[str, Any]
    ) -> dict[str, Any]:
        """向无人机发送 MAVLink 指令."""
        device = self._devices.get(device_id)
        if not device:
            return {"success": False, "error": f"设备 {device_id} 未找到"}

        cmd_type = command.get("type", "")
        logger.info("向设备 %s 发送指令: %s", device_id, cmd_type)

        channel = self._find_channel_for_device(device)
        if channel and channel.connection:
            return await self._send_mavlink_command(channel, device, command)

        return {
            "success": True,
            "message": f"模拟发送指令 {cmd_type} 到 {device_id}",
        }

    async def get_device_status(self, device_id: str) -> DeviceInfo | None:
        """获取指定无人机状态."""
        self._update_device_status()
        return self._devices.get(device_id)

    async def send_waypoints(
        self, device_id: str, waypoints: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """向无人机发送航点任务."""
        device = self._devices.get(device_id)
        if not device:
            return {"success": False, "error": f"设备 {device_id} 未找到"}

        logger.info("向设备 %s 发送 %d 个航点", device_id, len(waypoints))

        channel = self._find_channel_for_device(device)
        if channel and channel.connection:
            return await self._upload_mission(channel, device, waypoints)

        return {
            "success": True,
            "message": f"模拟发送 {len(waypoints)} 个航点到 {device_id}",
            "waypoint_count": len(waypoints),
        }

    # ── TCP 主动连接管理 ──────────────────────────────────────

    async def connect_drone(
        self, ip: str, port: int, label: str = ""
    ) -> dict[str, Any]:
        """
        主动 TCP 连接到指定无人机.

        brainBox 作为客户端，主动连接无人机的 TCP 端口（通常为 5760）。
        连接成功后等待心跳包，自动注册设备。

        Args:
            ip: 无人机 IP 地址，如 ``"192.168.43.1"``
            port: 无人机 TCP 端口，如 ``5760``
            label: 连接标签（可选，默认为 ``"tcp:{ip}:{port}"``）

        Returns:
            dict: ``{"success": True, "channel_label": ..., "connection_string": ...}``
        """
        connection_string = f"tcp:{ip}:{port}"
        if not label:
            label = connection_string

        # 检查是否已存在相同连接
        for ch in self._channels:
            if ch.entry.connection_string == connection_string:
                return {
                    "success": False,
                    "error": f"连接 {connection_string} 已存在 (通道: {ch.label})",
                }

        entry = MAVLinkConnectionEntry(
            connection_string=connection_string,
            label=label,
            baud_rate=self._config.baud_rate,
        )
        channel = _MAVLinkChannel(
            entry=entry,
            system_id=self._config.system_id,
            component_id=self._config.component_id,
            reconnect=self._config.reconnect,
        )
        try:
            await channel.open(self._devices)
            self._channels.append(channel)
            logger.info("TCP 主动连接成功: %s", connection_string)
            return {
                "success": True,
                "channel_label": label,
                "connection_string": connection_string,
            }
        except Exception as e:
            logger.exception("TCP 主动连接失败: %s", connection_string)
            return {"success": False, "error": str(e)}

    async def disconnect_drone(self, device_id: str) -> dict[str, Any]:
        """
        断开指定无人机的 TCP 连接.

        仅对通过 :meth:`connect_drone` 建立的 TCP 主动连接有效。
        UDP 被动连接通道不会被此方法关闭。

        Args:
            device_id: 无人机设备 ID，如 ``"drone_1"``

        Returns:
            dict: ``{"success": True, "device_id": ..., "channel_label": ...}``
        """
        device = self._devices.get(device_id)
        if not device:
            return {"success": False, "error": f"设备 {device_id} 未找到"}

        channel = self._find_channel_for_device(device)
        if not channel:
            return {"success": False, "error": f"设备 {device_id} 未找到对应通道"}

        # 只允许断开 TCP 主动连接通道
        if not channel.entry.connection_string.startswith("tcp:"):
            return {
                "success": False,
                "error": f"通道 '{channel.label}' 为非 TCP 通道，不支持单独断开",
            }

        channel_label = channel.label
        await channel.close()
        self._channels.remove(channel)

        # 移除该通道下的所有设备
        removed = [
            did for did, d in self._devices.items()
            if d.metadata.get("channel") == channel_label
        ]
        for did in removed:
            self._devices.pop(did, None)

        logger.info(
            "TCP 连接已断开: %s，移除设备: %s",
            channel_label,
            removed,
        )
        return {
            "success": True,
            "device_id": device_id,
            "channel_label": channel_label,
            "removed_devices": removed,
        }

    def list_tcp_connections(self) -> list[dict[str, Any]]:
        """列出所有 TCP 主动连接通道信息."""
        result = []
        for ch in self._channels:
            if ch.entry.connection_string.startswith("tcp:"):
                ip = _parse_ip(ch.entry.connection_string)
                port = _parse_port(ch.entry.connection_string)
                # 找到该通道下的设备
                channel_devices = [
                    did for did, d in self._devices.items()
                    if d.metadata.get("channel") == ch.label
                ]
                result.append({
                    "label": ch.label,
                    "connection_string": ch.entry.connection_string,
                    "ip": ip,
                    "port": port,
                    "simulated": ch.simulated,
                    "devices": channel_devices,
                    "reconnect": {
                        "enabled": ch._reconnect_cfg.enabled,
                        "attempts": ch._reconnect_attempts,
                        "max_attempts": ch._reconnect_cfg.max_attempts,
                    },
                })
        return result

    # ── 内部方法 ──────────────────────────────────────────────

    def _find_channel_for_device(self, device: DeviceInfo) -> _MAVLinkChannel | None:
        """根据设备 metadata 中的 channel 标签找到对应通道."""
        ch_label = device.metadata.get("channel", "")
        for ch in self._channels:
            if ch.label == ch_label:
                return ch
        return self._channels[0] if self._channels else None

    def _update_device_status(self) -> None:
        timeout = self._config.heartbeat_timeout
        for device in self._devices.values():
            if device.is_alive(timeout):
                if device.status != DeviceStatus.BUSY:
                    device.status = DeviceStatus.ONLINE
            else:
                device.status = DeviceStatus.OFFLINE

    @staticmethod
    def _get_target_system(device: DeviceInfo) -> int:
        return int(device.device_id.split("_")[1])

    async def _send_mavlink_command(
        self,
        channel: _MAVLinkChannel,
        device: DeviceInfo,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        cmd_type = command.get("type", "")
        conn = channel.connection
        target = self._get_target_system(device)

        def do_send() -> None:
            from pymavlink import mavutil  # noqa: PLC0415

            if cmd_type == "arm":
                conn.mav.command_long_send(
                    target, 0,
                    mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                    0, 1, 0, 0, 0, 0, 0, 0,
                )
            elif cmd_type == "disarm":
                conn.mav.command_long_send(
                    target, 0,
                    mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                    0, 0, 0, 0, 0, 0, 0, 0,
                )
            elif cmd_type == "takeoff":
                altitude = command.get("altitude", 10.0)
                conn.mav.command_long_send(
                    target, 0,
                    mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                    0, 0, 0, 0, 0, 0, 0, altitude,
                )
            elif cmd_type == "land":
                conn.mav.command_long_send(
                    target, 0,
                    mavutil.mavlink.MAV_CMD_NAV_LAND,
                    0, 0, 0, 0, 0, 0, 0, 0,
                )
            elif cmd_type == "goto":
                lat = command.get("latitude", 0)
                lon = command.get("longitude", 0)
                alt = command.get("altitude", 10)
                conn.mav.command_long_send(
                    target, 0,
                    mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                    0, 0, 0, 0, 0,
                    int(lat * 1e7), int(lon * 1e7), alt,
                )
            elif cmd_type == "set_mode":
                mode_name = command.get("mode", "STABILIZE")
                mode_mapping = conn.mode_mapping()
                if mode_name in mode_mapping:
                    mode_id = mode_mapping[mode_name]
                    conn.mav.set_mode_send(
                        target,
                        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                        mode_id,
                    )
                else:
                    raise ValueError(f"未知飞行模式: {mode_name}")

        if cmd_type not in ("arm", "disarm", "takeoff", "land", "goto", "set_mode"):
            return {"success": False, "error": f"未知指令类型: {cmd_type}"}

        await channel.send_to_device(target, do_send)
        return {"success": True, "command": cmd_type, "device_id": device.device_id}

    async def _upload_mission(
        self,
        channel: _MAVLinkChannel,
        device: DeviceInfo,
        waypoints: list[dict[str, Any]],
    ) -> dict[str, Any]:
        conn = channel.connection
        if not conn:
            return {"success": False, "error": "MAVLink 未连接"}

        target = self._get_target_system(device)

        def do_upload() -> None:
            from pymavlink import mavutil  # noqa: PLC0415

            conn.mav.mission_count_send(target, 0, len(waypoints))
            for i, wp in enumerate(waypoints):
                conn.mav.mission_item_int_send(
                    target, 0, i,
                    mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                    mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                    0, 1,
                    wp.get("hold_time", 0), 0, 0, 0,
                    int(wp.get("latitude", 0) * 1e7),
                    int(wp.get("longitude", 0) * 1e7),
                    wp.get("altitude", 10),
                )

        await channel.send_to_device(target, do_upload)
        return {"success": True, "waypoint_count": len(waypoints)}
