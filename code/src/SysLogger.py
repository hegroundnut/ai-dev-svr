import logging
import colorlog

class CSysLogger:
    def __init__(self, task_id):
        self.task_id = task_id

        # 设置日志颜色和级别
        log_colors = {
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'bold_red',
        }

        # 创建一个formatter，格式化输出
        self.formatter = colorlog.ColoredFormatter(
            "[%(asctime)s] - %(log_color)s%(levelname)s%(reset)s - \033[34m%(task_id)s\033[0m - [%(log_color)s%(message)s%(reset)s] - %(pathname)s:%(lineno)d",
            log_colors=log_colors
        )

        # 设置日志输出
        self.handler = colorlog.StreamHandler()

        # 使用formatter格式化handler
        self.handler.setFormatter(self.formatter)

        # 每个task_id都有独立的logger
        self.logger = logging.getLogger(f"Logger_{task_id}")
        self.logger.setLevel(logging.INFO)

        # 只添加一个handler，避免重复
        if not self.logger.handlers:
            self.logger.addHandler(self.handler)

        # 为日志加上task_id的过滤器
        task_id_filter = TaskIdFilter(lambda: self.task_id)
        self.logger.addFilter(task_id_filter)

    def log(self, level, msg):
        # 使用stacklevel确保记录调用位置正确（适用于Python 3.8+）
        self.logger.log(level, "%s", msg, stacklevel=3)

    def debug(self, msg):
        self.log(logging.DEBUG, msg)

    def info(self, msg):
        self.log(logging.INFO, msg)

    def warning(self, msg):
        self.log(logging.WARNING, msg)

    def error(self, msg):
        self.log(logging.ERROR, msg)

    def critical(self, msg):
        self.log(logging.CRITICAL, msg)

    def setTask(self, task_id):
        # 更新当前task_id
        self.task_id = task_id

    def clearTask(self):
        # 清空当前task_id
        self.task_id = '无任务'


class TaskIdFilter(logging.Filter):
    def __init__(self, get_task_id_func):
        super().__init__()
        self.get_task_id_func = get_task_id_func

    def filter(self, record):
        record.task_id = self.get_task_id_func()
        return True