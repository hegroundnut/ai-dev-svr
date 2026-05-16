"""
pull_stream.py — 拉流统一入口

根据 node_cfg["source"] 路由到对应的拉流实现：
  - "local" → SPullLocal（直接返回本地/RTMP 地址）
  - "uva"   → SPullUVA（通过 HTTP API 获取无人机流地址）
"""

from .pull_local import SPullLocal
from .pull_uva import SPullUVA

_PULL_REGISTRY = {
    "local": SPullLocal,
    "uva": SPullUVA,
}


class SPullStream:
    """
    拉流统一入口。

    Parameters
    ----------
    node_cfg : dict
        必须包含 "source" 字段，以及对应拉流实现所需的字段。
    """

    def __init__(self, node_cfg: dict):
        self.node_cfg = node_cfg
        source = node_cfg.get("source", "").lower()
        cls = _PULL_REGISTRY.get(source)
        if cls is None:
            raise ValueError(
                f"不支持的拉流源类型: '{source}'，"
                f"可用类型: {list(_PULL_REGISTRY.keys())}"
            )
        self._impl = cls(node_cfg)

    def get_stream_url(self) -> str:
        """返回实际可用的流地址。"""
        return self._impl.get_stream_url()
