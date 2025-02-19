# 使用官方的 Python 基础镜像
FROM python:3.13.1-slim

# 设置工作目录
WORKDIR /app

# 安装 uv
RUN pip install --no-cache-dir uv

# 复制当前目录内容到工作目录
COPY . /app

# 使用 uv 安装依赖
RUN uv sync

# 暴露 Streamlit 默认端口
EXPOSE 8501

# 运行 Streamlit 应用
CMD ["uv","run","streamlit", "run", "stockview/main.py"]