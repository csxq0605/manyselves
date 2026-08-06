#!/bin/bash
# SELinux 权限修复脚本 - CentOS

set -e

echo "=== 修复 SELinux 权限问题 ==="

echo "步骤 1：停止所有容器"
podman compose -f deploy/compose.yaml down 2>/dev/null || true

echo "步骤 2：设置 SELinux 上下文"
sudo semanage fcontext -a -t container_file_t "/data/manyselves(/.*)?" 2>/dev/null || true
sudo restorecon -Rv /data/manyselves

echo "步骤 3：设置目录权限"
sudo chmod -R 755 /data/manyselves

echo "步骤 4：验证配置文件"
if [ ! -f /data/manyselves/manyselves.config.yaml ]; then
  echo "创建配置文件..."
  sudo tee /data/manyselves/manyselves.config.yaml > /dev/null << 'EOF'
providers:
  active: mimo-openai
  configurations:
  - apiKey: null
    apiBase: https://token-plan-cn.xiaomimimo.com/v1
    defaultModel: mimo-v2.5-pro
    enabled: true
    id: mimo-openai
    name: MiMo AI
    provider: openai
EOF
  sudo restorecon -v /data/manyselves/manyselves.config.yaml
fi

echo "步骤 5：验证 SELinux 上下文"
ls -laZ /data/manyselves/

echo "步骤 6：启动容器"
podman compose -f deploy/compose.yaml --env-file deploy/.env up -d

echo "步骤 7：验证服务"
sleep 10
podman ps
podman logs manyselves-phase1_api_1 2>&1 | tail -30 | grep -E "model|mimo|Started"

echo ""
echo "✅ SELinux 权限已修复！"