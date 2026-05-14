#!/bin/bash
# 获取脚本文件所在的绝对路径
SCRIPT_PATH=$(realpath $0)
# 获取脚本文件所在的目录
SCRIPT_DIR=$(dirname $SCRIPT_PATH)
# 获取系统的当前登录名
DST_USER_NAME=""$(whoami)
# DST_USER_NAME="root"
# 虚拟环境路径
ENV_NAME="cskin"
# 部署路径
DEPLOY_LOC="/data_ware/tool_deploy/"$DST_USER_NAME"/ds_node"
# 应用程序路径
APP_PATH=$DEPLOY_LOC"/app.py"
# 日志文件路径
LOG_PATH="$DEPLOY_LOC/../logs/"

# 检查进程是否正在运行
function is_running {
    pgrep -f "python -u $APP_PATH $1" #> /dev/null
}

# 启动应用程序
function start_app
 {
    echo "Starting app..."
    # 检查是否安装了 conda
    if command -v conda &> /dev/null
    then
        source /usr/local/anaconda3/etc/profile.d/conda.sh
        conda activate $ENV_NAME
    else
        echo "Conda is not installed. Skipping environment activation."
    fi
    nohup env PYTHONUNBUFFERED=1 python -u $APP_PATH $1 > $LOG_PATH$1".log" 2>&1 &
    echo "App started. Logs are in "$LOG_PATH$1".log"
    # tail -f $LOG_PATH$1".log"
}

# 停止应用程序
function stop_app {
    echo "Stopping app for config: $1..."
    
    # 1. 使用 pgrep 找到【完整匹配】该配置参数的进程 ID
    # \b 是单词边界，防止 config1 匹配到 config11
    local pid=$(pgrep -f "python -u $APP_PATH $1\b")

    if [ -z "$pid" ]; then
        echo "No running process found for config: $1"
        return
    fi

    # 2. 尝试优雅停止 (SIGTERM)
    echo "Sending SIGTERM to PID: $pid"
    kill $pid
    
    # 3. 等待最多 5 秒检查进程是否退出
    for i in {1..5}; do
        if ! pgrep -f "python -u $APP_PATH $1\b" > /dev/null; then
            echo "App [$1] stopped successfully."
            return
        fi
        sleep 1
    done

    # 4. 如果还没停，强制杀掉 (SIGKILL)
    echo "Process still alive, forcing kill -9..."
    kill -9 $pid
}

# 重启应用程序
function restart_app {
    stop_app $1
    sleep 2
    start_app $1
}

function MainConsole
{
    if [ "$1" == "deploy" ] 
    then
        #  如果目标目录不存在则自动创建
        if [ ! -d $DEPLOY_LOC ]; then
            mkdir -p $DEPLOY_LOC
        fi
        if [ ! -d $LOG_PATH ]; then
            mkdir -p $LOG_PATH
        fi
        rm -rf $DEPLOY_LOC
        # 使用rsync替代cp，排除指定目录
        rsync -a --exclude="src/tools/ds_tpsvr_0003_VisionModelTrain/data/tmp" $SCRIPT_DIR/ $DEPLOY_LOC/
    elif [ "$1" == "start" ]
    then
        start_app $2
    elif [ "$1" == "stop" ]
    then
        stop_app $2
    elif [ "$1" == "restart" ]
    then
        restart_app $2
    elif [ "$1" == "isrunning" ]
    then
        is_running $2
    elif [ "$1" == "logs" ]
    then
        tail -f $LOG_PATH$2".log"
    elif [ "$1" == "remote_deploy" ]
    then
        echo $SCRIPT_DIR
        echo $DEPLOY_LOC
        # 通过 SSH 远程创建目录
        ssh -p $3 $2 "mkdir -p $DEPLOY_LOC"
        ssh -p $3 $2 "mkdir -p $LOG_PATH"
        # 使用rsync替代scp，排除指定目录
        rsync -av -e "ssh -p $3" --exclude="src/tools/ds_tpsvr_0003_VisionModelTrain/data/tmp" $SCRIPT_DIR/ $2:$DEPLOY_LOC/
        echo "remote deploy done"
    elif [ "$1" == "remote_login" ]
    then
        ssh -t -p $3 $2 "cd $DEPLOY_LOC/ && bash"
        echo "remote login done"
    else
        echo "命令格式: ./release_run.sh <命令> <配置文件名>"
        echo "--<配置文件名> 指configs目录中的配置文件名称(不包含.yml)"
        echo "./release_run.sh deploy               #把toolbox代码拷贝到指定的/data_ware/tool_deploy目录中"
        echo "./release_run.sh start    <配置文件名>  #启动"
        echo "./release_run.sh stop     <配置文件名>  #停止"
        echo "./release_run.sh restart  <配置文件名>  #重启"
        echo "./release_run.sh logs     <配置文件名>  #查看的运行日志"
        echo "./release_run.sh remote_deploy <用户名@IP> <PORT> #把toolbox代码拷贝远程服务器到指定的/data_ware/tool_deploy目录中"
        echo "./release_run.sh remote_login <用户名@IP> <PORT> #登录远程服务器"
    fi
}

# $1 命令类型：deploy/start/stop/restart
# $2 配置文件名，指定在configs目录下的配置文件名称（不包含.yml)
MainConsole $1 $2 $3

# 创建远程服务器免密登陆
# ssh-keygen -t rsa -b 4096 -C "your_email@example.com"
# ls ~/.ssh/id_*  # 确认存在 id_rsa（私钥）和 id_rsa.pub（公钥）
# ssh-copy-id -i ~/.ssh/id_rsa.pub username@remote_ip