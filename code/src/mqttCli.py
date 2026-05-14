import paho.mqtt.client as mqtt
from datetime import datetime
import json
import time

class CMqttCli:
    def __init__(self, mqttcfg):
        """
        host, port: MQTT服务器
        username, password: 登录认证
        topic: 订阅主题
        client_id: 如果需要离线消息队列，订阅端需要固定 client_id
        """
        host, port, username, password, topic, client_id = mqttcfg['host'], mqttcfg['port'], mqttcfg['username'], mqttcfg['password'], mqttcfg['topic'], mqttcfg['client_id']
        
        self.host = host
        self.port = port
        self.topic = topic
        self.username = username
        self.password = password
        self.client_id = client_id
        self.messages = {}  # 用于存储收到的消息
        self._init_client()

    
    def _init_client(self):
        # 创建客户端
        self.client = mqtt.Client(client_id=self.client_id, clean_session=False if self.client_id else True)
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

        # 设置回调
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect

        # 设置自动重连延迟
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

        # 连接服务器
        try:
            self.client.connect(self.host, self.port)
        except Exception as e:
            print("MQTT首次连接失败:", e)

        self.client.loop_start()  # 启动后台线程处理网络事件

    # 连接回调
    def on_connect(self, client, userdata, flags, rc):
        print(f"[连接返回码]: {rc}")
        if rc == 0:
            print("连接成功")
            if self.topic:
                print("self.topic: ", self.topic)
                result, mid = client.subscribe(self.topic, qos=1)
                print(f"订阅主题 {self.topic}, 结果: {result}, mid: {mid}")
        else:
            print(f"连接失败，返回码: {rc}")

    # 消息回调
    def on_message(self, client, userdata, msg):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            payload = json.loads(msg.payload.decode())
            info = payload
        except Exception as e:
            info = f"[{now}] {msg.topic} -> {msg.payload.decode()}"
            info = {'deal_time': int(time.time()), 'deal_percent': -1, 'deal_msg': 'error - {}'.format(e)}
        self.messages = info  # 保存到队列

    # 断开回调
    def on_disconnect(self, client, userdata, rc):
        print(f"[断开连接] 返回码: {rc}")
        if rc != 0:
            print("非正常断开，客户端将自动尝试重连")

    # 发布消息
    def pub(self, topic, msg, qos=1):
        try:
            info = self.client.publish(topic, msg, qos=qos)
            info.wait_for_publish()
            if info.is_published():
                print(f"[成功] 消息已发送")
            else:
                print(f"[失败] 消息发送失败")
        except Exception as e:
            # print("MQTT发送消息异常:", e)
            pass


    # 获取已接收消息并清空队列
    def sub(self):
        msgs = self.messages.copy()
        self.messages = {}
        return msgs

    # 停止客户端
    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()
        print("MQTT 客户端已断开")


if __name__ == '__main__':
    host = "47.98.151.104"
    port = 1883
    topic = "test-log"
    username = "admin"
    password = "Letseatbone874"
    mqttcfg = {
        'host': host,
        'port': port,
        'topic': topic,
        'username': username,
        'password': password,
        'client_id': "ds_master",

    }
    mqtt_logger = CMqttCli(mqttcfg)

    # 发布消息示例
    lists = []

    for i in range(3000):
        lists.append({'deal_time': int(time.time()), 'deal_percent': i, 'deal_msg': 'deal - {}'.format(i)})
    send_data = {
        'report_id': "44444",
        'node_no': 'zhl_test',
        'task_info': lists
    }
    for i in range(15): 
        mqtt_logger.pub(topic, json.dumps(send_data))
        print("消息发送完毕")

    # # 外部控制循环，轮询接收消息
    # try:
    #     while True:
    #         new_msgs = mqtt_logger.sub()
    #         print("new_msgs: ", new_msgs)
    #         for m in new_msgs:
    #             print("处理消息:", m)
    #         time.sleep(1)
    # except KeyboardInterrupt:
    #     mqtt_logger.stop()
