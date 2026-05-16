"""
manager.py — 视频处理任务管理器

负责任务的生命周期管理：创建、运行、查询、停止。

多模型配置说明
--------------
models_cfg 支持两种传参方式：

方式一（推荐）：直接传入结构化列表
    "models_cfg": [
        {"model_folder": "/path/to/weights", "model_name": "yolov8n.pt", "type": "yolo"},
        {"model_folder": "/path/to/weights", "model_name": "yolov8s.pt", "type": "yolomotion"}
    ]

方式二（兼容旧版）：展开为带下标的扁平参数
    "model_folder_0": "/path/to/weights",
    "model_name_0": "yolov8n.pt",
    "model_type_0": "yolo",
    "model_folder_1": "/path/to/weights",
    "model_name_1": "yolov8s.pt",
    "model_type_1": "yolomotion"

无模型直通模式
--------------
当 models_cfg 为空列表或不传时，Pipeline 直接转发原始帧，不调用任何 AI 模型。
此模式适用于纯推拉流转发场景，CPU/GPU 占用极低。

缓冲带大小
----------
通过 start_task 的 buffer_size 参数控制（默认 30 帧）。
较小的值可降低延迟，较大的值可提升处理速度不稳定时的流畅度。
"""

import time
import threading
import logging
import os
import sys
from typing import Dict, Any

logger = logging.getLogger(__name__)

# 确保可以导入同级目录下的 stream / process 模块
_feat_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _feat_dir not in sys.path:
    sys.path.insert(0, _feat_dir)

from stream.pull_stream import SPullStream
from stream.push_stream import SPushStream
from process.pipeline import PPipeline

_DEFAULT_BUFFER_SIZE = 30


class VideoProcessManager:
    """视频处理任务管理器（单例建议，每个工具实例持有一个）。"""

    def __init__(self):
        # task_id -> {"thread", "pipeline", "status", "info", ...}
        self.tasks: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_models_cfg(params: Dict[str, Any]):
        """解析 models_cfg，兼容结构化列表和扁平参数两种方式。"""
        models_cfg = params.get("models_cfg") or []
        if not models_cfg:
            # 兼容旧版扁平参数
            i = 0
            while f"model_folder_{i}" in params:
                models_cfg.append({
                    "model_folder": params.get(f"model_folder_{i}"),
                    "model_name":   params.get(f"model_name_{i}"),
                    "type":         params.get(f"model_type_{i}"),
                })
                i += 1
        return models_cfg

    @staticmethod
    def _build_push_cfg(params: Dict[str, Any]) -> Dict[str, Any]:
        """构建推流配置，自动拼接 RTMP URL。"""
        push_cfg = {
            "mode":       params.get("stream_push_mode", "ffmpeg"),
            "type":       params.get("stream_push_type", "rtmp"),
            "srs_addr":   params.get("stream_push_srs_addr", "localhost"),
            "srs_port":   params.get("stream_push_srs_port", 1935),
            "stream_key": params.get("stream_push_stream_key", "detected"),
            "url":        params.get("stream_push_url", ""),
        }
        if not push_cfg["url"]:
            addr = push_cfg["srs_addr"]
            port = push_cfg["srs_port"]
            key  = push_cfg["stream_key"]
            push_cfg["url"] = f"rtmp://{addr}:{port}/live/{key}"
        return push_cfg

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def start_task(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        启动视频处理任务。

        关键参数
        --------
        pull_source : str
            拉流源类型，"local" 或 "uva"。
        pull_url : str
            拉流地址（local 模式直接使用；uva 模式为 API 查询地址）。
        pull_type : str
            流协议类型，如 "rtmp"、"webrtc"。
        models_cfg : list, 可选
            模型配置列表。为空时进入无模型直通模式。
        output_type : str
            输出类型："stream"（推流）、"json"（结果查询）、"mysql"（入库）。
        buffer_size : int, 可选
            缓冲带大小（帧数），默认 30。
        stream_push_mode : str
            推流模式，目前支持 "ffmpeg"。
        stream_push_srs_addr / stream_push_srs_port / stream_push_stream_key : str/int
            SRS 服务器地址、端口、流名（当 stream_push_url 未填时自动拼接）。
        stream_push_url : str, 可选
            完整推流地址，填写后忽略上面三个参数。
        """
        tool_pkg = params.get("tool_package_snumber", "unknown")
        version  = params.get("version", "0.0.0")
        task_id  = f"{tool_pkg}-{int(time.time())}"

        models_cfg  = self._parse_models_cfg(params)
        buffer_size = int(params.get("buffer_size", _DEFAULT_BUFFER_SIZE))
        output_type = params.get("output_type", "stream")

        pull_cfg = {
            "source": params.get("pull_source", ""),
            "url":    params.get("pull_url", ""),
            "type":   params.get("pull_type", ""),
        }

        if output_type == "stream":
            push_cfg    = self._build_push_cfg(params)
            pull_stream = SPullStream(pull_cfg)
            push_stream = SPushStream(push_cfg)
            pipeline    = PPipeline(models_cfg, pull_stream, push_stream, buffer_size=buffer_size)

            def _run():
                try:
                    pipeline.start()
                    if task_id in self.tasks:
                        self.tasks[task_id]["status"] = "finished"
                except Exception as exc:
                    logger.error(f"任务 {task_id} 异常: {exc}")
                    if task_id in self.tasks:
                        self.tasks[task_id]["status"] = "error"
                        self.tasks[task_id]["info"]["error_msg"] = str(exc)

            t = threading.Thread(target=_run, daemon=True)
            t.start()

            self.tasks[task_id] = {
                "tool_package_snumber": tool_pkg,
                "version":  version,
                "status":   "running",
                "thread":   t,
                "pipeline": pipeline,
                "info": {
                    "input_url":  pull_stream.get_stream_url(),
                    "output_url": push_cfg["url"],
                    "models_count": len(models_cfg),
                    "buffer_size":  buffer_size,
                    "mode": "passthrough" if not models_cfg else "ai_process",
                },
            }
            return {
                "code": 0,
                "msg": "任务启动成功",
                "data": {
                    "task_id":    task_id,
                    "output_url": push_cfg["url"],
                    "mode":       "passthrough" if not models_cfg else "ai_process",
                },
            }

        elif output_type == "json":
            self.tasks[task_id] = {
                "tool_package_snumber": tool_pkg,
                "version": version,
                "status":  "running",
                "thread":  None,
                "pipeline": None,
                "info": {"result_api": f"/api/videoProcess/CvideoProcess/get_result"},
            }
            return {
                "code": 0,
                "msg": "任务启动成功",
                "data": {"task_id": task_id, "result_api": f"/api/videoProcess/CvideoProcess/get_result"},
            }

        elif output_type == "mysql":
            self.tasks[task_id] = {
                "tool_package_snumber": tool_pkg,
                "version": version,
                "status":  "running",
                "thread":  None,
                "pipeline": None,
                "info": {"mysql_table": task_id},
            }
            return {
                "code": 0,
                "msg": "任务启动成功",
                "data": {"task_id": task_id, "mysql_table": task_id},
            }

        else:
            return {"code": -1, "msg": f"不支持的输出类型: {output_type}", "data": {}}

    def get_result(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """查询任务状态及结果。"""
        task_id = params.get("task_id", "")
        task = self.tasks.get(task_id)
        if not task:
            return {"code": -1, "msg": f"任务不存在: {task_id}", "data": {}}
        return {
            "code": 0,
            "msg": "success",
            "data": {"status": task["status"], **task["info"]},
        }

    def list_tasks(self, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """列出所有任务。"""
        data = [
            {"task_id": tid, "status": t["status"], **t["info"]}
            for tid, t in self.tasks.items()
        ]
        return {"code": 0, "msg": "success", "data": data}

    def stop_task(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """停止并删除指定任务。"""
        task_id = params.get("task_id", "")
        task = self.tasks.get(task_id)
        if not task:
            return {"code": -1, "msg": f"任务不存在: {task_id}", "data": {}}
        pipeline = task.get("pipeline")
        if pipeline and hasattr(pipeline, "stop"):
            pipeline.stop()
        task["status"] = "stopped"
        self.tasks.pop(task_id, None)
        return {"code": 0, "msg": "success", "data": {"task_id": task_id, "status": "stopped"}}

    def get_stream_url(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """获取任务的输出流地址。"""
        task_id = params.get("task_id", "")
        task = self.tasks.get(task_id)
        if not task:
            return {"code": -1, "msg": f"任务不存在: {task_id}", "data": {}}
        return {
            "code": 0,
            "msg": "success",
            "data": {"stream_url": task["info"].get("output_url", "")},
        }
