"""Public brand and packaging contracts."""

from pathlib import Path
import tomllib

from autoreport.branding import APP_ICON_PATH, DESCRIPTOR_EN, PRODUCT_NAME, TAGLINE


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_manyselves_brand_contract() -> None:
    assert PRODUCT_NAME == "Manyselves"
    assert TAGLINE == "One runtime. Many selves."
    assert DESCRIPTOR_EN == "A local workspace for document-defined agent teams."
    assert APP_ICON_PATH == REPOSITORY_ROOT / "autoreport/resources/icon.png"
    assert APP_ICON_PATH.is_file()


def test_distribution_metadata_exposes_new_and_legacy_commands() -> None:
    data = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["name"] == "manyselves"
    assert data["project"]["description"] == DESCRIPTOR_EN
    assert data["project"]["scripts"] == {
        "manyselves": "autoreport.__main__:main",
        "autoreport": "autoreport.__main__:main",
    }
