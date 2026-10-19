# PowerShell 7.4+ / .NET 8. AES-256-GCM with a separate local key file.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Encrypt', 'Decrypt')][string]$Mode,
    [Parameter(Mandatory)][string]$InputPath,
    [Parameter(Mandatory)][string]$OutputPath,
    [Parameter(Mandatory)][string]$KeyPath
)
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $OutputPath) { throw 'Output already exists; select a new output path.' }
$magic = [Text.Encoding]::ASCII.GetBytes('MSAUDIT1')
$data = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $InputPath).Path)
if ($Mode -eq 'Encrypt') {
    if (Test-Path -LiteralPath $KeyPath) { throw 'Key file already exists; select a new private key path.' }
    $keyBytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    $nonce = [Security.Cryptography.RandomNumberGenerator]::GetBytes(12)
    $tag = [byte[]]::new(16)
    $cipher = [byte[]]::new($data.Length)
    $aes = [Security.Cryptography.AesGcm]::new($keyBytes, 16)
    try { $aes.Encrypt($nonce, $data, $cipher, $tag, $magic) } finally { $aes.Dispose() }
    $payload = [byte[]]::new(36 + $cipher.Length)
    [Array]::Copy($magic, 0, $payload, 0, 8)
    [Array]::Copy($nonce, 0, $payload, 8, 12)
    [Array]::Copy($tag, 0, $payload, 20, 16)
    [Array]::Copy($cipher, 0, $payload, 36, $cipher.Length)
    $keyRecord = @{format='MSAUDIT1'; algorithm='AES-256-GCM'; key_base64=[Convert]::ToBase64String($keyBytes)}
    [IO.File]::WriteAllText([IO.Path]::GetFullPath($KeyPath), ($keyRecord | ConvertTo-Json))
    [IO.File]::WriteAllBytes([IO.Path]::GetFullPath($OutputPath), $payload)
} else {
    if ($data.Length -lt 36 -or [Text.Encoding]::ASCII.GetString($data, 0, 8) -ne 'MSAUDIT1') { throw 'Invalid archive format.' }
    $keyRecord = Get-Content -LiteralPath $KeyPath -Raw | ConvertFrom-Json
    $keyBytes = [Convert]::FromBase64String($keyRecord.key_base64)
    $nonce = [byte[]]$data[8..19]
    $tag = [byte[]]$data[20..35]
    $cipher = [byte[]]$data[36..($data.Length - 1)]
    $plain = [byte[]]::new($cipher.Length)
    $aes = [Security.Cryptography.AesGcm]::new($keyBytes, 16)
    try { $aes.Decrypt($nonce, $cipher, $tag, $plain, $magic) } finally { $aes.Dispose() }
    [IO.File]::WriteAllBytes([IO.Path]::GetFullPath($OutputPath), $plain)
}
Write-Output "Archive $Mode completed: $OutputPath"
