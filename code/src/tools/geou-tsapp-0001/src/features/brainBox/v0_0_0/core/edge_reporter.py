"""边缘服务上报器 — 通过 WebSocket 周期性心跳和无人机状态上报."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, TYPE_CHECKING

from core.drone_manager import DroneManager

if TYPE_CHECKING:
    from core.ws_client import EdgeWSClient

logger = logging.getLogger("brainBox.core.edge_reporter")


class EdgeReporter:
    """
    边缘服务上报器 — 通过 WebSocket 长连接上报。

    负责：
    1. 周期性发送心跳到边缘控制服务
    2. 周期性上报无人机状态信息
    3. 接收到无人机状态变化时即时转发
    """

    def __init__(
        self,
        ws_client: "EdgeWSClient",
        drone_manager: DroneManager,
        heartbeat_interval: float = 5.0,
        report_interval: float = 2.0,
        box_id: str = "brain_box_001",
    ) -> None:
        self._ws_client = ws_client
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
        await self._ws_client.send_event("trajectory_report", payload)
        logger.info("轨迹已上报到边缘服务 (via WS)")
        return {"success": True}

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
                    "online_devices": [
                        {
                            "device_id": d["device_id"],
                            "status": d["status"],
                            "flight_mode": d.get("metadata", {}).get("flight_mode", ""),
                            "arm_state": d.get("metadata", {}).get("arm_state", ""),
                        }
                        for d in summary["devices"]
                        if d["status"] == "online"
                    ],
                }
                await self._ws_client.send_event("heartbeat", payload)
                logger.debug("心跳已发送")
            except Exception:
                logger.exception("心跳发送失败")
            await asyncio.sleep(self._heartbeat_interval)

    async def _report_loop(self) -> None:
        """周期性上报无人机状态（含完整传感器 metadata）."""
        while self._running:
            try:
                summary = self._drone_manager.get_all_devices_summary()
                if summary["total"] > 0:
                    payload = {
                        "box_id": self._box_id,
                        "timestamp": time.time(),
                        "devices": summary["devices"],
                    }
                    await self._ws_client.send_event("drone_report", payload)
                    logger.debug("无人机状态已上报 (via WS): %d 台", summary["total"])
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
            await self._ws_client.send_event("drone_report", payload)
            logger.info("设备状态变化已即时上报 (via WS): %s", device.device_id)
        except Exception:
            logger.exception("状态变化上报失败")
