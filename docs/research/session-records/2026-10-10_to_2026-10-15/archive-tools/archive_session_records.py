"""Pack local session records and an inventory; never upload credentials.

The ZIP contains customer-derived records and must be encrypted before upload.
Run from the repository root, using the existing local Python environment.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from manyselves.config.schema import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if output.is_relative_to(source):
        raise ValueError("Keep the private archive outside the record source tree")
    output.parent.mkdir(parents=True, exist_ok=True)

    # Read credential values locally solely to prevent embedding them in traces.
    secrets = set()
    config = Settings().load_config()
    for route in config.providers.configurations:
        if route.api_key:
            secrets.add(str(route.api_key).encode())
        for key, value in (route.extra_headers or {}).items():
            if any(term in key.lower() for term in ("authorization", "api-key", "token", "secret")) and value:
                secrets.add(str(value).encode())
    for key, value in os.environ.items():
        if key in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"} and value:
            secrets.add(value.encode())
    secrets = {value for value in secrets if len(value) >= 8}
    manifest = {"source": "audit_manyselves", "files": [], "excluded": [], "directory_aliases": []}
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source).as_posix()
            if path.is_symlink() and path.is_dir():
                manifest["directory_aliases"].append(relative)
                continue
            if not path.is_file():
                continue
            reason = None
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                reason = "compiled Python cache"
            elif path.name in {".env", "manyselves.config.yaml", "accounts.yaml"} or path.suffix in {".key", ".pem", ".pfx"}:
                reason = "credential/configuration/signing key"
            elif path.suffix == ".lock":
                reason = "ephemeral process lock"
            elif not path.resolve().is_relative_to(source.parent):
                reason = "link target outside authorized workspace"
            if reason:
                manifest["excluded"].append({"path": relative, "reason": reason})
                continue
            data = path.read_bytes()
            original_sha = hashlib.sha256(data).hexdigest()
            redacted = False
            for value in secrets:
                if value in data:
                    data = data.replace(value, b"[REDACTED_CREDENTIAL]")
                    redacted = True
            archive.writestr("audit_manyselves/" + relative, data)
            manifest["files"].append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                      "source_sha256": original_sha, "credential_redacted": redacted})
        archive.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"included_files": len(manifest["files"]), "excluded_files": len(manifest["excluded"]),
                      "directory_aliases": len(manifest["directory_aliases"]),
                      "credential_redacted_files": sum(item["credential_redacted"] for item in manifest["files"]),
                      "archive_bytes": output.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
