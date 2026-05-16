"""
push_stream.py — 推流统一入口

根据 node_cfg["mode"] 路由到对应的推流实现：
  - "ffmpeg" → FFmpegStreamer（通过 FFmpeg 子进程推送 RTMP 流）

扩展方法：在 _PUSH_REGISTRY 中注册新的 mode → 推流类即可。
"""

from .push_ffmpeg import FFmpegStreamer

_PUSH_REGISTRY = {
    "ffmpeg": FFmpegStreamer,
}


class SPushStream:
    """
    推流统一入口。

    Parameters
    ----------
    node_cfg : dict
        必须包含 "mode" 字段，以及对应推流实现所需的字段。
        对于 ffmpeg 模式，还需要 "url" 字段（RTMP 推流地址）。
    """

    def __init__(self, node_cfg: dict):
        self.node_cfg = node_cfg
        mode = node_cfg.get("mode", "").lower()
        cls = _PUSH_REGISTRY.get(mode)
        if cls is None:
            raise ValueError(
                f"不支持的推流模式: '{mode}'，"
                f"可用模式: {list(_PUSH_REGISTRY.keys())}"
            )
        self._impl = cls(node_cfg.get("url", ""))

    def set_frame_info(self, width: int, height: int, fps: int):
        self._impl.set_frame_info(width, height, fps)

    def start(self):
        self._impl.start()

    def write_frame(self, frame) -> bool:
        return self._impl.write_frame(frame)

    def stop(self):
        self._impl.stop()
