$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$python = if ($env:HARBOR_PYTHON) { $env:HARBOR_PYTHON } else { "python" }
$daemon = Join-Path $projectRoot "codex_job_daemon.py"
$log = if ($env:HARBOR_DAEMON_LOG) { $env:HARBOR_DAEMON_LOG } else { Join-Path $projectRoot "codex-job-daemon.log" }

while ($true) {
    $started = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $log "[$started] Starting Harness Harbor daemon"
    & $python $daemon >> $log 2>&1
    $exitCode = $LASTEXITCODE
    $stopped = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $log "[$stopped] Harness Harbor daemon exited: $exitCode; restarting in 5 seconds"
    Start-Sleep -Seconds 5
}
