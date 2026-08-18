param([string]$OutputDirectory = ".\backups")
$ErrorActionPreference = "Stop"
$dataRoot = $env:MANYSELVES_DATA_DIR
if (-not $dataRoot) { throw "Set MANYSELVES_DATA_DIR" }
$apiUrl = $env:MANYSELVES_API_URL
$username = if ($env:MANYSELVES_ADMIN_USERNAME) { $env:MANYSELVES_ADMIN_USERNAME } else { "admin" }
$password = $env:MANYSELVES_ADMIN_PASSWORD
$session = $null
$leaseToken = $null
$maintenanceToken = $null
try {
  if ($apiUrl) {
    if (-not $password) { throw "MANYSELVES_ADMIN_PASSWORD is required with MANYSELVES_API_URL" }
    $session = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
    $login = @{ username = $username; password = $password } | ConvertTo-Json -Compress
    Invoke-WebRequest -Method Post -Uri "$apiUrl/api/v1/auth/login" -WebSession $session -ContentType "application/json" -Body $login | Out-Null
    $lease = Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/control/lease" -WebSession $session -ContentType "application/json" -Body '{"clientId":"phase1-backup","actorId":"operator-backup"}'
    $leaseToken = $lease.leaseToken
    $headers = @{ "X-Control-Lease-Token" = $leaseToken }
    $maintenance = Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/maintenance/quiesce" -WebSession $session -Headers $headers
    $maintenanceToken = $maintenance.maintenanceToken
  }
  python "$PSScriptRoot\archive.py" backup --source $dataRoot --output $OutputDirectory
} finally {
  if ($session -and $maintenanceToken) {
    try {
      $headers = @{ "X-Control-Lease-Token" = $leaseToken }
      $body = @{ maintenanceToken = $maintenanceToken } | ConvertTo-Json -Compress
      Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/maintenance/release" -WebSession $session -Headers $headers -ContentType "application/json" -Body $body | Out-Null
    } catch {}
  }
  if ($session -and $leaseToken) {
    try {
      Invoke-RestMethod -Method Delete -Uri "$apiUrl/api/v1/control/lease" -WebSession $session -ContentType "application/json" -Body (@{ leaseToken = $leaseToken } | ConvertTo-Json -Compress) | Out-Null
    } catch {}
  }
  if ($session) {
    try { Invoke-WebRequest -Method Post -Uri "$apiUrl/api/v1/auth/logout" -WebSession $session | Out-Null } catch {}
  }
}
