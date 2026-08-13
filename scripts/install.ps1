$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvironmentPath = Join-Path $ProjectRoot ".conda-env"
$ConfigPath = Join-Path $ProjectRoot "mineru.json"
$ConfigTemplatePath = Join-Path $ProjectRoot "mineru.example.json"

if (-not (Test-Path $ConfigPath)) {
    if (-not (Test-Path $ConfigTemplatePath)) {
        throw "MinerU config template is missing: $ConfigTemplatePath"
    }
    Copy-Item -LiteralPath $ConfigTemplatePath -Destination $ConfigPath
}

# The same local paths are also declared in activate.ps1 for normal use.
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
    $env:CUDA_PATH,
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
    Write-Host "Creating local Python 3.12 environment..." -ForegroundColor Cyan
    conda create --prefix $EnvironmentPath --yes --no-default-packages --override-channels --channel conda-forge python=3.12 pip
    if ($LASTEXITCODE -ne 0) {
        throw "Conda environment creation failed with exit code $LASTEXITCODE."
    }
}

$CondaEnvironmentVariables = @(
    "CONDARC=$env:CONDARC",
    "CONDA_PKGS_DIRS=$env:CONDA_PKGS_DIRS",
    "PIP_CACHE_DIR=$env:PIP_CACHE_DIR",
    "UV_CACHE_DIR=$env:UV_CACHE_DIR",
    "XDG_CACHE_HOME=$env:XDG_CACHE_HOME",
    "HF_HOME=$env:HF_HOME",
    "HUGGINGFACE_HUB_CACHE=$env:HUGGINGFACE_HUB_CACHE",
    "MODELSCOPE_CACHE=$env:MODELSCOPE_CACHE",
    "TORCH_HOME=$env:TORCH_HOME",
    "CUDA_PATH=$env:CUDA_PATH",
    "GRADIO_TEMP_DIR=$env:GRADIO_TEMP_DIR",
    "MPLCONFIGDIR=$env:MPLCONFIGDIR",
    "NUMBA_CACHE_DIR=$env:NUMBA_CACHE_DIR",
    "MINERU_TOOLS_CONFIG_JSON=$env:MINERU_TOOLS_CONFIG_JSON",
    "MINERU_MODEL_SOURCE=$env:MINERU_MODEL_SOURCE",
    "MINERU_API_OUTPUT_ROOT=$env:MINERU_API_OUTPUT_ROOT",
    "TEMP=$env:TEMP",
    "TMP=$env:TMP"
)
conda env config vars set --prefix $EnvironmentPath @CondaEnvironmentVariables | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Persisting local environment paths failed with exit code $LASTEXITCODE."
}

$Python = Join-Path $EnvironmentPath "python.exe"
Write-Host "Installing MinerU and all supported Windows components..." -ForegroundColor Cyan
& $Python -m pip install --upgrade pip uv
if ($LASTEXITCODE -ne 0) {
    throw "pip/uv installation failed with exit code $LASTEXITCODE."
}
& $Python -m uv pip install --python $Python --upgrade "mineru[all]==3.4.4"
if ($LASTEXITCODE -ne 0) {
    throw "MinerU installation failed with exit code $LASTEXITCODE."
}

$NvidiaGpu = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($NvidiaGpu) {
    $CudaAvailable = & $Python -c "import torch; print(str(torch.cuda.is_available()).lower())"
    if ($CudaAvailable -ne "true") {
        Write-Host "NVIDIA GPU detected; installing matching CUDA 12.8 PyTorch wheels..." -ForegroundColor Cyan
        & $Python -m pip install --upgrade "torch==2.8.0+cu128" "torchvision==0.23.0+cu128" --index-url https://download.pytorch.org/whl/cu128
        if ($LASTEXITCODE -ne 0) {
            throw "CUDA PyTorch installation failed with exit code $LASTEXITCODE."
        }
    }
}

$TorchLibraryPath = Join-Path $EnvironmentPath "Lib\site-packages\torch\lib"
$CudaBinPath = Join-Path $env:CUDA_PATH "bin"
if ((Test-Path $TorchLibraryPath) -and -not (Test-Path $CudaBinPath)) {
    New-Item -ItemType Junction -Path $CudaBinPath -Target $TorchLibraryPath | Out-Null
}

Write-Host "Verifying installation..." -ForegroundColor Cyan
& $Python -c "import mineru, torch; print('MinerU:', getattr(mineru, '__version__', 'installed')); print('PyTorch:', torch.__version__); print('CUDA available:', torch.cuda.is_available()); print('CUDA runtime:', torch.version.cuda); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
& (Join-Path $EnvironmentPath "Scripts\mineru.exe") --version

Write-Host "Installation complete. Run: .\scripts\activate.ps1" -ForegroundColor Green
