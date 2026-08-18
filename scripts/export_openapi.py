"""Export the deterministic OpenAPI contract consumed by the frontend."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings

DEFAULT_CONTRACT_PATH = (
    REPOSITORY_ROOT / "frontend-contract" / "openapi.json"
)


def _contract_settings() -> WebSettings:
    """Return stable, non-sensitive settings used only for schema generation."""
    return WebSettings(
        data_root=Path("openapi-contract-data"),
        initial_project_id="openapi-contract-project",
        allowed_origins=[],
    )


def render_openapi_document() -> str:
    """Build the canonical JSON representation with stable ordering and newlines."""
    schema = create_app(_contract_settings()).openapi()
    return json.dumps(
        schema,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def write_or_check(destination: Path, *, check: bool) -> bool:
    """Write the contract or report whether an existing artifact is current."""
    expected = render_openapi_document().encode("utf-8")
    if check:
        return destination.is_file() and destination.read_bytes() == expected

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(expected)
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail without modifying the artifact when it is missing or stale",
    )
    parser.add_argument(
        "destination",
        nargs="?",
        default=DEFAULT_CONTRACT_PATH,
        type=Path,
        help="OpenAPI JSON artifact path (defaults to frontend-contract/openapi.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the exporter command-line interface."""
    args = _parse_args(argv)
    current = write_or_check(args.destination, check=args.check)
    if not current:
        print(
            f"OpenAPI contract is missing or stale: {args.destination}",
            file=sys.stderr,
        )
        return 1
    if not args.check:
        print(f"Wrote OpenAPI contract: {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
