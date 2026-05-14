"""通信协议注册中心 — 支持动态注册和发现通信协议."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from models.device import DeviceInfo

logger = logging.getLogger("brainBox.core.protocol_registry")


class DeviceProtocol(ABC):
    """
    设备通信协议抽象基类.

    所有设备通信方式（MAVLink, 串口, CAN 等）都实现此接口，
    实现与具体通信方式的解耦。
    """

    @property
    @abstractmethod
    def protocol_name(self) -> str:
        """协议名称标识."""

    @abstractmethod
    async def connect(self) -> None:
        """建立连接."""

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接."""

    @abstractmethod
    async def scan_devices(self) -> list[DeviceInfo]:
        """扫描发现设备."""

    @abstractmethod
    async def send_command(self, device_id: str, command: dict[str, Any]) -> dict[str, Any]:
        """向设备发送控制指令."""

    @abstractmethod
    async def get_device_status(self, device_id: str) -> DeviceInfo | None:
        """获取设备实时状态."""


class ProtocolRegistry:
    """
    通信协议注册中心.

    使用注册模式管理所有通信协议，支持运行时动态注册新协议。
    """

    def __init__(self) -> None:
        self._protocols: dict[str, DeviceProtocol] = {}

    def register(self, protocol: DeviceProtocol) -> None:
        """注册通信协议."""
        name = protocol.protocol_name
        if name in self._protocols:
            logger.warning("协议 '%s' 已存在，将被覆盖", name)
        self._protocols[name] = protocol
        logger.info("已注册通信协议: %s", name)

    def unregister(self, name: str) -> None:
        """注销通信协议."""
        if name in self._protocols:
            del self._protocols[name]
            logger.info("已注销通信协议: %s", name)

    def get(self, name: str) -> DeviceProtocol | None:
        """获取指定协议."""
        return self._protocols.get(name)

    def list_protocols(self) -> list[str]:
        """列出所有已注册协议."""
        return list(self._protocols.keys())

    async def connect_all(self) -> None:
        """连接所有协议."""
        for name, proto in self._protocols.items():
            try:
                await proto.connect()
                logger.info("协议 '%s' 连接成功", name)
            except Exception:
                logger.exception("协议 '%s' 连接失败", name)

    async def disconnect_all(self) -> None:
        """断开所有协议."""
        for name, proto in self._protocols.items():
            try:
                await proto.disconnect()
                logger.info("协议 '%s' 已断开", name)
            except Exception:
                logger.exception("协议 '%s' 断开失败", name)

    async def scan_all_devices(self) -> dict[str, Any]:
        """通过所有协议扫描设备."""
        results: dict[str, Any] = {}
        for name, proto in self._protocols.items():
            try:
                devices = await proto.scan_devices()
                results[name] = [d.to_dict() for d in devices]
            except Exception:
                logger.exception("协议 '%s' 扫描设备失败", name)
                results[name] = []
        return results