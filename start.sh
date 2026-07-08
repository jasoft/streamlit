#!/bin/bash
# 启动 nginx
service nginx start

# 启动 next.js 并在后台运行
cd /app/nextjs
npm start &

# 切回主目录并启动 streamlit，运行在 8502 端口
cd /app
uv run streamlit run stockview/app.py --server.port 8502
