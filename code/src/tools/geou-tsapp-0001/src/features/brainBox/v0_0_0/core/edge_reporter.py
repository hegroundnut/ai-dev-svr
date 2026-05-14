"""边缘服务上报器 — 周期性心跳和无人机状态上报."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import httpx
from config.settings import EdgeServerConfig

from core.drone_manager import DroneManager

logger = logging.getLogger("brainBox.core.edge_reporter")


class EdgeClient:
    """
    边缘控制服务客户端.

    所有与边缘控制服务的通信都通过 POST 请求，
    URL 路径完全可配置。
    """

    def __init__(self, config: EdgeServerConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """初始化 HTTP 客户端."""
        self._client = httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=self._config.timeout,
        )
        logger.info("边缘服务客户端已启动: %s", self._config.base_url)

    async def stop(self) -> None:
        """关闭 HTTP 客户端."""
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("边缘服务客户端已停止")

    async def send_heartbeat(self, data: dict[str, Any]) -> dict[str, Any]:
        """发送心跳到边缘服务."""
        return await self._post(self._config.heartbeat_path, data)

    async def report_drone_status(self, data: dict[str, Any]) -> dict[str, Any]:
        """上报无人机状态到边缘服务."""
        return await self._post(self._config.drone_report_path, data)

    async def report_trajectory(self, data: dict[str, Any]) -> dict[str, Any]:
        """上报导航轨迹到边缘服务."""
        return await self._post(self._config.trajectory_report_path, data)

    async def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """统一 POST 请求."""
        if not self._client:
            logger.warning("边缘服务客户端未初始化")
            return {"success": False, "error": "客户端未初始化"}
        try:
            response = await self._client.post(path, json=data)
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            logger.debug("POST %s 成功: %s", path, response.status_code)
            return result
        except httpx.HTTPStatusError as e:
            logger.error("POST %s 失败: %s", path, e.response.status_code)
            return {"success": False, "error": str(e), "status_code": e.response.status_code}
        except httpx.RequestError as e:
            logger.error("POST %s 请求异常: %s", path, e)
            return {"success": False, "error": str(e)}


class EdgeReporter:
    """
    边缘服务上报器.

    负责：
    1. 周期性发送心跳到边缘控制服务
    2. 周期性上报无人机状态信息
    3. 接收到无人机状态变化时即时转发
    """

    def __init__(
        self,
        edge_client: EdgeClient,
        drone_manager: DroneManager,
        heartbeat_interval: float = 5.0,
        report_interval: float = 2.0,
        box_id: str = "brain_box_001",
    ) -> None:
        self._edge_client = edge_client
        self._drone_manager = drone_manager
        self._heartbeat_interval = heartbeat_interval
        self._report_interval = report_interval
        self._box_id = box_id
        self._running = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._report_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动上报任务."""
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._report_task = asyncio.create_task(self._report_loop())
        self._drone_manager.on_status_change(self._on_drone_status_change)
        logger.info(
            "边缘上报器已启动 (心跳=%.1fs, 状态=%.1fs)",
            self._heartbeat_interval, self._report_interval,
        )

    async def stop(self) -> None:
        """停止上报任务."""
        self._running = False
        for task in (self._heartbeat_task, self._report_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        logger.info("边缘上报器已停止")

    async def report_trajectory(self, trajectory_data: dict[str, Any]) -> dict[str, Any]:
        """上报导航轨迹到边缘服务."""
        payload = {
            "box_id": self._box_id,
            "timestamp": time.time(),
            "trajectory": trajectory_data,
        }
        result = await self._edge_client.report_trajectory(payload)
        logger.info("轨迹已上报到边缘服务: %s", result)
        return result

    async def _heartbeat_loop(self) -> None:
        """周期性心跳."""
        while self._running:
            try:
                summary = self._drone_manager.get_all_devices_summary()
                payload = {
                    "box_id": self._box_id,
                    "timestamp": time.time(),
                    "status": "running",
                    "drone_count": summary["total"],
                    "online_count": summary["online"],
                }
                await self._edge_client.send_heartbeat(payload)
                logger.debug("心跳已发送")
            except Exception:
                logger.exception("心跳发送失败")
            await asyncio.sleep(self._heartbeat_interval)

    async def _report_loop(self) -> None:
        """周期性上报无人机状态."""
        while self._running:
            try:
                summary = self._drone_manager.get_all_devices_summary()
                if summary["total"] > 0:
                    payload = {
                        "box_id": self._box_id,
                        "timestamp": time.time(),
                        "devices": summary["devices"],
                    }
                    await self._edge_client.report_drone_status(payload)
                    logger.debug("无人机状态已上报: %d 台", summary["total"])
            except Exception:
                logger.exception("状态上报失败")
            await asyncio.sleep(self._report_interval)

    async def _on_drone_status_change(self, device: Any) -> None:
        """设备状态变化时即时上报."""
        try:
            payload = {
                "box_id": self._box_id,
                "timestamp": time.time(),
                "event": "status_change",
                "device": device.to_dict(),
            }
            await self._edge_client.report_drone_status(payload)
            logger.info("设备状态变化已即时上报: %s", device.device_id)
        except Exception:
            logger.exception("状态变化上报失败")