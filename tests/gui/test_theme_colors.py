import re
from pathlib import Path

from manyselves.gui import theme

ROOT = Path(__file__).resolve().parents[2]


def test_gui_components_do_not_define_local_color_palettes():
    files = [
        ROOT / "manyselves/gui/main_window.py",
        ROOT / "manyselves/gui/widgets/tool_call_group.py",
        ROOT / "manyselves/gui/icons.py",
        ROOT / "manyselves/gui/scintilla_utils.py",
        ROOT / "manyselves/gui/widgets/markdown_renderer.py",
    ]
    color_pattern = re.compile(r"#[0-9A-Fa-f]{6,8}|rgba?\([^)]*\)")

    offenders: list[str] = []
    for path in files:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if color_pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{line_no}: {line.strip()}")

    assert offenders == []


def test_dark_theme_palette_matches_design_tokens(monkeypatch):
    monkeypatch.setattr(theme, "is_dark_mode", lambda: True)

    colors = theme.get_theme_colors()

    assert colors["editor_bg"] == "#1a1f26"
    assert colors["editor_fg"] == "#ebe7df"
    assert colors["editor_margin"] == "#1a1f26"
    assert colors["input_bg"] == "#1a1f26"
    assert colors["input_border"] == "#69727e"
    assert colors["tab_active_bg"] == "#1a1f26"
    assert colors["status_success"] == "#6cc3bf"
    assert colors["buttonBlue"] == "#e3804f"
    assert colors["primaryBtnFg"] == "#1b120c"


def test_light_theme_palette_matches_design_tokens(monkeypatch):
    monkeypatch.setattr(theme, "is_dark_mode", lambda: False)

    colors = theme.get_theme_colors()

    assert colors["editor_bg"] == "#fcfbf8"
    assert colors["editor_fg"] == "#1b2129"
    assert colors["editor_margin"] == "#fcfbf8"
    assert colors["input_bg"] == "#fcfbf8"
    assert colors["input_border"] == "#8c8578"
    assert colors["tab_active_bg"] == "#fcfbf8"
    assert colors["status_success"] == "#1b6b6f"
    assert colors["buttonBlue"] == "#a8441b"
    assert colors["primaryBtnFg"] == "#ffffff"
