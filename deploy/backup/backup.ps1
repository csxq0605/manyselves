param([string]$OutputDirectory = ".\backups")
$ErrorActionPreference = "Stop"
$dataRoot = $env:MANYSELVES_DATA_DIR
if (-not $dataRoot) { throw "Set MANYSELVES_DATA_DIR" }
$apiUrl = $env:MANYSELVES_API_URL
$accessToken = $env:MANYSELVES_ACCESS_TOKEN
$leaseToken = $null
$maintenanceToken = $null
try {
  if ($apiUrl) {
    if (-not $accessToken) { throw "MANYSELVES_ACCESS_TOKEN is required with MANYSELVES_API_URL" }
    $headers = @{ Authorization = "Bearer $accessToken" }
    $lease = Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/control/lease" -Headers $headers -ContentType "application/json" -Body '{"clientId":"phase1-backup","actorId":"operator-backup"}'
    $leaseToken = $lease.leaseToken
    $headers["X-Control-Lease-Token"] = $leaseToken
    $maintenance = Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/maintenance/quiesce" -Headers $headers
    $maintenanceToken = $maintenance.maintenanceToken
  }
  python "$PSScriptRoot\archive.py" backup --source $dataRoot --output $OutputDirectory
} finally {
  if ($maintenanceToken) {
    $headers = @{ Authorization = "Bearer $accessToken"; "X-Control-Lease-Token" = $leaseToken }
    $body = @{ maintenanceToken = $maintenanceToken } | ConvertTo-Json -Compress
    Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/maintenance/release" -Headers $headers -ContentType "application/json" -Body $body | Out-Null
  }
}
