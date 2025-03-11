#!/bin/bash

echo "Deploying streamlit app..."

# 切换到工作目录
cd /root/streamlit

# 保存当前 commit hash
BEFORE_PULL=$(git rev-parse HEAD)

# 获取最新代码
unset GIT_DIR
git pull origin main

# 保存 pull 后的 commit hash
AFTER_PULL=$(git rev-parse HEAD)

# 检查是否有更新
if [ "$BEFORE_PULL" == "$AFTER_PULL" ]; then
    echo "No updates found, exiting..."
    exit 0
else
    echo "Updates found, rebuilding Docker container..."
    # 重建并重启 Docker 容器
    docker build -t stockview .
    docker rm -f stockview
    docker run -d --name stockview -p 8501:8501 stockview
    echo "Deployment completed"
fi