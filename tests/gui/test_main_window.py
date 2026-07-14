import shutil
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt

from pds_report.app.service import ApplicationReply
from pds_report.domain.models import RunStatus
from pds_report.gui.main_window import MainWindow

PROJECT_ROOT = Path(".test-projects/gui").resolve()


class FakeApplication:
    async def run_message(self, project_root: Path, message: str) -> ApplicationReply:
        output = project_root / "Outputs" / "Modules" / "2.4.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"module_id":"2.4"}\n', encoding="utf-8")
        return ApplicationReply(
            run_id="run-gui",
            status=RunStatus.COMPLETED,
            message=f"已处理：{message}",
            files=[output],
        )


@pytest.fixture
def project_root() -> Path:
    if PROJECT_ROOT.exists():
        shutil.rmtree(PROJECT_ROOT)
    (PROJECT_ROOT / "Inputs").mkdir(parents=True)
    return PROJECT_ROOT


def test_main_window_exposes_workspace_viewer_and_chat(qtbot, project_root: Path) -> None:
    window = MainWindow(project_root, FakeApplication())
    qtbot.addWidget(window)

    assert window.file_tree.objectName() == "projectFileTree"
    assert window.preview.objectName() == "filePreview"
    assert window.chat_input.objectName() == "mainAgentInput"
    assert window.conversation.objectName() == "mainAgentConversation"
    assert window.centralWidget().count() == 3


def test_open_text_file_renders_content(qtbot, project_root: Path) -> None:
    note = project_root / "Inputs" / "巡检记录.md"
    note.write_text("主进线柜温度为 80°C", encoding="utf-8")
    window = MainWindow(project_root, FakeApplication())
    qtbot.addWidget(window)

    window.open_file(note)

    assert "主进线柜温度为 80°C" in window.preview.toPlainText()


def test_send_message_runs_flow_and_refreshes_outputs(qtbot, project_root: Path) -> None:
    window = MainWindow(project_root, FakeApplication())
    qtbot.addWidget(window)
    window.show()

    qtbot.keyClicks(window.chat_input, "write report 2.4")
    with qtbot.waitSignal(window.flow_finished, timeout=3000):
        qtbot.mouseClick(window.send_button, Qt.MouseButton.LeftButton)

    messages = [window.conversation.item(i).text() for i in range(window.conversation.count())]
    assert messages[0] == "用户: write report 2.4"
    assert "主 Agent: 已处理：write report 2.4" in messages
    output = project_root / "Outputs" / "Modules" / "2.4.json"
    qtbot.waitUntil(lambda: window.file_model.index(str(output)).isValid(), timeout=3000)
    assert window.send_button.isEnabled()
