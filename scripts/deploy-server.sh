#!/bin/bash
# 服务器部署脚本 - CentOS 环境

set -e

# 配置参数
DEPLOY_USER="${USER:-algo_001}"
DEPLOY_HOST="${DEPLOY_HOST:-192.168.8.28}"
DEPLOY_DIR="/home/${DEPLOY_USER}/manyselves"
DATA_DIR="/data/manyselves"

echo "=== 步骤 1：加载镜像 ==="
cd /home/${DEPLOY_USER}
podman load -i manyselves-images.tar

# Podman 会自动添加 localhost/ 前缀，需要重新 tag
# 检查是否有 localhost/ 前缀
if podman images | grep -q "localhost/manyselves-api"; then
  echo "检测到 localhost/ 前缀，重新 tag 镜像..."
  podman tag localhost/manyselves-api:phase1 manyselves-api:phase1
  podman tag localhost/manyselves-web:phase1 manyselves-web:phase1
  echo "✅ 镜像 tag 完成"
fi

podman images | grep manyselves

echo "=== 步骤 2：解压源码 ==="
rm -rf ${DEPLOY_DIR}
mkdir -p ${DEPLOY_DIR}
tar -xzf manyselves-deploy.tar.gz -C ${DEPLOY_DIR} --strip-components=0
ls -la ${DEPLOY_DIR}/

echo "=== 步骤 3：创建数据目录 ==="
if [ ! -d ${DATA_DIR} ]; then
  echo "创建数据目录需要管理员权限，请输入密码："
  sudo mkdir -p ${DATA_DIR}
  sudo chmod -R 777 ${DATA_DIR}
else
  echo "数据目录已存在: ${DATA_DIR}"
  # 确保权限正确
  sudo chmod -R 777 ${DATA_DIR} 2>/dev/null || true
fi

echo "=== 步骤 4：创建配置文件 ==="
cat > ${DATA_DIR}/manyselves.config.yaml << 'EOF'
# ManySelves 生产环境配置
# 包含所有主流 AI 服务商的预配置

providers:
  active: mimo-openai
  configurations:
  # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  # 国内主流服务商
  # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  - apiKey: null
    apiBase: https://token-plan-cn.xiaomimimo.com/v1
    defaultModel: mimo-v2.5-pro
    enabled: true
    id: mimo-openai
    name: MiMo AI
    provider: openai
  - apiKey: null
    apiBase: https://api.deepseek.com
    defaultModel: deepseek-chat
    enabled: false
    id: deepseek
    name: DeepSeek
    provider: openai
  - apiKey: null
    apiBase: https://api.moonshot.cn/v1
    defaultModel: moonshot-v1-8k
    enabled: false
    id: kimi
    name: Kimi
    provider: openai
  - apiKey: null
    apiBase: https://dashscope.aliyuncs.com/compatible-mode/v1
    defaultModel: qwen-turbo
    enabled: false
    id: qwen
    name: 通义千问
    provider: openai
  - apiKey: null
    apiBase: https://open.bigmodel.cn/api/paas/v4
    defaultModel: glm-4-flash
    enabled: false
    id: glm
    name: 智谱AI
    provider: openai
  # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  # 国际主流服务商
  # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  - apiKey: null
    apiBase: https://api.openai.com/v1
    defaultModel: gpt-4o
    enabled: false
    id: openai-official
    name: OpenAI (ChatGPT)
    provider: openai
  - apiKey: null
    apiBase: https://api.anthropic.com
    defaultModel: claude-sonnet-4-20250514
    enabled: false
    id: anthropic-official
    name: Claude
    provider: anthropic
  - apiKey: null
    apiBase: https://generativelanguage.googleapis.com/v1beta/openai
    defaultModel: gemini-2.0-flash-exp
    enabled: false
    id: gemini
    name: Google Gemini
    provider: openai
  # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  # 聚合平台
  # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  - apiKey: null
    apiBase: https://openrouter.ai/api/v1
    defaultModel: anthropic/claude-sonnet-4.6
    enabled: false
    id: openrouter
    name: OpenRouter
    provider: openai
  - apiKey: null
    apiBase: https://api.groq.com/openai/v1
    defaultModel: llama-3.3-70b-versatile
    enabled: false
    id: groq
    name: Groq
    provider: openai
EOF

chmod 666 ${DATA_DIR}/manyselves.config.yaml 2>/dev/null || sudo chmod 666 ${DATA_DIR}/manyselves.config.yaml
echo "配置文件已创建: ${DATA_DIR}/manyselves.config.yaml"
echo "包含所有主流 AI 服务商（MiMo、DeepSeek、Kimi、Qwen、GLM、OpenAI、Claude、Gemini、OpenRouter、Groq）"

echo "=== 步骤 5：创建环境变量 ==="
cd ${DEPLOY_DIR}/deploy

if [ -f .env ]; then
  echo ".env 文件已存在，跳过创建"
else
  cat > .env << 'EOF'
# ManySelves 环境配置

# 数据目录
MANYSELVES_DATA_DIR=/data/manyselves

# MiMo API Key（请修改）
MANYSELVES_OPENAI_API_KEY=tp-YOUR-API-KEY-HERE

# 管理员账号
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=yuanxi@2026

# 网络配置
MANYSELVES_ALLOWED_ORIGINS=["http://192.168.8.28:9090"]
MANYSELVES_HTTP_BIND=0.0.0.0
MANYSELVES_HTTP_PORT=9090

# 默认模型配置
MANYSELVES_BOOTSTRAP_PROVIDER=openai
MANYSELVES_BOOTSTRAP_API_BASE=https://token-plan-cn.xiaomimimo.com/v1
MANYSELVES_BOOTSTRAP_MODEL=mimo-v2.5-pro
EOF

  chmod 600 .env
  echo ""
  echo "⚠️  重要：请编辑 .env 文件设置 API Key"
  echo "   vi ${DEPLOY_DIR}/deploy/.env"
  echo "   修改：MANYSELVES_OPENAI_API_KEY=tp-你的密钥"
  echo ""
  echo "编辑完成后，重新运行此脚本"
  exit 1
fi

echo "=== 步骤 6：停止旧容器 ==="
cd ${DEPLOY_DIR}
podman compose -f deploy/compose.yaml --env-file deploy/.env down 2>/dev/null || true

echo "=== 步骤 7：启动容器 ==="
podman compose -f deploy/compose.yaml --env-file deploy/.env up -d

echo "=== 步骤 8：等待服务就绪 ==="
sleep 10

echo "=== 步骤 9：验证部署 ==="
echo ""
echo "容器状态："
podman ps | grep manyselves

echo ""
echo "服务日志："
podman logs manyselves-phase1_api_1 2>&1 | tail -30 | grep -E "model|mimo|Started|Uvicorn|error" || podman logs manyselves-phase1_api_1 2>&1 | tail -10

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 部署完成！"
echo ""
echo "访问地址：http://${DEPLOY_HOST}:9090"
echo ""
echo "常用命令："
echo "  查看日志：podman logs -f manyselves-phase1_api_1"
echo "  重启服务：podman compose -f deploy/compose.yaml restart"
echo "  停止服务：podman compose -f deploy/compose.yaml down"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"