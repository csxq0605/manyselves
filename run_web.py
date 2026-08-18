#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""直接运行 ManySelves Web API（无需 Docker）

使用方法：
    # 开发环境（自动重载）
    python run_web.py

    # 生产环境（多进程）
    python run_web.py --workers 4

    # 指定端口和地址
    python run_web.py --host 0.0.0.0 --port 9090

环境变量：
    复制 deploy/.env.example 到 .env 并修改配置
    或直接设置环境变量
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn

# 修复 Windows 命令行编码问题
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


def parse_args():
    parser = argparse.ArgumentParser(
        description="运行 ManySelves Web API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MANYSELVES_HTTP_BIND", "127.0.0.1"),
        help="监听地址 (默认: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MANYSELVES_HTTP_PORT", "9000")),
        help="监听端口 (默认: 9000)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="工作进程数 (默认: 1，生产环境建议 4)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="启用自动重载（开发模式）",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.getenv("MANYSELVES_DATA_DIR", ".manyselves")),
        help="数据目录 (默认: .manyselves)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="日志级别 (默认: info)",
    )
    parser.add_argument(
        "--accounts-file",
        type=Path,
        default=(
            Path(os.environ["MANYSELVES_ACCOUNTS_FILE"])
            if os.getenv("MANYSELVES_ACCOUNTS_FILE")
            else None
        ),
        help="多账户清单；同一 HTTP 服务内为每个账户创建独立 worker 和写入根",
    )
    return parser.parse_args()


def setup_environment(data_dir: Path, *, multi_account: bool = False):
    """设置环境变量"""
    # 确保数据目录存在
    data_dir.mkdir(parents=True, exist_ok=True)

    # 设置必需的环境变量（如果未设置）
    env_defaults = {
        "MANYSELVES_DATA_ROOT": str(data_dir.resolve()),
        # 初始项目 ID 不再硬编码，首次启动时会自动生成 UUID
        "MANYSELVES_ADMIN_USERNAME": os.getenv("MANYSELVES_ADMIN_USERNAME", "admin"),
        "MANYSELVES_ADMIN_PASSWORD": os.getenv("MANYSELVES_ADMIN_PASSWORD", "yuanxi@2026"),
    }

    for key, value in env_defaults.items():
        if not os.getenv(key):
            os.environ[key] = value

    # 检查必需配置
    provider_key_names = (
        "MIMO_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MANYSELVES_MIMO_API_KEY",
        "MANYSELVES_OPENAI_API_KEY",
        "MANYSELVES_ANTHROPIC_API_KEY",
    )
    if not multi_account and not any(os.getenv(name) for name in provider_key_names):
        print("⚠️  警告：未设置 API Key")
        print("请设置环境变量或在 .env 文件中配置：")
        print("  MANYSELVES_OPENAI_API_KEY=your-api-key")
        print()
        print("或在运行时指定：")
        print("  MANYSELVES_OPENAI_API_KEY=your-key python run_web.py")
        print()


def main():
    args = parse_args()

    if args.accounts_file is not None:
        if args.workers != 1:
            raise SystemExit("多账户 worker 由单一服务进程管理，--workers 必须为 1")
        os.environ["MANYSELVES_ACCOUNTS_FILE"] = str(args.accounts_file.resolve())

    # 设置环境
    setup_environment(args.data_dir, multi_account=args.accounts_file is not None)

    # 打印启动信息
    print("=" * 60)
    print("🚀 ManySelves Web API")
    print("=" * 60)
    print(f"📡 地址: http://{args.host}:{args.port}")
    print(f"📂 数据: {args.data_dir.resolve()}")
    print(f"🔧 工作进程: {args.workers}")
    if args.accounts_file is not None:
        print(f"👥 账户清单: {args.accounts_file.resolve()}")
    print(f"📝 日志级别: {args.log_level}")
    if args.reload:
        print("🔄 自动重载: 启用")
    print("=" * 60)
    print()
    print(f"📚 API 文档: http://{args.host}:{args.port}/docs")
    print("📖 使用说明: https://github.com/your-org/manyselves")
    print()
    print("按 Ctrl+C 停止服务")
    print("=" * 60)

    # 运行服务
    # 注意：直接使用模块级 app 实例，避免 factory 模式导致的重复创建
    uvicorn.run(
        "manyselves.webapi.main:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
