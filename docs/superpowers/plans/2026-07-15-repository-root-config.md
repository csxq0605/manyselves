# Repository-root AutoReport Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make normal AutoReport startup load and save exactly one repository-root `autoreport.config.yaml`, regardless of the process working directory.

**Architecture:** Define the repository root and canonical configuration path beside the `Settings` model, then use a Pydantic `default_factory` for the default `config_path`. Explicit `config_path` arguments remain authoritative, so tests and embedded callers retain isolated configuration behavior.

**Tech Stack:** Python 3.11+, Pydantic Settings, pytest, uv-managed virtual environment

## Global Constraints

- The canonical configuration file is `/Users/zzymima0000/Documents/Codex/autoreport-power-distribution/autoreport.config.yaml`.
- Normal startup must not create configuration, cache, or migration files outside the repository.
- Existing explicit `config_path` arguments must keep their current behavior.
- API keys and configuration contents must not be logged or committed.
- The user-owned `uv.lock` modification must remain unstaged by this change.

---

### Task 1: Make the default configuration path independent of `cwd`

**Files:**
- Modify: `tests/test_schema.py`
- Modify: `autoreport/config/schema.py:1-100`
- Modify: `README.md:113`

**Interfaces:**
- Consumes: `Settings(config_path: Path | None)` behavior and `Path(__file__)` from the source checkout.
- Produces: `REPOSITORY_ROOT: Path`, `DEFAULT_CONFIG_PATH: Path`, and a `Settings.config_path` default equal to `DEFAULT_CONFIG_PATH`.

- [ ] **Step 1: Write the failing regression test**

Add this test to `tests/test_schema.py`:

```python
def test_settings_default_config_path_is_independent_of_cwd(monkeypatch, tmp_path):
    import autoreport.config.schema as schema

    expected = Path(schema.__file__).resolve().parents[2] / "autoreport.config.yaml"
    monkeypatch.chdir(tmp_path)

    settings = Settings(_env_file=None)

    assert settings.config_path == expected
    assert settings.config_path.is_absolute()
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
TMPDIR="$PWD/.build-tmp" HOME="$PWD/.test-home" PYTHONPYCACHEPREFIX="$PWD/.build-tmp/pycache" .venv/bin/pytest tests/test_schema.py::test_settings_default_config_path_is_independent_of_cwd -q
```

Expected: FAIL because the current value is the relative path `autoreport.config.yaml`, not the absolute repository-root path.

- [ ] **Step 3: Implement the repository-root default**

In `autoreport/config/schema.py`, add these constants after the imports:

```python
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "autoreport.config.yaml"
```

Replace the current `Settings.config_path` field with:

```python
config_path: Path = Field(default_factory=lambda: DEFAULT_CONFIG_PATH)
```

This changes only the default. `Settings(config_path=some_path)` continues to use `some_path`.

- [ ] **Step 4: Run focused configuration tests and verify GREEN**

Run:

```bash
TMPDIR="$PWD/.build-tmp" HOME="$PWD/.test-home" PYTHONPYCACHEPREFIX="$PWD/.build-tmp/pycache" .venv/bin/pytest tests/test_schema.py tests/test_config.py -q
```

Expected: all tests pass, including the new `cwd` regression test and existing explicit-path tests.

- [ ] **Step 5: Document the canonical path**

Replace the README configuration line with:

```markdown
Configuration file: repository-root `autoreport.config.yaml`. AutoReport uses this same file regardless of the directory from which `uv run autoreport` is launched.
```

- [ ] **Step 6: Run quality and full regression verification**

Run:

```bash
TMPDIR="$PWD/.build-tmp" HOME="$PWD/.test-home" PYTHONPYCACHEPREFIX="$PWD/.build-tmp/pycache" .venv/bin/ruff check autoreport/config/schema.py tests/test_schema.py tests/test_config.py
TMPDIR="$PWD/.build-tmp" HOME="$PWD/.test-home" PYTHONPYCACHEPREFIX="$PWD/.build-tmp/pycache" .venv/bin/pytest -q
git diff --check
```

Expected: Ruff reports no errors, the full test suite passes, and `git diff --check` prints no output.

- [ ] **Step 7: Commit only the implementation files**

```bash
git add autoreport/config/schema.py tests/test_schema.py README.md
git commit -m "fix: anchor API config to repository root"
```

Expected: the commit contains only the schema, regression test, and README. `uv.lock` remains modified and unstaged.
