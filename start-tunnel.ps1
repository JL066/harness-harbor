$ErrorActionPreference = "Stop"

if (-not $env:HARBOR_TUNNEL_EXE -or -not $env:HARBOR_TUNNEL_PROFILE_DIR -or -not $env:HARBOR_TUNNEL_PROFILE) {
    throw "Set HARBOR_TUNNEL_EXE, HARBOR_TUNNEL_PROFILE_DIR, and HARBOR_TUNNEL_PROFILE before starting the tunnel supervisor."
}

$tunnelExe = $env:HARBOR_TUNNEL_EXE
$profileDir = $env:HARBOR_TUNNEL_PROFILE_DIR
$profile = $env:HARBOR_TUNNEL_PROFILE
$supervisorLog = if ($env:HARBOR_TUNNEL_LOG) { $env:HARBOR_TUNNEL_LOG } else { Join-Path $PSScriptRoot "tunnel-supervisor.log" }

while ($true) {
    $started = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $supervisorLog "[$started] Starting tunnel-client"
    & $tunnelExe run --profile-dir $profileDir --profile $profile
    $exitCode = $LASTEXITCODE
    $stopped = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $supervisorLog "[$stopped] tunnel-client exited: $exitCode; restarting in 5 seconds"
    Start-Sleep -Seconds 5
}
