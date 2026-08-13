$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvironmentPath = Join-Path $ProjectRoot ".conda-env"

# Keep Conda, Python, model, UI, and temporary caches inside this project.
$env:CONDARC = Join-Path $ProjectRoot ".condarc"
$env:CONDA_PKGS_DIRS = Join-Path $ProjectRoot ".conda-pkgs"
$env:PIP_CACHE_DIR = Join-Path $ProjectRoot ".cache\pip"
$env:UV_CACHE_DIR = Join-Path $ProjectRoot ".cache\uv"
$env:XDG_CACHE_HOME = Join-Path $ProjectRoot ".cache"
$env:HF_HOME = Join-Path $ProjectRoot ".cache\huggingface"
$env:HUGGINGFACE_HUB_CACHE = Join-Path $ProjectRoot ".cache\huggingface\hub"
$env:MODELSCOPE_CACHE = Join-Path $ProjectRoot ".cache\modelscope"
$env:TORCH_HOME = Join-Path $ProjectRoot ".cache\torch"
$env:CUDA_PATH = Join-Path $ProjectRoot ".cuda"
$env:GRADIO_TEMP_DIR = Join-Path $ProjectRoot ".cache\gradio"
$env:MPLCONFIGDIR = Join-Path $ProjectRoot ".cache\matplotlib"
$env:NUMBA_CACHE_DIR = Join-Path $ProjectRoot ".cache\numba"
$env:MINERU_TOOLS_CONFIG_JSON = Join-Path $ProjectRoot "mineru.json"
$env:MINERU_MODEL_SOURCE = "local"
$env:MINERU_API_OUTPUT_ROOT = Join-Path $ProjectRoot "output"
$env:TEMP = Join-Path $ProjectRoot ".tmp"
$env:TMP = $env:TEMP

$LocalDirectories = @(
    $env:CONDA_PKGS_DIRS,
    $env:PIP_CACHE_DIR,
    $env:UV_CACHE_DIR,
    $env:HF_HOME,
    $env:MODELSCOPE_CACHE,
    $env:TORCH_HOME,
    $env:GRADIO_TEMP_DIR,
    $env:MPLCONFIGDIR,
    $env:NUMBA_CACHE_DIR,
    $env:MINERU_API_OUTPUT_ROOT,
    $env:TEMP,
    (Join-Path $ProjectRoot "input")
)
foreach ($Directory in $LocalDirectories) {
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
}

if (-not (Test-Path (Join-Path $EnvironmentPath "python.exe"))) {
    throw "MinerU environment is missing. Run .\scripts\install.ps1 first."
}

(& conda "shell.powershell" "hook") | Out-String | Invoke-Expression
conda activate $EnvironmentPath
Set-Location $ProjectRoot

Write-Host "MinerU environment activated: $EnvironmentPath" -ForegroundColor Green
Write-Host "Input:  $ProjectRoot\input"
Write-Host "Output: $ProjectRoot\output"
