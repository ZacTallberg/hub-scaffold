# Launch a local worker only after a browser grant is authoritatively consumed.
#
# The worker command is an operator-supplied wrapper (a script or executable). It receives its
# context through HUB_AGENT_ID, HUB_TASK_ID, HUB_REPO, HUB_DIR, and HUB_WORKER_PROMPT_FILE. The
# launcher intentionally knows nothing about a particular agent vendor or model.
param(
    [string]$Url = "",
    [int]$Count = 0,
    [string]$Protocol = "hub-worker",
    [string]$IssuerUrl = "",
    [string]$TokenFile = "",
    [string]$WorkerCommand = "",
    [string]$Repo = "",
    [string]$Python = "python",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
if ($Protocol -notmatch '^hub-[a-z0-9][a-z0-9+.-]{0,26}$') { throw "Protocol must be a hub-* URL scheme: $Protocol" }
if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
else { $Repo = (Resolve-Path -LiteralPath $Repo).Path }
if (-not (Test-Path -LiteralPath (Join-Path $Repo "hub_core\launch_grant.py"))) {
    throw "Repo does not contain hub_core/launch_grant.py: $Repo"
}
if (-not $WorkerCommand) { $WorkerCommand = $env:HUB_WORKER_COMMAND }
if (-not $WorkerCommand) { throw "WorkerCommand is required (point it at your agent wrapper)." }
$cmd = Get-Command $WorkerCommand -ErrorAction SilentlyContinue
if (-not $cmd -and -not (Test-Path -LiteralPath $WorkerCommand)) {
    throw "Worker command was not found: $WorkerCommand"
}
if ($cmd -and $cmd.Source) { $WorkerCommand = $cmd.Source }
elseif (Test-Path -LiteralPath $WorkerCommand) { $WorkerCommand = (Resolve-Path -LiteralPath $WorkerCommand).Path }

$Task = ""
$Grant = ""
if ($Url) {
    $uri = [System.Uri]$Url
    if ($uri.Scheme -ne $Protocol -or $uri.Host -ne "start") {
        Write-Host "LAUNCH REFUSED - unexpected protocol or action" -ForegroundColor Red
        exit 3
    }
    $Task = [System.Uri]::UnescapeDataString($uri.AbsolutePath.TrimStart('/'))
    Add-Type -AssemblyName System.Web -ErrorAction SilentlyContinue
    $query = [System.Web.HttpUtility]::ParseQueryString($uri.Query)
    if ($Count -le 0) { [void][int]::TryParse($query.Get("count"), [ref]$Count) }
    $Grant = $query.Get("grant")
}
if ($Count -le 0) { $Count = 1 }
if ($Count -lt 1 -or $Count -gt 8) {
    Write-Host "LAUNCH REFUSED - count must be 1..8" -ForegroundColor Red
    exit 3
}

# A URL invocation is untrusted until this completes. No process is started above this line.
if ($Url) {
    if (-not $Grant) {
        Write-Host "LAUNCH REFUSED - URL carries no single-use grant" -ForegroundColor Red
        exit 3
    }
    Push-Location $Repo
    try {
        $grantArgs = @(
            "-m", "hub_core.launch_grant", "--consume-authoritative", $Grant,
            "--action", "start", ("--task=" + $Task), "--count", [string]$Count
        )
        if ($IssuerUrl) { $grantArgs += @("--issuer-url", $IssuerUrl) }
        if ($TokenFile) { $grantArgs += @("--token-file", $TokenFile) }
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $verdict = & $Python @grantArgs 2>&1
            $grantExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousPreference
        }
    } finally {
        Pop-Location
    }
    if ($grantExit -ne 0) {
        Write-Host ("LAUNCH REFUSED - " + ($verdict -join " ")) -ForegroundColor Red
        exit 3
    }
}

function Quote-Single([string]$Value) { return $Value.Replace("'", "''") }

for ($index = 1; $index -le $Count; $index++) {
    $agent = "worker-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + $index + "-" + (Get-Random -Maximum 10000)
    $promptFile = Join-Path $env:TEMP ("hub-worker-prompt-" + $agent + ".txt")
    $childFile = Join-Path $env:TEMP ("hub-worker-run-" + $agent + ".ps1")
    $prompt = @"
You are an independent worker on the Hub, agent id '$agent'. Read AGENTS.md and
OPERATING-AGREEMENT.md in full, then use the Hub's DISCOVER -> CLAIM -> IMPLEMENT -> RECORD ->
VERIFY loop. Work only from claimed tasks. If the launcher targeted '$Task', prefer it when ready.
Continue until no ready task remains. Keep the board synchronized with reality.
"@
    Set-Content -LiteralPath $promptFile -Value $prompt -Encoding UTF8
    $child = @"
`$ErrorActionPreference = 'Stop'
Set-Location '$(Quote-Single $Repo)'
`$env:HUB_AGENT_ID = '$(Quote-Single $agent)'
`$env:HUB_TASK_ID = '$(Quote-Single $Task)'
`$env:HUB_REPO = '$(Quote-Single $Repo)'
`$env:HUB_DIR = '$(Quote-Single (Join-Path $Repo "PROJECT\.hub"))'
`$env:HUB_WORKER_PROMPT_FILE = '$(Quote-Single $promptFile)'
`$Host.UI.RawUI.WindowTitle = '$(Quote-Single $agent)'
try {
    & '$(Quote-Single $WorkerCommand)'
    `$workerExit = if (`$null -eq `$LASTEXITCODE) { 0 } else { `$LASTEXITCODE }
} catch {
    Write-Error `$_
    `$workerExit = 1
} finally {
    Remove-Item -LiteralPath '$(Quote-Single $promptFile)' -Force -ErrorAction SilentlyContinue
}
exit `$workerExit
"@
    Set-Content -LiteralPath $childFile -Value $child -Encoding UTF8
    if ($DryRun) {
        Write-Host "DRY RUN - would launch $agent with $WorkerCommand" -ForegroundColor Yellow
        Remove-Item -LiteralPath $promptFile, $childFile -Force -ErrorAction SilentlyContinue
        continue
    }
    # The child owns exactly the worker's lifetime. No -NoExit: when the worker ends, its window
    # ends too instead of accumulating a permanently stale terminal.
    Start-Process powershell.exe -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $childFile + '"')
    ) | Out-Null
    Write-Host "launched $agent" -ForegroundColor Green
}
