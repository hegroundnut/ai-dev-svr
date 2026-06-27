# BrainBox 类脑盒子

无人机管控与导航系统 — 通过 WebSocket 长连接与边缘控制服务（manageServer）通信，负责 MAVLink 无人机接入、协议转换、局部导航算法执行及状态上报。

## 架构

```
┌────────────────┐   WebSocket 长连接   ┌──────────────┐     MAVLink      ┌──────────┐
│  边缘控制服务   │ ◄─────────────────► │  类脑盒子     │ ◄──────────────► │  无人机   │
│  manageServer  │     (port 15002)     │  BrainBox    │    (TCP/UDP)     │  Drones  │
└────────────────┘                      └──────────────┘                   └──────────┘
```

brainBox 主动向 manageServer 发起 WebSocket 连接，所有双向通信通过该长连接完成，无需内网穿透。

### 通信方式

| 方向 | 类型 | 说明 |
|------|------|------|
| brainBox → manageServer | WS `event` | 心跳（heartbeat）、无人机状态上报（drone_report）、轨迹上报（trajectory_report） |
| manageServer → brainBox | WS `req`/`resp` | 无人机连接/断开、扫描、查询、控制指令、导航指令等 |

### 模块结构 (v0_0_0)

```
src/features/brainBox/v0_0_0/
├── brainBox.py           # 工具入口 (CbrainBox 类) — 自动启动 WS 客户端
├── config/
│   ├── __init__.py
│   └── settings.py       # 配置管理 (YAML + 环境变量)
├── core/
│   ├── __init__.py
│   ├── manager.py        # 核心管理器 (BrainBoxManager) + WS 请求分发
│   ├── ws_client.py      # WebSocket 客户端 (EdgeWSClient) — 自动重连
│   ├── edge_reporter.py  # 边缘上报器 (EdgeReporter) — 通过 WS 上报
│   ├── mavlink_comm.py   # MAVLink 通信协议 (UDP 监听 / TCP 主动连接)
│   ├── drone_manager.py  # 无人机管理器
│   ├── navigation_service.py  # 导航服务 + 算法注册
│   └── protocol_registry.py   # 通信协议注册中心
├── models/
│   ├── __init__.py
│   ├── device.py         # 设备数据模型
│   └── algorithm.py      # 导航算法数据模型
├── storage/
│   ├── __init__.py
│   └── database.py       # 本地 SQLite 存储
└── utils/
    ├── __init__.py
    └── logger.py         # 日志工具
```

## 快速开始

### 安装

```bash
pip install -r requirements.txt
```

### 配置

通过环境变量覆盖默认配置:

```bash
export BRAIN_BOX_ID="brain_box_001"
export BRAIN_BOX_EDGE_WS_URL="ws://<manageServer_IP>:15002"
export BRAIN_BOX_MAVLINK_CONNECTION="udpin:0.0.0.0:14550"
export BRAIN_BOX_LOG_LEVEL=DEBUG
```

| 环境变量 | 说明 | 默认值 |
|------|------|--------|
| `BRAIN_BOX_ID` | 类脑盒子唯一标识 | `brain_box_002` |
| `BRAIN_BOX_EDGE_WS_URL` | manageServer WebSocket 地址 | `ws://47.97.154.110:15002` |
| `BRAIN_BOX_EDGE_HEARTBEAT_INTERVAL` | 心跳间隔（秒） | `5.0` |
| `BRAIN_BOX_MAVLINK_CONNECTION` | MAVLink 连接串 | `udpin:0.0.0.0:14550` |
| `BRAIN_BOX_LOG_LEVEL` | 日志级别 | `INFO` |

### 运行

brainBox 作为平台工具由框架自动加载，实例化时自动启动 WebSocket 客户端连接 manageServer。

如需独立测试，可直接在代码中实例化:

```python
from brainBox import CbrainBox

box = CbrainBox(node_cfg={}, process_comm=None, proc_modules_obj=None, progress_callback=print)
# WS 客户端已自动启动，心跳和状态上报已开始
```

## 扩展

### 添加新通信协议

```python
from core.protocol_registry import DeviceProtocol

class MyProtocol(DeviceProtocol):
    @property
    def protocol_name(self) -> str:
        return "my_protocol"

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def scan_devices(self) -> list: ...
    async def send_command(self, device_id, command) -> dict: ...
    async def get_device_status(self, device_id): ...

protocol_registry.register(MyProtocol())
```

### 添加新导航算法

```python
from models.algorithm import NavigationAlgorithm, NavigationTrajectory

class MyAlgorithm(NavigationAlgorithm):
    @property
    def algorithm_name(self) -> str:
        return "my_algorithm"

    async def generate_trajectory(self, device_id, current_position,
                                   target_position, parameters=None) -> NavigationTrajectory:
        ...

algorithm_registry.register(MyAlgorithm())
```
