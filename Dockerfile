# 使用官方的 Python 基础镜像
FROM python:3.13.1-slim

# 安装 Node.js
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 安装 uv
RUN pip install --no-cache-dir uv

# 复制当前目录内容到工作目录
COPY . /app

# 构建 Next.js 项目
WORKDIR /app/nextjs
RUN npm install
RUN npm run build

# 切回主工作目录
WORKDIR /app

# 使用 uv 安装依赖
RUN uv sync

# 暴露 Streamlit 和 Next.js 端口
EXPOSE 8501
EXPOSE 3000

# 运行启动脚本
CMD ["bash", "start.sh"]
