from __future__ import annotations

from pathlib import Path
from typing import override

from PyQt6.QtCore import QDir, QModelIndex, QThread, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QFileSystemModel
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from pds_report.app.service import ApplicationReply, ReportApplication
from pds_report.gui.worker import FlowWorker, ReportApplicationPort
from pds_report.infrastructure.project_store import ProjectStore


class MainWindow(QMainWindow):
    flow_finished = pyqtSignal(object)

    TEXT_FORMATS = {".csv", ".json", ".md", ".txt", ".yaml", ".yml"}
    MAX_PREVIEW_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        project_root: Path,
        application: ReportApplicationPort | None = None,
    ) -> None:
        super().__init__()
        self.application = application or ReportApplication()
        self.project_root = project_root.expanduser().resolve()
        self._thread: QThread | None = None
        self._worker: FlowWorker | None = None
        self._flow_result: object | None = None

        self.setWindowTitle("配电报告多 Agent 工作区")
        self.resize(1440, 860)

        splitter = QSplitter()
        self.setCentralWidget(splitter)
        splitter.addWidget(self._build_file_workspace())
        splitter.addWidget(self._build_preview())
        splitter.addWidget(self._build_chat())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 3)
        self.set_project_root(self.project_root)

    def _build_file_workspace(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        title = QLabel("项目文件")
        controls = QHBoxLayout()
        self.open_project_button = QPushButton("打开项目")
        self.new_project_button = QPushButton("新建项目")
        controls.addWidget(self.open_project_button)
        controls.addWidget(self.new_project_button)

        self.file_model = QFileSystemModel(self)
        self.file_model.setFilter(
            QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot
        )
        self.file_tree = QTreeView()
        self.file_tree.setObjectName("projectFileTree")
        self.file_tree.setModel(self.file_model)
        self.file_tree.setSortingEnabled(True)
        self.file_tree.hideColumn(1)
        self.file_tree.hideColumn(2)
        self.file_tree.hideColumn(3)

        layout.addWidget(title)
        layout.addLayout(controls)
        layout.addWidget(self.file_tree)
        self.file_tree.doubleClicked.connect(self._open_index)
        self.open_project_button.clicked.connect(self._choose_existing_project)
        self.new_project_button.clicked.connect(self._create_project)
        return panel

    def _build_preview(self) -> QTextBrowser:
        self.preview = QTextBrowser()
        self.preview.setObjectName("filePreview")
        self.preview.setPlaceholderText("从项目文件树选择文件进行预览")
        return self.preview

    def _build_chat(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("主 Agent 对话"))
        self.conversation = QListWidget()
        self.conversation.setObjectName("mainAgentConversation")
        self.chat_input = QLineEdit()
        self.chat_input.setObjectName("mainAgentInput")
        self.chat_input.setPlaceholderText("例如：写作配电报告，要求深度思考，先做2.4")
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("sendToMainAgent")
        input_row = QHBoxLayout()
        input_row.addWidget(self.chat_input)
        input_row.addWidget(self.send_button)
        layout.addWidget(self.conversation)
        layout.addLayout(input_row)
        self.send_button.clicked.connect(self.send_message)
        self.chat_input.returnPressed.connect(self.send_message)
        return panel

    def set_project_root(self, project_root: Path) -> None:
        store = ProjectStore(project_root)
        store.create()
        self.project_root = store.root
        model_root = self.file_model.setRootPath(str(self.project_root))
        self.file_tree.setRootIndex(model_root)
        self.setWindowTitle(f"配电报告多 Agent 工作区 — {self.project_root.name}")

    def open_file(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        if not resolved.is_relative_to(self.project_root) or not resolved.is_file():
            self.preview.setPlainText("无法打开项目目录之外的文件。")
            return
        if resolved.stat().st_size > self.MAX_PREVIEW_BYTES:
            self.preview.setPlainText("文件超过 2 MB，请使用系统应用打开。")
            return
        if resolved.suffix.lower() not in self.TEXT_FORMATS:
            self.preview.setPlainText(
                f"当前内置查看器暂不支持 {resolved.suffix or '无扩展名'} 文件。"
            )
            return
        try:
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self.preview.setPlainText(f"读取文件失败：{exc}")
            return
        self.preview.setPlainText(text)

    def send_message(self) -> None:
        message = self.chat_input.text().strip()
        if not message or self._thread is not None:
            return
        self.conversation.addItem(f"用户: {message}")
        self.chat_input.clear()
        self.send_button.setEnabled(False)
        self._flow_result = None

        thread = QThread(self)
        worker = FlowWorker(self.application, self.project_root, message)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.reply.connect(self._on_reply)
        worker.error.connect(self._on_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _open_index(self, index: QModelIndex) -> None:
        self.open_file(Path(self.file_model.filePath(index)))

    def _on_reply(self, reply: ApplicationReply) -> None:
        self.conversation.addItem(f"主 Agent: {reply.message}")
        for path in reply.files:
            self.conversation.addItem(f"文件: {path.relative_to(self.project_root)}")
        self.file_model.setRootPath(str(self.project_root))
        self._flow_result = reply

    def _on_error(self, message: str) -> None:
        self.conversation.addItem(f"主 Agent: 流程失败：{message}")
        self._flow_result = message

    def _thread_finished(self) -> None:
        result = self._flow_result
        self._thread = None
        self._worker = None
        self.send_button.setEnabled(True)
        self.flow_finished.emit(result)

    def _choose_existing_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "打开客户项目",
            str(self.project_root.parent),
        )
        if selected:
            self.set_project_root(Path(selected))

    def _create_project(self) -> None:
        name, accepted = QInputDialog.getText(self, "新建项目", "项目名称")
        name = name.strip()
        if not accepted or not name:
            return
        if "/" in name or "\\" in name or name in {".", ".."}:
            QMessageBox.warning(self, "项目名称无效", "项目名称不能包含路径分隔符。")
            return
        self.set_project_root(self.project_root.parent / name)

    @override
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(5000):
                event.ignore()
                return
        super().closeEvent(event)
