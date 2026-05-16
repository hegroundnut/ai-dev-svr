"""
process_frame_yolo.py — YOLO 目标检测处理器

使用 Ultralytics YOLO 对帧进行推理，检测人员（class 0）并在帧上绘制边框与计数。
"""

import os
import cv2

try:
    from ultralytics import YOLO
    _ULTRALYTICS_AVAILABLE = True
except ImportError:
    _ULTRALYTICS_AVAILABLE = False

try:
    import torch
    _CUDA_AVAILABLE = torch.cuda.is_available()
except Exception:
    _CUDA_AVAILABLE = False


class PProcessFrameYOLO:
    """
    YOLO 人员检测处理器。

    Parameters
    ----------
    cfg : dict
        必须包含：
          - model_folder : 模型文件所在目录
          - model_name   : 模型文件名（如 yolov8n.pt）
        可选：
          - conf_threshold : 置信度阈值，默认 0.4
          - target_class   : 目标类别索引，默认 0（人）
    """

    def __init__(self, cfg: dict):
        if not _ULTRALYTICS_AVAILABLE:
            raise ImportError("ultralytics 未安装，请执行: pip install ultralytics")

        self.cfg = cfg
        self.conf_threshold = float(cfg.get("conf_threshold", 0.4))
        self.target_class = int(cfg.get("target_class", 0))

        model_path = os.path.join(cfg["model_folder"], cfg["model_name"])
        self.model = YOLO(model_path)
        device = "cuda:0" if _CUDA_AVAILABLE else "cpu"
        self.model.to(device)

    def process(self, frame):
        """推理并绘制检测结果，返回标注后的帧。"""
        results = self.model(frame, verbose=False)
        detections = results[0].boxes
        count = 0

        for box in detections:
            cls = int(box.cls[0])
            conf = float(box.conf[0])
            if cls == self.target_class and conf > self.conf_threshold:
                count += 1
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(
                    frame, f"Person {conf:.2f}",
                    (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2,
                )

        cv2.putText(
            frame, f"Count: {count}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2,
        )
        return frame
