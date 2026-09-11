# Launches Harness Harbor Launcher silently using pythonw.exe.
#
# Configuration: every deployment-specific path is resolved at startup from
# HARBOR_* environment variables (e.g. HARBOR_HOME, HARBOR_TUNNEL_EXE). See
# docs/CONFIGURATION.md for the full list. Set them in this shell before
# invoking the launcher to override checkout-relative defaults:
#
#   $env:HARBOR_HOME = "C:\Users\Example\HarnessHarbor"
#   .\run-launcher.ps1
#
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$pythonw = Join-Path $scriptDir ".venv\Scripts\pythonw.exe"
$mainScript = Join-Path $scriptDir "run_launcher.py"

if (-not (Test-Path $pythonw)) {
    $pythonw = "pythonw.exe"
}

Start-Process -FilePath $pythonw -ArgumentList "`"$mainScript`"" -WindowStyle Hidden
