#!/bin/bash
# 诊断脚本 - 检查虚拟环境和依赖状态

# 切换到项目根目录
cd "$(dirname "$0")/.."

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ManySelves 环境诊断"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo ""
echo "📁 当前目录："
pwd

echo ""
echo "🔍 检查虚拟环境："
if [ -d ".venv" ]; then
    echo "  ✅ .venv 目录存在"

    if [ -f ".venv/bin/activate" ]; then
        echo "  ✅ Linux activate 脚本存在"
        ACTIVATE_TYPE="Linux"
    elif [ -f ".venv/Scripts/activate" ]; then
        echo "  ✅ Windows activate 脚本存在"
        ACTIVATE_TYPE="Windows"
    else
        echo "  ❌ activate 脚本不存在"
        ACTIVATE_TYPE="None"
    fi

    if [ -f ".venv/.installed" ]; then
        echo "  ✅ .installed 标记文件存在"
        INSTALLED=true
    else
        echo "  ❌ .installed 标记文件不存在"
        INSTALLED=false
    fi

    if [ -d ".venv/lib" ]; then
        echo "  ✅ lib 目录存在"
        PYTHON_VERSION=$(ls .venv/lib/ 2>/dev/null | grep python | head -1)
        if [ -n "$PYTHON_VERSION" ]; then
            echo "     Python 版本：$PYTHON_VERSION"

            # 检查已安装的包
            PACKAGES_COUNT=$(find .venv/lib/$PYTHON_VERSION/site-packages -maxdepth 1 -type d 2>/dev/null | wc -l)
            echo "     已安装包数量：约 $PACKAGES_COUNT 个"
        fi
    else
        echo "  ❌ lib 目录不存在"
    fi
else
    echo "  ❌ .venv 目录不存在"
fi

echo ""
echo "🔍 检查前端构建："
if [ -d "frontend/dist" ]; then
    echo "  ✅ frontend/dist 目录存在"

    if [ -f "frontend/dist/index.html" ]; then
        echo "  ✅ index.html 存在"

        # 检查构建时间
        BUILD_TIME=$(stat -c %Y frontend/dist/index.html 2>/dev/null || echo 0)
        if [ "$BUILD_TIME" -gt 0 ]; then
            BUILD_DATE=$(date -d "@$BUILD_TIME" "+%Y-%m-%d %H:%M:%S" 2>/dev/null || echo "未知")
            echo "     构建时间：$BUILD_DATE"
        fi
    else
        echo "  ❌ index.html 不存在"
    fi
else
    echo "  ❌ frontend/dist 目录不存在"
fi

echo ""
echo "🔍 检查环境配置："
if [ -f ".env" ]; then
    echo "  ✅ .env 文件存在"

    # 检查是否设置了 API Key
    if grep -q "MANYSELVES_OPENAI_API_KEY=." .env 2>/dev/null; then
        echo "  ✅ OpenAI API Key 已设置"
    else
        echo "  ⚠️  OpenAI API Key 未设置"
    fi
else
    echo "  ❌ .env 文件不存在"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📝 建议："
echo ""

if [ "$ACTIVATE_TYPE" = "None" ]; then
    echo "  ❌ 需要重新创建虚拟环境"
    echo "     运行：./scripts/start.sh"
elif [ "$INSTALLED" = false ]; then
    echo "  ❌ 需要安装依赖"
    echo "     运行：./scripts/start.sh"
else
    echo "  ✅ 环境完整，可以直接启动"
    echo "     运行：./start.sh"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"