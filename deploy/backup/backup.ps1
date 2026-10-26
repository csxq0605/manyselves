param([string]$OutputDirectory = ".\backups")
$ErrorActionPreference = "Stop"
$dataRoot = $env:MANYSELVES_DATA_DIR
if (-not $dataRoot) { throw "Set MANYSELVES_DATA_DIR" }
$apiUrl = $env:MANYSELVES_API_URL
$username = if ($env:MANYSELVES_ADMIN_USERNAME) { $env:MANYSELVES_ADMIN_USERNAME } else { "admin" }
$password = $env:MANYSELVES_ADMIN_PASSWORD
# The archive covers the whole data root, so every account is paused first.
$accountsFile = if ($env:MANYSELVES_BACKUP_ACCOUNTS_FILE) { $env:MANYSELVES_BACKUP_ACCOUNTS_FILE } else { Join-Path $dataRoot ".manyselves\accounts.yaml" }
$paused = [System.Collections.Generic.List[object]]::new()

function Suspend-Account([string]$User, [string]$Secret) {
  $entry = [pscustomobject]@{ Session = [Microsoft.PowerShell.Commands.WebRequestSession]::new(); Lease = $null; Maintenance = $null }
  $paused.Add($entry)
  $login = @{ username = $User; password = $Secret } | ConvertTo-Json -Compress
  Invoke-WebRequest -Method Post -Uri "$apiUrl/api/v1/auth/login" -WebSession $entry.Session -ContentType "application/json" -Body $login | Out-Null
  $lease = Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/control/lease" -WebSession $entry.Session -ContentType "application/json" -Body '{"clientId":"phase1-backup","actorId":"operator-backup"}'
  $entry.Lease = $lease.leaseToken
  $headers = @{ "X-Control-Lease-Token" = $entry.Lease }
  $maintenance = Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/maintenance/quiesce" -WebSession $entry.Session -Headers $headers
  $entry.Maintenance = $maintenance.maintenanceToken
}

try {
  if ($apiUrl) {
    if (Test-Path -LiteralPath $accountsFile) {
      foreach ($line in (python "$PSScriptRoot\archive.py" accounts --file $accountsFile)) {
        $user, $passwordEnv = $line -split "`t", 2
        $secret = [Environment]::GetEnvironmentVariable($passwordEnv)
        if (-not $secret) { throw "$passwordEnv is required to pause account $user" }
        Suspend-Account $user $secret
      }
    } else {
      if (-not $password) { throw "MANYSELVES_ADMIN_PASSWORD is required with MANYSELVES_API_URL" }
      Suspend-Account $username $password
    }
  }
  python "$PSScriptRoot\archive.py" backup --source $dataRoot --output $OutputDirectory
} finally {
  foreach ($entry in $paused) {
    if ($entry.Maintenance) {
      try {
        $headers = @{ "X-Control-Lease-Token" = $entry.Lease }
        $body = @{ maintenanceToken = $entry.Maintenance } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/maintenance/release" -WebSession $entry.Session -Headers $headers -ContentType "application/json" -Body $body | Out-Null
      } catch {}
    }
    if ($entry.Lease) {
      try {
        Invoke-RestMethod -Method Delete -Uri "$apiUrl/api/v1/control/lease" -WebSession $entry.Session -ContentType "application/json" -Body (@{ leaseToken = $entry.Lease } | ConvertTo-Json -Compress) | Out-Null
      } catch {}
    }
    try { Invoke-WebRequest -Method Post -Uri "$apiUrl/api/v1/auth/logout" -WebSession $entry.Session | Out-Null } catch {}
  }
}
