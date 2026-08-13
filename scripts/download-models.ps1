param(
    [ValidateSet("auto", "huggingface", "modelscope")]
    [string]$Source = "modelscope",

    [ValidateSet("pipeline", "vlm", "all")]
    [string]$ModelType = "all"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "activate.ps1")

# The downloader stores models below this project and writes the resolved
# machine-specific model paths to the ignored root mineru.json.
mineru-models-download --source $Source --model_type $ModelType
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
