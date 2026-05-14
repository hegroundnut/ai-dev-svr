#!/usr/bin/env python
import json
import os
import sys
import threading
import time
import requests
import multiprocessing
import traceback
import signal
from collections import deque

from src.SysLogger import CSysLogger
from src.Util import loadPyFile

# 确保使用 spawn 模式以兼容 CUDA
if multiprocessing.get_start_method(allow_none=True) != "spawn":
    multiprocessing.set_start_method("spawn", force=True)


def run_task_entry(node_cfg, task_queue, progress_queue, entire_queue):
    """常驻子进程：加载一次模型，循环处理任务"""
    try:
        import time as _time
        print(f"[{_time.strftime('%H:%M:%S')}] 子进程 {os.getpid()} 启动，初始化加载...")

        tool_class = loadPyFile('App', 'CApp', 'src.tools.{}'.format(node_cfg['tool_package_name']))

        def dummy_callback(deal_percent, deal_msg, deal_status=None, func_callback=None):
            """
            deal_percent: -2~100
            deal_msg: 状态说明
            """
            try:
                progress_queue.put({
                    "deal_percent": deal_percent,
                    "deal_time": float(_time.time()),
                    "deal_msg": deal_msg,
                    "status": deal_status
                })
                if func_callback and entire_queue:
                    entire_queue.put(func_callback)
            except: pass

        tool_ins = tool_class(node_cfg, dummy_callback)
        print(f"[{_time.strftime('%H:%M:%S')}] 子进程模型加载就绪。")

        while True:
            param = task_queue.get()
            if param is None: 
                break

            try:
                print(f"子进程开始处理新任务...")
                tool_ins.ProcessTask(param)
                time.sleep(2)

                # 成功完成
                dummy_callback(100, "TASK_FINISHED_okESSFULLY", "ok")
            except Exception as e:
                print(f"子进程执行任务异常: {e}")
                traceback.print_exc()
                time.sleep(2)

                dummy_callback(-1, f"TASK_ERROR: {str(e)}", "failed")
                continue

    except Exception:
        traceback.print_exc()
        os._exit(2)


class ProcessorFramework(object):
    def __init__(self, node_cfg):
        signal.signal(signal.SIGINT, self._handle_sigint)
        signal.signal(signal.SIGTERM, self._handle_sigint)

        self.node_cfg = node_cfg
        self.logger = CSysLogger(self.node_cfg['node_id'])

        self.live_status = 2        # 2-空闲, 1-繁忙
        self.report_id = None
        self.db_id = -1
        self.kill_flag = False
        self.current_task_status = None  # 'ok' / 'failed' / 'killed' / None

        self._task_queue = multiprocessing.Queue()
        self._progress_queue = multiprocessing.Queue()
        self._entire_queue = multiprocessing.Queue()
        self.current_task_process = None

        self.progress_callback_queue = deque()
        self.progress_callback_lock = threading.Lock()
        self.entire_progress_callback = {}
        self.entire_progress_callback_lock = threading.Lock()

        if self.node_cfg.get('master_ip'):
            self._start_threads()

        self.logger.info("服务启动成功!")

    def _start_threads(self):
        ts = [
            self._read_progress_queue_thread,
            self._read_entire_queue_thread,
            self.node_heart_beat,
            self.node_task_processing,
            self.node_task_entire_processing,
            self.monitor_kill_signal,
            self.node_process_worker
        ]
        for t in ts:
            threading.Thread(target=t, daemon=True).start()

    # ------------------ 子进程管理 ------------------
    def ensure_child_process(self):
        if self.current_task_process is None or not self.current_task_process.is_alive():
            self.logger.info("重燃子进程及模型加载...")
            self.current_task_process = multiprocessing.Process(
                target=run_task_entry,
                args=(self.node_cfg, self._task_queue, self._progress_queue, self._entire_queue)
            )
            self.current_task_process.start()

    # ------------------ 核心任务调度 ------------------
    def node_process_worker(self):
        while True:
            self.live_status = 2
            time.sleep(2)
            if self.report_id: continue

            try:
                url = f"http://{self.node_cfg['master_ip']}:{self.node_cfg['master_port']}/ai-master-svr/handle/"
                res = requests.post(url, data={'node_no': self.node_cfg['node_id']}, timeout=5)
                data = res.json()
                if not data.get('report_id'): continue

                self.report_id = data['report_id']
                self.db_id = data.get('db_id', -1)
                self.kill_flag = False
                self.current_task_status = None

                # 参数规整
                p = data.get('param')
                self.current_param = json.loads(p) if isinstance(p, str) else p

                self.ensure_child_process()
                self.live_status = 1
                self._task_queue.put(self.current_param)
                self.logger.info(f"🚀 任务启动 | ID: {self.report_id} | DB_ID: {self.db_id}")

                while self.report_id is not None:
                    if not self.current_task_process.is_alive():
                        if not self.kill_flag and self.current_task_status is None:
                            self.logger.error(f"❌ 子进程崩溃 | ID: {self.report_id}")
                            self.current_task_status = "failed"
                            self.update_task_status("failed")
                        self.reset_all()
                        break
                    time.sleep(1)
            except: continue

    # ------------------ kill 监控 ------------------
    def monitor_kill_signal(self):
        url = f"http://{self.node_cfg['master_ip']}:{self.node_cfg['master_port']}/ai-master-svr/get-task-info/"
        while True:
            time.sleep(1.5)
            if not self.report_id: continue
            try:
                res = requests.post(url, data={'report_id': self.report_id}, timeout=3)
                if res.status_code == 200 and res.json().get('data', {}).get('deal_status') == 'killed':
                    self.kill_flag = True
                    self.current_task_status = "killed"
                    self.logger.info(f"🛑 收到中止指令 | ID: {self.report_id}")

                    self.update_task_status("killed")

                    if self.current_task_process and self.current_task_process.is_alive():
                        self.current_task_process.terminate()
                    self.reset_all()
                    self.logger.info(f"✅ 已终止程序 | ID: {self.report_id}")

            except Exception as e: 
                self.logger.error(f"ProcessFramework.monitor_kill_signal ERROR: {e}")
                # pass

    # ------------------ 状态上报 ------------------
    def update_task_status(self, status):
        url = f"http://{self.node_cfg['master_ip']}:{self.node_cfg['master_port']}/ai-master-svr/update-task-status/"
        send_data = {
            'node_no': self.node_cfg['node_id'],
            'status': status,
            'report_id': self.report_id,
            'db_id': self.db_id,
        }
        self.logger.info(f"[{send_data}] - 状态更新上报 - {os.path.basename(__file__)}:{sys._getframe().f_lineno}")
        try:
            requests.post(url, data=send_data, timeout=4)
            self.logger.info(f"[{send_data}] - ✅状态更新上报完成 - {os.path.basename(__file__)}:{sys._getframe().f_lineno}")
        except Exception as e: 
            self.logger.error(f"ProcessFramework.update_task_status ❌状态更新失败, ERROR: {e}")

    # ------------------ 子进程进度处理 ------------------
    def _read_progress_queue_thread(self):
        while True:
            try:
                msg = self._progress_queue.get(timeout=1)
                if not self.report_id: continue

                if self.kill_flag:
                    self.current_task_status = "killed"
                    self.update_task_status("killed")
                    self.reset_all()
                    continue

                status = msg.get("status", None)
                if status in ("ok", "failed"):
                    self.current_task_status = status
                    self.update_task_status(status)
                    self.reset_all()
                else:
                    # 普通进度消息，可追加到批量队列
                    with self.progress_callback_lock:
                        self.progress_callback_queue.append(msg)
            except: continue

    # ------------------ 重置 ------------------
    def reset_all(self):
        self.report_id = None
        self.db_id = -1
        self.kill_flag = False
        self.current_task_status = None
        with self.progress_callback_lock: self.progress_callback_queue.clear()
        with self.entire_progress_callback_lock: self.entire_progress_callback = {}

    # ------------------ 心跳 / 批量上报 ------------------
    def node_heart_beat(self):
        while True:
            time.sleep(5)
            url = f"http://{self.node_cfg['master_ip']}:{self.node_cfg['master_port']}/ai-master-svr/report-node-live-status/"
            try:
                requests.post(url, data={'node_no': self.node_cfg['node_id'], 'node_op_status': self.live_status}, timeout=4)
            except: pass

    def node_task_processing(self):
        url = f"http://{self.node_cfg['master_ip']}:{self.node_cfg['master_port']}/ai-master-svr/report-node-processing-batch/"
        while True:
            time.sleep(1)
            if not self.report_id: continue
            with self.progress_callback_lock:
                if not self.progress_callback_queue: continue
                info = list(self.progress_callback_queue)
                self.progress_callback_queue.clear()
            try:
                requests.post(url, json={'report_id': self.report_id, 'node_no': self.node_cfg['node_id'], 'task_info': info}, timeout=4)
            except: pass

    def node_task_entire_processing(self):
        url = f"http://{self.node_cfg['master_ip']}:{self.node_cfg['master_port']}/ai-master-svr/update-task-entire-progress/"
        while True:
            time.sleep(1)
            if not self.report_id: continue
            with self.entire_progress_callback_lock:
                if not self.entire_progress_callback: continue
                prog = self.entire_progress_callback
                self.entire_progress_callback = {}
            try:
                requests.post(url, json={'report_id': self.report_id, 'node_no': self.node_cfg['node_id'], 'db_id': self.db_id, 'progress': prog}, timeout=4)
            except: pass

    def _read_entire_queue_thread(self):
        while True:
            try:
                func_cb = self._entire_queue.get(timeout=1)
                with self.entire_progress_callback_lock: self.entire_progress_callback = func_cb
            except: continue

    # ------------------ 信号处理 ------------------
    def _handle_sigint(self, signum, frame):
        if self.current_task_process and self.current_task_process.is_alive():
            self.current_task_process.terminate()
        os._exit(0)