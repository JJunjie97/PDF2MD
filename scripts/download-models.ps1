param(
    [ValidateSet("auto", "huggingface", "modelscope")]
    [string]$Source = "modelscope",

    [ValidateSet("pipeline", "vlm", "all")]
    [string]$ModelType = "all"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "runtime.ps1")
$Paths = Initialize-PDF2MDRuntime -CreateConfig
$Python = Join-Path $Paths.Environment "python.exe"
if (-not (Test-Path $Python)) {
    throw "PDF2MD environment is missing. Run .\scripts\install.ps1 first."
}

# Models are stored under models/ and machine-specific paths are written to
# runtime/pdf2md.json.
& $Python -m mineru.cli.models_download --source $Source --model_type $ModelType
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
