"""可配置的系统设置，支持 YAML 文件和环境变量覆盖."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class EdgeServerConfig:
    """边缘控制服务器配置 (WebSocket 长连接)."""

    ws_url: str = "ws://127.0.0.1:15002"
    heartbeat_interval: float = 5.0
    report_interval: float = 2.0
    timeout: float = 10.0


@dataclass
class MAVLinkConnectionEntry:
    """单个 MAVLink 连接配置."""

    connection_string: str = "udpin:0.0.0.0:14550"
    label: str = ""
    baud_rate: int = 57600


@dataclass
class ReconnectConfig:
    """TCP 连接自动重连配置.

    仅对 TCP 主动连接通道生效，UDP 通道不适用。
    """

    enabled: bool = True
    max_attempts: int = 5
    base_delay: float = 2.0
    max_delay: float = 60.0
    idle_timeout: float = 10.0


@dataclass
class MAVLinkConfig:
    """MAVLink 通信配置（支持多连接）."""

    connection_string: str = "udpin:0.0.0.0:14550"
    connections: list[MAVLinkConnectionEntry] = field(default_factory=list)
    system_id: int = 255
    component_id: int = 0
    scan_interval: float = 3.0
    heartbeat_timeout: float = 10.0
    baud_rate: int = 57600
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)

    def get_connections(self) -> list[MAVLinkConnectionEntry]:
        """返回所有连接配置; 若无显式 connections 则用 connection_string 回退."""
        if self.connections:
            return list(self.connections)
        return [MAVLinkConnectionEntry(
            connection_string=self.connection_string,
            label="default",
            baud_rate=self.baud_rate,
        )]


@dataclass
class LoggingConfig:
    """日志配置."""

    level: str = "INFO"
    console_enabled: bool = True
    file_enabled: bool = True
    log_dir: str = "logs"
    log_file: str = "brain_box.log"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


@dataclass
class StorageConfig:
    """本地存储配置."""

    db_path: str = "data/brain_box.db"
    device_evict_timeout: float = 60.0


def _apply_section(data: dict[str, Any], key: str, target: Any) -> None:
    """将 data[key] 中的字段赋值到 target 对象."""
    section = data.get(key)
    if not section:
        return
    for k, v in section.items():
        if hasattr(target, k):
            setattr(target, k, v)


@dataclass
class Settings:
    """全局配置（不含 server 段，server 由 main.py 独立管理）."""

    box_id: str = "brain_box_002"
    edge: EdgeServerConfig = field(default_factory=EdgeServerConfig)
    mavlink: MAVLinkConfig = field(default_factory=MAVLinkConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        """从 YAML 文件加载配置."""
        path = Path(path)
        if not path.exists():
            return cls()
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> Settings:
        settings = cls()
        if "box_id" in data:
            settings.box_id = data["box_id"]
        _apply_section(data, "edge", settings.edge)
        cls._apply_mavlink_section(data, settings)
        _apply_section(data, "logging", settings.logging)
        _apply_section(data, "storage", settings.storage)
        return settings

    @staticmethod
    def _apply_mavlink_section(data: dict[str, Any], settings: Settings) -> None:
        if "mavlink" not in data:
            return
        mav_data = dict(data["mavlink"])
        conn_list_raw = mav_data.pop("connections", None)
        reconnect_raw = mav_data.pop("reconnect", None)
        _apply_section({"mavlink": mav_data}, "mavlink", settings.mavlink)
        if conn_list_raw and isinstance(conn_list_raw, list):
            settings.mavlink.connections = [
                MAVLinkConnectionEntry(
                    connection_string=c.get("connection_string", "udpin:0.0.0.0:14550"),
                    label=c.get("label", ""),
                    baud_rate=c.get("baud_rate", settings.mavlink.baud_rate),
                )
                for c in conn_list_raw
            ]
        if reconnect_raw and isinstance(reconnect_raw, dict):
            _apply_section({"reconnect": reconnect_raw}, "reconnect", settings.mavlink.reconnect)

    def apply_env_overrides(self) -> None:
        """环境变量覆盖配置 (优先级最高)."""
        box_id_env = os.environ.get("BRAIN_BOX_ID")
        if box_id_env:
            self.box_id = box_id_env
        env_map = {
            "BRAIN_BOX_EDGE_WS_URL": ("edge", "ws_url"),
            "BRAIN_BOX_EDGE_HEARTBEAT_INTERVAL": ("edge", "heartbeat_interval"),
            "BRAIN_BOX_MAVLINK_CONNECTION": ("mavlink", "connection_string"),
            "BRAIN_BOX_MAVLINK_SYSTEM_ID": ("mavlink", "system_id"),
            "BRAIN_BOX_MAVLINK_RECONNECT_ENABLED": ("mavlink", "reconnect", "enabled"),
            "BRAIN_BOX_MAVLINK_RECONNECT_MAX_ATTEMPTS": ("mavlink", "reconnect", "max_attempts"),
            "BRAIN_BOX_MAVLINK_RECONNECT_BASE_DELAY": ("mavlink", "reconnect", "base_delay"),
            "BRAIN_BOX_MAVLINK_RECONNECT_MAX_DELAY": ("mavlink", "reconnect", "max_delay"),
            "BRAIN_BOX_MAVLINK_RECONNECT_IDLE_TIMEOUT": ("mavlink", "reconnect", "idle_timeout"),
            "BRAIN_BOX_LOG_LEVEL": ("logging", "level"),
            "BRAIN_BOX_LOG_DIR": ("logging", "log_dir"),
            "BRAIN_BOX_DB_PATH": ("storage", "db_path"),
            "BRAIN_BOX_DEVICE_EVICT_TIMEOUT": ("storage", "device_evict_timeout"),
        }
        for env_key, target in env_map.items():
            val = os.environ.get(env_key)
            if val is None:
                continue
            *sections, attr = target
            obj = self
            for sec in sections:
                obj = getattr(obj, sec)
            current = getattr(obj, attr)
            if isinstance(current, int):
                setattr(obj, attr, int(val))
            elif isinstance(current, float):
                setattr(obj, attr, float(val))
            elif isinstance(current, bool):
                setattr(obj, attr, val.lower() in ("true", "1", "yes"))
            else:
                setattr(obj, attr, val)

    def to_dict(self) -> dict[str, Any]:
        """将配置导出为字典."""
        result: dict[str, Any] = {}
        result["box_id"] = self.box_id
        result["edge"] = asdict(self.edge)
        mav = asdict(self.mavlink)
        mav["connections"] = [asdict(c) for c in self.mavlink.connections]
        result["mavlink"] = mav
        result["logging"] = asdict(self.logging)
        result["storage"] = asdict(self.storage)
        return result

    def update_from_dict(self, data: dict[str, Any]) -> None:
        """从字典更新配置（支持部分更新）."""
        if "box_id" in data:
            self.box_id = data["box_id"]
        if "edge" in data:
            _apply_section(data, "edge", self.edge)
        if "mavlink" in data:
            self._apply_mavlink_section(data, self)
        if "logging" in data:
            _apply_section(data, "logging", self.logging)
        if "storage" in data:
            _apply_section(data, "storage", self.storage)


_settings: Settings | None = None


def get_settings(config_path: str | Path | None = None) -> Settings:
    """获取全局单例配置."""
    global _settings  # noqa: PLW0603
    if _settings is None:
        path = config_path or os.environ.get("BRAIN_BOX_CONFIG", "config.yaml")
        _settings = Settings.from_yaml(path)
        _settings.apply_env_overrides()
    return _settings


def reset_settings() -> None:
    """重置配置 (测试用)."""
    global _settings  # noqa: PLW0603
    _settings = None
