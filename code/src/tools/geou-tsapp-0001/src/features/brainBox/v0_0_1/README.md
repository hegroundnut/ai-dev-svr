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
| manageServer → brainBox | WS `req`/`resp` | 无人机连接/断开、扫描、查询、控制指令、导航指令、航点飞行任务（waypoint_mission）等 |

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

## 航点飞行服务（waypoint_mission）

接收外部传入的航点经纬度和高度数据，通过完整 MAVLink 任务协议向无人机下发航点飞行任务并启动执行：

```
MISSION_COUNT → (MISSION_REQUEST → MISSION_ITEM_INT)* → MISSION_ACK → 解锁 → MISSION_START → 切任务模式
```

启动流程要点：

- **解锁**（`auto_arm`，默认 `true`）：未解锁时飞控不会执行任务。若当前处于 AUTO 模式，
  先切到 LOITER/GUIDED 再解锁（多数固件禁止在 AUTO 下解锁）；解锁结果通过心跳确认，
  失败时错误信息会带上飞控的具体原因（`COMMAND_ACK` 结果码与 PreArm/WARNING 等
  `STATUSTEXT` 文本），便于定位安全开关、未校准、GPS/EKF、电池电压等问题。
- **启动**：发送 `MISSION_START` 后切换任务模式（ArduPilot 为 `AUTO`，PX4 为
  `AUTO_MISSION`），并通过飞控心跳确认模式真正生效；`SET_MODE` 未生效时改用
  `MAV_CMD_DO_SET_MODE` 重试（飞控会回 `COMMAND_ACK`，可拿到拒绝原因）。旧版
  pymavlink 无 `mission_start_send` 时自动降级为 `command_long` 发送等价的
  `MAV_CMD_MISSION_START`。
- **自动起飞**（ArduPilot）：从地面进入 AUTO 时必须有 `NAV_TAKEOFF` 才会起飞，用户未
  提供 `command: "takeoff"` 时自动插入一项（起飞高度取首个航点高度）。

飞控若不逐项请求（不回 MISSION_REQUEST），自动回退为逐条盲发；盲发期间及之后仍会响应
飞控迟到的 MISSION_REQUEST（自动补发对应航点），直到收到 MISSION_ACK。
任务参数按 MAVLink 规范映射：param1=hold_time、param2=accept_radius、
param3=pass_radius、param4=yaw，且不发送 NaN（部分飞控会以
MAV_MISSION_INVALID_PARAM 拒绝含 NaN 的任务项）。
任务执行期间，飞控上报的 MISSION_CURRENT / MISSION_ITEM_REACHED 会持续更新到设备
`metadata["mission_progress"]`，可通过 `query` / `summary` 查询，并随 `drone_report`
周期上报到 manageServer。字段说明:
`total`（用户航点数）、`current_seq`（当前执行的用户航点序号 0..N-1）、
`last_reached_seq`（最近到达的用户航点序号）、`raw_seq`/`raw_reached_seq`（飞控任务项
原始序号）、`item_total`（飞控任务项总数）、`user_offset`（首个用户航点前的合成任务项数）。

ArduPilot home 约定：ArduPilot 任务项 0 固定保留给 home（起飞点），brainBox 对
ArduPilot 设备自动在 seq0 追加 home 项（取设备当前位置，解锁起飞后飞控会自动更新为
真实起飞点），用户航点顺移到其后的序号 —— 地面站航点列表中会看到 home + 自动起飞项 +
全部用户航点。PX4 无此约定，直接从 seq0 上传用户航点。

### WS 请求（manageServer → brainBox）

```json
{
  "type": "req",
  "action": "waypoint_mission",
  "request_id": "xxx",
  "payload": {
    "device_id": "drone_1",
    "auto_start": true,
    "auto_arm": true,
    "waypoints": [
      {"command": "takeoff",   "latitude": 47.397742, "longitude": 8.545594, "altitude": 15, "hold_time": 5},
      {"latitude": 47.397800, "longitude": 8.545700, "altitude": 20, "hold_time": 3},
      {"latitude": 47.397900, "longitude": 8.545400, "altitude": 20, "accept_radius": 2.0, "yaw": 90},
      {"command": "land",     "latitude": 47.397742, "longitude": 8.545594, "altitude": 0}
    ]
  }
}
```

航点字段说明:

| 字段 | 必填 | 说明 |
|------|------|------|
| `latitude` / `longitude` | 是 | 经纬度（度） |
| `altitude` | 否 | 相对起飞点高度（米），默认 10 |
| `command` | 否 | `waypoint`（默认）/ `takeoff` / `land` / `rtl` / `loiter` |
| `hold_time` | 否 | 航点停留时间（秒），默认 0 |
| `accept_radius` | 否 | 到达判定半径（米），waypoint/loiter 默认 2，其他指令 0 |
| `pass_radius` | 否 | 过点半径（米），0=直接飞向航点（默认），对应 MISSION_ITEM_INT 的 param3 |
| `yaw` | 否 | 到达航点时的航向（度），默认 0（正北），对应 param4 |

`auto_start`（默认 `true`）控制上传成功后是否立即发送 MISSION_START 启动任务。

### HTTP 本地测试

```bash
curl -X POST http://127.0.0.1:15001/api/brainBox/CbrainBox/waypoint_mission \
  -H "Content-Type: application/json" \
  -d '{"device_id": "drone_1", "waypoints": [{"latitude": 39.9042, "longitude": 116.4074, "altitude": 20}]}'
```

## 设备运维（reboot_drone / drone_logs）

两个接口同时提供 WS action 与盒子直连 HTTP 入口，请求/响应体一致。

| 接口 | 说明 | 关键参数 |
|------|------|----------|
| `reboot_drone` | 远程重启飞控 / 伴飞计算机（`MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN`，ID 246） | `device_id`、`component`（`autopilot` 1 / `companion` 2 / `both` 3，默认 `autopilot`） |
| `drone_logs` | 增量查询飞控文本日志（`STATUSTEXT`，每设备缓存最近 300 条） | `device_id`、`since`（Unix 秒游标，只返回 `time > since`）、`limit`（默认 20，上限 300） |

`reboot_drone` 失败场景：设备已解锁（`设备已解锁，禁止远程重启...`）、非法 `component`、
无可用连接（`设备 xxx 无可用连接`）、模拟模式（`模拟模式下不支持远程重启`）。
接口返回成功仅表示指令已下发，飞控约 9 秒后完成重启；重启后可查询 `drone_logs`
观察 `ArduPilot Ready`、`EKF3 IMU0 initialised` 等冷启动消息。

`drone_logs` 返回 `status_texts`（按 `time` 升序）、`count`、`has_more`、`next_since`；
`has_more=true` 时用 `next_since` 继续请求，直到 `has_more=false`。
条目含 `text`、`severity`（MAV_SEVERITY 0~7）、`severity_name`、`time`。

### HTTP 本地测试

```bash
curl -X POST http://127.0.0.1:15001/api/brainBox/CbrainBox/reboot_drone \
  -H "Content-Type: application/json" \
  -d '{"device_id": "drone_1", "component": "autopilot"}'
```

```bash
curl -X POST http://127.0.0.1:15001/api/brainBox/CbrainBox/drone_logs \
  -H "Content-Type: application/json" \
  -d '{"device_id": "drone_1", "since": 1762934420.31, "limit": 100}'
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
