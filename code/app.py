
from flask import Flask
from flask import request, jsonify
from flask_cors import CORS
from ruamel.yaml import YAML
import requests
import uuid
import os, sys
import time
from src.SysLogger import CSysLogger
from src.ProcessFramework import ProcessorFramework

base_dir = os.path.dirname(os.path.abspath(__file__))
flask_app = Flask(__name__)

# 配置CORS，允许跨域请求
CORS(flask_app, resources={r"/*": {"origins": ["http://work.datashell.cn:8502","http://work.datashell.cn:31093", "*"]}})
yaml = YAML()
yaml.preserve_quotes = True  # 保留引号
yaml.indent(mapping=2, sequence=4, offset=2)
yaml.allow_unicode = True
yaml.width = 120
yaml.default_flow_style = False
logger = CSysLogger("app")
class CNodeApp:
    def __init__(self,config_name):
        # 1. 读取部署文件config.yml
        self.configs = None
        self.node_cfg ={}
        with open(base_dir + '/configs/' + config_name + '.yml', 'r', encoding='utf-8') as f:
            self.configs = yaml.load(f)

        # Setting up two queues
        for item in self.configs:
            self.node_cfg[item] = self.configs[item]
        
        # 2. 读取toolconfig.yml配置下的ToolId
        with open(base_dir + "/src/tools/" + self.node_cfg['tool_package_name'] + '/toolconfig.yml', 'r', encoding='utf-8') as f:
            tool_configs = yaml.load(f)
        
        # 重新设置临时目录（临时目录加上config_name）
        self.node_cfg['tmp_data'] =  os.path.join(self.node_cfg['tmp_data'] ,config_name)
        self.node_cfg['persist_data'] =  os.path.join(self.node_cfg['persist_data'], tool_configs['tool_package_snumber'])
        self.node_cfg['loading_tools'] = self.node_cfg['loading_tools']
        if 'cloud_storage_dconn_cfg' in self.node_cfg:
            self.node_cfg['dconn_name'] = self.node_cfg['cloud_storage_dconn_cfg']
        self.node_cfg['config_name'] =  config_name

        # 自动创建ai目录, 包括各级目录
        aipre_dir = os.path.join(self.node_cfg['tmp_aidata_exchange'].rstrip('/'), self.node_cfg['config_name'])
        if not os.path.exists(aipre_dir):
            os.makedirs(aipre_dir)

        # 工具所有配置传入node_cfg
        for item in tool_configs:
            self.node_cfg[item] = tool_configs[item]
        
        try:
            mqttcfg = self.configs['progressCfg']['mqtt']
            self.node_cfg['mqtt'] = mqttcfg
        except Exception as e:
            self.node_cfg['mqtt'] = ""
        
        """
        插个眼: masterip都不填的话, 证明他是一个单机工具, 就不运行调度服务了
        """        
        if "" != self.node_cfg['master_ip']:
            self.checkConfigValid()
        # 进行配置文件有效性验证
        self.processor = ProcessorFramework(self.node_cfg)
    
    # 对整个node配置文件进行验证
    def checkConfigValid(self):
        #对master_ip和master_port有效性进行校验，通过尝试请求的方式验证
        try:
            logger.info("成功连接master服务器")
            self.registerNode2Master()
            self.node_cfg['master_valid'] = True
        except Exception as e:
            logger.error("不能连接master服务器: {}".format(e))
            return
    
    def getNodeCfg(self):
        return self.node_cfg
    
    def getProcessorObj(self):
        return self.processor
    
    # 退出之前处理的事件
    def onClose(self,signum,frame):
        logger.info(f"收到信号 {signum}，程序即将退出...")
        self.processor.onClose()
        sys.exit(0) 

    # 注册节点
    def registerNode2Master(self):
        # 如果master节点无效，则无需注册
        tool_package_snumber = self.node_cfg['tool_package_snumber'] # 工具id(平台申请)
        node_loc = self.node_cfg['deploy_loc'] # 节点的物理部署位置
        deal_type_version = '001' # 处理类型描述, 这个可能要删除
        node_skills = ['11'] # 这个是定义节点要发布的工具列表

        # 自动检查调度状态并且确保自身节点信息一定被master记录
        while True:
            time.sleep(0.5)
            if "" == self.node_cfg["node_id"]:
                gen_port = str(uuid.uuid4())
            else:
                gen_port = self.node_cfg["node_id"]
            url = "http://{}:{}/ai-master-svr/register-node/".format(self.node_cfg['master_ip'], self.node_cfg['master_port'])
            send_data = {
                'node_no': gen_port,
                'deal_type_no': tool_package_snumber,
                'deal_type_version': deal_type_version,
                'node_loc': node_loc,
                'node_skills': node_skills
            }
            try:
                res = requests.post(url, data=send_data, timeout=5)
                if res.status_code == 200:
                    res_json = res.json()
                    if res_json.get('data') != {}:
                        self.configs['node_id'] = gen_port
                        # 同步更新到 node_cfg的node_id
                        self.node_cfg['node_id'] = gen_port
                        with open(base_dir + '/configs/' + config_name + '.yml', 'w', encoding='utf-8') as f:
                            yaml.dump(self.configs, f)
                        logger.info("注册成功")
                        break

                    else:
                        logger.info("注册失败: {}".format(res_json.get('data')))
                else:
                    logger.info("HTTP状态异常: {}".format(res.status_code))
            except Exception as e:
                logger.error("节点请求 master 失败: {}, e: {}".format(url, e))
        time.sleep(0.5)
    
    def checkParamValid(self,param,paramlist):
        """
        @param param: 输入参数
        @param paramlist: 参数列表 ['id','users']
        @return:
        flag: True/False
        retjson: 错误的对象
        """
        msg = 'error param : '
        flag = True
        for paramitem in paramlist:
            if paramitem not in param:
                flag =  False
                msg  = msg + paramitem + ','
        if not flag:
            msg = msg.strip(',')
        return flag,msg

    def quoted_presenter(self,dumper, data):
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='"')    

@flask_app.route('/api/<apimodule>/<apiclass>/<apimethod>', methods=['POST']) # 处理任务
def apiprocess(apimodule,apiclass, apimethod):
    param = request.form.to_dict()
    res = {}
    try:
        res = app.getProcessorObj().ProcessAPI('api',apimodule,apiclass, apimethod, param)
    except :
        res = {
        'code': -1,
        'msg': 'failed',
        'data': {},
    }
    return jsonify(res)

@flask_app.route('/html/<apimodule>/<apiclass>/<apimethod>', methods=['GET']) # HTML的请求链接
def htmlprocess(apimodule,apiclass, apimethod):
    param = request.args.to_dict()
    logger.info(param)
    res = ''
    try:
        res = app.getProcessorObj().ProcessAPI('html',apimodule,apiclass, apimethod, param)
    except :
        res = '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Document</title></head><body><h1>Hello World!</h1></body></html>'
    return res

def check_ip_port(ip, port):
    """
    检查IP地址和端口是否有效
    :param ip: IP地址
    :param port: 端口号
    :return: 如果有效则返回True，否则返回False
    """
    import socket
    try:
        socket.create_connection((ip, port), timeout=5)
        return True
    except:
        return False

if __name__ == '__main__':
    """
    @param 配置文件名, 输入一个放在config目录下的 xxx名称(不带路径,不带.yml)
    """
    config_name = ''
    if len(sys.argv) < 2:
        config_name = 'config'
    else:
        config_name = sys.argv[1]
    app = CNodeApp(config_name)
    if "debug" == app.getNodeCfg()['dev_mode']: # 服务器开发
        # 配置文件可以设置多个端口，依次尝试打开，直到成功为止
        port = app.getNodeCfg()['port'] 
        flask_app.run(host='0.0.0.0', port=port)      
    else: # 生产环境
        flask_app.run(host='0.0.0.0', port=8080)