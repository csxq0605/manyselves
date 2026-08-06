#!/bin/bash
# 本地构建脚本 - Windows WSL 环境

set -e

echo "=== 步骤 1：清理旧文件 ==="
rm -f manyselves-images.tar manyselves-deploy.tar.gz

echo "=== 步骤 2：创建临时配置 ==="
cat > deploy/.env.build << 'EOF'
MANYSELVES_DATA_DIR=/tmp/manyselves
MANYSELVES_OPENAI_API_KEY=tp-build-dummy-key
MANYSELVES_ADMIN_USERNAME=admin
MANYSELVES_ADMIN_PASSWORD=admin
MANYSELVES_ALLOWED_ORIGINS=["http://localhost:9090"]
EOF

echo "=== 步骤 3：构建镜像 ==="
docker compose -f deploy/compose.yaml --env-file deploy/.env.build build

echo "=== 步骤 4：保存镜像 ==="
# 使用完整镜像名称（与 compose.yaml 一致）
docker save -o manyselves-images.tar \
  docker.io/library/manyselves-api:phase1 \
  docker.io/library/manyselves-web:phase1

ls -lh manyselves-images.tar

echo "=== 步骤 5：打包部署文件 ==="
tar -czf manyselves-deploy.tar.gz \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='.venv' \
  --exclude='*.pyc' \
  --exclude='test-results' \
  --exclude='dist' \
  --exclude='.build-tmp' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  --exclude='*.tar' \
  --exclude='*.tar.gz' \
  deploy/ manyselves/ scripts/ pyproject.toml uv.lock README.md
ls -lh manyselves-deploy.tar.gz

echo "=== 步骤 6：清理临时文件 ==="
rm -f deploy/.env.build

echo ""
echo "✅ 构建完成！"
echo ""
echo "生成的文件："
echo "  - manyselves-images.tar    ($(du -h manyselves-images.tar | cut -f1))"
echo "  - manyselves-deploy.tar.gz ($(du -h manyselves-deploy.tar.gz | cut -f1))"
echo ""
echo "下一步：传输到服务器"
echo "  scp manyselves-images.tar manyselves-deploy.tar.gz algo_001@192.168.8.28:/home/algo_001/"