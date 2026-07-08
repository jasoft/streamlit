# 使用 Ubuntu 24.04 作为基础镜像 (包含更新的 glibc)
FROM ubuntu:24.04

# 避免交互式提示
ENV DEBIAN_FRONTEND=noninteractive

# 安装 Python、Node.js 和 Nginx
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    nginx \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# 设置 Nginx 配置
COPY nginx.conf /etc/nginx/sites-available/default

# 设置工作目录
WORKDIR /app

# 安装 uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

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
