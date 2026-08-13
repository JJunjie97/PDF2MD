param(
    [ValidateSet("auto", "huggingface", "modelscope")]
    [string]$Source = "modelscope",

    [ValidateSet("pipeline", "vlm", "all")]
    [string]$ModelType = "all"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "runtime.ps1")
$Paths = Initialize-MinerURuntime -CreateConfig
$Downloader = Join-Path $Paths.Environment "Scripts\mineru-models-download.exe"
if (-not (Test-Path $Downloader)) {
    throw "MinerU environment is missing. Run .\scripts\install.ps1 first."
}

# Models are stored under models/ and machine-specific paths are written to
# runtime/mineru.json.
& $Downloader --source $Source --model_type $ModelType
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
