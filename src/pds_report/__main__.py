from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from pds_report.app.service import ReportApplication
from pds_report.gui.main_window import MainWindow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地配电报告多 Agent 工作区")
    parser.add_argument("--project", type=Path, required=True, help="客户项目目录")
    parser.add_argument("--headless", action="store_true", help="不启动 GUI")
    parser.add_argument("--message", help="headless 模式下的报告请求")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.headless:
        if not args.message:
            raise SystemExit("--headless 需要 --message")
        reply = asyncio.run(ReportApplication().run_message(args.project, args.message))
        print(reply.model_dump_json(indent=2))
        return 0 if not reply.errors else 1

    app = QApplication(sys.argv)
    window = MainWindow(args.project)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
