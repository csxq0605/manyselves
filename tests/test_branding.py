"""Public brand and packaging behavior."""

import tomllib
from pathlib import Path

from manyselves.branding import APP_ICON_PATH, DESCRIPTOR_EN, PRODUCT_NAME, TAGLINE

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_manyselves_brand_values_and_icon() -> None:
    assert PRODUCT_NAME == "Manyselves"
    assert TAGLINE == "One runtime. Many selves."
    assert DESCRIPTOR_EN == "A local workspace for document-defined agent teams."
    assert APP_ICON_PATH == REPOSITORY_ROOT / "manyselves/resources/icon.png"
    assert APP_ICON_PATH.is_file()


def test_distribution_metadata_exposes_manyselves_command() -> None:
    data = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["name"] == "manyselves"
    assert data["project"]["description"] == DESCRIPTOR_EN
    assert data["project"]["scripts"] == {
        "manyselves": "manyselves.__main__:main",
        "manyselves-headless": "manyselves.headless_service:main",
    }
