#!/bin/bash
# 一键部署脚本（简化版，适合局域网环境）
# 无严格权限限制，直接运行

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  ManySelves 一键部署（简化版）${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ 错误：未找到 Docker${NC}"
    exit 1
fi

if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo -e "${RED}❌ 错误：未找到 Docker Compose${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Docker 已安装${NC}"

# 切换到部署目录
cd deploy

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 步骤 1: 初始化配置文件
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${YELLOW}步骤 1/4: 初始化配置文件${NC}"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

CONFIG_FILE="config/manyselves.config.yaml"
EXAMPLE_FILE="config/manyselves.config.example.yaml"

if [ ! -f "$CONFIG_FILE" ]; then
    if [ ! -f "$EXAMPLE_FILE" ]; then
        echo -e "${RED}❌ 错误：配置模板文件不存在${NC}"
        echo "请确保文件存在：deploy/$EXAMPLE_FILE"
        exit 1
    fi

    echo -e "${YELLOW}📋 复制配置模板...${NC}"
    cp "$EXAMPLE_FILE" "$CONFIG_FILE"
    echo -e "${GREEN}✅ 配置文件已创建${NC}"
else
    echo -e "${GREEN}✅ 配置文件已存在${NC}"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 步骤 2: 检查 API Key
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${YELLOW}步骤 2/4: 检查 API Key${NC}"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

if [ -z "$MANYSELVES_OPENAI_API_KEY" ] && [ -z "$MANYSELVES_ANTHROPIC_API_KEY" ]; then
    echo -e "${YELLOW}⚠️  警告：未设置 API Key${NC}"
    echo ""
    echo "请设置环境变量："
    echo "  export MANYSELVES_OPENAI_API_KEY=your-api-key"
    echo ""
    echo "或编辑配置文件："
    echo "  nano deploy/config/manyselves.config.yaml"
    echo ""
    echo -e "${YELLOW}继续部署（稍后设置 API Key）...${NC}"
else
    echo -e "${GREEN}✅ API Key 已设置${NC}"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 步骤 3: 构建镜像
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${YELLOW}步骤 3/4: 构建 Docker 镜像${NC}"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# 使用简化版 compose 文件
COMPOSE_FILE="compose.simple.yaml"

echo -e "${YELLOW}🔨 构建镜像（这可能需要几分钟）...${NC}"
docker-compose -f "$COMPOSE_FILE" build

echo -e "${GREEN}✅ 镜像构建完成${NC}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 步骤 4: 启动服务
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${YELLOW}步骤 4/4: 启动服务${NC}"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# 创建数据目录
mkdir -p ../data/manyselves

# 停止旧容器
echo -e "${YELLOW}🛑 停止旧容器（如果存在）...${NC}"
docker-compose -f "$COMPOSE_FILE" down 2>/dev/null || true

# 启动新容器
echo -e "${YELLOW}🚀 启动服务...${NC}"
docker-compose -f "$COMPOSE_FILE" up -d

# 等待健康检查
echo -e "${YELLOW}⏳ 等待服务启动（约 30 秒）...${NC}"
sleep 10

# 检查服务状态
if docker-compose -f "$COMPOSE_FILE" ps | grep -q "healthy\|running"; then
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}✅ 部署成功！${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "  访问地址: ${GREEN}http://localhost:9090${NC}"
    echo -e "  API 文档: ${GREEN}http://localhost:9000/docs${NC}"
    echo ""
    echo -e "${YELLOW}查看日志：${NC}"
    echo -e "  docker-compose -f $COMPOSE_FILE logs -f"
    echo ""
    echo -e "${YELLOW}停止服务：${NC}"
    echo -e "  docker-compose -f $COMPOSE_FILE down"
    echo ""
else
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}❌ 部署失败${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "查看日志："
    echo "  docker-compose -f $COMPOSE_FILE logs"
    exit 1
fi