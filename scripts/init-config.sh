#!/bin/bash
# 初始化配置文件脚本

set -e

CONFIG_DIR="deploy/config"
EXAMPLE_CONFIG="$CONFIG_DIR/manyselves.config.example.yaml"
ACTUAL_CONFIG="$CONFIG_DIR/manyselves.config.yaml"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ManySelves 配置初始化"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 检查模板文件是否存在
if [ ! -f "$EXAMPLE_CONFIG" ]; then
    echo "❌ 错误：配置模板文件不存在"
    echo "   期望位置：$EXAMPLE_CONFIG"
    exit 1
fi

# 如果实际配置文件已存在，询问是否覆盖
if [ -f "$ACTUAL_CONFIG" ]; then
    echo "⚠️  配置文件已存在：$ACTUAL_CONFIG"
    echo ""
    read -p "是否覆盖？(y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "✅ 保留现有配置文件"
        exit 0
    fi
fi

# 复制模板文件
echo "📋 复制配置模板..."
cp "$EXAMPLE_CONFIG" "$ACTUAL_CONFIG"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ 配置文件已创建：$ACTUAL_CONFIG"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📝 下一步："
echo ""
echo "1. 编辑配置文件，添加你的 API Key："
echo "   nano $ACTUAL_CONFIG"
echo ""
echo "2. 或者设置环境变量："
echo "   export MANYSELVES_OPENAI_API_KEY=your-api-key"
echo ""
echo "3. 启动服务："
echo "   ./start.sh  # 本地启动"
echo "   cd deploy && docker-compose up -d  # Docker 启动"
echo ""