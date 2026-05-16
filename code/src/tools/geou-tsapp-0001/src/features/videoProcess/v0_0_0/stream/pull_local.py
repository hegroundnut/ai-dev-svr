"""
pull_local.py — 本地/直连流拉取

直接返回配置中的 URL，适用于本地文件、RTMP 流、RTSP 流等可被 OpenCV 直接打开的地址。
"""


class SPullLocal:
    """
    本地流拉取。

    Parameters
    ----------
    node_cfg : dict
        必须包含 "url" 字段，值为 OpenCV 可直接打开的流地址或文件路径。
    """

    def __init__(self, node_cfg: dict):
        self.node_cfg = node_cfg

    def get_stream_url(self) -> str:
        return self.node_cfg.get("url", "")
