"""Capture current Manyselves dialogs for repository documentation."""

import tempfile
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from manyselves.config import ConfigManager
from manyselves.core.recent_projects import RecentProjects
from manyselves.gui.config_dialog import ConfigDialog
from manyselves.gui.project_dialog import ProjectDialog

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "screenshots"


def capture(widget, name: str) -> None:
    widget.show()
    QApplication.processEvents()
    if not widget.grab().save(str(OUTPUT / name)):
        raise RuntimeError(f"Could not save {name}")
    widget.close()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Manyselves")
    app.setApplicationDisplayName("Manyselves")

    with tempfile.TemporaryDirectory(prefix="manyselves-brand-") as temp_dir:
        temp = Path(temp_dir)
        RecentProjects.STORAGE_FILE = temp / "recent_projects.json"
        config = ConfigManager(config_path=temp / "manyselves.config.yaml")
        capture(ProjectDialog(config), "start-window.png")
        capture(ConfigDialog(config), "configuration-window.png")


if __name__ == "__main__":
    main()
