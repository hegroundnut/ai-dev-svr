"""
videoProcess.py — 视频处理工具入口类

遵循 ai-dev-svr 平台工具规范：
  - 构造函数签名：(node_cfg, process_comm, proc_modules_obj, progress_callback)
  - 每个公开方法接收 params: dict，返回 {"code": int, "msg": str, "data": dict}
  - 通过 _handle_result 统一回调 progress_callback
"""

import json
import logging
import os
import sys

logger = logging.getLogger(__name__)

# 确保 core 目录可被导入
_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

from core.manager import VideoProcessManager


class CvideoProcess:
    """
    视频处理工具入口类。

    由平台框架（Process.py）通过 loading_tools 配置动态加载并实例化。
    """

    def __init__(self, node_cfg, process_comm, proc_modules_obj, progress_callback):
        self.node_cfg = node_cfg
        self.process_comm = process_comm
        self.proc_modules_obj = proc_modules_obj
        self.progress_callback = progress_callback
        self._manager = VideoProcessManager()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _handle_result(self, func_name: str, result: dict) -> dict:
        """统一处理返回结果，回调进度并返回结果字典。"""
        if result.get("code", -1) == 0:
            self.progress_callback(
                100,
                json.dumps(result, ensure_ascii=False, default=str),
                "ok",
            )
        else:
            self.progress_callback(
                -1,
                json.dumps(result, ensure_ascii=False, default=str),
                "failed",
            )
        return result

    # ------------------------------------------------------------------
    # 公开接口（与 toolconfig.yml 中 subfuncs 对应）
    # ------------------------------------------------------------------
    def start_task(self, params: dict) -> dict:
        """启动视频处理任务。"""
        self.progress_callback(10, "正在启动视频处理任务")
        result = self._manager.start_task(params)
        return self._handle_result("start_task", result)

    def get_result(self, params: dict) -> dict:
        """查询任务状态及结果。"""
        self.progress_callback(10, "正在查询任务结果")
        result = self._manager.get_result(params)
        return self._handle_result("get_result", result)

    def list_tasks(self, params: dict) -> dict:
        """列出所有任务。"""
        self.progress_callback(10, "正在查询任务列表")
        result = self._manager.list_tasks(params)
        return self._handle_result("list_tasks", result)

    def stop_task(self, params: dict) -> dict:
        """停止并删除指定任务。"""
        self.progress_callback(10, "正在停止任务")
        result = self._manager.stop_task(params)
        return self._handle_result("stop_task", result)

    def get_stream_url(self, params: dict) -> dict:
        """获取任务的输出流地址。"""
        self.progress_callback(10, "正在获取流地址")
        result = self._manager.get_stream_url(params)
        return self._handle_result("get_stream_url", result)
