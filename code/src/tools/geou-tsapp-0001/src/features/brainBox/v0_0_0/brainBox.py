"""
类脑盒子 brainBox 工具入口 — CbrainBox 类
平台通过 ProcessTask 调用 subfuncs 中定义的方法，每个方法接收 params 字典。

架构:
  ┌────────────────┐     HTTP/POST     ┌──────────────┐     MAVLink      ┌──────────┐
  │  边缘控制服务   │ ◄──────────────► │  类脑盒子     │ ◄──────────────► │  无人机   │
  │  Edge Server   │                   │  BrainBox    │                   │  Drones  │
  └────────────────┘                   └──────────────┘                   └──────────┘

支持的子功能:

  配置管理:
    get_config            获取当前配置信息
    update_config         更新配置信息

  无人机管理:
    scan_drones           扫描网络中的无人机
    query_drones          查询无人机信息
    send_command          向指定无人机发送控制指令
    drones_summary        获取无人机汇总信息

  导航:
    navigation_instruction  接收导航指令，生成轨迹
    execute_trajectory      执行导航轨迹
    list_trajectories       列出待执行轨迹
    list_algorithms         列出可用导航算法

  系统:
    system_status         获取系统状态
    list_protocols        列出已注册通信协议

--- params JSON 格式示例 ---

get_config:
{}

update_config:
{
    "edge": {
        "base_url": "http://10.0.0.1:8080",
        "heartbeat_interval": 10.0
    },
    "mavlink": {
        "scan_interval": 5.0
    }
}

scan_drones:
{}

query_drones:
{
    "device_id": "drone_sim_0"
}

send_command:
{
    "device_id": "drone_sim_0",
    "command": {
        "type": "takeoff",
        "altitude": 50.0
    }
}

drones_summary:
{}

navigation_instruction:
{
    "instruction_id": "nav_20240510_001",
    "device_id": "drone_sim_0",
    "target_position": {
        "latitude": 39.91,
        "longitude": 116.42,
        "altitude": 120.0
    },
    "algorithm": "simple_linear",
    "parameters": {
        "step_count": 5,
        "speed": 8.0
    }
}

execute_trajectory:
{
    "trajectory_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
}

list_trajectories:
{}

list_algorithms:
{}

system_status:
{}

list_protocols:
{}
"""

import json
import os
import sys

_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

from config.settings import get_settings  # noqa: E402
from core.manager import BrainBoxManager  # noqa: E402
from utils.logger import setup_logging  # noqa: E402

logger = setup_logging("brainBox")


class CbrainBox:
    """
    类脑盒子 brainBox 工具入口类。

    由平台框架通过 _load_train_version 自动实例化，
    每个公开方法对应一个 subfunc。
    """

    def __init__(self, node_cfg, process_comm, proc_modules_obj, progress_callback):
        self.node_cfg = node_cfg
        self.process_comm = process_comm
        self.proc_modules_obj = proc_modules_obj
        self.progress_callback = progress_callback

        self._manager = BrainBoxManager()
    # ------------------------------------------------------------------
    #  辅助
    # ------------------------------------------------------------------

    def _handle_result(self, func_name, result):
        print(func_name)
        print(result)
        if result.get("code", -1) == 0:
            self.progress_callback(
                100,
                json.dumps(result, ensure_ascii=False, default=str),
                "ok",
            )
        else:
            self.progress_callback(
                -1,
                json.dumps(result, ensure_ascii=False, default=str),
                "failed",
            )
        return result

    # ==================================================================
    #  配置管理
    # ==================================================================

    def get_config(self, params):
        """获取当前配置信息."""
        self.progress_callback(10, "正在获取配置信息")
        settings = get_settings()
        result = {"code": 0, "msg": "success", "data": settings.to_dict()}
        return self._handle_result("get_config", result)

    def update_config(self, params):
        """更新配置信息（支持部分更新）."""
        self.progress_callback(10, "正在更新配置")
        settings = get_settings()
        settings.update_from_dict(params)
        result = {"code": 0, "msg": "配置已更新", "data": settings.to_dict()}
        return self._handle_result("update_config", result)

    # ==================================================================
    #  无人机管理
    # ==================================================================

    async def scan(self, params):
        """扫描网络中的无人机."""
        self.progress_callback(10, "正在扫描无人机")
        result = await self._manager.scan_drones()
        return self._handle_result("scan", result)

    def query(self, params):
        """查询无人机信息."""
        self.progress_callback(10, "正在查询无人机")
        result = self._manager.query_drones(params)
        return self._handle_result("query", result)

    async def command(self, params):
        """向指定无人机发送控制指令."""
        device_id = params.get("device_id", "")
        self.progress_callback(10, f"正在发送指令到 {device_id}")
        result = await self._manager.send_command(params)
        return self._handle_result("command", result)

    def summary(self, params):
        """获取无人机汇总信息."""
        self.progress_callback(10, "正在获取无人机汇总")
        result = self._manager.drones_summary()
        return self._handle_result("summary", result)

    # ==================================================================
    #  导航
    # ==================================================================

    async def instruction(self, params):
        """接收导航指令，生成轨迹."""
        device_id = params.get("device_id", "")
        self.progress_callback(10, f"正在处理导航指令 (device={device_id})")
        result = await self._manager.navigation_instruction(params)
        return self._handle_result("instruction", result)

    async def execute(self, params):
        """执行导航轨迹."""
        trajectory_id = params.get("trajectory_id", "")
        self.progress_callback(10, f"正在执行轨迹: {trajectory_id}")
        result = await self._manager.execute_trajectory(params)
        print(result)
        return self._handle_result("execute", result)

    def trajectories(self, params):
        """列出待执行轨迹."""
        self.progress_callback(10, "正在查询轨迹列表")
        result = self._manager.list_trajectories()
        return self._handle_result("trajectories", result)

    def algorithms(self, params):
        """列出可用导航算法."""
        self.progress_callback(10, "正在查询算法列表")
        result = self._manager.list_algorithms()
        return self._handle_result("algorithms", result)

    # ==================================================================
    #  系统
    # ==================================================================

    def status(self, params):
        """获取系统状态."""
        self.progress_callback(10, "正在查询系统状态")
        result = self._manager.system_status()
        return self._handle_result("status", result)

    def protocols(self, params):
        """列出已注册通信协议."""
        self.progress_callback(10, "正在查询协议列表")
        result = self._manager.list_protocols()
        return self._handle_result("protocols", result)
