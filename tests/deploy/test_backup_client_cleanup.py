from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).parents[2]


def _posix_shell() -> str | None:
    candidates = (
        Path(r"D:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\bin\bash.exe"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("bash") or shutil.which("sh")


def _events(path: Path) -> list[str]:
    return path.read_text("utf-8").splitlines()


@pytest.mark.skipif(_posix_shell() is None, reason="requires a POSIX shell; Linux CI exercises this test")
def test_backup_sh_continues_cleanup_after_maintenance_release_failure(tmp_path: Path) -> None:
    shell = _posix_shell()
    assert shell is not None
    data_root = tmp_path / "data"
    data_root.mkdir()
    output_dir = tmp_path / "backups"
    events = tmp_path / "events.log"
    bash_env = tmp_path / "fake-commands.sh"
    bash_env.write_text(
        textwrap.dedent(
            """\
            if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]; then
              fake_events=$(cygpath -u "$FAKE_EVENTS")
            else
              fake_events=$FAKE_EVENTS
            fi
            curl() {
              local method=GET url= cookie_jar=
              while [[ "$#" -gt 0 ]]; do
                case "$1" in
                  -X) method=$2; shift 2 ;;
                  --cookie-jar) cookie_jar=$2; shift 2 ;;
                  --data-binary|-H|--cookie) shift 2 ;;
                  --fail|--silent|--show-error) shift ;;
                  http://*|https://*) url=$1; shift ;;
                  *) shift ;;
                esac
              done
              cat >/dev/null || true
              printf '%s %s\n' "$method" "$url" >> "$fake_events"
              case "$url" in
                */auth/login) : > "$cookie_jar" ;;
                */control/lease)
                  if [[ "$method" == POST ]]; then printf '%s' '{"leaseToken":"lease-1"}'; fi
                  ;;
                */maintenance/quiesce) printf '%s' '{"maintenanceToken":"maintenance-1"}' ;;
                */maintenance/release) return 55 ;;
              esac
            }
            rm() {
              local argument
              for argument in "$@"; do
                if [[ -f "$argument" ]]; then printf '%s\n' "cookie-removed" >> "$fake_events"; fi
              done
              command rm "$@"
            }
            """
        ),
        encoding="utf-8",
    )

    environment = os.environ | {
        "FAKE_EVENTS": str(events),
        "BASH_ENV": str(bash_env),
        "MANYSELVES_DATA_DIR": str(data_root),
        "MANYSELVES_API_URL": "http://fake.example",
        "MANYSELVES_ADMIN_PASSWORD": "password",
    }
    result = subprocess.run(
        [shell, str(ROOT / "deploy" / "backup" / "backup.sh"), str(output_dir)],
        cwd=ROOT,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert _events(events) == [
        "POST http://fake.example/api/v1/auth/login",
        "POST http://fake.example/api/v1/control/lease",
        "POST http://fake.example/api/v1/maintenance/quiesce",
        "POST http://fake.example/api/v1/maintenance/release",
        "DELETE http://fake.example/api/v1/control/lease",
        "POST http://fake.example/api/v1/auth/logout",
        "cookie-removed",
    ]


def test_backup_ps1_continues_cleanup_after_maintenance_release_failure(tmp_path: Path) -> None:
    events = tmp_path / "events.log"
    harness = tmp_path / "backup-harness.ps1"
    backup = ROOT / "deploy" / "backup" / "backup.ps1"
    harness.write_text(
        "\n".join(
            [
                '$ErrorActionPreference = "Stop"',
                f'$env:MANYSELVES_DATA_DIR = "{tmp_path / "data"}"',
                '$env:MANYSELVES_API_URL = "http://fake.example"',
                '$env:MANYSELVES_ADMIN_PASSWORD = "password"',
                'New-Item -ItemType Directory -Force -Path $env:MANYSELVES_DATA_DIR | Out-Null',
                "Add-Type -TypeDefinition 'namespace Microsoft.PowerShell.Commands { public class WebRequestSession {} }'",
                'function Add-Event([string]$Method, [string]$Uri) {',
                f'  Add-Content -LiteralPath "{events}" -Value "$Method $Uri"',
                '}',
                'function Invoke-WebRequest {',
                '  param([string]$Method, [string]$Uri, [Parameter(ValueFromRemainingArguments=$true)]$Arguments)',
                '  Add-Event $Method $Uri',
                '}',
                'function Invoke-RestMethod {',
                '  param([string]$Method, [string]$Uri, [Parameter(ValueFromRemainingArguments=$true)]$Arguments)',
                '  Add-Event $Method $Uri',
                '  if ($Uri.EndsWith("/control/lease") -and $Method -eq "Post") {',
                '    return [pscustomobject]@{ leaseToken = "lease-1" }',
                '  }',
                '  if ($Uri.EndsWith("/maintenance/quiesce")) {',
                '    return [pscustomobject]@{ maintenanceToken = "maintenance-1" }',
                '  }',
                '  if ($Uri.EndsWith("/maintenance/release")) {',
                '    throw "forced maintenance release failure"',
                '  }',
                '}',
                'function python { throw "forced archive failure" }',
                f'. "{backup}" -OutputDirectory "{tmp_path / "backups"}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert events.exists(), result.stderr
    assert result.returncode != 0
    assert _events(events) == [
        "Post http://fake.example/api/v1/auth/login",
        "Post http://fake.example/api/v1/control/lease",
        "Post http://fake.example/api/v1/maintenance/quiesce",
        "Post http://fake.example/api/v1/maintenance/release",
        "Delete http://fake.example/api/v1/control/lease",
        "Post http://fake.example/api/v1/auth/logout",
    ]
