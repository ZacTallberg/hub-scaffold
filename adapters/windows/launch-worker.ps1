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
Keep the board synchronized with reality.

You are measured by COMPLETIONS, not by being busy: a seat that holds a claim and finishes nothing
is failing however active it looks. An empty rail is a signal to REFILL it from real evidence -
open findings, audit violations, unlanded work - not a reason to stop; "everything is done" is a
fleet judgement one seat cannot make. If you finish a cycle having completed nothing, CHANGE what
you are doing rather than repeating it: report a real step, then release the claim so a worker
with fresh context can take it (a held claim starves every other seat), then record a finding
naming the evidence and take work from a different source. Releasing is progress and an honest
finding is a completion.
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
# THE SEAT DOES NOT END. Invoking the worker command once and exiting makes a seat's entire
# existence a single run: the agent stops generating, the shell exits, the seat is gone - and no
# amount of queue can save it. This loop is the seat's lifetime and has no terminal condition.
# Your WorkerCommand should start a FRESH agent session each time it is called: the context is
# meant to be disposable and the BOARD is the memory, which is what keeps a long-lived seat from
# degrading as its window fills, and why it never needs to be told the overall goal.
`$__heartbeat = Join-Path `$env:HUB_DIR ('seat-' + `$env:HUB_AGENT_ID + '.heartbeat')
`$__cycle = 0
`$__fails = 0
`$__barren = 0
while (`$true) {
    `$__cycle++
    # Liveness stamp BEFORE the run, so a seat killed mid-cycle still leaves evidence it existed.
    # Whoever reads this must judge by the PID, never by the timestamp: a seat inside a long cycle
    # is alive however old its stamp, and reaping on age kills seats for thinking hard.
    Set-Content -LiteralPath `$__heartbeat -Value ((Get-Date).ToUniversalTime().ToString('o') + ' cycle ' + `$__cycle + ' pid ' + `$PID) -Encoding ASCII -ErrorAction SilentlyContinue

    # COMPLETIONS ARE THE GREEN CONDITION, read from the ledger either side of the run. A seat
    # cannot forge this: a `done` costs a receipt through the write gate. `is-active` is not
    # `is-working`, and every other signal - a pid, a window, a heartbeat - calls a permanently
    # stuck seat healthy.
    `$__before = 0
    try { `$__before = [int](& python (Join-Path `$env:HUB_REPO 'tools/seat_productivity.py') --agent `$env:HUB_AGENT_ID --done-count 2>`$null) } catch { }

    # The seat's context is fresh each cycle, so it cannot remember being stuck; the ledger
    # remembers for it and this variable is how that measurement reaches the worker.
    `$env:HUB_BARREN_CYCLES = `$__barren
    try {
        & '$(Quote-Single $WorkerCommand)'
        `$workerExit = if (`$null -eq `$LASTEXITCODE) { 0 } else { `$LASTEXITCODE }
    } catch {
        Write-Error `$_
        `$workerExit = 1
    }

    `$__after = `$__before
    try { `$__after = [int](& python (Join-Path `$env:HUB_REPO 'tools/seat_productivity.py') --agent `$env:HUB_AGENT_ID --done-count 2>`$null) } catch { }
    if (`$__after -gt `$__before) {
        `$__barren = 0
        Write-Host ('completed ' + (`$__after - `$__before) + ' task(s) this cycle (total ' + `$__after + ')') -ForegroundColor Green
    } else {
        `$__barren++
        Write-Host ('no completion this cycle (' + `$__barren + ' barren) - change what you do, do not repeat it') -ForegroundColor Yellow
    }

    # Backoff is DURABILITY, not a brake: the loop never exits on either branch. A transient
    # outage - quota, network, a locked board - is survived rather than spun through at full speed
    # until whatever would have recovered on its own is exhausted.
    if (`$workerExit -eq 0) { `$__fails = 0; `$__wait = 3 }
    else {
        `$__fails++
        `$__wait = [Math]::Min(300, 10 * [Math]::Pow(2, [Math]::Min(`$__fails, 5)))
        Write-Host ('cycle exited ' + `$workerExit + ' (consecutive ' + `$__fails + ') - back in ' + `$__wait + 's; the seat does not stop') -ForegroundColor Yellow
    }
    Start-Sleep -Seconds `$__wait
}
"@
    Set-Content -LiteralPath $childFile -Value $child -Encoding UTF8
    if ($DryRun) {
        Write-Host "DRY RUN - would launch $agent with $WorkerCommand" -ForegroundColor Yellow
        Remove-Item -LiteralPath $promptFile, $childFile -Force -ErrorAction SilentlyContinue
        continue
    }
    # The child IS the seat's lifetime and does not end on its own - it loops until the operator
    # stops it. No -NoExit: if it ever does end, its window ends with it rather than accumulating
    # a permanently stale terminal. The prompt file is deliberately NOT deleted after one run: the
    # worker command reads it every cycle.
    Start-Process powershell.exe -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $childFile + '"')
    ) | Out-Null
    Write-Host "launched $agent" -ForegroundColor Green
}
