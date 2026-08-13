param(
    [switch]$KeepWork
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "runtime.ps1")

$Paths = Initialize-PDF2MDRuntime -CreateConfig
$Python = Join-Path $Paths.Environment "python.exe"
if (-not (Test-Path $Python)) {
    throw "PDF2MD environment is missing. Run .\scripts\install.ps1 first."
}

# Keep the project environment first so PyInstaller resolves Tcl/Tk and
# runtime DLLs from the same Prefix instead of an active system Conda.
$env:PATH = @(
    $Paths.Environment,
    (Join-Path $Paths.Environment "Scripts"),
    (Join-Path $Paths.Environment "Library\bin"),
    (Join-Path $Paths.Cuda "bin"),
    $env:PATH
) -join [System.IO.Path]::PathSeparator

$Spec = Join-Path $Paths.ProjectRoot "packaging\PDF2MD.spec"
$Work = Join-Path $Paths.Runtime "build"
Push-Location (Split-Path -Parent $Spec)
try {
    & $Python -m PyInstaller --noconfirm --clean --distpath $Paths.ProjectRoot --workpath $Work $Spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

if (-not $KeepWork -and (Test-Path $Work)) {
    Remove-Item -LiteralPath $Work -Recurse -Force
}

Write-Host "Built: $($Paths.ProjectRoot)\PDF2MD.exe" -ForegroundColor Green
