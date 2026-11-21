# Build the V1.1 offline wheelhouse for Windows x86_64 / CPython 3.13.
#
# Usage (from the repository root):
#   pwsh -File scripts/build-wheelhouse.ps1
#   pwsh -File scripts/build-wheelhouse.ps1 -OutDir wheels -Clean
#
# The generated wheels/ directory is a release asset, not a Git artifact.
param(
    [string]$OutDir = "wheels",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Lock = Join-Path $Root "requirements.lock"
$Python = if ($env:VHCU_PYTHON) { $env:VHCU_PYTHON } else { "py" }
$PythonArgs = if ($env:VHCU_PYTHON) { @() } else { @("-3") }

if (-not (Test-Path $Lock)) { throw "requirements.lock not found: $Lock" }
$Version = & $Python @PythonArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($Version.Trim() -ne "3.13") {
    throw "V1.1 wheelhouse target is CPython 3.13; selected interpreter reports $Version"
}

$Target = Join-Path $Root $OutDir
if ($Clean -and (Test-Path $Target)) {
    Remove-Item -LiteralPath $Target -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Target | Out-Null
if (-not $Clean -and (Get-ChildItem -LiteralPath $Target -Force | Measure-Object).Count -gt 0) {
    throw "Output directory is not empty: $Target (pass -Clean to replace it)"
}

Write-Host "Building wheelhouse: $Target (CPython 3.13, win_amd64)"
& $Python @PythonArgs -m pip download `
    --requirement $Lock `
    --dest $Target `
    --only-binary=:all: `
    --platform win_amd64 `
    --python-version 3.13 `
    --implementation cp `
    --abi cp313 `
    --no-deps
if ($LASTEXITCODE -ne 0) { throw "pip download failed with exit code $LASTEXITCODE" }

$Files = Get-ChildItem -LiteralPath $Target -File | Sort-Object Name
if ($Files.Count -eq 0) { throw "wheelhouse is empty: $Target" }
$Manifest = foreach ($file in $Files) {
    [ordered]@{
        name = $file.Name
        bytes = $file.Length
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$Payload = [ordered]@{
    schema = "dsh-vhcu-wheelhouse/v1"
    target = "CPython 3.13 / win_amd64"
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    wheel_count = $Files.Count
    wheels = @($Manifest)
}
$Payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $Target "manifest.json") -Encoding utf8
Write-Host "Wheelhouse ready: $($Files.Count) wheels, manifest.json written."
