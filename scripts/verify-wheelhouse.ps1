# Verify the V1.1 offline wheelhouse without network access.
#
# Usage (from the repository root):
#   pwsh -File scripts/verify-wheelhouse.ps1
#   pwsh -File scripts/verify-wheelhouse.ps1 -WheelDir wheels
param(
    [string]$WheelDir = "wheels",
    [switch]$KeepVenv
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$WheelPath = Join-Path $Root $WheelDir
$Lock = Join-Path $Root "requirements.lock"
$Venv = Join-Path $Root ".venv-wheelcheck"
$Python = if ($env:VHCU_PYTHON) { $env:VHCU_PYTHON } else { "py" }
$PythonArgs = if ($env:VHCU_PYTHON) { @() } else { @("-3") }

if (-not (Test-Path $WheelPath)) { throw "wheelhouse not found: $WheelPath" }
$Wheels = Get-ChildItem -LiteralPath $WheelPath -Filter *.whl -File
if ($Wheels.Count -eq 0) { throw "no wheels in $WheelPath" }
$ManifestPath = Join-Path $WheelPath "manifest.json"
if (Test-Path $ManifestPath) {
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    foreach ($entry in $Manifest.wheels) {
        $file = Join-Path $WheelPath $entry.name
        if (-not (Test-Path $file)) { throw "manifest wheel missing: $($entry.name)" }
        $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne $entry.sha256) { throw "wheel hash mismatch: $($entry.name)" }
    }
}

if (Test-Path $Venv) { Remove-Item -LiteralPath $Venv -Recurse -Force }
& $Python @PythonArgs -m venv $Venv
if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
$VenvPython = Join-Path $Venv "Scripts\python.exe"
& $VenvPython -m pip install --no-index --find-links $WheelPath -r $Lock
if ($LASTEXITCODE -ne 0) { throw "offline install failed" }
& $VenvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw "pip check failed" }
& $VenvPython -c "import mss, numpy, soundcard, webrtcvad, faster_whisper; print('offline imports ok')"
if ($LASTEXITCODE -ne 0) { throw "offline import smoke failed" }
# UIA is verified as installed without importing it: comtypes may initialize
# COM in a host-specific threading mode and fail unrelated to the wheel bytes.
& $VenvPython -c "import importlib.util; assert importlib.util.find_spec('uiautomation'); print('uiautomation wheel installed')"
if ($LASTEXITCODE -ne 0) { throw "uiautomation wheel missing" }

Write-Host "Offline wheelhouse verified: $($Wheels.Count) wheels."
if (-not $KeepVenv) { Remove-Item -LiteralPath $Venv -Recurse -Force }
