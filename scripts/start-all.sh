#!/bin/bash
# ManySelves 启动脚本（别名）
# 前端已嵌入 FastAPI，一键启动

set -e

# 切换到项目根目录
cd "$(dirname "$0")/.."

# 直接调用 start.sh
exec scripts/start.sh