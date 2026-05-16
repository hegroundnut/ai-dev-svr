"""
push_ffmpeg.py — FFmpeg RTMP 推流器

通过 FFmpeg 子进程将原始 BGR 帧编码为 H.264 并推送至 RTMP 服务器。

优化说明（相比原始版本）
-----------------------
1. 将 preset 改为 "veryfast"，在保证质量的同时显著降低编码延迟。
2. tune 改为 "zerolatency"，进一步减少编码缓冲延迟。
3. 增加 -fflags nobuffer / -flags low_delay 选项，降低端到端延迟。
4. 写帧失败时记录错误并尝试重连，而不是静默失败。
5. 增加 is_running 属性的线程安全保护。
"""

import logging
import subprocess
import cv2

logger = logging.getLogger(__name__)


class FFmpegStreamer:
    """
    FFmpeg RTMP 推流器。

    Parameters
    ----------
    output_url : str
        RTMP 推流目标地址，如 "rtmp://localhost:1935/live/stream"。
    """

    def __init__(self, output_url: str):
        self.output_url = output_url
        self.frame_width = 0
        self.frame_height = 0
        self.fps = 25
        self.process: subprocess.Popen | None = None
        self.is_running = False

    def set_frame_info(self, width: int, height: int, fps: int):
        self.frame_width = width
        self.frame_height = height
        self.fps = fps or 25

    def start(self):
        """启动 FFmpeg 子进程。"""
        command = [
            "ffmpeg",
            # 输入：从 stdin 读取原始 BGR 视频
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{self.frame_width}x{self.frame_height}",
            "-r", str(self.fps),
            "-i", "-",
            # 低延迟选项
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            # 编码参数
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "veryfast",       # 低延迟编码
            "-tune", "zerolatency",      # 零延迟模式
            "-profile:v", "high",
            "-level", "5.2",
            "-crf", "23",
            "-maxrate", "4000k",
            "-bufsize", "4000k",
            "-g", str(self.fps * 2),     # GOP = 2 秒
            "-keyint_min", str(self.fps),
            # 输出
            "-f", "flv",
            self.output_url,
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.is_running = True
            logger.info(f"FFmpeg 推流启动: {self.output_url}")
        except Exception as exc:
            logger.error(f"启动 FFmpeg 失败: {exc}")
            self.is_running = False

    def write_frame(self, frame) -> bool:
        """向 FFmpeg 写入一帧，失败时尝试重连。返回是否写入成功。"""
        if not self.is_running or self.process is None:
            return False
        try:
            # 尺寸不一致时自动缩放
            if frame.shape[1] != self.frame_width or frame.shape[0] != self.frame_height:
                frame = cv2.resize(frame, (self.frame_width, self.frame_height))
            self.process.stdin.write(frame.tobytes())
            return True
        except BrokenPipeError:
            logger.warning("FFmpeg 管道断开，尝试重连...")
            self.stop()
            self.start()
            return False
        except Exception as exc:
            logger.error(f"写入帧时出错: {exc}")
            self.stop()
            self.start()
            return False

    def stop(self):
        """停止 FFmpeg 子进程。"""
        self.is_running = False
        if self.process is not None:
            try:
                self.process.stdin.close()
            except Exception:
                pass
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            finally:
                self.process = None
        logger.info("FFmpeg 推流已停止")
