"""
process_frame.py — 帧处理分发器

根据 cfg["type"] 将帧路由到对应的模型处理器。
当前支持：
  - "yolo"        → PProcessFrameYOLO
  - "yolomotion"  → PProcessFrameYOLOMotion

扩展方法：在 _REGISTRY 中注册新的 type → 处理器类即可。
"""

from .process_frame_yolo import PProcessFrameYOLO
from .process_frame_yolomotion import PProcessFrameYOLOMotion

# 类型注册表，便于后续扩展
_REGISTRY = {
    "yolo": PProcessFrameYOLO,
    "yolomotion": PProcessFrameYOLOMotion,
}


class PProcessFrame:
    """
    帧处理分发器。

    Parameters
    ----------
    cfg : dict
        模型配置，必须包含 "type" 字段，以及对应处理器所需的其他字段。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        model_type = cfg.get("type", "")
        cls = _REGISTRY.get(model_type)
        if cls is None:
            raise ValueError(
                f"不支持的模型类型: '{model_type}'，"
                f"可用类型: {list(_REGISTRY.keys())}"
            )
        self.process_model = cls(cfg)

    def process(self, frame):
        """对单帧执行推理，返回标注后的帧。"""
        return self.process_model.process(frame)
