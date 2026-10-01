$ErrorActionPreference = "Continue"

$harborHome = if ($env:HARBOR_HOME) { $env:HARBOR_HOME } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$tunnelExe = if ($env:HARBOR_TUNNEL_EXE) { $env:HARBOR_TUNNEL_EXE } else { (Get-Command tunnel-client.exe -ErrorAction SilentlyContinue).Source }
if (-not $tunnelExe) { throw "Set HARBOR_TUNNEL_EXE or add tunnel-client.exe to PATH" }
$profileDir = if ($env:HARBOR_TUNNEL_PROFILE_DIR) { $env:HARBOR_TUNNEL_PROFILE_DIR } else { Join-Path $env:APPDATA "tunnel-client" }
$profile = "chatgpt-harbor"
$supervisorLog = Join-Path $harborHome "tunnel-supervisor.log"
if (-not $env:HARBOR_JOBS_DIR) { $env:HARBOR_JOBS_DIR = Join-Path $harborHome ".jobs" }

while ($true) {
    $started = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $supervisorLog "[$started] Starting tunnel-client"

    & $tunnelExe run `
        --profile-dir $profileDir `
        --profile $profile

    $exitCode = $LASTEXITCODE
    $stopped = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $supervisorLog "[$stopped] tunnel-client exited: $exitCode; restarting in 5 seconds"

    Start-Sleep -Seconds 5
}
