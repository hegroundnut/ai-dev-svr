# BrainBox 类脑盒子服务器

无人机管控与导航系统 — 连接边缘控制服务和无人机群，提供导航轨迹生成与任务执行。

## 架构

```
┌────────────────┐     HTTP/POST     ┌──────────────┐     MAVLink      ┌──────────┐
│  边缘控制服务   │ ◄──────────────► │  类脑盒子     │ ◄──────────────► │  无人机   │
│  Edge Server   │                   │  BrainBox    │                   │  Drones  │
└────────────────┘                   └──────────────┘                   └──────────┘
```

### 模块结构 (v0_0_0)

```
src/features/brainBox/v0_0_0/
├── brainBox.py           # 工具入口 (CbrainBox 类)
├── main.py               # FastAPI 统一入口
├── config/
│   ├── __init__.py
│   └── settings.py       # 配置管理 (YAML + 环境变量, 不含 server 段)
├── core/
│   ├── __init__.py
│   ├── manager.py        # 核心管理器 (整合所有子系统)
│   ├── mavlink_comm.py   # MAVLink 通信协议 (支持 UDP 监听与 TCP 主动连接)
│   ├── drone_manager.py  # 无人机管理器
│   ├── edge_reporter.py  # 边缘服务上报器 + HTTP 客户端
│   ├── navigation_service.py  # 导航服务 + 算法注册
│   └── protocol_registry.py   # 通信协议注册中心
├── models/
│   ├── __init__.py
│   ├── device.py         # 设备数据模型
│   └── algorithm.py      # 导航算法数据模型
└── utils/
    ├── __init__.py
    └── logger.py         # 日志工具
```

## 快速开始

### 安装

```bash
pip install -e .
```

安装 MAVLink 支持 (可选):

```bash
pip install -e ".[mavlink]"
```

### 配置

编辑 `config.yaml` 或通过环境变量覆盖:

```bash
export BRAIN_BOX_ID="my_box_001"
export BRAIN_BOX_EDGE_URL="http://10.0.0.1:15000"
export BRAIN_BOX_LOG_LEVEL=DEBUG
```

> **注意**: v0_0_0 版本的配置中不再包含 `server` 段，HTTP 服务端口由 `main.py` 独立管理。

### 运行

```bash
cd src/features/brainBox/v0_0_0
python main.py
```

服务默认监听 `0.0.0.0:15001`。

## API 接口

所有接口使用 **POST** 方法，统一入口为:

```
POST /api/brainBox/CbrainBox/{subfunc}
```

请求体为 JSON 格式的参数字典。

### 配置管理

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `get_config` | 获取当前配置信息 | `{}` |
| `update_config` | 更新配置信息（支持部分更新） | `{"edge": {"base_url": "http://10.0.0.1:15000"}}` |

### 无人机管理

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `connect_drone` | 主动 TCP 连接到指定无人机 | `{"ip": "192.168.43.1", "port": 5760, "label": "drone_tcp_1"}` |
| `disconnect_drone` | 断开指定无人机的 TCP 连接 | `{"device_id": "drone_1"}` |
| `connections` | 列出所有 TCP 主动连接通道 | `{}` |
| `scan` | 扫描已连接的无人机 | `{}` |
| `query` | 查询无人机信息 | `{"device_id": "drone_sim_0"}` |
| `command` | 向指定无人机发送控制指令 | `{"device_id": "drone_sim_0", "command": {"type": "takeoff", "altitude": 50.0}}` |
| `summary` | 获取无人机汇总信息 | `{}` |

### 导航

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `instruction` | 接收导航指令，生成轨迹 | `{"instruction_id": "nav_001", "device_id": "drone_sim_0", "target_position": {"latitude": 39.91, "longitude": 116.42, "altitude": 120.0}, "algorithm": "simple_linear", "parameters": {"step_count": 5, "speed": 8.0}}` |
| `execute` | 执行导航轨迹 | `{"trajectory_id": "uuid-string"}` |
| `trajectories` | 列出待执行轨迹 | `{}` |
| `algorithms` | 列出可用导航算法 | `{}` |

### 系统

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `status` | 获取系统状态 | `{}` |
| `protocols` | 列出已注册通信协议 | `{}` |

### 响应格式

所有接口统一返回:

```json
{
    "code": 0,
    "msg": "success",
    "data": { ... }
}
```

- `code`: 0 表示成功，-1 表示失败
- `msg`: 状态消息
- `data`: 响应数据

### 配置管理接口示例

**获取配置:**

```bash
curl -X POST http://localhost:15001/api/brainBox/CbrainBox/get_config \
  -H "Content-Type: application/json" \
  -d '{}'
```

**更新配置 (部分更新):**

```bash
curl -X POST http://localhost:15001/api/brainBox/CbrainBox/update_config \
  -H "Content-Type: application/json" \
  -d '{"edge": {"base_url": "http://10.0.0.1:15000", "heartbeat_interval": 10.0}}'
```

**主动连接 TCP 无人机:**

```bash
curl -X POST http://localhost:15001/api/brainBox/CbrainBox/connect_drone \
  -H "Content-Type: application/json" \
  -d '{"ip": "192.168.43.1", "port": 5760}'
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

# 注册到系统
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

# 注册到系统
algorithm_registry.register(MyAlgorithm())
```

## 开发

```bash
pip install -e ".[dev]"
pytest
ruff check src/
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BRAIN_BOX_CONFIG` | 配置文件路径 | `config.yaml` |
| `BRAIN_BOX_EDGE_URL` | 边缘服务地址 | `http://127.0.0.1:15000` |
| `BRAIN_BOX_EDGE_HEARTBEAT_PATH` | 心跳上报路径 | `/api/manageServer/CmanageServer/heartbeat` |
| `BRAIN_BOX_EDGE_DRONE_REPORT_PATH` | 无人机上报路径 | `/api/manageServer/CmanageServer/drone_report` |
| `BRAIN_BOX_EDGE_TRAJECTORY_REPORT_PATH` | 轨迹上报路径 | `/api/manageServer/CmanageServer/trajectory_report` |
| `BRAIN_BOX_MAVLINK_CONNECTION` | MAVLink 连接串 | `udpin:0.0.0.0:14550` |
| `BRAIN_BOX_LOG_LEVEL` | 日志级别 | `INFO` |
| `BRAIN_BOX_LOG_DIR` | 日志目录 | `logs` |
