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

function Invoke-Release($Entry) {
  $headers = @{ "X-Control-Lease-Token" = $Entry.Lease }
  $body = @{ maintenanceToken = $Entry.Maintenance } | ConvertTo-Json -Compress
  Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/maintenance/release" -WebSession $Entry.Session -Headers $headers -ContentType "application/json" -Body $body | Out-Null
}

# Renew every paused account's lease while the archive runs, so the release at
# the end is still allowed however long the archive takes.  It runs in its own
# runspace in this process and shares the paused sessions.
function Start-LeaseHeartbeat {
  $seconds = if ($env:MANYSELVES_BACKUP_HEARTBEAT_SECONDS) { [double]$env:MANYSELVES_BACKUP_HEARTBEAT_SECONDS } else { 20 }
  $heartbeat = [powershell]::Create()
  $null = $heartbeat.AddScript({
    param($Entries, $ApiUrl, $Seconds)
    while ($true) {
      Start-Sleep -Milliseconds ([int]($Seconds * 1000))
      foreach ($entry in @($Entries)) {
        if (-not $entry.Lease) { continue }
        try {
          Invoke-RestMethod -Method Post -Uri "$ApiUrl/api/v1/control/lease/heartbeat" -WebSession $entry.Session -ContentType "application/json" -Body (@{ leaseToken = $entry.Lease } | ConvertTo-Json -Compress) | Out-Null
        } catch {}
      }
    }
  }).AddArgument($paused).AddArgument($apiUrl).AddArgument($seconds)
  $null = $heartbeat.BeginInvoke()
  return $heartbeat
}

$heartbeat = $null
$stillPaused = $false
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
  if ($paused.Count -gt 0) { $heartbeat = Start-LeaseHeartbeat }
  python "$PSScriptRoot\archive.py" backup --source $dataRoot --output $OutputDirectory
} finally {
  if ($heartbeat) {
    try { $heartbeat.Stop() } catch {}
    $heartbeat.Dispose()
  }
  foreach ($entry in $paused) {
    if ($entry.Maintenance) {
      try {
        Invoke-Release $entry
      } catch {
        # The lease may have expired during a long archive: take a fresh one
        # (nobody else can hold it while the account is paused) and retry.
        try {
          $lease = Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/control/lease" -WebSession $entry.Session -ContentType "application/json" -Body '{"clientId":"phase1-backup","actorId":"operator-backup"}'
          if ($lease.leaseToken) { $entry.Lease = $lease.leaseToken }
          Invoke-Release $entry
        } catch {
          $stillPaused = $true
          Write-Warning "An account is still paused for maintenance. Maintenance token: $($entry.Maintenance). Release it with POST /api/v1/maintenance/release, or restart the service."
        }
      }
    }
    if ($entry.Lease) {
      try {
        Invoke-RestMethod -Method Delete -Uri "$apiUrl/api/v1/control/lease" -WebSession $entry.Session -ContentType "application/json" -Body (@{ leaseToken = $entry.Lease } | ConvertTo-Json -Compress) | Out-Null
      } catch {}
    }
    try { Invoke-WebRequest -Method Post -Uri "$apiUrl/api/v1/auth/logout" -WebSession $entry.Session | Out-Null } catch {}
  }
}
if ($stillPaused) { throw "Backup finished but at least one account is still paused for maintenance" }
