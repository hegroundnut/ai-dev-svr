FROM python:3.8

RUN echo "deb http://mirrors.tuna.tsinghua.edu.cn/debian/ buster main contrib non-free" > /etc/apt/sources.list \
    && echo "deb http://mirrors.tuna.tsinghua.edu.cn/debian/ buster-updates main contrib non-free" >> /etc/apt/sources.list \
    && echo "deb http://mirrors.tuna.tsinghua.edu.cn/debian-security buster/updates main contrib non-free" >> /etc/apt/sources.list \
    && apt-get update

RUN apt update && apt install -y \
    build-essential \
    cmake \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6

RUN pip3 install setuptools
#RUN ["apt-get","install","vim","-y","--allow-unauthenticated"]


ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN mkdir -p /app
COPY . /app
WORKDIR /app

# 添加环境变量设置到 .bashrc
ENV LD_LIBRARY_PATH=/usr/lib64:$LD_LIBRARY_PATH

RUN tool_package_name=$(grep -oP '"tool_package_name":\s*"\K[^"]+' /app/config.yml) && \
    echo "Tool name is: ${tool_package_name}" && \
    cp "/app/src/tools/${tool_package_name}/requirements.txt" "/app/requirements.txt"

ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple 

RUN pip install --upgrade pip
RUN pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple



CMD [ "python", "app.py" ,"config"]
