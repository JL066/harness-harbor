$ErrorActionPreference = "Continue"

$harborHome = if ($env:HARBOR_HOME) { $env:HARBOR_HOME } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$python = if ($env:HARBOR_VENV_PYTHON) { $env:HARBOR_VENV_PYTHON } else { Join-Path $harborHome ".venv-legacy\Scripts\python.exe" }
$daemon = Join-Path $harborHome "codex_job_daemon.py"
$log = Join-Path $harborHome "codex-job-daemon.log"
if (-not $env:HARBOR_JOBS_DIR) { $env:HARBOR_JOBS_DIR = Join-Path $harborHome ".jobs" }

while ($true) {
    $started = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $log "[$started] Starting Harness Harbor daemon"

    & $python $daemon >> $log 2>&1

    $exitCode = $LASTEXITCODE
    $stopped = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $log "[$stopped] Harness Harbor daemon exited: $exitCode; restarting in 5 seconds"

    Start-Sleep -Seconds 5
}
