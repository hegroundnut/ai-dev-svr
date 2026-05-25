# 边缘控制服务 (ManageServer)

边缘服务器无人机管控工具 — 通过 WebSocket 长连接管理类脑盒子（BrainBox）集群，接收无人机状态上报，转发导航指令，实现云-边-端协同调度。

## 架构

```
┌────────────────┐   WebSocket 长连接  ┌──────────────┐    MAVLink      ┌──────────┐
│  边缘控制服务   │ ◄────────────────► │  类脑盒子     │ ◄──────────────► │  无人机   │
│  manageServer  │     (port 15002)    │  BrainBox    │   (TCP/UDP)     │  Drones  │
└────────────────┘                     └──────────────┘                  └──────────┘
```

brainBox 通过 WebSocket 主动连接 manageServer，所有双向通信通过该长连接完成。
brainBox 无需公网 IP — 只需 manageServer 有可访问的地址。

### 通信方式

| 方向 | 类型 | 说明 |
|------|------|------|
| brainBox → manageServer | WS `event` | 心跳、无人机状态上报、轨迹上报 |
| manageServer → brainBox | WS `req`/`resp` | 连接/断开/扫描/控制/导航等指令 |

### 模块结构 (v0_0_0)

```
src/features/manageServer/v0_0_0/
├── manageServer.py       # 工具入口 (CmanageServer 类)
├── main.py               # FastAPI 测试入口 (port 15000)
├── api-docs.json         # Apifox 可导入的 OpenAPI 3.0 文档
├── config/
│   ├── __init__.py
│   └── settings.py       # 配置管理 (心跳超时、WS 端口等)
├── core/
│   ├── __init__.py
│   ├── manager.py        # 核心管理器 (EdgeManager)
│   ├── ws_server.py      # WebSocket 服务器 (BrainBoxWSManager)
│   └── heartbeat.py      # 心跳监控守护线程
├── models/
│   ├── __init__.py
│   ├── base.py           # 基础枚举
│   ├── brain_box.py      # 类脑盒子数据模型
│   ├── device.py         # 无人机设备数据模型
│   └── task.py           # 导航任务数据模型
└── utils/
    ├── __init__.py
    └── logger.py         # 日志工具
```

## 快速开始

### 运行

```bash
cd src/features/manageServer/v0_0_0
python main.py
```

服务监听:
- HTTP API: `0.0.0.0:15000`
- WebSocket: `0.0.0.0:15002`

## API 接口

所有接口使用 **POST** 方法，统一入口为:

```
POST /api/manageServer/CmanageServer/{subfunc}
```

请求体为 JSON 格式的参数字典。

### 类脑盒子管理

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `add_brain_box` | 注册类脑盒子（也可由 WS 首次心跳自动注册） | `{"box_id": "brain_box_001", "metadata": {"location": "机房A"}}` |
| `remove_brain_box` | 移除类脑盒子及关联设备 | `{"box_id": "brain_box_001"}` |
| `list_brain_boxes` | 获取已注册的类脑盒子列表 | `{}` |
| `get_brain_box_status` | 查询类脑盒子详细状态（通过 WS 远程调用） | `{"box_id": "brain_box_001"}` |

### 数据接收（brainBox → manageServer，优先 WS）

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `heartbeat` | 接收类脑盒子心跳（WS 自动上报，HTTP 备用） | `{"box_id": "brain_box_001", "timestamp": 1715340000.123, "status": "running", "drone_count": 3, "online_count": 3}` |
| `drone_report` | 接收无人机状态上报 | `{"box_id": "brain_box_001", "timestamp": 1715340000.123, "devices": [...]}` |
| `trajectory_report` | 接收导航轨迹上报 | `{"box_id": "brain_box_001", "timestamp": 1715340000.123, "trajectory": {...}}` |

### 指令转发（manageServer → brainBox，通过 WS）

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `connect_drone` | 转发 TCP 连接无人机指令 | `{"box_id": "brain_box_001", "ip": "192.168.43.1", "port": 5760, "label": "drone_tcp_1"}` |
| `disconnect_drone` | 转发断开无人机指令 | `{"box_id": "brain_box_001", "device_id": "drone_1"}` |
| `list_connections` | 查询 TCP 连接列表 | `{"box_id": "brain_box_001"}` |
| `scan_drones` | 转发扫描指令 | `{"box_id": "brain_box_001"}` |
| `query_drones` | 查询无人机信息 | `{"box_id": "brain_box_001", "device_id": "drone_sim_0"}` |
| `send_command` | 发送控制指令（arm/takeoff/land/goto/set_mode 等） | `{"box_id": "brain_box_001", "device_id": "drone_sim_0", "command": {"type": "takeoff", "altitude": 50.0}}` |

### 导航任务

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `navigation_instruction` | 下发导航指令，生成轨迹 | `{"box_id": "brain_box_001", "instruction_id": "nav_20240510_001", "device_id": "drone_sim_0", "target_position": {"latitude": 39.91, "longitude": 116.42, "altitude": 120.0}, "algorithm": "simple_linear", "parameters": {"step_count": 5, "speed": 8.0}}` |
| `execute_trajectory` | 执行轨迹（完成后任务状态自动更新为 completed） | `{"box_id": "brain_box_001", "trajectory_id": "a1b2c3d4-..."}` |

### 设备查询

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `list_devices` | 获取已知无人机设备列表 | `{"box_id": "all"}` 或 `{"box_id": "brain_box_001"}` |
| `get_device_info` | 获取设备详情（需 box_id + device_id） | `{"box_id": "brain_box_001", "device_id": "drone_sim_0"}` |
| `list_tasks` | 查询导航任务列表 | `{"box_id": "all"}` 或 `{"box_id": "brain_box_001"}` |

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

## 核心机制

1. **WebSocket 长连接**: brainBox 启动后主动连接 manageServer 的 WS 端口（15002），发送 `auth` 消息注册。之后所有心跳、上报、指令全通过该连接。
2. **自动注册**: 首次收到 brainBox 的 WS 心跳时自动注册该类脑盒子，无需手动调用 `add_brain_box`。
3. **心跳与超时管理**: 若超过 `box_timeout_s`（默认 30 秒）未收到心跳，标记该类脑盒子为离线，其下设备也标记为离线。
4. **重复连接拒绝**: 同一 box_id 已有活跃 WS 连接时，新的连接请求被拒绝（close code 4002）。
5. **指令透传**: 所有控制指令通过 WS 发送至 brainBox，由 brainBox 通过 MAVLink 下发至无人机。
