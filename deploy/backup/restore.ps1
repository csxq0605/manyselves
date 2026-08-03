param([Parameter(Mandatory = $true)][string]$Archive, [switch]$Force)
$ErrorActionPreference = "Stop"
if ($env:MANYSELVES_SERVICES_STOPPED -ne "yes") { throw "Stop Compose and set MANYSELVES_SERVICES_STOPPED=yes" }
if (-not $env:MANYSELVES_DATA_DIR) { throw "Set MANYSELVES_DATA_DIR" }
$arguments = @("$PSScriptRoot\archive.py", "restore", "--archive", $Archive, "--target", $env:MANYSELVES_DATA_DIR)
if ($Force) { $arguments += "--force" }
python @arguments
