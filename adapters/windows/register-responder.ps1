# Schedule the unattended responder poll for the current Windows user (no admin).
#
#   register-responder.ps1 -HomeDir C:\Users\me\.hub-responder            # arm (idempotent: re-run = repair)
#   register-responder.ps1 -Remove                                         # leave: removes exactly what arming added
#
# The task runs `python -m hub_core.responder --home <HomeDir> poll` every few minutes under
# pythonw.exe. That choice is the point of this script: Task Scheduler shows a console
# program's window on an interactive user's desktop for as long as it runs, so python.exe
# would flash a black window every interval. pythonw.exe is the same interpreter with no
# console; the responder logs to <HomeDir>\responder.log. The sessions it launches get a
# hidden console of their own (see hub_core/responder.py), so nothing below it pops either.
#
# Configuration lives in <HomeDir>\responder.env (KEY=VALUE lines: HUB_API_BASE, HUB_AGENT_ID,
# HUB_RESPONDER_RUNTIME, HUB_RESPONDER_WORKSPACE, HUB_AGENT_TOKEN_FILE). The token itself is
# never written to the task definition, the registry or a process argument — only the path of
# a file readable by this user.
param(
    [switch]$Remove,
    [string]$TaskName = "HubResponder",
    [string]$HomeDir = "",
    [int]$EveryMinutes = 5,
    [string]$Python = "",
    [string]$Repo = ""
)

$ErrorActionPreference = "Stop"
if ($TaskName -notmatch '^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$') { throw "TaskName is not a plain name: $TaskName" }

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed scheduled task $TaskName" -ForegroundColor Green
    } else {
        Write-Host "no scheduled task named $TaskName; nothing to remove" -ForegroundColor DarkGray
    }
    Write-Host "state, ledger and log were left in place (delete them yourself if you want them gone)" -ForegroundColor DarkGray
    return
}

if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
else { $Repo = (Resolve-Path -LiteralPath $Repo).Path }
if (-not (Test-Path -LiteralPath (Join-Path $Repo "hub_core\responder.py"))) {
    throw "hub_core\responder.py is not under $Repo"
}
if (-not $HomeDir) { $HomeDir = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".hub-responder" }
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
$HomeDir = (Resolve-Path -LiteralPath $HomeDir).Path
$envFile = Join-Path $HomeDir "responder.env"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Write $envFile first (HUB_API_BASE, HUB_AGENT_ID, HUB_RESPONDER_RUNTIME, HUB_AGENT_TOKEN_FILE, ...). Arming a poll with no configuration would only log failures."
}

if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "python is not on PATH; pass -Python <path to python.exe>" }
    $Python = $cmd.Source
}
$pyw = Join-Path (Split-Path -Parent $Python) "pythonw.exe"
if (Test-Path -LiteralPath $pyw) { $runAs = $pyw }
else {
    Write-Warning "no pythonw.exe beside $Python - the scheduled poll will show a console window each run"
    $runAs = $Python
}
foreach ($value in @($runAs, $HomeDir, $Repo)) {
    if ($value.Contains('"')) { throw 'Paths containing a double quote are not supported.' }
}

$action = New-ScheduledTaskAction -Execute $runAs `
    -Argument ('-m hub_core.responder --home "{0}" poll' -f $HomeDir) -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
# A poll that has not returned in ten minutes is itself stuck; the responder sessions it starts
# are detached and bounded by their own clocks, so this limit never cuts one short.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Unattended one-item responders over a Hub board (hub_core.responder)." -Force | Out-Null

Write-Host "armed scheduled task $TaskName every $EveryMinutes min" -ForegroundColor Green
Write-Host "runs: $runAs -m hub_core.responder --home `"$HomeDir`" poll   (in $Repo)" -ForegroundColor DarkGray
Write-Host "kill switch: create $(Join-Path $HomeDir 'DISABLED')   leave: register-responder.ps1 -Remove" -ForegroundColor DarkGray
