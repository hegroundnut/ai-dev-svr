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
from collections import deque
from typing import Any

from config.settings import MAVLinkConfig, MAVLinkConnectionEntry, ReconnectConfig
from models.device import DeviceInfo, DeviceStatus

from core.mavlink.protocol import DeviceProtocol
from core.mavlink.parser import (
    get_mav_type_name,
    parse_ip,
    parse_port,
    process_attitude,
    process_battery,
    process_command_ack,
    process_gps_raw,
    process_heartbeat,
    process_mission_current,
    process_mission_item_reached,
    process_position,
    process_raw_imu,
    process_scaled_pressure,
    process_statustext,
    process_sys_status,
    process_vfr_hud,
    resolve_device_id,
)

logger = logging.getLogger("brainBox.core.mavlink")

_MAX_SIMULATED_DRONES = 3

# 每设备 STATUSTEXT 环形缓存上限（条）
_STATUS_TEXT_CACHE_SIZE = 300

# MAV_SEVERITY 名称映射
_SEVERITY_NAMES = {
    0: "EMERGENCY",
    1: "ALERT",
    2: "CRITICAL",
    3: "ERROR",
    4: "WARNING",
    5: "NOTICE",
    6: "INFO",
    7: "DEBUG",
}

# 远程重启对象 → MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN 的 param1
_REBOOT_COMPONENTS = {
    "autopilot": 1,
    "companion": 2,
    "both": 3,
}

# 无真实连接时返回的常见 ArduCopter 飞行模式（仅模拟调试用）
_SIMULATED_MODES = (
    "STABILIZE", "ACRO", "ALT_HOLD", "AUTO", "GUIDED", "LOITER",
    "RTL", "LAND", "POSHOLD", "DRIFT", "SPORT", "FLIP",
    "AUTOTUNE", "GUIDED_NOGPS", "SMART_RTL",
)

# ── 航点任务协议参数 ──────────────────────────────────────────
# 接收循环需要捕获/处理的任务协议消息类型
_MISSION_MSG_TYPES = (
    "MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK",
    "MISSION_CURRENT", "MISSION_ITEM_REACHED",
)
# 等待飞控首个 MISSION_REQUEST 的超时（超时则开始盲发兜底）
_MISSION_REQUEST_TIMEOUT = 3.0
# 任务上传总超时（含盲发与等待 MISSION_ACK）
_MISSION_UPLOAD_TIMEOUT = 30.0
# 盲发模式相邻航点的发送间隔（秒），防止总线拥塞
_MISSION_ITEM_INTERVAL = 0.15
# 自动解锁后等待飞控确认解锁的超时（秒）
_ARM_TIMEOUT = 12.0
# MAV_CMD_COMPONENT_ARM_DISARM（解锁/上锁指令 ID）
_ARM_CMD = 400
# MAV_CMD_DO_SET_MODE（切模式指令 ID）
_DO_SET_MODE_CMD = 176
# 切换飞行模式后等待飞控确认模式的超时（秒）
_MODE_TIMEOUT = 3.0
# 需要自动插入起飞任务项的 ArduPilot 载具类型（排除地面/水面/水下载具）
_TYPES_NO_TAKEOFF = (10, 11, 12)
# 航点指令名 → MAVLink 指令常量名映射
_MISSION_COMMANDS = {
    "waypoint": "MAV_CMD_NAV_WAYPOINT",
    "takeoff": "MAV_CMD_NAV_TAKEOFF",
    "land": "MAV_CMD_NAV_LAND",
    "rtl": "MAV_CMD_NAV_RETURN_TO_LAUNCH",
    "loiter": "MAV_CMD_NAV_LOITER_TIME",
}
# MISSION_ACK 错误码 → 可读描述
_MISSION_ACK_ERRORS = {
    1: "通用错误 (ERROR)",
    2: "不支持的任务坐标系 (UNSUPPORTED_FRAME)",
    3: "不支持的指令 (UNSUPPORTED)",
    4: "任务存储空间不足 (NO_SPACE)",
    5: "任务项无效 (INVALID)",
    6: "param1 无效 (INVALID_PARAM1)",
    7: "param2 无效 (INVALID_PARAM2)",
    8: "param3 无效 (INVALID_PARAM3)",
    9: "param4 无效 (INVALID_PARAM4)",
    10: "param5/x 无效 (INVALID_PARAM5_X)",
    11: "param6/y 无效 (INVALID_PARAM6_Y)",
    12: "param7/z 无效 (INVALID_PARAM7)",
    13: "任务序号无效 (INVALID_SEQUENCE)",
    14: "操作不被支持 (NOT_SUPPORTED)",
    15: "任务被拒绝 (DENIED)",
}


async def _wait_mission_msg(
    queue: asyncio.Queue, types: tuple[str, ...], timeout: float
) -> Any:
    """从任务消息捕获队列等待指定类型的消息（忽略其他类型）."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if msg.get_type() in types:
            return msg


def _item_label(item: dict[str, Any]) -> str:
    """返回任务项的日志标签（合成项如 home/自动起飞会标注）."""
    label = item.get("_label")
    return f" [{label}]" if label else ""


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
        box_id: str = "",
    ) -> None:
        self.entry = entry
        self.system_id = system_id
        self.component_id = component_id
        self.box_id = box_id
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
        # 每设备飞控文本消息（STATUSTEXT）环形缓存（供 drone_logs 增量查询）
        self.status_texts: dict[str, deque[dict[str, Any]]] = {}
        # 航点任务：串行化同通道的任务操作，捕获队列由接收循环投递任务协议消息
        self.mission_lock = asyncio.Lock()
        self._mission_queue: asyncio.Queue | None = None

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

    def start_mission_capture(self) -> asyncio.Queue:
        """开始捕获任务协议消息（航点上传期间由接收循环投递到队列）."""
        self._mission_queue = asyncio.Queue()
        return self._mission_queue

    def stop_mission_capture(self) -> None:
        """停止捕获任务协议消息."""
        self._mission_queue = None

    # ── 统一消息接收循环 ──────────────────────────────────────

    async def _recv_loop(self, devices: dict[str, DeviceInfo]) -> None:
        """统一消息接收 — TCP 通道在断线时自动重连."""
        _consecutive_idle = 0
        _idle_threshold = max(1, int(self._reconnect_cfg.idle_timeout))

        while self._running:
            if self.connection is None:
                if self._is_tcp and self._reconnect_cfg.enabled:
                    await self._try_reconnect()
                if self.connection is None:
                    break
                continue

            try:
                msg = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.connection.recv_match(
                        blocking=True, timeout=1
                    ),
                )
            except OSError:
                if self._is_tcp and self._reconnect_cfg.enabled:
                    await self._try_reconnect()
                else:
                    break
                continue
            except Exception:
                await asyncio.sleep(0.5)
                continue

            if msg is None:
                if self._is_tcp and self._reconnect_cfg.enabled:
                    _consecutive_idle += 1
                    if _consecutive_idle >= _idle_threshold:
                        await self._try_reconnect()
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
                process_heartbeat(
                    msg, devices, self.label,
                    box_id=self.box_id,
                    explicit_label=self.entry.label,
                )
            elif msg_type == "GLOBAL_POSITION_INT":
                process_position(msg, devices)
            elif msg_type == "ATTITUDE":
                process_attitude(msg, devices)
            elif msg_type == "VFR_HUD":
                process_vfr_hud(msg, devices)
            elif msg_type == "BATTERY_STATUS":
                process_battery(msg, devices)
            elif msg_type == "SYS_STATUS":
                process_sys_status(msg, devices)
            elif msg_type == "RAW_IMU":
                process_raw_imu(msg, devices)
            elif msg_type == "SCALED_PRESSURE":
                process_scaled_pressure(msg, devices)
            elif msg_type == "GPS_RAW_INT":
                process_gps_raw(msg, devices)
            elif msg_type == "COMMAND_ACK":
                process_command_ack(msg, devices)
            elif msg_type == "STATUSTEXT":
                process_statustext(msg, devices)
                self._process_status_text(msg, devices)
            elif msg_type in _MISSION_MSG_TYPES:
                # 任务进度更新元数据；上传期间的协议应答投递到捕获队列
                if msg_type == "MISSION_CURRENT":
                    process_mission_current(msg, devices)
                elif msg_type == "MISSION_ITEM_REACHED":
                    process_mission_item_reached(msg, devices)
                elif msg_type == "MISSION_ACK":
                    logger.debug("收到任务应答: type=%s", msg.type)
                if self._mission_queue is not None:
                    self._mission_queue.put_nowait(msg)

            await asyncio.sleep(0.01)

    def _process_status_text(
        self, msg: Any, devices: dict[str, DeviceInfo]
    ) -> None:
        """缓存飞控 STATUSTEXT 文本消息（每设备最近 300 条，供 drone_logs 增量查询）."""
        device_id = resolve_device_id(devices, msg.get_srcSystem())
        if not device_id:
            return

        text = getattr(msg, "text", "")
        if isinstance(text, (bytes, bytearray)):
            text = text.decode("utf-8", errors="replace")
        text = str(text or "").rstrip("\x00").strip()
        if not text:
            return

        try:
            severity = int(getattr(msg, "severity", 6))
        except (TypeError, ValueError):
            severity = 6

        entry = {
            "text": text,
            "severity": severity,
            "severity_name": _SEVERITY_NAMES.get(severity, "UNKNOWN"),
            "time": time.time(),
        }
        buffer = self.status_texts.setdefault(
            device_id, deque(maxlen=_STATUS_TEXT_CACHE_SIZE)
        )
        buffer.append(entry)

    # ── TCP 自动重连 ──────────────────────────────────────────

    async def _try_reconnect(self) -> None:
        """TCP 断线重连：关闭旧连接 → 退避等待 → 建立新连接（内部循环至成功或放弃）."""
        max_attempts = self._reconnect_cfg.max_attempts
        max_attempts_str = str(max_attempts) if max_attempts > 0 else "∞"

        self._mark_devices_offline()
        self._close_connection()

        while self._running:
            self._reconnect_attempts += 1

            if max_attempts > 0 and self._reconnect_attempts > max_attempts:
                logger.error(
                    "TCP 通道 %s 重连 %d 次均失败，放弃",
                    self.label, max_attempts,
                )
                self._running = False
                return

            delay = min(
                self._reconnect_cfg.base_delay * (2 ** (self._reconnect_attempts - 1)),
                self._reconnect_cfg.max_delay,
            )

            logger.info(
                "TCP %s 重连 %d/%s，等待 %.0fs",
                self.label, self._reconnect_attempts, max_attempts_str, delay,
            )
            await asyncio.sleep(delay)

            if not self._running:
                return

            try:
                from pymavlink import mavutil  # noqa: PLC0415

                self.connection = mavutil.mavlink_connection(
                    self.entry.connection_string,
                    source_system=self.system_id,
                    source_component=self.component_id,
                    baud=self.entry.baud_rate,
                )
                self._reconnect_attempts = 0
                logger.info("TCP %s 重连成功", self.label)
                return
            except Exception:
                pass  # 继续循环尝试

    def _mark_devices_offline(self) -> None:
        if not self._devices_ref:
            return
        for device in self._devices_ref.values():
            if device.metadata.get("channel") == self.label:
                device.status = DeviceStatus.OFFLINE

    def _close_connection(self) -> None:
        if self.connection:
            with contextlib.suppress(Exception):
                self.connection.close()
            self.connection = None

    async def _simulated_loop(
        self, devices: dict[str, DeviceInfo]
    ) -> None:
        """模拟心跳循环（pymavlink 不可用时）."""
        sim_id = 0
        prefix = (self.entry.label or self.box_id or "box").replace(" ", "_")
        while self._running:
            sim_id_str = f"sim_{prefix}_{sim_id}"
            if sim_id_str not in devices and sim_id < _MAX_SIMULATED_DRONES:
                devices[sim_id_str] = DeviceInfo(
                    device_id=sim_id_str,
                    device_type="quadcopter",
                    protocol="mavlink",
                    status=DeviceStatus.ONLINE,
                    ip_address="127.0.0.1",
                    port=parse_port(self.entry.connection_string) + sim_id,
                    last_heartbeat=time.time(),
                    position={
                        "latitude": 39.9042 + sim_id * 0.001,
                        "longitude": 116.4074 + sim_id * 0.001,
                        "altitude": 100.0 + sim_id * 10,
                    },
                    metadata={
                        "sysid": None,
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

    def __init__(self, config: MAVLinkConfig, box_id: str = "") -> None:
        self._config = config
        self._box_id = box_id
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
                box_id=self._box_id,
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
        self,
        device_id: str,
        waypoints: list[dict[str, Any]],
        auto_start: bool = True,
        auto_arm: bool = False,
    ) -> dict[str, Any]:
        """向无人机发送航点飞行任务（完整 MAVLink 任务协议）.

        流程: MISSION_COUNT → (MISSION_REQUEST → MISSION_ITEM_INT)* → MISSION_ACK
        → 可选 解锁 → MISSION_START/切任务模式；执行进度通过 MISSION_CURRENT
        持续更新到设备 metadata["mission_progress"]。

        Args:
            device_id: 无人机设备 ID
            waypoints: 航点列表，每项支持:
                latitude/longitude（必填，度）、altitude（米，相对高度，默认10）、
                hold_time（停留秒，默认0）、accept_radius（到达半径米，默认2）、
                pass_radius（过点半径米，默认0）、yaw（航向度，默认0）、
                command（waypoint/takeoff/land/rtl/loiter，默认 waypoint）
            auto_start: 上传成功后是否启动任务（默认 True）
            auto_arm: 启动前若未解锁是否自动解锁（默认 False，避免轨迹等
                内部调用意外解锁；waypoint_mission 接口默认传 True）
        """
        device = self._devices.get(device_id)
        if not device:
            return {"success": False, "error": f"设备 {device_id} 未找到"}
        if not waypoints:
            return {"success": False, "error": "航点列表为空"}

        logger.info("向设备 %s 发送 %d 个航点", device_id, len(waypoints))

        channel = self._find_channel_for_device(device)
        if channel and channel.connection:
            return await self._run_mission_upload(
                channel, device, waypoints, auto_start, auto_arm
            )

        # 无真实连接时模拟成功
        device.metadata["mission_progress"] = {
            "total": len(waypoints),
            "item_total": len(waypoints),
            "user_offset": 0,
            "home_prefix": 0,
            "current_seq": 0,
            "last_reached_seq": None,
            "last_update": time.time(),
        }
        return {
            "success": True,
            "simulated": True,
            "message": f"模拟发送 {len(waypoints)} 个航点到 {device_id}",
            "waypoint_count": len(waypoints),
            "started": auto_start,
        }

    async def _run_mission_upload(
        self,
        channel: _MAVLinkChannel,
        device: DeviceInfo,
        waypoints: list[dict[str, Any]],
        auto_start: bool,
        auto_arm: bool = True,
    ) -> dict[str, Any]:
        """执行完整 MAVLink 任务协议：上传航点 → 等待 ACK → 解锁 → 启动任务."""
        from pymavlink import mavutil  # noqa: PLC0415
        mavlink = mavutil.mavlink
        conn = channel.connection
        target_sys = self._get_target_system(device)
        target_comp = 0

        is_ardupilot = self._is_ardupilot(device, conn)
        mav_type = device.metadata.get("mav_type", 0)

        # ArduPilot 任务协议约定: 任务项 0 固定为 home（起飞点），直接把用户航点
        # 放在 seq0 会被飞控当作 home 存掉（地面站不显示、不执行）。
        # 因此 ArduPilot 设备在 seq0 追加 home 项（取当前位置，解锁后飞控自动更新），
        # 用户航点顺移到 seq 1..N。PX4 无此约定，不需要追加。
        home_prefix = 1 if is_ardupilot else 0
        upload_items = list(waypoints)
        # 用户航点前插入的合成任务项数量（home / 起飞），用于进度序号换算
        user_offset = 0
        if home_prefix:
            pos = device.position or {}
            home_lat = pos.get("latitude")
            home_lon = pos.get("longitude")
            if not home_lat or not home_lon:
                # 尚无有效定位时，退回首个用户航点坐标（解锁起飞后飞控会更新为真实起飞点）
                home_lat = waypoints[0].get("latitude", 0.0)
                home_lon = waypoints[0].get("longitude", 0.0)
            upload_items.insert(0, {
                "latitude": home_lat,
                "longitude": home_lon,
                "altitude": 0.0,
                "_label": "home",
            })
            user_offset = 1
            logger.info(
                "ArduPilot 兼容: seq0 追加 home 任务项 (总任务项 %d)", len(upload_items)
            )

            # ArduPilot 飞行器从地面进入 AUTO 时，必须由 NAV_TAKEOFF 触发起飞，
            # 否则只会在原地等待。用户未显式提供起飞指令时自动插入一项。
            has_takeoff = any(
                str(wp.get("command", "waypoint")).lower() == "takeoff"
                for wp in waypoints
            )
            if not has_takeoff and mav_type not in _TYPES_NO_TAKEOFF:
                takeoff_alt = float(waypoints[0].get("altitude", 10.0) or 10.0)
                upload_items.insert(user_offset, {
                    "latitude": home_lat,
                    "longitude": home_lon,
                    "altitude": takeoff_alt,
                    "_label": "takeoff(自动)",
                    "command": "takeoff",
                })
                user_offset += 1
                logger.info(
                    "ArduPilot 兼容: 未提供起飞指令，自动插入起飞任务项 (高度 %.1fm)",
                    takeoff_alt,
                )
        count = len(upload_items)

        async with channel.mission_lock:
            queue = channel.start_mission_capture()
            try:
                # 1. 发送 MISSION_COUNT，告知飞控任务项总数
                await channel.send_to_device(
                    target_sys,
                    lambda: conn.mav.mission_count_send(target_sys, target_comp, count),
                )
                logger.info(
                    "已发送 MISSION_COUNT count=%d (设备 %s)：%d 个用户航点"
                    " + %d 个自动插入任务项",
                    count, device.device_id, len(waypoints), user_offset,
                )

                # 2. 上传航点（请求-应答优先；飞控不请求时自动盲发兜底，
                #    期间及之后仍响应迟到的 MISSION_REQUEST）
                ack = await self._upload_items(
                    channel, queue, mavutil, target_sys, target_comp, upload_items,
                )

                # 3. 校验 MISSION_ACK
                if ack is None:
                    return {"success": False, "error": "航点上传超时：未收到 MISSION_ACK"}
                if ack.type != mavlink.MAV_MISSION_ACCEPTED:
                    desc = _MISSION_ACK_ERRORS.get(ack.type, "未知错误")
                    logger.error(
                        "设备 %s 航点上传被拒绝: 错误码 %s (%s)",
                        device.device_id, ack.type, desc,
                    )
                    return {
                        "success": False,
                        "error": f"航点上传被飞控拒绝: {desc} (MAV_MISSION 错误码 {ack.type})",
                        "ack_type": ack.type,
                    }

                # 4. 上传成功 → 初始化任务进度元数据（MISSION_CURRENT 持续更新）
                #    current_seq 按用户航点序号（0..N-1）；raw_seq 为飞控任务项序号
                device.metadata["mission_progress"] = {
                    "total": len(waypoints),       # 用户航点数
                    "item_total": count,           # 飞控任务项总数（含自动插入项）
                    "user_offset": user_offset,    # 首个用户航点前的合成任务项数
                    "home_prefix": home_prefix,    # ArduPilot=1（seq0 为 home），其他 0
                    "current_seq": 0,
                    "last_reached_seq": None,
                    "last_update": time.time(),
                }

                result: dict[str, Any] = {
                    "success": True,
                    "waypoint_count": len(waypoints),
                    "mission_item_count": count,
                    "started": False,
                }

                # 5. 解锁并启动任务（兼容旧版 pymavlink 与 ArduPilot）
                if auto_start:
                    start_result = await self._start_mission(
                        channel, device, auto_arm
                    )
                    result.update(start_result)
                    result["started"] = bool(start_result.get("started"))
                    if start_result.get("error"):
                        result["success"] = False
                        result["error"] = (
                            f"任务已上传，但启动失败: {start_result['error']}"
                        )

                return result
            finally:
                channel.stop_mission_capture()

    @staticmethod
    def _is_ardupilot(device: DeviceInfo, conn: Any = None) -> bool:
        """判断飞控是否为 ArduPilot（决定是否追加 home/自动起飞任务项）.

        优先用心跳上报的 autopilot 字段；字段缺失或为 0（未知）时，用飞控
        模式表推断：ArduPilot 有 AUTO + LOITER/STABILIZE，PX4 的模式名不同。
        """
        autopilot = device.metadata.get("autopilot")
        if autopilot in (3, "3"):
            return True
        if autopilot not in (0, "0", None, ""):
            return False  # 明确是其他飞控（如 PX4=12）
        try:
            mapping = dict((conn.mode_mapping() if conn else None) or {})
        except Exception:
            mapping = {}
        return "AUTO" in mapping and (
            "LOITER" in mapping or "STABILIZE" in mapping
        )

    async def _start_mission(
        self,
        channel: _MAVLinkChannel,
        device: DeviceInfo,
        auto_arm: bool = True,
    ) -> dict[str, Any]:
        """解锁并启动航点任务.

        顺序: 解锁（可选）→ MISSION_START → 切换任务模式（AUTO/AUTO_MISSION）。

        - 未解锁时飞控不会执行任务（ArduPilot 地面状态仅预置模式），故先解锁；
          解锁在 AUTO 模式下可能被拒绝，此时先切出 AUTO 再解锁；
        - 新版 pymavlink 有 ``mission_start_send`` 时直接使用，旧版降级为
          ``command_long`` 发送 MAV_CMD_MISSION_START（等价）；
        - ArduPilot 通过切 AUTO 启动任务，PX4 对应模式名为 AUTO_MISSION。
        """
        from pymavlink import mavutil  # noqa: PLC0415
        mavlink = mavutil.mavlink
        conn = channel.connection
        target = self._get_target_system(device)
        # MAV_CMD_MISSION_START = 300，旧版 pymavlink 可能缺少该常量
        mission_start_cmd = getattr(mavlink, "MAV_CMD_MISSION_START", 300)
        result: dict[str, Any] = {"started": False, "armed": False}

        mode_mapping = await asyncio.get_event_loop().run_in_executor(
            None, lambda: dict(conn.mode_mapping() or {})
        )

        # ── 1. 解锁 ──
        if device.metadata.get("arm_state") == "已解锁":
            result["armed"] = True
        elif auto_arm:
            if str(device.metadata.get("flight_mode", "")).upper() in (
                "AUTO", "AUTO_MISSION",
            ):
                # AUTO 模式下多数固件拒绝解锁，先切到手动模式
                await self._set_mode(
                    channel, device, target, mode_mapping,
                    ("LOITER", "GUIDED", "POSCTL", "STABILIZE"),
                )
                await asyncio.sleep(0.5)
            arm_reason = ""
            for attempt in (1, 2):
                arm_sent_at = time.time()
                await self._send_arm(channel, target, True)
                if await self._wait_arm_state(device, True, _ARM_TIMEOUT):
                    result["armed"] = True
                    break
                arm_reason = self._command_failure_reason(
                    device, arm_sent_at, _ARM_CMD
                )
                logger.error(
                    "设备 %s 第 %d 次解锁失败%s",
                    device.device_id, attempt,
                    f": {arm_reason}" if arm_reason else "（飞控无反馈）",
                )
                # 飞控已明确应答（拒绝/失败）时重试无意义，直接结束
                ack = device.metadata.get("last_command_ack") or {}
                acked = (
                    ack.get("command") == _ARM_CMD
                    and ack.get("time", 0) >= arm_sent_at
                )
                if attempt == 2 or acked:
                    break
                await asyncio.sleep(1.5)
            if not result["armed"]:
                reason = (
                    f"解锁失败：{arm_reason}" if arm_reason
                    else "解锁失败（飞控未响应解锁指令）"
                )
                result["error"] = (
                    f"{reason}（当前模式 {device.metadata.get('flight_mode', '未知')}，"
                    f"状态 {device.metadata.get('arm_state', '未知')}）"
                )
                return result
        else:
            result["error"] = (
                f"设备未解锁且 auto_arm=false（当前状态: "
                f"{device.metadata.get('arm_state', '未知')}）"
            )
            return result

        # ── 2. 发送 MISSION_START ──
        def do_start() -> None:
            if hasattr(conn.mav, "mission_start_send"):
                conn.mav.mission_start_send(target, 0, 0, 0)
            else:
                conn.mav.command_long_send(
                    target, 0, mission_start_cmd,
                    0, 0, 0, 0, 0, 0, 0, 0,
                )

        await channel.send_to_device(target, do_start)
        logger.info("已发送 MISSION_START (设备 %s)", device.device_id)

        # 地面等待期间飞控可能自动上锁（DISARM_DELAY），切任务模式前复核一次
        if auto_arm and device.metadata.get("arm_state") != "已解锁":
            logger.warning("设备 %s 启动前已自动上锁，重新解锁", device.device_id)
            await self._send_arm(channel, target, True)
            if not await self._wait_arm_state(device, True, _ARM_TIMEOUT):
                reason = self._command_failure_reason(device, time.time() - 1, _ARM_CMD)
                result["error"] = (
                    "启动前设备已上锁且重新解锁失败"
                    + (f"：{reason}" if reason else "")
                )
                return result
            result["armed"] = True

        # ── 3. 切换任务模式，飞控据此开始执行航点 ──
        mode_sent_at = time.time()
        mode = await self._set_mode(
            channel, device, target, mode_mapping,
            ("AUTO", "AUTO_MISSION", "MISSION"),
        )
        if mode is None:
            reason = self._command_failure_reason(
                device, mode_sent_at, _DO_SET_MODE_CMD
            )
            result["error"] = (
                "无法进入任务模式（可用模式: "
                f"{sorted(mode_mapping.keys())}；当前 "
                f"{device.metadata.get('flight_mode', '未知')}）"
                + (f"：{reason}" if reason else "")
            )
            return result

        result["mode"] = mode
        result["started"] = True
        logger.info(
            "设备 %s 已进入 %s 模式，开始执行航点任务",
            device.device_id, mode,
        )
        return result

    async def _send_arm(
        self, channel: _MAVLinkChannel, target: int, arm: bool
    ) -> None:
        """发送解锁/上锁指令 (MAV_CMD_COMPONENT_ARM_DISARM)."""
        from pymavlink import mavutil  # noqa: PLC0415
        conn = channel.connection
        cmd = mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM

        def do_send() -> None:
            conn.mav.command_long_send(
                target, 0, cmd, 0, 1 if arm else 0, 0, 0, 0, 0, 0, 0,
            )

        await channel.send_to_device(target, do_send)
        logger.info("已发送%s指令 (target=%s)", "解锁" if arm else "上锁", target)

    async def _wait_arm_state(
        self, device: DeviceInfo, armed: bool, timeout: float = _ARM_TIMEOUT
    ) -> bool:
        """轮询心跳维护的解锁状态，等待飞控确认（解锁较慢，通常 3-10 秒）."""
        expect = "已解锁" if armed else "已锁定"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if device.metadata.get("arm_state") == expect:
                logger.info("设备 %s %s", device.device_id, expect)
                return True
            await asyncio.sleep(0.3)
        return False

    @staticmethod
    def _command_failure_reason(
        device: DeviceInfo, since: float, command_id: int | None = None
    ) -> str:
        """汇总飞控对指令的反馈（COMMAND_ACK 结果 + STATUSTEXT 文本）."""
        parts: list[str] = []
        ack = device.metadata.get("last_command_ack") or {}
        if (
            ack.get("time", 0) >= since
            and (command_id is None or ack.get("command") == command_id)
        ):
            parts.append(f"飞控返回 {ack.get('result_name', ack.get('result'))}")
        status = device.metadata.get("last_status_text") or {}
        if status.get("time", 0) >= since and status.get("text"):
            parts.append(f"飞控消息: {status['text']}")
        return "；".join(parts)

    async def _set_mode(
        self,
        channel: _MAVLinkChannel,
        device: DeviceInfo,
        target: int,
        mode_mapping: dict[str, int],
        candidates: tuple[str, ...],
    ) -> str | None:
        """按候选顺序切换飞行模式，并通过心跳确认，返回最终生效的模式名.

        优先发 SET_MODE；飞控未确认（如 AUTO 初始化失败自动回退原模式）时，
        改用 COMMAND_LONG 的 MAV_CMD_DO_SET_MODE 重试 —— 该方式飞控会回
        COMMAND_ACK，可拿到拒绝原因（如任务无效、EKF/GPS 未就绪）。
        """
        from pymavlink import mavutil  # noqa: PLC0415
        conn = channel.connection
        mavlink = mavutil.mavlink
        do_set_mode_cmd = getattr(mavlink, "MAV_CMD_DO_SET_MODE", 176)

        for name in candidates:
            mode_id = mode_mapping.get(name)
            if mode_id is None:
                continue

            def do_set(mid: int = mode_id) -> None:
                conn.mav.set_mode_send(
                    target, mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mid,
                )

            await channel.send_to_device(target, do_set)
            logger.info("已发送模式切换: %s (target=%s)", name, target)
            if await self._wait_flight_mode(device, (name.upper(),)):
                logger.info("已切换到 %s 模式", name)
                return name

            def do_set_cmd(mid: int = mode_id) -> None:
                conn.mav.command_long_send(
                    target, 0, do_set_mode_cmd, 0,
                    mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mid,
                    0, 0, 0, 0, 0,
                )

            await channel.send_to_device(target, do_set_cmd)
            logger.info(
                "SET_MODE 未生效，改用 MAV_CMD_DO_SET_MODE 重试: %s (target=%s)",
                name, target,
            )
            if await self._wait_flight_mode(device, (name.upper(),)):
                logger.info("已通过 MAV_CMD_DO_SET_MODE 切换到 %s 模式", name)
                return name
            logger.warning(
                "切换到 %s 未被飞控确认（当前 %s），尝试下一个候选",
                name, device.metadata.get("flight_mode", "未知"),
            )
        logger.warning(
            "模式切换失败：候选 %s 均未生效 (飞控模式表: %s)",
            candidates, sorted(mode_mapping.keys()),
        )
        return None

    async def _wait_flight_mode(
        self, device: DeviceInfo, expect: tuple[str, ...],
        timeout: float = _MODE_TIMEOUT,
    ) -> bool:
        """轮询心跳维护的飞行模式，等待飞控确认切换到目标任务模式."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if str(device.metadata.get("flight_mode", "")).upper() in expect:
                return True
            await asyncio.sleep(0.3)
        return False

    async def _upload_items(
        self,
        channel: _MAVLinkChannel,
        queue: asyncio.Queue,
        mavutil: Any,
        target_sys: int,
        target_comp: int,
        items: list[dict[str, Any]],
    ) -> Any:
        """上传任务项 — 请求-应答优先，盲发兜底，全程响应 MISSION_REQUEST.

        ``items`` 为飞控任务项列表（ArduPilot 时含 seq0 的 home 项）。

        - 标准飞控（PX4/ArduPilot）: MISSION_COUNT 后逐个发 MISSION_REQUEST，
          按请求序号应答对应任务项；
        - 推送式飞控: 超过 _MISSION_REQUEST_TIMEOUT 仍无请求时，按序盲发全部任务项；
        - 无论处于何种阶段，收到 MISSION_REQUEST 均立即应答（覆盖请求迟到、
          丢包重传场景），直到收到 MISSION_ACK 或总超时返回 None。
        """
        mavlink = mavutil.mavlink
        count = len(items)
        deadline = time.monotonic() + _MISSION_UPLOAD_TIMEOUT
        request_deadline = time.monotonic() + _MISSION_REQUEST_TIMEOUT
        blast_seq = 0
        saw_request = False

        while True:
            now = time.monotonic()
            if now >= deadline:
                return None

            if not saw_request and blast_seq < count:
                if now >= request_deadline:
                    # 盲发窗口：飞控不逐项请求，按序推送下一个任务项
                    await self._send_mission_item(
                        channel, mavutil, target_sys, target_comp,
                        blast_seq, items[blast_seq],
                    )
                    logger.info(
                        "已盲发任务项 seq=%d/%d%s",
                        blast_seq, count - 1, _item_label(items[blast_seq]),
                    )
                    blast_seq += 1
                    wait_for = _MISSION_ITEM_INTERVAL
                else:
                    # 仍在等待飞控首个 MISSION_REQUEST
                    wait_for = request_deadline - now
            else:
                # 请求-应答模式，或盲发完毕等待 MISSION_ACK
                wait_for = deadline - now

            if wait_for <= 0:
                continue

            msg = await _wait_mission_msg(
                queue,
                ("MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK"),
                wait_for,
            )
            if msg is None:
                continue  # 本轮等待超时，回到循环顶部处理盲发节奏/总超时

            if msg.get_type() == "MISSION_ACK":
                if msg.type == mavlink.MAV_MISSION_ERROR:
                    # 部分飞控以 ERROR + param1 指示需要重传的航点序号
                    retry_seq = getattr(msg, "param1", -1)
                    if isinstance(retry_seq, (int, float)) and 0 <= retry_seq < count:
                        await self._send_mission_item(
                            channel, mavutil, target_sys, target_comp,
                            int(retry_seq), items[int(retry_seq)],
                        )
                        continue
                return msg

            saw_request = True
            seq = msg.seq
            if 0 <= seq < count:
                await self._send_mission_item(
                    channel, mavutil, target_sys, target_comp, seq, items[seq],
                )
                logger.info(
                    "已发送任务项 seq=%d/%d%s (请求-应答)",
                    seq, count - 1, _item_label(items[seq]),
                )
            else:
                logger.warning("飞控请求了无效航点序号 seq=%s，忽略", seq)

    async def _send_mission_item(
        self,
        channel: _MAVLinkChannel,
        mavutil: Any,
        target_sys: int,
        target_comp: int,
        seq: int,
        wp: dict[str, Any],
    ) -> None:
        """构造并发送单个 MISSION_ITEM_INT 消息（经纬度*1e7，z 单位米）.

        参数按 MAVLink 规范映射:
            param1=hold_time(停留秒), param2=accept_radius(到达半径),
            param3=pass_radius(过点半径，0=直接飞向航点), param4=yaw(到达航向度).
        注意: param3/param4 必须为有限数值 —— 部分飞控会校验并拒绝 NaN
        (MAV_MISSION_INVALID_PARAM3/4)。yaw 默认 0（正北）；
        accept_radius 仅对 waypoint/loiter 默认 2，其余指令置 0。
        """
        mavlink = mavutil.mavlink
        conn = channel.connection
        cmd_name = str(wp.get("command", "waypoint")).lower()
        command = getattr(mavlink, _MISSION_COMMANDS.get(cmd_name, "MAV_CMD_NAV_WAYPOINT"))

        hold_time = float(wp.get("hold_time") or 0.0)
        accept_raw = wp.get("accept_radius")
        if accept_raw is not None:
            accept_radius = float(accept_raw)
        else:
            accept_radius = 2.0 if cmd_name in ("waypoint", "loiter") else 0.0
        pass_radius = float(wp.get("pass_radius") or 0.0)
        yaw_raw = wp.get("yaw")
        yaw = float(yaw_raw) if yaw_raw is not None else 0.0
        lat = float(wp.get("latitude", 0.0))
        lon = float(wp.get("longitude", 0.0))
        alt = float(wp.get("altitude", 10.0))

        def do_send() -> None:
            conn.mav.mission_item_int_send(
                target_sys, target_comp, seq,
                mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                command,
                0,  # current: 协议上传时恒为 0
                1,  # autocontinue: 自动继续下一航点
                hold_time,      # param1: 停留时间(s)
                accept_radius,  # param2: 到达半径(m)
                pass_radius,    # param3: 过点半径(m)，0=直接过点，不可为 NaN
                yaw,            # param4: 到达航向(°)，0=正北，不可为 NaN
                int(lat * 1e7), int(lon * 1e7),
                alt,            # z: 相对高度（米）
            )

        await channel.send_to_device(target_sys, do_send)

    async def reboot(
        self, device_id: str, component: str = "autopilot"
    ) -> dict[str, Any]:
        """远程重启飞控 / 伴飞计算机（MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN）."""
        device = self._devices.get(device_id)
        if not device:
            return {"success": False, "error": f"设备 {device_id} 未找到"}

        if component not in _REBOOT_COMPONENTS:
            return {"success": False, "error": f"不支持的重启对象: {component}"}

        if device.metadata.get("arm_state") == "已解锁":
            return {
                "success": False,
                "error": "设备已解锁，禁止远程重启，请先降落并上锁",
            }

        channel = self._find_channel_for_device(device)
        if channel is None:
            return {"success": False, "error": f"设备 {device_id} 无可用连接"}
        if channel.simulated:
            return {"success": False, "error": "模拟模式下不支持远程重启"}
        if not channel.connection:
            return {"success": False, "error": f"设备 {device_id} 无可用连接"}

        conn = channel.connection
        target = self._get_target_system(device)
        param1 = _REBOOT_COMPONENTS[component]

        def do_reboot() -> None:
            from pymavlink import mavutil  # noqa: PLC0415

            conn.mav.command_long_send(
                target, 0,
                mavutil.mavlink.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN,
                0, param1, 0, 0, 0, 0, 0, 0,
            )

        await channel.send_to_device(target, do_reboot)
        logger.info("已向设备 %s 下发重启指令 (component=%s)", device_id, component)
        return {"success": True, "device_id": device_id, "component": component}

    def get_status_texts(
        self,
        device_id: str,
        since: float = 0.0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """增量查询飞控文本日志（STATUSTEXT）."""
        device = self._devices.get(device_id)
        if not device:
            return {"success": False, "error": f"设备 {device_id} 未找到"}

        channel = self._find_channel_for_device(device)
        entries = (
            list(channel.status_texts.get(device_id, []))
            if channel else []
        )
        matched = sorted(
            (entry for entry in entries if entry["time"] > since),
            key=lambda entry: entry["time"],
        )
        selected = matched[:limit]
        next_since = selected[-1]["time"] if selected else since

        return {
            "success": True,
            "device_id": device_id,
            "since": since,
            "count": len(selected),
            "has_more": len(matched) > len(selected),
            "next_since": next_since,
            "status_texts": selected,
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
            box_id=self._box_id,
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
                ip = parse_ip(ch.entry.connection_string)
                port = parse_port(ch.entry.connection_string)
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
        """从设备 metadata 读取 MAVLink sysid（不再依赖设备 ID 字符串反解）."""
        sysid = device.metadata.get("sysid")
        if sysid is not None:
            return int(sysid)
        # 兼容旧格式 device_id（如 "drone_1"）
        try:
            return int(device.device_id.split("_")[-1])
        except (IndexError, ValueError):
            raise ValueError(
                f"无法确定设备 {device.device_id} 的 MAVLink system id"
            ) from None

    async def get_available_modes(self, device_id: str) -> dict[str, Any]:
        """查询指定无人机可用飞行模式列表（通过已建立连接的 mode_mapping 动态获取）."""
        device = self._devices.get(device_id)
        if not device:
            return {"success": False, "error": f"设备 {device_id} 未找到"}

        channel = self._find_channel_for_device(device)
        current_mode = device.metadata.get("flight_mode", "")

        if channel and channel.connection and not channel.simulated:
            conn = channel.connection

            def do_get() -> dict[str, int]:
                return dict(conn.mode_mapping() or {})

            try:
                mapping = await asyncio.get_event_loop().run_in_executor(None, do_get)
            except Exception as e:
                logger.exception("获取设备 %s 模式列表失败", device_id)
                return {"success": False, "error": f"获取模式列表失败: {e}"}
            return {
                "success": True,
                "device_id": device_id,
                "simulated": False,
                "current_mode": current_mode,
                "modes": sorted(mapping.keys()),
                "mode_mapping": mapping,
            }

        # 无真实连接时返回常见 ArduCopter 模式列表（仅供调试参考）
        return {
            "success": True,
            "device_id": device_id,
            "simulated": True,
            "current_mode": current_mode,
            "modes": list(_SIMULATED_MODES),
            "mode_mapping": {m: i for i, m in enumerate(_SIMULATED_MODES)},
            "message": "无真实 MAVLink 连接，返回常见 ArduCopter 模式列表（模拟）",
        }

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
