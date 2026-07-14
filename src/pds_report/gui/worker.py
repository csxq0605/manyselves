from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from pds_report.app.service import ApplicationReply


class ReportApplicationPort(Protocol):
    async def run_message(self, project_root: Path, message: str) -> ApplicationReply: ...


class FlowWorker(QObject):
    reply = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        application: ReportApplicationPort,
        project_root: Path,
        message: str,
    ) -> None:
        super().__init__()
        self.application = application
        self.project_root = project_root
        self.message = message

    @pyqtSlot()
    def run(self) -> None:
        try:
            reply = asyncio.run(
                self.application.run_message(self.project_root, self.message)
            )
        except Exception as exc:
            self.error.emit(str(exc) or type(exc).__name__)
        else:
            self.reply.emit(reply)
        finally:
            self.finished.emit()

