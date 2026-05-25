# 边缘控制服务 (ManageServer)

边缘服务器无人机管控工具 — 管理类脑盒子（BrainBox）集群，接收无人机状态上报，转发导航指令，实现云—边—端协同调度。

## 架构

```
┌────────────────┐     HTTP/POST     ┌──────────────┐     MAVLink      ┌──────────┐
│  边缘控制服务   │ ◄──────────────► │  类脑盒子     │ ◄──────────────► │  无人机   │
│  Edge Server   │                   │  BrainBox    │                   │  Drones  │
└────────────────┘                   └──────────────┘                   └──────────┘
```

### 模块结构 (v0_0_0)

```
src/features/manageServer/v0_0_0/
├── manageServer.py       # 工具入口 (CmanageServer 类)
├── main.py               # FastAPI 统一入口
├── config/
│   ├── __init__.py
│   └── settings.py       # 配置管理 (心跳超时、请求超时等)
├── core/
│   ├── __init__.py
│   ├── manager.py        # 核心管理器 (EdgeManager)
│   └── brain_box_client.py # HTTP 客户端 (用于向 BrainBox 转发指令)
├── models/
│   ├── __init__.py
│   ├── base.py           # 基础枚举 (BrainBoxStatus, DeviceStatus, NavigationStatus)
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

服务默认监听 `0.0.0.0:15000`。

## API 接口

所有接口使用 **POST** 方法，统一入口为:

```
POST /api/manageServer/CmanageServer/{subfunc}
```

请求体为 JSON 格式的参数字典。

### 类脑盒子管理

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `add_brain_box` | 注册类脑盒子实例到边缘服务器 | `{"box_id": "brain_box_001", "ip_address": "192.168.1.50", "port": 15001}` |
| `remove_brain_box` | 移除类脑盒子实例 | `{"box_id": "brain_box_001"}` |
| `list_brain_boxes` | 获取已注册的类脑盒子列表 | `{}` |
| `get_brain_box_status` | 查询类脑盒子详细状态（远程调用） | `{"box_id": "brain_box_001"}` |

### 数据接收（类脑盒子 → 边缘服务器）

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `heartbeat` | 接收类脑盒子心跳上报 | `{"box_id": "brain_box_001", "timestamp": 1715340000.123, "status": "running", "drone_count": 3, "online_count": 3, "online_devices": [...]}` |
| `drone_report` | 接收无人机状态上报 | `{"box_id": "brain_box_001", "timestamp": 1715340000.123, "devices": [...]}` |
| `trajectory_report` | 接收导航轨迹上报 | `{"box_id": "brain_box_001", "timestamp": 1715340000.123, "trajectory": {...}}` |

### 指令转发（边缘服务器 → 类脑盒子）

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `connect_drone` | 转发 TCP 连接无人机指令到类脑盒子 | `{"box_id": "brain_box_001", "ip": "192.168.43.1", "port": 5760, "label": "drone_tcp_1"}` |
| `disconnect_drone` | 转发断开无人机指令到类脑盒子 | `{"box_id": "brain_box_001", "device_id": "drone_1"}` |
| `list_connections` | 转发查询 TCP 连接列表指令到类脑盒子 | `{"box_id": "brain_box_001"}` |
| `scan_drones` | 转发扫描指令到类脑盒子 | `{"box_id": "brain_box_001"}` |
| `query_drones` | 转发查询指令到类脑盒子 | `{"box_id": "brain_box_001", "device_id": "drone_sim_0"}` |
| `send_command` | 转发控制指令到类脑盒子 | `{"box_id": "brain_box_001", "device_id": "drone_sim_0", "command": {"type": "takeoff", "altitude": 50.0}}` |

### 导航任务

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `navigation_instruction` | 下发导航指令（经由类脑盒子到无人机） | `{"box_id": "brain_box_001", "instruction_id": "nav_20240510_001", "device_id": "drone_sim_0", "target_position": {"latitude": 39.91, "longitude": 116.42, "altitude": 120.0}, "algorithm": "simple_linear", "parameters": {"step_count": 5, "speed": 8.0}}` |
| `execute_trajectory` | 转发轨迹执行指令 | `{"box_id": "brain_box_001", "trajectory_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"}` |

### 设备查询

| subfunc | 说明 | 参数示例 |
|---------|------|----------|
| `list_devices` | 获取所有已知无人机设备列表 | `{"box_id": "all"}` |
| `get_device_info` | 获取设备详细信息 | `{"box_id": "brain_box_001", "device_id": "drone_sim_0"}` |
| `list_tasks` | 查询导航任务列表 | `{"box_id": "all"}` |

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

1. **心跳与超时管理**: 边缘服务器通过 `heartbeat` 接口接收类脑盒子的心跳。若超过 `box_timeout_s`（默认 30 秒）未收到心跳，将标记该类脑盒子为离线状态。
2. **设备状态同步**: 类脑盒子通过 `drone_report` 接口周期性上报其管辖的无人机状态，边缘服务器据此更新内部的设备注册表。
3. **指令透传**: 边缘服务器不直接连接无人机，所有控制指令（如 `connect_drone`, `send_command`, `navigation_instruction`）均通过 HTTP 转发至对应的类脑盒子，由类脑盒子通过 MAVLink 协议下发至无人机。
