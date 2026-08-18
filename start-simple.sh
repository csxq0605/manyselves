#!/usr/bin/env bash
# 简单启动脚本 - 不使用 reload 模式，避免端口冲突

set -e

echo "🚀 启动 ManySelves Web API..."
echo ""

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "❌ 未找到虚拟环境，请先运行："
    echo "   python -m venv .venv"
    echo "   source .venv/bin/activate  # Linux/Mac"
    echo "   .venv\\Scripts\\activate      # Windows"
    exit 1
fi

# 激活虚拟环境（如果在 WSL）
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# 检查端口是否被占用
check_port() {
    local port=$1
    if netstat -tuln 2>/dev/null | grep -q ":$port " || \
       ss -tuln 2>/dev/null | grep -q ":$port "; then
        echo "⚠️  端口 $port 已被占用"
        echo "   请先关闭占用端口的进程："
        echo "   lsof -ti:$port | xargs kill -9  # Linux/Mac"
        echo "   或更换端口："
        echo "   MANYSELVES_HTTP_PORT=9001 python run_web.py"
        return 1
    fi
    return 0
}

# 检查配置的端口
PORT=${MANYSELVES_HTTP_PORT:-9000}
if ! check_port $PORT; then
    exit 1
fi

# 启动服务（不使用 reload 模式）
python run_web.py

# 如果需要开发模式（自动重载），使用：
# python run_web.py --reload