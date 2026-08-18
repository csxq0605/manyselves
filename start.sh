#!/bin/bash
# ManySelves 启动脚本
# 自动构建前端并启动 FastAPI 服务

# 切换到脚本所在目录
cd "$(dirname "$0")"

# 调用 scripts/start.sh
exec scripts/start.sh