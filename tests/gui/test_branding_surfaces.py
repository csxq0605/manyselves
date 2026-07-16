"""Visible desktop surfaces follow the Manyselves product identity."""

from PyQt6.QtWidgets import QLabel

from manyselves.config import ConfigManager
from manyselves.core.user_settings import UserSettings
from manyselves.gui.onboarding import OnboardingDialog, PreProjectGuide
from manyselves.gui.project_dialog import ProjectDialog

LEGACY_PRODUCT_NAME = "Auto" + "Report"


def _label_text(widget) -> str:
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


def test_project_dialog_uses_manyselves_brand(qtbot, tmp_path) -> None:
    config = ConfigManager(config_path=tmp_path / "manyselves.config.yaml")
    dialog = ProjectDialog(config)
    qtbot.addWidget(dialog)

    labels = _label_text(dialog)
    assert dialog.windowTitle() == "Manyselves"
    assert "Manyselves" in labels
    assert "由文档定义 Agent 团队" in labels
    assert LEGACY_PRODUCT_NAME not in labels
    assert "物理实验" not in labels


def test_welcome_guide_presents_reporting_as_bundled_capability(
    qtbot, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(UserSettings, "STORAGE_FILE", tmp_path / "user_settings.json")
    guide = PreProjectGuide()
    qtbot.addWidget(guide)

    labels = _label_text(guide)
    assert guide.windowTitle() == "Manyselves — 欢迎"
    assert "欢迎使用 Manyselves" in labels
    assert "由文档定义 Agent 团队" in labels
    assert "当前仓库内置配电报告能力" in labels
    assert LEGACY_PRODUCT_NAME not in labels
    assert "🧪" not in labels


def test_onboarding_dialog_uses_manyselves_brand(qtbot, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(UserSettings, "STORAGE_FILE", tmp_path / "user_settings.json")
    dialog = OnboardingDialog()
    qtbot.addWidget(dialog)

    labels = _label_text(dialog)
    assert dialog.windowTitle() == "Manyselves — 新手引导"
    assert "Manyselves" in labels
    assert LEGACY_PRODUCT_NAME not in labels
