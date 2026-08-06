@echo off
REM ManySelves 快速启动脚本（Windows）

echo ====================================
echo   ManySelves Web API 快速启动
echo ====================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python
    echo 请先安装 Python 3.12+
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version') do set PYTHON_VERSION=%%i
echo [OK] Python 版本: %PYTHON_VERSION%

REM 检查虚拟环境
if not exist ".venv" (
    echo [创建] 虚拟环境...
    python -m venv .venv
)

REM 激活虚拟环境
echo [激活] 虚拟环境...
call .venv\Scripts\activate.bat

REM 安装依赖
if not exist ".venv\.installed" (
    echo [安装] 依赖...
    pip install --upgrade pip
    pip install -e .
    echo. > .venv\.installed
    echo [OK] 依赖安装完成
) else (
    echo [OK] 依赖已安装
)

REM 检查 .env 文件
if not exist ".env" (
    echo [警告] 未找到 .env 文件
    echo 使用默认配置...

    (
        echo MANYSELVES_DATA_DIR=.manyselves
        echo MANYSELVES_INITIAL_PROJECT_ID=default
        echo MANYSELVES_ADMIN_USERNAME=admin
        echo MANYSELVES_ADMIN_PASSWORD=yuanxi@2026
        echo MANYSELVES_HTTP_BIND=127.0.0.1
        echo MANYSELVES_HTTP_PORT=9090
        echo MANYSELVES_ALLOWED_ORIGINS='["http://localhost:9090"]'
    ) > .env

    echo [OK] 创建默认 .env 文件
)

REM 检查 API Key（使用不带前缀的变量名）
if "%OPENAI_API_KEY%"=="" (
    if "%ANTHROPIC_API_KEY%"=="" (
        echo ====================================
        echo [警告] 未设置 API Key
        echo ====================================
        echo.
        echo 请设置环境变量：
        echo   set OPENAI_API_KEY=your-api-key
        echo.
        echo 或编辑 .env 文件添加：
        echo   OPENAI_API_KEY=your-api-key
        echo.
    )
)

REM 加载 .env 中的环境变量
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        REM 跳过注释和空行
        echo %%a | findstr /r "^[#]" >nul || set "%%a=%%b"
    )
)

REM 获取端口（默认 9090）
set HTTP_PORT=%MANYSELVES_HTTP_PORT%
if "%HTTP_PORT%"=="" set HTTP_PORT=9090

REM 启动服务
echo ====================================
echo 启动服务...
echo ====================================
echo.
echo   访问地址: http://localhost:%HTTP_PORT%
echo   API 文档: http://localhost:%HTTP_PORT%/docs
echo.

python run_web.py --reload

pause