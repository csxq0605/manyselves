#!/bin/bash
# ManySelves 启动脚本（WSL 优化版）

set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "ManySelves Web API 启动"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 激活虚拟环境
if [ -f ".venv/bin/activate" ]; then
    echo "✓ 激活虚拟环境"
    source .venv/bin/activate
else
    echo "✗ 未找到虚拟环境，使用系统 Python"
fi

# 检查端口
PORT=${MANYSELVES_HTTP_PORT:-9000}
echo ""
echo "端口配置: $PORT"
echo ""

# 清理可能的残留进程
pkill -f "python.*run_web" 2>/dev/null || true
sleep 2

# 启动服务（不使用 reload 模式）
echo "启动服务..."
echo ""
python run_web.py