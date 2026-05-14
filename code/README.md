# 安装conda环境, 并激活环境
    conda create -n <YOUR_ENV> python=3.10
    conda activate <YOUR_ENV>
    pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 安装dstashell_conn包, 【可以忽略】
cd /home/gugm/workcode/tool_dev/ds_ai_svr/dssdk/datashell_conn && python setup.py bdist_wheel
pip install dist/datashell_conn-0.1.0-py3-none-any.whl -i https://pypi.tuna.tsinghua.edu.cn/simple


# 运行方法
## 1.执行下列命令
    python app.py [配置文件名称（不含yml）]
    如：python app.py dev

## 2. 注册能力
    2.0 访问open.datashell.cn, 登录
    2.1 计算管理->工具发布
    2.2 选择服务ip: work.datashell.cn,服务端口: 8500的网关
    2.3 节点配置能力ID为2046762615821168642,工具版本
    
# 简单调用
curl --location --request POST 'http://work.datashell.cn:8500/ai-master-svr/create-task/' \
--data-urlencode 'capability_id=2046762615821168642' \
--data-urlencode 'param=[{"dtype":"demo","version":"0.0.0","subfuncs":[{"func_name":"sum","func_desc":"A+B求和, 额外+1","params":{"a":0,"b":0}}]},{"dtype":"demo","version":"0.0.1","subfuncs":[{"func_name":"sum","func_desc":"A+B求和","params":{"a":0,"b":0}}]}]' \
--data-urlencode 'deal_port=<你执行python app.py dev后, dev.yml内的port值>'
--data-urlencode 'report_id=<你自己写一个uuid>' \
