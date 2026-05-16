"""
pipeline.py — 视频处理流水线

缓冲策略说明
-----------
原始实现使用有界 FIFO 队列，当处理速度低于采集速度时，队列会被旧帧填满，
导致推流延迟持续累积（"帧积压"问题）。

本实现改为 **抽帧（Frame-Sampling）缓冲**：
- frame_queue / processed_queue 均为有界队列，大小由 buffer_size 参数控制。
- 当队列已满时，**丢弃队列中最旧的帧**，再放入新帧，始终保持缓冲区内是最新帧。
- 这样推流端看到的延迟始终约等于 buffer_size 帧的处理时间，而不会无限累积。

无模型直通
----------
当 models_cfg 为空列表或 None 时，跳过所有模型推理，直接将原始帧写入推流队列，
实现纯粹的推拉流转发（零 AI 处理开销）。
"""

import cv2
import queue
import threading
import logging
import time

from .process_frame import PProcessFrame

logger = logging.getLogger(__name__)

_DEFAULT_BUFFER_SIZE = 30  # 默认缓冲带大小（帧数）


def _put_drop_oldest(q: queue.Queue, item):
    """向有界队列放入元素；若队列已满，先丢弃最旧的一帧再放入新帧。"""
    while True:
        try:
            q.put_nowait(item)
            return
        except queue.Full:
            try:
                q.get_nowait()   # 丢弃最旧帧
                q.task_done()
            except queue.Empty:
                pass


class PPipeline:
    """
    三线程流水线：读帧 → 处理（可选）→ 推流

    Parameters
    ----------
    models_cfg : list
        模型配置列表。传入空列表或 None 时进入无模型直通模式。
    pull_stream : SPullStream
        拉流对象，提供 get_stream_url()。
    push_stream : SPushStream
        推流对象，提供 start() / write_frame() / stop() / set_frame_info()。
    buffer_size : int
        缓冲带大小（帧数），同时作用于 frame_queue 和 processed_queue。
        默认 30，可通过 start_task API 的 buffer_size 参数覆盖。
    """

    def __init__(self, models_cfg, pull_stream, push_stream, buffer_size: int = _DEFAULT_BUFFER_SIZE):
        self.models_cfg = models_cfg or []
        self.pull_stream = pull_stream
        self.push_stream = push_stream
        self.buffer_size = max(1, int(buffer_size))
        self.is_processing = False

        # 抽帧缓冲队列（有界）
        self.frame_queue: queue.Queue = queue.Queue(maxsize=self.buffer_size)
        self.processed_queue: queue.Queue = queue.Queue(maxsize=self.buffer_size)

    # ------------------------------------------------------------------
    # 线程：读帧
    # ------------------------------------------------------------------
    def _frame_reader_thread(self, cap: cv2.VideoCapture):
        logger.info("帧读取线程启动")
        while self.is_processing:
            try:
                success, frame = cap.read()
                if not success:
                    time.sleep(0.01)
                    continue
                _put_drop_oldest(self.frame_queue, frame)
            except Exception as exc:
                logger.error(f"读取帧错误: {exc}")
                time.sleep(0.01)
        logger.info("帧读取线程退出")

    # ------------------------------------------------------------------
    # 线程：处理帧（含无模型直通）
    # ------------------------------------------------------------------
    def _frame_processor_thread(self, process_list):
        logger.info("帧处理线程启动（%s）", "无模型直通" if not process_list else f"{len(process_list)} 个模型")
        while self.is_processing:
            try:
                try:
                    frame = self.frame_queue.get(timeout=0.05)
                except queue.Empty:
                    continue

                try:
                    if process_list:
                        processed = frame.copy()
                        for proc in process_list:
                            processed = proc.process(processed)
                    else:
                        # 无模型直通：直接转发原始帧
                        processed = frame

                    _put_drop_oldest(self.processed_queue, processed)
                except Exception as exc:
                    logger.error(f"处理帧时出错: {exc}")
                finally:
                    self.frame_queue.task_done()
            except Exception as exc:
                logger.error(f"处理线程错误: {exc}")
                time.sleep(0.01)
        logger.info("帧处理线程退出")

    # ------------------------------------------------------------------
    # 线程：推流
    # ------------------------------------------------------------------
    def _frame_writer_thread(self):
        logger.info("帧推送线程启动")
        while self.is_processing:
            try:
                try:
                    frame = self.processed_queue.get(timeout=0.05)
                except queue.Empty:
                    continue

                try:
                    if not self.push_stream.write_frame(frame):
                        time.sleep(1)
                except Exception as exc:
                    logger.error(f"推送帧时出错: {exc}")
                finally:
                    self.processed_queue.task_done()
            except Exception as exc:
                logger.error(f"推送线程错误: {exc}")
                time.sleep(0.01)
        logger.info("帧推送线程退出")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def start(self):
        """启动流水线，阻塞直到流结束或调用 stop()。"""
        cap = cv2.VideoCapture(self.pull_stream.get_stream_url(), cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise ConnectionError("无法打开视频流")

        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = int(cap.get(cv2.CAP_PROP_FPS)) or 25

        self.push_stream.set_frame_info(frame_width, frame_height, fps)
        self.push_stream.start()
        self.is_processing = True

        # 初始化模型列表（无模型时为空列表）
        process_list = []
        for cfg in self.models_cfg:
            try:
                process_list.append(PProcessFrame(cfg))
            except Exception as exc:
                logger.error(f"模型初始化失败 ({cfg}): {exc}")

        threads = [
            threading.Thread(target=self._frame_reader_thread, args=(cap,), daemon=True),
            threading.Thread(target=self._frame_processor_thread, args=(process_list,), daemon=True),
            threading.Thread(target=self._frame_writer_thread, daemon=True),
        ]
        for t in threads:
            t.start()

        try:
            while self.is_processing and any(t.is_alive() for t in threads):
                time.sleep(0.5)
        except Exception as exc:
            logger.error(f"流水线主循环错误: {exc}")
        finally:
            self.is_processing = False
            cap.release()
            self.push_stream.stop()
            logger.info("视频流处理已停止")

    def stop(self):
        """异步停止流水线。"""
        self.is_processing = False
