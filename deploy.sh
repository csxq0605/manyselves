#!/bin/bash
# 完整部署流程（本地 Windows WSL 执行）

set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "ManySelves 部署工具"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 步骤 1：本地构建
echo "【步骤 1/3】本地构建镜像和打包源码"
echo ""
chmod +x scripts/build-local.sh
./scripts/build-local.sh

echo ""
echo "【步骤 2/3】传输文件到服务器"
echo ""
read -p "服务器地址 (默认: 192.168.8.28): " SERVER_HOST
SERVER_HOST=${SERVER_HOST:-192.168.8.28}

read -p "服务器用户 (默认: algo_001): " SERVER_USER
SERVER_USER=${SERVER_USER:-algo_001}

echo ""
echo "正在传输文件到 ${SERVER_USER}@${SERVER_HOST}..."
scp manyselves-images.tar manyselves-deploy.tar.gz \
  ${SERVER_USER}@${SERVER_HOST}:/home/${SERVER_USER}/

echo ""
echo "【步骤 3/3】服务器部署"
echo ""
echo "请 SSH 到服务器执行以下命令："
echo ""
echo "  ssh ${SERVER_USER}@${SERVER_HOST}"
echo "  mkdir -p ~/manyselves"
echo "  tar -xzf manyselves-deploy.tar.gz -C ~/manyselves --strip-components=0"
echo "  cd ~/manyselves"
echo "  chmod +x scripts/deploy-server.sh"
echo "  ./scripts/deploy-server.sh"
echo ""
echo "首次运行会提示设置 API Key，设置后再次运行即可完成部署。"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"