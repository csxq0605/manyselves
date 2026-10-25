"""Canonical public identity for the Manyselves desktop application."""

from pathlib import Path

PRODUCT_NAME = "Manyselves"
TAGLINE = "One runtime. Many selves."
DESCRIPTOR_EN = "A file-defined multi-agent runtime with durable workflows and reconstructible execution."
DESCRIPTOR_ZH = "由文档定义 Agent 团队的本地工作空间"
_RESOURCES = Path(__file__).resolve().parent / "resources"
APP_ICON_PATH = _RESOURCES / "icon.png"
# The module-M mark without a background, for light and dark surfaces.
MARK_PATH = _RESOURCES / "mark.png"
MARK_ON_DARK_PATH = _RESOURCES / "mark-on-dark.png"
