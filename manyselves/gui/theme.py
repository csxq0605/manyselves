"""Unified theme management for Manyselves GUI.

Provides centralized color definitions for dark/light modes. Layout, text,
action and status colours mirror the Manyselves design tokens (纸面 light /
墨面 dark); syntax-highlighting colours stay on the VS Code Modern palettes.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication


def is_dark_mode() -> bool:
    """Detect if system is in dark mode."""
    app = QApplication.instance()
    if app is None:
        return False

    hints = app.styleHints()
    if hasattr(hints, "colorScheme"):
        try:
            if hints.colorScheme() == Qt.ColorScheme.Dark:
                return True
            if hints.colorScheme() == Qt.ColorScheme.Light:
                return False
        except Exception:
            pass

    # Fallback for platforms where colorScheme is unavailable/unreliable.
    # Compare window/background luminance from current palette.
    window = app.palette().window().color()
    return window.lightness() < 128


def get_theme_colors() -> dict[str, str]:
    """Get unified theme colors for current mode.

    Returns:
        Dictionary of color name -> hex value.
        Layout, text, action and status colours follow the Manyselves design tokens.
    """
    dark = is_dark_mode()

    # === Base Colors (Manyselves design tokens) ===
    bg = "#1a1f26" if dark else "#fcfbf8"  # surface-raised
    surface = "#13171c" if dark else "#f4f2ec"  # surface
    sunken = "#0e1115" if dark else "#ebe7de"  # surface-sunken
    panel_bg = surface
    titlebar_bg = surface
    messages_bg = panel_bg
    border = "#2b323b" if dark else "#ddd8cc"  # line
    border_strong = "#69727e" if dark else "#8c8578"  # line-strong
    fg = "#ebe7df" if dark else "#1b2129"  # ink
    muted = "#a5abb3" if dark else "#585e66"  # ink-muted
    faint = "#868d96" if dark else "#767b82"  # ink-faint

    # Key names keep their historical "blue" spelling: buttonBlue is the
    # primary action (accent), selectionBlue the accent tint.
    button_blue = "#e3804f" if dark else "#a8441b"  # accent
    on_accent = "#1b120c" if dark else "#ffffff"
    selection_blue = "#3a2419" if dark else "#f5e3d8"  # accent-tint
    hover = border
    danger = "#f08a80" if dark else "#b3261e"  # failed
    on_danger = "#13171c" if dark else "#ffffff"
    success = "#6cc3bf" if dark else "#1b6b6f"  # done
    running = "#7fb0e6" if dark else "#245a96"
    waiting = "#e0b25a" if dark else "#865400"
    input_bg = bg
    input_border = border_strong
    input_fg = fg
    disabled_fg = muted
    secondary_bg = sunken
    secondary_border = border_strong
    detail_card_bg = bg
    detail_card_border = border
    detail_muted = muted
    detail_fg = fg

    return {
        # === Layout ===
        "bg": bg,  # Main content area
        "surface": surface,  # Sidebar/chrome (file tree, header)
        "panel_bg": panel_bg,  # Agent panel background (darker than editor)
        "titlebar_bg": titlebar_bg,  # Custom window title bar background
        "messages_bg": messages_bg,  # Agent conversation area background
        "card": bg,  # Elevated cards
        "border": border,
        "fg": fg,
        "muted": muted,
        "buttonBlue": button_blue,
        "selectionBlue": selection_blue,
        "radius_sm": "4px",
        "radius_md": "6px",
        "radius_lg": "10px",

        # === Font Weights ===
        "fw_normal": "400",
        "fw_medium": "500",
        "fw_semibold": "600",
        "fw_bold": "700",

        # === Aliases for specific components ===
        "title": fg,  # Title text

        # === Interactive ===
        "hover": hover,  # --vscode-toolbar-hoverBackground

        # === Input ===
        "input_bg": input_bg,
        "input_border": input_border,

        # === Buttons ===
        "danger": danger,
        "danger_hover": "#f4a39b" if dark else "#8e1d17",

        # === Scrollbar ===
        "scrollbar": border_strong,
        "scrollbar_hover": faint,

        # === Chat/Agent Panel ===
        "bubble_bg": sunken,
        "bubble_hover": border if dark else sunken,
        # Neutral gray (R=G=B, no green/blue cast) for user-bubble and
        # context-chip borders — light enough to read as ash-gray, not white.
        "gray_white": border_strong,
        "edit_bubble_bg": bg,
        "edit_bubble_border": border_strong,
        "avatar_bg": "#9aa4ae" if dark else "#55606b",  # self-6
        "avatar_fg": "#13171c" if dark else "#ffffff",  # on-self

        # === Status Indicators ===
        "status_tool": waiting,
        "status_running": running,
        "status_success": success,
        "status_error": danger,
        "status_debug": "#b896d6" if dark else "#6d4a86",  # self-5
        "status_idle": muted,
        "timeline_dot": muted,

        # === File Tree ===
        "tree_hover": hover,
        "tree_sel_bg": selection_blue,
        "tree_sel_fg": fg,

        # === Editor/Preview ===
        "editor_bg": bg,
        "editor_fg": fg,
        "editor_margin": bg,
        "editor_caret_fg": button_blue,
        "markdown_table_header_bg": surface,

        # === Tabs ===
        "tab_active_bg": bg,
        "tab_active_fg": fg,
        "tab_inactive_fg": muted,

        # === Context Menu ===
        "context_bg": bg,
        "context_border": border,
        "popup_fg": fg,
        "popup_hover": hover,

        # === Tool Calls ===
        "tool_fg": fg,
        "tool_detail": muted,
        "detail_card_bg": detail_card_bg,
        "detail_card_border": detail_card_border,
        "detail_muted": detail_muted,
        "detail_fg": detail_fg,
        "detail_hover_fg": fg,

        # === Message Controls ===
        "message_expand_bg": bg,
        "message_expand_fg": fg,
        "message_expand_border": border,
        "message_expand_hover_bg": hover,

        # === Icon Semantic Colors ===
        "icon_default": fg,
        "icon_run": success,
        "icon_preview": success,
        "icon_context_eye": muted,
        "agent_main": button_blue,
        # Compatibility colors for historical task/message records.
        "agent_data_analysis": "#6dbba5" if dark else "#2a6e5e",  # self-3
        "agent_plotting": "#b896d6" if dark else "#6d4a86",  # self-5
        "agent_theory": "#d3ad5b" if dark else "#7a5a12",  # self-4
        "agent_report": "#7fa9d9" if dark else "#2f5f8f",  # self-2

        # === Syntax Highlighting ===
        "syntax_keyword": "#C586C0" if dark else "#AF00DB",
        "syntax_string": "#CE9178" if dark else "#A31515",
        "syntax_comment": "#6A9955" if dark else "#008000",
        "syntax_number": "#B5CEA8" if dark else "#098658",
        "syntax_operator": "#D4D4D4" if dark else "#000000",
        "syntax_identifier": "#9CDCFE" if dark else "#001080",
        "syntax_class_name": "#4EC9B0" if dark else "#267F99",
        "syntax_function": "#DCDCAA" if dark else "#795E26",

        # === Markdown tokens (VSCode dark-modern / light-modern) ===
        # markup.heading / markup.bold — bold blue (dark) vs navy/maroon (light).
        "md_heading": "#569CD6" if dark else "#800000",
        "md_bold": "#569CD6" if dark else "#000080",
        # markup.italic — *italic* / _italic_ (same hue as bold, italic via font).
        "md_italic": "#569CD6" if dark else "#000080",
        # markup.underline.link — [text](url) link target text.
        "md_link": "#4CB9FF" if dark else "#0451A5",
        # markup.strikethrough — ~~strikethrough~~ (muted grey).
        "md_strikethrough": "#808080" if dark else "#555555",
        # markup.inline.raw — inline `code` (own key so light mode uses blue,
        # not the red from syntax_string).
        "md_code": "#CE9178" if dark else "#0451A5",
        # punctuation.definition.list.begin — list markers (-, *, 1.).
        "md_list": "#6796E6" if dark else "#0451A5",
        # punctuation.definition.quote.begin — block-quote marker (>).
        "md_quote": "#6A9955" if dark else "#008000",
        # markup.horizontal-rule — --- / *** (subtle, same as comment or muted).
        "md_hr": "#6A9955" if dark else "#008000",

        # === TeX tokens (VSCode dark-modern / light-modern) ===
        # support.function.general.tex — \section, \textbf etc.
        "tex_command": "#DCDCAA" if dark else "#795E26",
        # keyword.control.tex — \begin, \end, \usepackage, \documentclass etc.
        "tex_keyword": "#C586C0" if dark else "#AF00DB",
        # constant.character.escape — $ # % & ~ _ ^ \ (foreground = punctuation).
        "tex_special": "#D4D4D4" if dark else "#000000",
        # punctuation.definition.group.brace — {…} (foreground = punctuation).
        "tex_group": "#D4D4D4" if dark else "#000000",
        # string.other.math — $E=mc^2$ math content (string, not number).
        "tex_math": "#CE9178" if dark else "#A31515",

        # === Dialog/Config Specific ===
        "activeFg": fg,
        "checkFg": success,

        # === Input Variations ===
        "inputBg": input_bg,
        "inputBorder": input_border,
        "inputFg": input_fg,
        "inputDisabledBg": sunken,
        "inputDisabledFg": disabled_fg,

        # === Primary Button Variations ===
        "primaryBtnFg": on_accent,

        # === Secondary Button ===
        "secondaryBtnBg": secondary_bg,
        "secondaryBtnBorder": secondary_border,
        "secondaryBtnFg": fg,
        "secondaryBtnHoverBg": hover,
        "secondaryBtnHoverBorder": muted,

        # === Action Buttons ===
        "addBtnBorder": secondary_border,
        "addBtnFg": fg,
        "deleteFg": danger,
        "deleteHoverBg": danger,
        "deleteHoverFg": on_danger,
        "resetFg": fg,
        "resetHoverFg": fg,

        # === Test/Connection Buttons ===
        "cancelHoverFg": fg,

        # === Path/Link ===
        "pathFg": fg,

        # === Warning/Error ===
        "warningBg": "#3a1a18" if dark else "#f9e3e0",  # failed-tint
        "warningBorder": danger,
        "warningFg": danger,
        "successFg": success,
    }


def scrollbar_stylesheet(
    *,
    selector: str = "QScrollBar",
    orientation: str = "vertical",
    background_color: str = "transparent",
    thickness: str = "8px",
    min_handle_extent: str = "30px",
    radius: str = "4px",
    margin: str | None = None,
    handle_margin: str | None = None,
    colors: dict[str, str] | None = None,
) -> str:
    """Build themed scrollbar QSS while leaving layout choices to callers."""
    if colors is None:
        colors = get_theme_colors()
    if orientation not in {"vertical", "horizontal"}:
        raise ValueError("orientation must be 'vertical' or 'horizontal'")

    thickness_prop = "width" if orientation == "vertical" else "height"
    min_extent_prop = "min-height" if orientation == "vertical" else "min-width"
    line_extent_prop = "height" if orientation == "vertical" else "width"
    margin_rule = f"\n                margin: {margin};" if margin else ""
    handle_margin_rule = f"\n                margin: {handle_margin};" if handle_margin else ""

    return f"""
            {selector}:{orientation} {{
                background-color: {background_color};
                {thickness_prop}: {thickness};
                border: none;
                {margin_rule}
            }}
            {selector}::handle:{orientation} {{
                background-color: {colors["scrollbar"]};
                {min_extent_prop}: {min_handle_extent};
                border-radius: {radius};
                {handle_margin_rule}
            }}
            {selector}::handle:{orientation}:hover,
            {selector}::handle:{orientation}:pressed {{
                background-color: {colors["scrollbar_hover"]};
            }}
            {selector}::groove:{orientation} {{
                background-color: {background_color};
                border: none;
            }}
            {selector}::add-line:{orientation},
            {selector}::sub-line:{orientation} {{
                {line_extent_prop}: 0;
                margin: 0;
                border: none;
                background-color: transparent;
            }}
            {selector}::add-page:{orientation},
            {selector}::sub-page:{orientation} {{
                background-color: transparent;
            }}
        """
