#!/bin/bash
set -e

APP_NAME="stockview"
ENV_FILE=".env.production"

echo "Deploying ${APP_NAME}..."

# 拉取最新代码
unset GIT_DIR
git pull origin main

# 构建镜像
DOCKER_HOST=ssh://docker docker build -t ${APP_NAME} .

# 停止并删除旧容器
DOCKER_HOST=ssh://docker docker rm -f ${APP_NAME} 2>/dev/null || true

# 启动新容器，加载环境变量文件
if [ -f "${ENV_FILE}" ]; then
  echo "Loading env from ${ENV_FILE}"
  DOCKER_HOST=ssh://docker docker run -d \
    --name ${APP_NAME} \
    -p 8501:8501 \
    -p 3000:3000 \
    --env-file ${ENV_FILE} \
    --restart unless-stopped \
    ${APP_NAME}
else
  echo "WARNING: ${ENV_FILE} not found, starting without env file"
  DOCKER_HOST=ssh://docker docker run -d \
    --name ${APP_NAME} \
    -p 8501:8501 \
    -p 3000:3000 \
    --restart unless-stopped \
    ${APP_NAME}
fi

echo "Deployment completed. Service: http://docker.home:8501 and http://docker.home:3000"
