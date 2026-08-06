#!/bin/bash
# ManySelves 完整启动脚本（前端 + 后端）

set -e

# 切换到项目根目录
cd "$(dirname "$0")/.."

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  ManySelves 完整启动${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 步骤 1: 检查并构建前端
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${YELLOW}1️⃣  检查前端构建...${NC}"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

FRONTEND_DIST="frontend/dist"
NEED_BUILD=false

if [ ! -d "$FRONTEND_DIST" ] || [ ! -f "$FRONTEND_DIST/index.html" ]; then
    NEED_BUILD=true
    echo -e "${YELLOW}📦 前端未构建，开始构建...${NC}"
else
    # 检查源码是否有更新
    if [ -d "frontend/src" ]; then
        LATEST_SRC=$(find frontend/src -type f -name "*.tsx" -o -name "*.ts" | xargs stat -c %Y 2>/dev/null | sort -rn | head -1)
        LATEST_DIST=$(stat -c %Y "$FRONTEND_DIST/index.html" 2>/dev/null || echo 0)

        if [ "$LATEST_SRC" -gt "$LATEST_DIST" ] 2>/dev/null; then
            NEED_BUILD=true
            echo -e "${YELLOW}🔄 检测到源码更新，重新构建前端...${NC}"
        fi
    fi
fi

if [ "$NEED_BUILD" = true ]; then
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
    fi

    # 构建前端
    echo -e "${YELLOW}🔨 构建前端...${NC}"
    "$NPM_BIN" run build

    cd ..
    echo -e "${GREEN}✅ 前端构建完成${NC}"
else
    echo -e "${GREEN}✅ 前端已构建，跳过构建步骤${NC}"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 步骤 2: 启动后端
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${YELLOW}2️⃣  启动后端服务...${NC}"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ 错误：未找到 Python 3${NC}"
    echo "请先安装 Python 3.12+"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | awk '{print $2}')
echo -e "✅ Python 版本: ${PYTHON_VERSION}"

# 检查 venv 模块是否可用
if ! python3 -c "import venv" 2>/dev/null; then
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}❌ 错误：Python venv 模块不可用${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "在 Ubuntu/Debian 系统中，需要单独安装 python3-venv 包。"
    echo ""
    echo "请运行以下命令安装："
    echo ""
    echo -e "${YELLOW}  sudo apt update${NC}"
    echo -e "${YELLOW}  sudo apt install python3-venv${NC}"
    echo ""
    echo "或者安装特定版本的包（如果上面的命令不工作）："
    echo ""
    echo -e "${YELLOW}  sudo apt install python${PYTHON_VERSION%%.*}-venv${NC}"
    echo ""
    echo "安装完成后，重新运行此脚本。"
    exit 1
fi

# 检查虚拟环境
ACTIVATE_SCRIPT=""
if [ -f ".venv/bin/activate" ]; then
    # Linux/macOS 虚拟环境
    ACTIVATE_SCRIPT=".venv/bin/activate"
    echo -e "${GREEN}✅ 检测到 Linux 虚拟环境${NC}"
elif [ -f ".venv/Scripts/activate" ]; then
    # Windows 虚拟环境（在 Git Bash/WSL 中）
    # 提示用户需要重新创建
    echo -e "${YELLOW}⚠️  检测到 Windows 创建的虚拟环境${NC}"
    echo -e "${YELLOW}   正在删除并重新创建...${NC}"
    rm -rf .venv
    python3 -m venv .venv
    ACTIVATE_SCRIPT=".venv/bin/activate"
else
    # 不存在虚拟环境，创建新的
    echo -e "${YELLOW}📦 创建虚拟环境（首次运行）...${NC}"
    python3 -m venv .venv
    ACTIVATE_SCRIPT=".venv/bin/activate"
fi

# 激活虚拟环境
echo -e "${YELLOW}🔧 激活虚拟环境...${NC}"
source "$ACTIVATE_SCRIPT"

# 安装依赖
if [ ! -f ".venv/.installed" ]; then
    echo -e "${YELLOW}📦 安装依赖（首次运行）...${NC}"
    pip install --upgrade pip
    pip install -e .
    touch .venv/.installed
    echo -e "${GREEN}✅ 依赖安装完成${NC}"
else
    echo -e "${GREEN}✅ 依赖已安装，跳过安装${NC}"
fi

# 检查环境变量
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}⚠️  未找到 .env 文件${NC}"
    echo "使用默认配置..."

    # 创建默认 .env（使用一致的端口 9090）
    cat > .env << EOF
MANYSELVES_DATA_ROOT=.manyselves
MANYSELVES_INITIAL_PROJECT_ID=default
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=yuanxi@2026
MANYSELVES_HTTP_BIND=127.0.0.1
MANYSELVES_HTTP_PORT=9090
MANYSELVES_ALLOWED_ORIGINS='["http://localhost:9090"]'
EOF
    echo -e "${GREEN}✅ 创建默认 .env 文件${NC}"
fi

# 加载环境变量
export $(cat .env | grep -v '^#' | xargs)

# 检查 API Key（使用不带前缀的变量名）
if [ -z "$OPENAI_API_KEY" ] && [ -z "$ANTHROPIC_API_KEY" ]; then
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}⚠️  警告：未设置 API Key${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "请设置环境变量："
    echo "  export OPENAI_API_KEY=your-api-key"
    echo ""
    echo "或编辑 .env 文件添加："
    echo "  OPENAI_API_KEY=your-api-key"
    echo ""
    echo -e "${YELLOW}提示：按 Ctrl+C 停止服务${NC}"
    echo ""
fi

# 从环境变量获取端口（支持动态显示）
HTTP_PORT="${MANYSELVES_HTTP_PORT:-9000}"

# 启动服务
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}🚀 启动服务...${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  访问地址: ${GREEN}http://localhost:${HTTP_PORT}${NC}"
echo -e "  API 文档: ${GREEN}http://localhost:${HTTP_PORT}/docs${NC}"
echo ""
echo -e "${YELLOW}提示：按 Ctrl+C 停止服务${NC}"
echo ""

python run_web.py --reload
