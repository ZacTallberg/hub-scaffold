# Register the optional hub-worker:// adapter for the current Windows user (HKCU; no admin).
param(
    [switch]$Remove,
    [string]$Protocol = "hub-worker",
    [string]$IssuerUrl = "{{LIVE_URL}}/hub/api/launch-grant/consume",
    [string]$TokenFile = "",
    [string]$WorkerCommand = "",
    [string]$Repo = ""
)

$ErrorActionPreference = "Stop"
if ($Protocol -notmatch '^hub-[a-z0-9][a-z0-9+.-]{0,26}$') { throw "Protocol must be a hub-* URL scheme: $Protocol" }
$base = "HKCU:\Software\Classes\$Protocol"
if ($Remove) {
    if (Test-Path -LiteralPath $base) { Remove-Item -LiteralPath $base -Recurse -Force }
    Write-Host "unregistered $($Protocol)://" -ForegroundColor Green
    return
}

$launch = Join-Path $PSScriptRoot "launch-worker.ps1"
if (-not (Test-Path -LiteralPath $launch)) { throw "launch-worker.ps1 is missing: $launch" }
if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
else { $Repo = (Resolve-Path -LiteralPath $Repo).Path }
if (-not $TokenFile) { $TokenFile = $env:HUB_SYNC_TOKEN_FILE }
if (-not $TokenFile -or -not (Test-Path -LiteralPath $TokenFile)) {
    throw "TokenFile must name an existing file containing this Hub's write token. The token itself is never stored in the registry."
}
$TokenFile = (Resolve-Path -LiteralPath $TokenFile).Path
if (-not $WorkerCommand) { $WorkerCommand = $env:HUB_WORKER_COMMAND }
if (-not $WorkerCommand) { throw "WorkerCommand is required (your vendor-specific agent wrapper)." }
$cmd = Get-Command $WorkerCommand -ErrorAction SilentlyContinue
if ($cmd -and $cmd.Source) { $WorkerCommand = $cmd.Source }
elseif (Test-Path -LiteralPath $WorkerCommand) { $WorkerCommand = (Resolve-Path -LiteralPath $WorkerCommand).Path }
else { throw "Worker command was not found: $WorkerCommand" }

$issuer = [System.Uri]$IssuerUrl
$loopback = $issuer.Host -in @("localhost", "127.0.0.1", "::1")
if ($issuer.Scheme -ne "https" -and -not ($issuer.Scheme -eq "http" -and $loopback)) {
    throw "IssuerUrl must use HTTPS (HTTP is allowed only for loopback)."
}
if (-not $issuer.AbsolutePath.EndsWith("/api/launch-grant/consume")) {
    throw "IssuerUrl must name the Hub launch-grant consume endpoint."
}
foreach ($value in @($launch, $IssuerUrl, $TokenFile, $WorkerCommand, $Repo)) {
    if ($value.Contains('"')) { throw 'Paths and URLs containing a double quote are not supported.' }
}

New-Item -Path $base -Force | Out-Null
Set-ItemProperty -LiteralPath $base -Name "(default)" -Value "URL:$Protocol Protocol"
Set-ItemProperty -LiteralPath $base -Name "URL Protocol" -Value ""
$commandKey = "$base\shell\open\command"
New-Item -Path $commandKey -Force | Out-Null
$powershell = Join-Path $PSHOME "powershell.exe"
$command = ('"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}" -Protocol "{2}" ' +
            '-IssuerUrl "{3}" -TokenFile "{4}" -WorkerCommand "{5}" -Repo "{6}" -Url "%1"') -f
            $powershell, $launch, $Protocol, $IssuerUrl, $TokenFile, $WorkerCommand, $Repo
Set-ItemProperty -LiteralPath $commandKey -Name "(default)" -Value $command

Write-Host "registered $($Protocol):// for the current user" -ForegroundColor Green
Write-Host "worker command: $WorkerCommand" -ForegroundColor DarkGray
Write-Host "issuing Hub: $IssuerUrl" -ForegroundColor DarkGray
