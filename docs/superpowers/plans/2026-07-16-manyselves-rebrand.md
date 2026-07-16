# Manyselves Rebrand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the public AutoReport identity with Manyselves, deliver a new domain-neutral visual system and documentation, and retain explicit compatibility for existing projects.

**Architecture:** Centralize public brand copy in a small `autoreport.branding` module, consume it from CLI and GUI surfaces, and retain the existing import package and state/config paths as compatibility interfaces. Treat power-distribution reporting as a bundled capability package in documentation and onboarding instead of the product boundary.

**Tech Stack:** Python 3.12, Typer, PyQt6, Markdown, PNG/SVG assets, pytest, pytest-qt, Ruff.

## Global Constraints

- Public product name: `Manyselves`.
- Tagline: `One runtime. Many selves.`
- English descriptor: `A local workspace for document-defined agent teams.`
- Chinese descriptor: `由文档定义 Agent 团队的本地工作空间`.
- Primary command: `manyselves`; compatibility command: `autoreport`.
- Keep the `autoreport` Python import package, repository-root `autoreport.config.yaml`, project-local `.autoreport` state directory, and explicit config-path overrides compatible.
- Preserve all unrelated working-tree changes and do not include them in rebrand commits.
- Keep configuration and application state inside the repository or active project workspace.

---

## File Structure

- `autoreport/branding.py`: canonical public brand constants and asset path.
- `autoreport/app.py`: Typer and QApplication brand consumption.
- `autoreport/gui/{main_window,project_dialog,onboarding}.py`: visible product copy.
- `autoreport/templates/agents/main_agent.md`: hosting-product terminology only; domain instructions remain intact.
- `pyproject.toml`: distribution metadata and dual CLI entry points.
- `tests/test_branding.py`: constants, packaged icon, and metadata contract.
- `tests/test_cli.py`: public CLI help contract.
- `tests/gui/test_project_dialog.py`: welcome-screen brand contract.
- `autoreport/resources/icon.png`: square Manyselves application icon.
- `assets/brand/manyselves-mark.png`: reusable high-resolution brand mark.
- `assets/screenshots/title.png`: Manyselves README banner.
- `assets/screenshots/workflow.png`: domain-neutral product model.
- `README.md`, `README_zh.md`: public product narrative and compatibility guide.

### Task 1: Brand Contract and Compatibility Entry Points

**Files:**
- Create: `autoreport/branding.py`
- Create: `tests/test_branding.py`
- Modify: `tests/test_cli.py`
- Modify: `pyproject.toml`
- Modify: `autoreport/app.py`
- Modify: `autoreport/__init__.py`
- Modify: `autoreport/__main__.py`

**Interfaces:**
- Produces: `PRODUCT_NAME: str`, `TAGLINE: str`, `DESCRIPTOR_EN: str`, `DESCRIPTOR_ZH: str`, `APP_ICON_PATH: Path`.
- Produces: console scripts `manyselves` and `autoreport`, both targeting `autoreport.__main__:main`.

- [ ] **Step 1: Write the failing brand contract tests**

```python
from pathlib import Path
import tomllib

from autoreport.branding import APP_ICON_PATH, DESCRIPTOR_EN, PRODUCT_NAME, TAGLINE


def test_manyselves_brand_contract() -> None:
    assert PRODUCT_NAME == "Manyselves"
    assert TAGLINE == "One runtime. Many selves."
    assert DESCRIPTOR_EN == "A local workspace for document-defined agent teams."
    assert APP_ICON_PATH == Path(__file__).parents[1] / "autoreport/resources/icon.png"
    assert APP_ICON_PATH.is_file()


def test_distribution_metadata_exposes_new_and_legacy_commands() -> None:
    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert data["project"]["name"] == "manyselves"
    assert data["project"]["scripts"] == {
        "manyselves": "autoreport.__main__:main",
        "autoreport": "autoreport.__main__:main",
    }
```

- [ ] **Step 2: Run the tests and verify the new module/metadata contract fails**

Run: `uv run pytest tests/test_branding.py tests/test_cli.py -q`

Expected: collection failure for `autoreport.branding` or assertions showing the old metadata/help text.

- [ ] **Step 3: Implement the centralized brand module and dual entry points**

```python
from pathlib import Path

PRODUCT_NAME = "Manyselves"
TAGLINE = "One runtime. Many selves."
DESCRIPTOR_EN = "A local workspace for document-defined agent teams."
DESCRIPTOR_ZH = "由文档定义 Agent 团队的本地工作空间"
APP_ICON_PATH = Path(__file__).resolve().parent / "resources" / "icon.png"
```

Update Typer help and command docstrings to use the constants, set the application display name, use `APP_ICON_PATH`, change startup logs to Manyselves, set project metadata to `manyselves`, and add both scripts to `[project.scripts]`. Keep `AutoReportApp` as an internal class name for compatibility in this iteration.

- [ ] **Step 4: Run focused CLI and brand tests**

Run: `uv run pytest tests/test_branding.py tests/test_cli.py -q`

Expected: all selected tests pass and CLI output contains `Manyselves` but not the old physics-experiment description.

- [ ] **Step 5: Commit the brand contract**

```bash
git add autoreport/branding.py autoreport/app.py autoreport/__init__.py autoreport/__main__.py pyproject.toml tests/test_branding.py tests/test_cli.py
git commit -m "feat: establish Manyselves brand contract"
```

### Task 2: Rebrand Visible Desktop Surfaces

**Files:**
- Modify: `autoreport/gui/main_window.py`
- Modify: `autoreport/gui/project_dialog.py`
- Modify: `autoreport/gui/onboarding.py`
- Modify: `autoreport/templates/agents/main_agent.md`
- Modify: hosting-product docstrings under `autoreport/`
- Modify: `tests/gui/test_project_dialog.py`
- Create or modify: `tests/gui/test_onboarding.py`

**Interfaces:**
- Consumes: the constants from `autoreport.branding`.
- Produces: every visible application title and global workspace description using Manyselves while leaving the bundled reporting instructions domain-specific.

- [ ] **Step 1: Add failing GUI copy tests**

```python
def test_project_dialog_uses_manyselves_brand(qtbot, config_manager):
    dialog = ProjectDialog(config_manager)
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == "Manyselves"
    labels = "\n".join(label.text() for label in dialog.findChildren(QLabel))
    assert "Manyselves" in labels
    assert "由文档定义 Agent 团队" in labels
    assert "物理实验" not in labels
```

Add equivalent checks for `PreProjectGuide` and `OnboardingDialog`, asserting that Manyselves is global and distribution reporting is described as the bundled workspace available in the current repository.

- [ ] **Step 2: Run GUI copy tests and verify they fail on legacy text**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_project_dialog.py tests/gui/test_onboarding.py -q`

Expected: assertions fail on `AutoReport` or `物理实验`.

- [ ] **Step 3: Replace visible product copy with imported brand constants**

Use `PRODUCT_NAME` for window/menu/title text, `DESCRIPTOR_ZH` for the general welcome description, and wording equivalent to “当前仓库内置配电报告 Agent 团队与工作流” for the bundled capability. Update only hosting-product references in prompts and docstrings; retain `.autoreport`, `autoreport.config.yaml`, import paths, and domain instructions.

- [ ] **Step 4: Run focused GUI tests**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/gui/test_project_dialog.py tests/gui/test_onboarding.py tests/gui/test_main_window_task_format.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit desktop copy changes without unrelated files**

```bash
git add autoreport/gui/main_window.py autoreport/gui/project_dialog.py autoreport/gui/onboarding.py autoreport/templates/agents/main_agent.md tests/gui/test_project_dialog.py tests/gui/test_onboarding.py
git commit -m "feat: rebrand desktop surfaces as Manyselves"
```

### Task 3: Create the Manyselves Visual System

**Files:**
- Modify: `autoreport/resources/icon.png`
- Create: `assets/brand/manyselves-mark.png`
- Modify: `assets/screenshots/title.png`
- Modify: `assets/screenshots/workflow.png`
- Remove if still misleading: `assets/screenshots/main-window.png`
- Remove if still misleading: `assets/screenshots/start-window.png`

**Interfaces:**
- Produces: one square application mark, one horizontal brand banner, and one domain-neutral workflow graphic.

- [ ] **Step 1: Generate the master mark**

Generate a clean vector-like square mark on transparent or solid midnight-indigo background: one stable central core opening into several connected facets, midnight indigo, electric violet, and cyan, no text, robot, face, chat bubble, document page, report, electrical symbol, physics formula, or LaTeX mark.

- [ ] **Step 2: Inspect and install the master mark**

Inspect the generated file at full size, crop it to a square if required without redrawing, save the master to `assets/brand/manyselves-mark.png`, and export the packaged 1024×1024 icon to `autoreport/resources/icon.png`.

- [ ] **Step 3: Create the brand banner**

Compose a 1800×480 horizontal banner with the mark, `Manyselves`, `One runtime. Many selves.`, and `A local workspace for document-defined agent teams.` Keep all copy exact and render-verify it for clipping and legibility.

- [ ] **Step 4: Create the product-model graphic**

Compose a 1800×1000 graphic showing `Identity docs + Skills + Tools` flowing into the `Manyselves runtime`, branching into a configurable Agent team, and producing `Project artifacts`. Use English labels only, no industry-specific modules, and inspect every label for exact spelling.

- [ ] **Step 5: Remove stale screenshots that cannot truthfully represent the current app**

Delete the legacy physics/LaTeX start and main screenshots if current branded screenshots cannot be captured reliably. Do not leave them referenced from documentation.

- [ ] **Step 6: Verify image dimensions and visual integrity**

Run: `file autoreport/resources/icon.png assets/brand/manyselves-mark.png assets/screenshots/title.png assets/screenshots/workflow.png`

Expected: all paths are valid PNG images; the icon and master mark are square; the banner and workflow are landscape.

- [ ] **Step 7: Commit visual assets**

```bash
git add autoreport/resources/icon.png assets/brand assets/screenshots
git commit -m "design: add Manyselves visual identity"
```

### Task 4: Rewrite Public Documentation

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `autoreport.config.example.yaml` only if comments expose legacy product copy.

**Interfaces:**
- Consumes: exact brand constants, installed assets, dual CLI names, and current reporting workflow behavior.
- Produces: parallel English and Chinese documentation with the configurable runtime first and distribution reporting clearly labeled as bundled capability.

- [ ] **Step 1: Add a documentation contract test**

Extend `tests/reporting/test_documentation_contract.py` to assert both READMEs contain `Manyselves`, the tagline, the primary `manyselves` command, a compatibility note for `autoreport`, and the local asset paths; assert they do not reference the upstream AutoReport screenshot URLs, Star History, or physics-experiment copy.

- [ ] **Step 2: Run the documentation contract and verify it fails**

Run: `uv run pytest tests/reporting/test_documentation_contract.py -q`

Expected: failures identify legacy README positioning and stale remote assets.

- [ ] **Step 3: Rewrite both READMEs around the approved structure**

Lead with the banner, tagline, and general workspace model. Explain document-defined identities, Skills, tools, local state, and provider support before introducing “Bundled capability: Power-distribution reporting.” Keep the current V2 evidence/revision/delivery details, installation commands, repository-root config guarantee, project layout, architecture, development commands, origin attribution, and license. Remove stale screenshots and upstream Star History.

- [ ] **Step 4: Run documentation and link checks**

Run: `uv run pytest tests/reporting/test_documentation_contract.py -q`

Run: `rg -o '\]\(([^)]+)\)' README.md README_zh.md`

Expected: documentation contract passes and every relative asset target exists.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md README_zh.md tests/reporting/test_documentation_contract.py
git commit -m "docs: present Manyselves as a configurable agent workspace"
```

### Task 5: Repository-Wide Brand and Regression Verification

**Files:**
- Modify only files identified by verification as unintended public legacy branding.

**Interfaces:**
- Consumes: all previous task outputs.
- Produces: audit evidence that the rebrand is complete and compatibility is intact.

- [ ] **Step 1: Audit remaining legacy strings**

Run: `rg -n 'AutoReport|自动化物理实验|物理实验报告' autoreport README.md README_zh.md pyproject.toml --glob '!*.lock' --glob '!*.docx' --glob '!*.ttf'`

Expected: only intentional historical attribution, internal compatibility names, or code-level class/import names remain; no visible product surface uses AutoReport.

- [ ] **Step 2: Verify both installed command definitions and help copy**

Run: `uv run manyselves --help`

Run: `uv run autoreport --help`

Expected: both exit 0 and display Manyselves help.

- [ ] **Step 3: Run focused brand and GUI tests**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest tests/test_branding.py tests/test_cli.py tests/gui/test_project_dialog.py tests/gui/test_onboarding.py tests/reporting/test_documentation_contract.py -q`

Expected: all selected tests pass.

- [ ] **Step 4: Run the full test suite**

Run: `QT_QPA_PLATFORM=offscreen uv run pytest -q`

Expected: all tests pass, with only the repository's established skips.

- [ ] **Step 5: Run Ruff and diff validation**

Run: `uv run ruff check autoreport tests`

Run: `git diff --check`

Expected: both commands exit 0.

- [ ] **Step 6: Inspect final assets and working-tree scope**

Open the packaged icon, brand banner, and workflow graphic for visual inspection. Run `git status --short` and distinguish pre-existing unrelated changes from Manyselves changes; do not revert or absorb unrelated work.

- [ ] **Step 7: Record verification-only corrections in the owning task**

If the audit finds a defect, return to the task that owns that file, add a
focused regression assertion, make the smallest correction, rerun that task's
checks, and commit the named files with the task's commit message. If the audit
finds no defect, do not create an empty verification commit.
