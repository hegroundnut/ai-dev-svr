"""
pull_uva.py — 无人机视频流拉取

通过 HTTP API 向无人机管理服务请求流地址，支持 RTMP 和 WebRTC 两种协议类型。
"""

import logging
import requests

logger = logging.getLogger(__name__)


class SPullUVA:
    """
    无人机视频流拉取。

    Parameters
    ----------
    node_cfg : dict
        必须包含：
          - url  : 无人机管理服务的流地址查询 API URL
          - type : 流协议类型，"rtmp" 或 "webrtc"
        可选：
          - timeout : HTTP 请求超时秒数，默认 5
    """

    def __init__(self, node_cfg: dict):
        self.node_cfg = node_cfg

    def get_stream_url(self) -> str:
        """请求 API 获取无人机流地址，失败时返回空字符串。"""
        api_url = self.node_cfg.get("url", "")
        stream_type = self.node_cfg.get("type", "rtmp").lower()
        timeout = int(self.node_cfg.get("timeout", 5))

        try:
            resp = requests.get(api_url, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") == 0:
                stream_data = data.get("data", {})
                if stream_type == "rtmp":
                    return stream_data.get("drone_rtmp_url", "")
                elif stream_type == "webrtc":
                    return stream_data.get("drone_webrtc_url", "")
                else:
                    logger.error(f"不支持的流协议类型: {stream_type}")
            else:
                logger.error(f"API 返回错误: {data.get('message', '未知错误')}")
        except requests.exceptions.Timeout:
            logger.error(f"请求超时: {api_url}")
        except requests.exceptions.RequestException as exc:
            logger.error(f"请求异常: {exc}")
        except Exception as exc:
            logger.error(f"获取流地址时发生未知错误: {exc}")

        return ""
