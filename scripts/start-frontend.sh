#!/bin/bash
# ManySelves 前端启动脚本（Linux/macOS）

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  ManySelves 前端启动${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# 检查 Node.js
NODE_BIN=""
if command -v node &> /dev/null; then
    NODE_BIN="$(command -v node)"
elif command -v node.exe &> /dev/null; then
    NODE_BIN="$(command -v node.exe)"
fi

if [ -z "$NODE_BIN" ]; then
    echo -e "${RED}❌ 错误：未找到 Node.js${NC}"
    echo "请先在当前终端环境安装 Node.js 22+；WSL 中运行时需要 WSL 能找到 node 或 node.exe"
    exit 1
fi

NPM_BIN=""
if command -v npm &> /dev/null; then
    NPM_BIN="$(command -v npm)"
fi

if [ -z "$NPM_BIN" ]; then
    echo -e "${RED}❌ 错误：未找到 npm${NC}"
    echo "请先在当前终端环境安装 npm，或确认 Node.js 安装目录已加入 PATH"
    exit 1
fi

NODE_VERSION=$("$NODE_BIN" --version | sed 's/v//')
echo -e "✅ Node 版本: ${NODE_VERSION}"

# 进入前端目录
cd frontend

# 检查 node_modules
if [ ! -d "node_modules" ]; then
    echo -e "${YELLOW}📦 安装前端依赖...${NC}"
    "$NPM_BIN" install
    echo -e "${GREEN}✅ 依赖安装完成${NC}"
fi

# 启动前端
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}🚀 启动前端开发服务器...${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  前端地址: ${GREEN}http://localhost:5173${NC}"
echo ""
echo -e "${YELLOW}提示：按 Ctrl+C 停止服务${NC}"
echo ""

"$NPM_BIN" run dev
