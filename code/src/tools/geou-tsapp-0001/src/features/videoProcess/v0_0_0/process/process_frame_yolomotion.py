"""
process_frame_yolomotion.py — YOLO + 运动检测处理器

结合 YOLO 目标检测与背景减除（MOG2）运动分析，仅标注正在移动的人员。

修复说明（相比原始版本）
-----------------------
原始代码在 process() 方法内每帧都重新创建 BackgroundSubtractorMOG2 实例，
导致背景模型无法积累历史帧，运动检测始终失效。
本版本将 backSub 提升为实例变量，在 __init__ 中初始化一次，跨帧持续学习背景。
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


class PProcessFrameYOLOMotion:
    """
    YOLO + MOG2 运动人员检测处理器。

    Parameters
    ----------
    cfg : dict
        必须包含：
          - model_folder : 模型文件所在目录
          - model_name   : 模型文件名
        可选：
          - conf_threshold    : YOLO 置信度阈值，默认 0.4
          - target_class      : 目标类别索引，默认 0（人）
          - motion_min_area   : 运动轮廓最小面积，默认 100
    """

    def __init__(self, cfg: dict):
        if not _ULTRALYTICS_AVAILABLE:
            raise ImportError("ultralytics 未安装，请执行: pip install ultralytics")

        self.cfg = cfg
        self.conf_threshold = float(cfg.get("conf_threshold", 0.4))
        self.target_class = int(cfg.get("target_class", 0))
        self.motion_min_area = int(cfg.get("motion_min_area", 100))

        model_path = os.path.join(cfg["model_folder"], cfg["model_name"])
        self.model = YOLO(model_path)
        device = "cuda:0" if _CUDA_AVAILABLE else "cpu"
        self.model.to(device)

        # 背景减除器：在实例化时创建一次，跨帧持续学习背景（修复原始 bug）
        self.back_sub = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=16, detectShadows=False
        )

    def process(self, frame):
        """推理并仅标注移动中的人员，返回标注后的帧。"""
        # 背景减除（持续更新背景模型）
        fg_mask = self.back_sub.apply(frame)
        contours, _ = cv2.findContours(
            fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        motion_contours = [c for c in contours if cv2.contourArea(c) > self.motion_min_area]

        # YOLO 推理
        results = self.model(frame, verbose=False)
        detections = results[0].boxes
        count = 0

        for box in detections:
            cls = int(box.cls[0])
            conf = float(box.conf[0])
            if cls != self.target_class or conf < self.conf_threshold:
                continue

            x1, y1, x2, y2 = map(int, box.xyxy[0])
            cx = x1 + (x2 - x1) // 2
            cy = y1 + (y2 - y1) // 2

            # 判断检测框中心是否落在运动区域内
            moving = any(
                cv2.pointPolygonTest(c, (cx, cy), False) >= 0
                for c in motion_contours
            )
            if moving:
                count += 1
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(
                    frame, f"Moving {conf:.2f}",
                    (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2,
                )

        cv2.putText(
            frame, f"Moving Count: {count}",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2,
        )
        return frame
