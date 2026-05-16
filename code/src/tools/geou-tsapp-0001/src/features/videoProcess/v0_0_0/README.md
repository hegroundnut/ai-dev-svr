# 视频处理服务 (VideoProcess)

视频处理服务（VideoProcess）是 `ai-dev-svr` 平台下的一个核心功能模块，负责视频流的拉取、AI 算法处理（如 YOLO 目标检测、运动检测）以及处理后视频流的推送或结果存储。

## 架构与特性

- **多模型串联**：支持配置多个模型，按顺序对每一帧进行流水线处理。
- **无模型直通模式**：当不配置任何模型时，系统自动进入直通模式，仅完成推拉流转发，实现零 AI 开销的纯流媒体转发。
- **抽帧缓冲策略**：采用自适应抽帧缓冲队列，当处理速度低于拉流速度时，自动丢弃中间帧保留最新帧，彻底解决传统 FIFO 队列导致的推流延迟累积（帧积压）问题。
- **低延迟推流**：FFmpeg 推流端采用 `veryfast` 预设和 `zerolatency` 调优，结合无缓冲标志，实现端到端极低延迟。

### 模块结构 (v0_0_0)

```text
src/features/videoProcess/v0_0_0/
├── videoProcess.py       # 工具入口 (CvideoProcess 类，供平台动态加载)
├── main.py               # FastAPI 独立运行入口（用于独立部署或调试）
├── core/
│   ├── __init__.py
│   └── manager.py        # 核心任务管理器 (VideoProcessManager)
├── process/
│   ├── __init__.py
│   ├── pipeline.py       # 三线程流水线（读帧、处理、推流），包含抽帧缓冲逻辑
│   ├── process_frame.py  # 帧处理分发器
│   ├── process_frame_yolo.py       # YOLO 目标检测处理器
│   └── process_frame_yolomotion.py # YOLO + MOG2 运动检测处理器
└── stream/
    ├── __init__.py
    ├── pull_stream.py    # 拉流统一入口
    ├── pull_local.py     # 本地/直连流拉取
    ├── pull_uva.py       # 无人机 API 流拉取
    ├── push_stream.py    # 推流统一入口
    └── push_ffmpeg.py    # FFmpeg RTMP 推流器
```

## 快速开始

### 平台集成运行

本模块已集成至 `ai-dev-svr` 平台，通过 `toolconfig.yml` 和 `loading_tools` 动态加载。
在平台的 `node_cfg` 中添加如下 `loading_tools` 配置即可加载：

```json
{
  "dtype": "videoProcess",
  "version": "0.0.0",
  "dir_name": "videoProcess",
  "file_name": "videoProcess",
  "class_name": "CvideoProcess"
}
```

### 独立调试运行

```bash
cd src/features/videoProcess/v0_0_0
python main.py
```
服务默认监听 `0.0.0.0:13212`。

## API 接口

所有接口使用 **POST** 方法，统一入口为:
```text
POST /api/videoProcess/CvideoProcess/{subfunc}
```

### 子功能列表

| subfunc | 说明 | 参数示例 |
| :--- | :--- | :--- |
| `start_task` | 启动视频处理任务 | `{"pull_source": "local", "pull_url": "...", "models_cfg": [...], "output_type": "stream"}` |
| `get_result` | 获取任务状态及结果 | `{"task_id": "..."}` |
| `list_tasks` | 列出所有正在运行或已完成的任务 | `{}` |
| `stop_task` | 停止并删除指定任务 | `{"task_id": "..."}` |
| `get_stream_url` | 获取任务的输出流地址 | `{"task_id": "..."}` |

## 核心参数说明

### start_task

启动任务时，支持以下关键参数：

- `pull_source`: 拉流源类型，支持 `local`（本地/RTMP/RTSP直连）或 `uva`（无人机 API）。
- `pull_url`: 拉流地址。
- `pull_type`: 流协议类型，如 `rtmp`、`webrtc`。
- `models_cfg`: 模型配置列表（**可选**）。
  - **多模型配置**：传入列表，如 `[{"model_folder": "/path", "model_name": "yolov8n.pt", "type": "yolo"}]`。
  - **无模型直通**：**不传此参数或传入空列表 `[]` 时，系统将跳过 AI 处理，仅进行推拉流转发。**
- `buffer_size`: 缓冲带大小（帧数），默认 `30`。控制抽帧队列的容量，较小值降低延迟，较大值提升网络抖动时的流畅度。
- `output_type`: 输出类型，支持 `stream`（推流）、`json`（结果查询）、`mysql`（入库）。
- `stream_push_mode`: 推流模式，目前支持 `ffmpeg`。
- `stream_push_url`: 完整推流地址（如 `rtmp://localhost:1935/live/detected`）。若不填，则根据 `stream_push_srs_addr`、`stream_push_srs_port` 和 `stream_push_stream_key` 自动拼接。

### 多模型配置示例 (models_cfg)

```json
"models_cfg": [
    {
        "model_folder": "/home/ubuntu/weights",
        "model_name": "yolov8n.pt",
        "type": "yolo"
    }
]
```
*注：也兼容旧版的扁平化传参方式（如 `model_folder_0`, `model_name_0`, `model_type_0`）。*

## 扩展指南

### 添加新的 AI 模型处理器

1. 在 `process/` 目录下创建新的处理器类（如 `process_frame_custom.py`），实现 `process(self, frame)` 方法。
2. 在 `process/process_frame.py` 的 `_REGISTRY` 字典中注册该类型。

### 添加新的推拉流协议

1. **拉流**：在 `stream/` 目录下创建新的拉流类，实现 `get_stream_url(self)` 方法，并在 `pull_stream.py` 的 `_PULL_REGISTRY` 中注册。
2. **推流**：在 `stream/` 目录下创建新的推流类，实现 `start()`, `write_frame()`, `stop()` 等方法，并在 `push_stream.py` 的 `_PUSH_REGISTRY` 中注册。
