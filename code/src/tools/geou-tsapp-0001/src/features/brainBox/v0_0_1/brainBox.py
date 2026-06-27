"""
类脑盒子 brainBox 工具入口 — CbrainBox 类
平台通过 ProcessTask 调用 subfuncs 中定义的方法。

brainBox 通过 WebSocket 长连接与 manageServer 通信，
所有无人机管理与导航指令均由 manageServer 通过 WS 下发。
main.py 提供独立的 HTTP API 用于本地测试和调试。
"""

import asyncio
import json
import logging
import threading

logger = logging.getLogger("brainBox")

from core.manager import BrainBoxManager  # noqa: E402
from config.settings import get_settings  # noqa: E402
from utils.logger import setup_logging  # noqa: E402

setup_logging("brainBox")


class CbrainBox:
    """
    类脑盒子 brainBox 工具入口类。

    由平台框架通过 _load_train_version 自动实例化。
    实例化时自动启动 WS 客户端连接到 manageServer，
    心跳、状态上报、指令接收全部通过该长连接完成。
    """

    def __init__(self, node_cfg, process_comm, proc_modules_obj, progress_callback):
        self.node_cfg = node_cfg
        self.process_comm = process_comm
        self.proc_modules_obj = proc_modules_obj
        self.progress_callback = progress_callback

        self._manager = BrainBoxManager()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

        # Start async services in background daemon thread
        self._start_async_services()

    # ------------------------------------------------------------------
    #  Async lifecycle
    # ------------------------------------------------------------------

    def _start_async_services(self) -> None:
        """Launch the BrainBoxManager async services in a daemon thread."""
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_async_loop, name="brainbox-async", daemon=True
        )
        self._loop_thread.start()

    def _run_async_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._manager.start())
        # Keep the loop running for background tasks (heartbeat, WS connection, etc.)
        self._loop.run_forever()

    def _stop_async_services(self) -> None:
        """Gracefully stop async services."""
        if not self._loop:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._manager.stop(), self._loop)
            future.result(timeout=5.0)
        except Exception:
            logger.exception("Error stopping brainBox async services")
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ------------------------------------------------------------------
    #  配置 & 系统方法（仍可通过平台框架直接调用）
    # ------------------------------------------------------------------

    def get_config(self, params: dict) -> dict:
        """获取当前配置信息."""
        settings = get_settings()
        return {"code": 0, "msg": "success", "data": settings.to_dict()}

    def update_config(self, params: dict) -> dict:
        """更新配置信息（支持部分更新）."""
        settings = get_settings()
        settings.update_from_dict(params)
        return {"code": 0, "msg": "配置已更新", "data": settings.to_dict()}

    def status(self, params: dict) -> dict:
        """获取系统状态."""
        return self._manager.system_status()

    def protocols(self, params: dict) -> dict:
        """列出已注册通信协议."""
        return self._manager.list_protocols()

    # ------------------------------------------------------------------
    #  Cleanup
    # ------------------------------------------------------------------

    def onClose(self) -> None:
        """平台框架退出时调用."""
        self._stop_async_services()
        logger.info("brainBox 已退出")
